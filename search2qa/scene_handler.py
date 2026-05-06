"""
Search2QA Scene Handler — 沙箱内执行 Search2QA Pipeline

与 backend.py 集成，负责：
1. 创建沙箱并安装依赖
2. 上传 search2qa 脚本到沙箱
3. 运行 pipeline 并实时采集日志
4. 收集输出轨迹数据

使用方式（在 backend.py 中）：
    from search2qa.scene_handler import run_search2qa_in_sandbox

Internal naming note (A-2 issue #1, closed as naming-only):
    本模块 ``run_search2qa_in_sandbox`` 的入参 ``config`` 内部使用字段名
    ``mode``，取值 ``"question"`` / ``"answer"``（见 :185 schema 与 :241 读取点）。

    对应 manifest SSOT
    （``ray-data-agent-proto/skills/trajectory-search2qa/manifest.json``）
    定义的字段名是 ``qa_mode``，``required_params`` 与 ``must_clarify`` 均使用
    ``qa_mode``。

    ``qa_mode → mode`` 的翻译发生在生产调用方 ``backend.py:469``，
    ``qa_mode`` 的值会被正确消费，pipeline 不会 fallback 到默认值。

    内外命名分离是当前架构现状，并非 bug；如需统一命名，需同步评估
    ``backend.py`` 对其他 4 个场景的对称性，跨场景一致性要求超出
    search2qa 单场景修整的范围。
"""

import asyncio
import json
import os
import shlex
import time
from datetime import timedelta
from typing import Callable, Optional

import httpx
from opensandbox.sandbox import Sandbox
from opensandbox.models.execd import RunCommandOpts
from opensandbox.config import ConnectionConfig

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sandbox_utils import _parse_sandbox_endpoint

# ─── 配置 ─────────────────────────────────────────────────────────────────────

OPENSANDBOX_SERVER = os.environ.get("OPENSANDBOX_SERVER", "http://127.0.0.1:8080")

# Container Python with pip; /usr/bin/python3 in code-interpreter:v1.0.2
# is bare 3.12 without pip. The cpython-3.14.* venv ships pip.
# Glob is expanded by the container shell so aarch64 / x86_64 suffixes
# both resolve at runtime.
SANDBOX_PYTHON = "/opt/python/versions/cpython-3.13.*/bin/python3"

# Sandbox image override. Default points at the dedicated Search2QA sandbox
# image built from Dockerfile.search2qa-sandbox (deps preinstalled, see T1).
SEARCH2QA_SANDBOX_IMAGE = os.environ.get(
    "SEARCH2QA_SANDBOX_IMAGE",
    "hidataagent-trajectory-search2qa-sandbox:demo",
)

# 包名（pip） -> import 名 映射，import check 用。版本约束维护在
# sandbox-requirements.txt（仓库根），两处需保持包列表一致。
SEARCH2QA_SANDBOX_REQUIRED_IMPORTS = {
    "ddgs": "ddgs",
    "requests": "requests",
    "beautifulsoup4": "bs4",
    "lxml": "lxml",
    "trafilatura": "trafilatura",
    "crawl4ai": "crawl4ai",
    "PyMuPDF": "fitz",
    "pymupdf4llm": "pymupdf4llm",
    "openai": "openai",
    "python-dotenv": "dotenv",
    "tqdm": "tqdm",
}


def _result_failed(result) -> bool:
    """Check if a sandbox command result indicates failure.

    OpenSandbox SDK result has no ``exit_code`` field; the public probe
    showed it exposes ``error`` (str when failed, falsy on success) plus
    ``result`` / ``logs`` / ``execution_count`` / ``id``. Wired in by a
    later commit once verified against real failures.
    """
    err = getattr(result, "error", None)
    if err:
        return True
    return False


# search2qa 脚本所在目录（相对于项目根目录）
SEARCH2QA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))

# 需要上传到沙箱的文件
UPLOAD_FILES = [
    "main.py",
    "llm_engine.py",
    "tools.py",
    "prompts.py",
    "trace_manager.py",
    "requirements.txt",
]


# ─── 沙箱管理 ─────────────────────────────────────────────────────────────────

async def create_sandbox() -> str:
    """创建沙箱实例"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes",
            json={
                "image": {"uri": SEARCH2QA_SANDBOX_IMAGE},
                "entrypoint": ["/opt/opensandbox/code-interpreter.sh"],
                "resourceLimits": {},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def connect_sandbox(sandbox_id: str) -> Sandbox:
    """连接到已创建的沙箱"""
    domain, protocol = _parse_sandbox_endpoint(OPENSANDBOX_SERVER)
    config = ConnectionConfig(domain=domain, protocol=protocol)
    return await Sandbox.connect(sandbox_id, connection_config=config)


async def delete_sandbox(sandbox_id: str):
    """删除沙箱"""
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes/{sandbox_id}",
            timeout=30,
        )


# ─── 文件上传 ──────────────────────────────────────────────────────────────────

async def upload_scripts_to_sandbox(sandbox: Sandbox, emit: Callable = None):
    """将 search2qa 脚本上传到沙箱的 /workspace/search2qa/ 目录"""

    # 创建工作目录
    await sandbox.commands.run("mkdir -p /workspace/search2qa")
    await sandbox.commands.run("mkdir -p /workspace/output/trace")

    for filename in UPLOAD_FILES:
        filepath = os.path.join(SEARCH2QA_DIR, filename)
        if not os.path.exists(filepath):
            if emit:
                emit("warning", f"文件不存在，跳过: {filename}")
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # 使用 SDK 的文件写入
        from opensandbox.models import WriteEntry
        await sandbox.files.write_file(
            f"/workspace/search2qa/{filename}",
            content.encode("utf-8"),
        )

        if emit:
            emit("upload", f"已上传: {filename}")


# ─── 诊断辅助 ──────────────────────────────────────────────────────────────────

def _emit_full_stderr(
    emit: Callable,
    event_type: str,
    label: str,
    stderr: str,
    head_chars: int = 500,
    log_prefix: str = "search2qa_install_stderr",
) -> None:
    """Emit stderr without silently truncating.

    Long stderr (>1000 chars) is written to /tmp/<log_prefix>_<ts>.log;
    the emitted message contains the file path plus a head excerpt so the
    full traceback is recoverable for diagnostics.
    """
    if not stderr:
        return
    if len(stderr) > 1000:
        ts = int(time.time() * 1000)
        log_path = f"/tmp/{log_prefix}_{label}_{ts}.log"
        try:
            with open(log_path, "w") as f:
                f.write(stderr)
            emit(
                event_type,
                f"⚠️ {label} stderr ({len(stderr)} chars, see {log_path}):\n{stderr[:head_chars]}",
            )
        except Exception:
            emit(event_type, f"⚠️ {label} stderr ({len(stderr)} chars):\n{stderr[:head_chars]}")
    else:
        emit(event_type, f"⚠️ {label} stderr:\n{stderr}")


# ─── 依赖安装 ──────────────────────────────────────────────────────────────────

async def install_dependencies(sandbox: Sandbox, emit: Callable = None):
    """Search2QA sandbox 依赖检查 + 可选 fallback 安装。

    行为：
    1. 在 sandbox 内 import-check 所有依赖（SEARCH2QA_SANDBOX_REQUIRED_IMPORTS）。
    2. 全部齐全：emit "skip pip install" 后直接返回。
    3. 缺失依赖：
       - 默认（SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL != "1"）→ raise RuntimeError，
         错误信息含缺失包名清单与镜像名，提示应改用预装镜像。
       - 显式 SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL == "1" → 走 runtime pip install
         legacy fallback（仅作为应急路径，不建议 demo 使用）。

    历史背景：原实现先 ensurepip --upgrade --break-system-packages 再分组 pip install，
    但 ensurepip 模块不接受 --break-system-packages 参数；本次重写把依赖固化到
    专用 sandbox 镜像（Dockerfile.search2qa-sandbox），import-check 是默认路径。
    """
    required = SEARCH2QA_SANDBOX_REQUIRED_IMPORTS

    if emit:
        emit("install", f"import-check sandbox deps ({len(required)} packages)...")

    # 把 import-check 脚本写入 sandbox /tmp 后执行；用文件而非 -c 单行，
    # 是为避免 dict 字面量在 shell 引号嵌套里被误转义。
    check_script = (
        "import importlib, json, sys\n"
        f"required = {required!r}\n"
        "missing = []\n"
        "for pkg, mod in required.items():\n"
        "    try:\n"
        "        importlib.import_module(mod)\n"
        "    except Exception as e:\n"
        "        missing.append({'pkg': pkg, 'mod': mod, 'err': str(e)})\n"
        "print('IMPORT_CHECK_RESULT:' + json.dumps(missing))\n"
        "sys.exit(0 if not missing else 1)\n"
    )

    await sandbox.files.write_file(
        "/tmp/_search2qa_import_check.py",
        check_script.encode("utf-8"),
    )
    check_result = await sandbox.commands.run(
        f"{SANDBOX_PYTHON} /tmp/_search2qa_import_check.py",
        opts=RunCommandOpts(timeout=timedelta(seconds=60)),
    )

    stdout = "\n".join([l.text for l in check_result.logs.stdout]) if check_result.logs.stdout else ""
    stderr = "\n".join([l.text for l in check_result.logs.stderr]) if check_result.logs.stderr else ""

    missing_payload = ""
    for line in stdout.splitlines():
        if line.startswith("IMPORT_CHECK_RESULT:"):
            missing_payload = line[len("IMPORT_CHECK_RESULT:"):]
            break

    try:
        missing_list = json.loads(missing_payload) if missing_payload else None
    except json.JSONDecodeError:
        missing_list = None

    if missing_list is None:
        # import-check 自身没跑出 IMPORT_CHECK_RESULT 行（如解释器路径错 / 沙箱被破坏）
        diag = (stderr or stdout or "no IMPORT_CHECK_RESULT marker").strip()
        msg = (
            f"Search2QA sandbox import-check failed to produce a result line; "
            f"sandbox image '{SEARCH2QA_SANDBOX_IMAGE}' may be malformed. "
            f"Diagnostic: {diag[:500]}"
        )
        if emit:
            emit("install_error", msg)
        raise RuntimeError(msg)

    if not missing_list:
        if emit:
            emit("install", "sandbox dependencies already installed; skip pip install")
        return

    missing_summary = ", ".join(
        f"{m.get('pkg')} (import {m.get('mod')}): {m.get('err')}"
        for m in missing_list
    )

    allow_runtime = os.environ.get("SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL") == "1"

    if not allow_runtime:
        msg = (
            f"Search2QA sandbox dependencies missing: {missing_summary}. "
            f"Sandbox image '{SEARCH2QA_SANDBOX_IMAGE}' should be built with deps "
            f"preinstalled (see Dockerfile.search2qa-sandbox). "
            f"Set SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL=1 to fallback to runtime "
            f"pip install (not recommended for demo)."
        )
        if emit:
            emit("install_error", msg)
        raise RuntimeError(msg)

    # ─── Legacy fallback: runtime pip install ──────────────────────────────
    # 版本约束与 sandbox-requirements.txt 同源；保留原分组结构以维持超时友好性。
    if emit:
        emit(
            "install",
            "SEARCH2QA_ALLOW_RUNTIME_PIP_INSTALL=1; "
            "falling back to runtime pip install (legacy path, not recommended)",
        )

    dep_groups = [
        f"{SANDBOX_PYTHON} -m pip install --break-system-packages 'ddgs>=9.0,<10'",
        (
            f"{SANDBOX_PYTHON} -m pip install --break-system-packages "
            f"'requests>=2.31,<3' 'beautifulsoup4>=4.12,<5' 'lxml>=4.9,<6' 'trafilatura>=1.6,<2'"
        ),
        f"{SANDBOX_PYTHON} -m pip install --break-system-packages 'crawl4ai>=0.3,<1'",
        f"{SANDBOX_PYTHON} -m pip install --break-system-packages 'PyMuPDF>=1.23,<2' 'pymupdf4llm>=0.0.4,<1'",
        f"{SANDBOX_PYTHON} -m pip install --break-system-packages 'openai>=1,<2'",
        f"{SANDBOX_PYTHON} -m pip install --break-system-packages 'python-dotenv>=1,<2' 'tqdm>=4.66,<5'",
    ]

    for cmd in dep_groups:
        if emit:
            emit("install", f"执行: {cmd}")
        result = await sandbox.commands.run(
            cmd,
            opts=RunCommandOpts(timeout=timedelta(seconds=120)),
        )
        stderr_grp = "\n".join([l.text for l in result.logs.stderr]) if result.logs.stderr else ""
        if stderr_grp and "error" in stderr_grp.lower():
            if emit:
                _emit_full_stderr(emit, "install_warn", cmd.split()[-1], stderr_grp)

    if emit:
        emit("install", "runtime pip install fallback complete")


# ─── 核心执行 ──────────────────────────────────────────────────────────────────

async def run_search2qa_in_sandbox(
    config: dict,
    emit: Callable = None,
) -> dict:
    """
    在沙箱中执行完整的 Search2QA Pipeline

    Args:
        config: {
            "seed": str,               # 种子词或已知答案
            "mode": str,               # "question" 或 "answer"
            "model": str,              # LLM 模型
            "temperature": float,      # 温度
            "max_turns": int,          # 每阶段最大轮次
            "max_evolutions": int,     # 复杂化迭代次数
            "enable_evolution": bool,  # 是否启用复杂化
            "enable_rewrite": bool,    # 是否启用轨迹改写
            "deepseek_api_key": str,   # DeepSeek API Key
            "deepseek_base_url": str,  # DeepSeek API URL
        }
        emit: 事件回调函数 emit(event_type, message, data={})

    Returns:
        {
            "status": "success" | "failed",
            "final_question": str,
            "final_answer": str,
            "total_tokens": int,
            "trajectory_data": dict,   # 完整轨迹数据
        }
    """
    def _emit(event_type, message, data=None):
        if emit:
            emit(event_type, message, data or {})

    sandbox_id = None

    try:
        # 1. 创建沙箱
        _emit("sandbox_create", "正在创建沙箱实例...")
        sandbox_id = await create_sandbox()
        _emit("sandbox_ready", f"沙箱已创建: {sandbox_id[:12]}...")
        await asyncio.sleep(3)

        # 2. 连接沙箱
        sandbox = await connect_sandbox(sandbox_id)

        async with sandbox:
            # 3. 上传脚本
            _emit("upload_start", "上传 Search2QA 脚本到沙箱...")
            await upload_scripts_to_sandbox(sandbox, lambda t, m: _emit(t, m))

            # 4. 安装依赖
            _emit("install_start", "安装 Python 依赖...")
            await install_dependencies(sandbox, lambda t, m: _emit(t, m))

            # 5. 设置环境变量
            api_key = config.get("deepseek_api_key", "")
            base_url = config.get("deepseek_base_url", "https://api.deepseek.com")
            env_cmd = (
                f'export DEEPSEEK_API_KEY="{api_key}" && '
                f'export DEEPSEEK_BASE_URL="{base_url}"'
            )

            # 6. 构建运行命令
            seed = config.get("seed", "")
            mode = config.get("mode", "question")
            model = config.get("model", "deepseek-chat")
            temperature = config.get("temperature", 0.7)
            max_turns = config.get("max_turns", 20)
            max_evolutions = config.get("max_evolutions", 2)
            enable_evolution = config.get("enable_evolution", True)
            enable_rewrite = config.get("enable_rewrite", True)

            run_cmd = (
                f'{env_cmd} && cd /workspace/search2qa && '
                f'{SANDBOX_PYTHON} main.py '
                f'--seed {shlex.quote(seed)} '
                f'--mode {shlex.quote(mode)} '
                f'--model {shlex.quote(model)} '
                f'--temperature {temperature} '
                f'--max-turns {max_turns} '
                f'--evolutions {max_evolutions} '
                f'--output-dir /workspace/output/trace '
            )

            if not enable_evolution:
                run_cmd += " --no-evolution"
            if not enable_rewrite:
                run_cmd += " --no-rewrite"

            # 7. 执行 Pipeline
            _emit("pipeline_start", "开始执行 Search2QA Pipeline...")

            timeout_minutes = config.get("timeout_minutes", 15)
            result = await sandbox.commands.run(
                run_cmd,
                opts=RunCommandOpts(timeout=timedelta(minutes=timeout_minutes)),
            )

            stdout = "\n".join([l.text for l in result.logs.stdout]) if result.logs.stdout else ""
            stderr = "\n".join([l.text for l in result.logs.stderr]) if result.logs.stderr else ""
            exit_code = getattr(result, "exit_code", None)
            if exit_code is None:
                exit_code = getattr(result, "return_code", None)

            # 实时日志
            if stdout:
                for line in stdout.split("\n"):
                    if line.strip():
                        _emit("pipeline_log", line.strip())

            if stderr:
                _emit_full_stderr(
                    lambda t, m: _emit(t, m),
                    "pipeline_warn",
                    "pipeline",
                    stderr,
                    head_chars=1500,
                    log_prefix="search2qa_pipeline_stderr",
                )

            # exit_code fail-fast: 非 0 立即抛错，避免继续走到 collect_start
            # 而误报 'final_output.json not found'
            if exit_code is not None and exit_code != 0:
                raise RuntimeError(
                    f"search2qa pipeline exited with code {exit_code}. "
                    f"See pipeline_warn event for stderr "
                    f"(or /tmp/search2qa_pipeline_stderr_*.log)."
                )

            # 8. 收集输出文件
            _emit("collect_start", "收集输出轨迹数据...")

            # 列出输出目录
            ls_result = await sandbox.commands.run("ls -la /workspace/output/trace/")
            ls_stdout = "\n".join([l.text for l in ls_result.logs.stdout]) if ls_result.logs.stdout else ""

            # 找到最新的运行文件夹
            find_result = await sandbox.commands.run(
                "find /workspace/output/trace -name 'final_output.json' -type f | head -1"
            )
            final_path = ""
            if find_result.logs.stdout:
                final_path = find_result.logs.stdout[0].text.strip()

            trajectory_data = {}
            if final_path:
                cat_result = await sandbox.commands.run(f"cat '{final_path}'")
                if cat_result.logs.stdout:
                    try:
                        raw = "\n".join([l.text for l in cat_result.logs.stdout])
                        trajectory_data = json.loads(raw)
                    except json.JSONDecodeError:
                        _emit("warning", "无法解析 final_output.json")

                # 也收集改写后的轨迹
                run_dir = os.path.dirname(final_path)
                rewrite_result = await sandbox.commands.run(
                    f"cat '{run_dir}/trace_rewrite.json' 2>/dev/null || echo '{{}}'"
                )
                if rewrite_result.logs.stdout:
                    try:
                        raw = "\n".join([l.text for l in rewrite_result.logs.stdout])
                        rewrite_data = json.loads(raw)
                        trajectory_data["rewrite_trace"] = rewrite_data
                    except json.JSONDecodeError:
                        pass

            # 持久化沙箱内 trace 文件到主机,供 demo / debug 查看。
            # 失败不阻塞 cleanup,沙箱仍正常销毁。
            try:
                task_id_for_dir = (config or {}).get("task_id") or sandbox_id or "unknown"
                repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                host_trace_dir = os.path.join(repo_root, "output", "trace", task_id_for_dir)
                find_result = await sandbox.commands.run(
                    "find /workspace/output/trace -type f"
                )
                trace_paths = []
                if find_result.logs.stdout:
                    trace_paths = [
                        l.text.strip() for l in find_result.logs.stdout if l.text.strip()
                    ]
                for sandbox_path in trace_paths:
                    rel = sandbox_path[len("/workspace/output/trace/"):]
                    local_path = os.path.join(host_trace_dir, rel)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    cat = await sandbox.commands.run(f"cat '{sandbox_path}'")
                    raw = "\n".join([l.text for l in cat.logs.stdout]) if cat.logs.stdout else ""
                    with open(local_path, "w") as f:
                        f.write(raw)
                if trace_paths:
                    rel_dir = os.path.relpath(host_trace_dir, repo_root)
                    _emit(
                        "trace_persisted",
                        f"轨迹已保存: {rel_dir}/ ({len(trace_paths)} 文件)",
                    )
                else:
                    _emit("trace_persisted_warn", "trace 目录为空，未持久化")
            except Exception as _persist_e:
                _emit(
                    "trace_persisted_warn",
                    f"trace 持久化失败（不阻塞 cleanup）: {_persist_e}",
                )

            _emit("pipeline_complete", "Search2QA Pipeline 执行完成")

            return {
                "status": "success",
                "final_question": trajectory_data.get("final_question", ""),
                "final_answer": trajectory_data.get("final_answer", ""),
                "total_tokens": trajectory_data.get("total_tokens", 0),
                "trajectory_data": trajectory_data,
                "stdout": stdout[-2000:],  # 保留最后部分日志
            }

    except Exception as e:
        _emit("error", f"Pipeline 执行失败: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "final_question": "",
            "final_answer": "",
            "total_tokens": 0,
            "trajectory_data": {},
        }

    finally:
        if sandbox_id:
            try:
                await delete_sandbox(sandbox_id)
                _emit("sandbox_cleanup", "沙箱已清理")
            except Exception:
                pass
