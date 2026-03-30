"""
Search2QA Scene Handler — 沙箱内执行 Search2QA Pipeline

与 backend.py 集成，负责：
1. 创建沙箱并安装依赖
2. 上传 search2qa 脚本到沙箱
3. 运行 pipeline 并实时采集日志
4. 收集输出轨迹数据

使用方式（在 backend.py 中）：
    from search2qa.scene_handler import run_search2qa_in_sandbox
"""

import asyncio
import json
import os
import time
from datetime import timedelta
from typing import Callable, Optional

import httpx
from opensandbox.sandbox import Sandbox
from opensandbox.config import ConnectionConfig

# ─── 配置 ─────────────────────────────────────────────────────────────────────

OPENSANDBOX_SERVER = os.environ.get("OPENSANDBOX_SERVER", "http://127.0.0.1:8080")

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
                "image": {"uri": "opensandbox/code-interpreter:v1.0.2"},
                "entrypoint": ["/opt/opensandbox/code-interpreter.sh"],
                "resourceLimits": {},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def connect_sandbox(sandbox_id: str) -> Sandbox:
    """连接到已创建的沙箱"""
    config = ConnectionConfig(domain="127.0.0.1:8080", protocol="http")
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
        await sandbox.files.write(
            f"/workspace/search2qa/{filename}",
            WriteEntry(data=content.encode("utf-8"))
        )

        if emit:
            emit("upload", f"已上传: {filename}")


# ─── 依赖安装 ──────────────────────────────────────────────────────────────────

async def install_dependencies(sandbox: Sandbox, emit: Callable = None):
    """在沙箱中安装 search2qa 的 Python 依赖"""

    if emit:
        emit("install", "开始安装 Python 依赖...")

    # 安装依赖（分批安装避免超时）
    dep_groups = [
        # 搜索相关
        "pip install duckduckgo-search>=4.1.0",
        # 爬虫相关
        "pip install requests beautifulsoup4 lxml trafilatura",
        # crawl4ai
        "pip install crawl4ai>=0.3.0",
        # PDF 处理
        "pip install PyMuPDF pymupdf4llm",
        # LLM 客户端
        "pip install openai",
        # 工具类
        "pip install python-dotenv tqdm",
    ]

    for cmd in dep_groups:
        if emit:
            emit("install", f"执行: {cmd}")

        result = await sandbox.commands.run(
            cmd,
            timeout=timedelta(seconds=120),
        )
        stdout = "\n".join([l.text for l in result.logs.stdout]) if result.logs.stdout else ""
        stderr = "\n".join([l.text for l in result.logs.stderr]) if result.logs.stderr else ""

        if stderr and "error" in stderr.lower():
            if emit:
                emit("install_warn", f"安装警告: {stderr[:200]}")

    # 安装 playwright（可选，失败不阻断）
    try:
        if emit:
            emit("install", "安装 playwright（可选）...")
        await sandbox.commands.run(
            "pip install playwright && playwright install chromium --with-deps",
            timeout=timedelta(seconds=180),
        )
    except Exception:
        if emit:
            emit("install_warn", "playwright 安装跳过（非必需）")

    if emit:
        emit("install", "✅ 依赖安装完成")


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
                f'python3 main.py '
                f'--seed "{seed}" '
                f'--mode {mode} '
                f'--model {model} '
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
                timeout=timedelta(minutes=timeout_minutes),
            )

            stdout = "\n".join([l.text for l in result.logs.stdout]) if result.logs.stdout else ""
            stderr = "\n".join([l.text for l in result.logs.stderr]) if result.logs.stderr else ""

            # 实时日志
            if stdout:
                for line in stdout.split("\n"):
                    if line.strip():
                        _emit("pipeline_log", line.strip())

            if stderr and "error" in stderr.lower():
                _emit("pipeline_warn", f"stderr: {stderr[:500]}")

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
