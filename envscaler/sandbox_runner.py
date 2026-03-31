"""
Step 1: 沙箱 MCP Server 部署

负责:
  - 在 OpenSandbox 中创建沙箱实例
  - 上传场景文件和 MCP Server 脚本
  - 安装依赖（fastmcp, qwen-agent 等）
  - 启动 MCP Server
  - 验证 MCP Server 可用性
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
from opensandbox.models import WriteEntry

from .config import (
    EnvScalerPipelineConfig,
    SceneFile,
    MCP_SERVER_TEMPLATE,
    OPENSANDBOX_SERVER,
)


# ─── 沙箱管理 ────────────────────────────────────────────────────────────────

async def create_sandbox() -> str:
    """创建 OpenSandbox 沙箱实例"""
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


# ─── 文件上传 ─────────────────────────────────────────────────────────────────

async def upload_scene_to_sandbox(
    sandbox: Sandbox,
    scene: SceneFile,
    emit: Callable = None,
):
    """
    上传场景文件和 MCP Server 脚本到沙箱

    目录结构:
      /workspace/envscaler/
        ├── main_mcp.py            # MCP Server 脚本
        └── data/
            ├── env_scenario.json
            └── filtered_env_metadata.json
    """
    _emit = emit or (lambda t, m: None)

    # 创建目录
    await sandbox.commands.run("mkdir -p /workspace/envscaler/data")

    # 上传 MCP Server 脚本
    _emit("upload", "上传 MCP Server 脚本...")
    await sandbox.files.write(
        "/workspace/envscaler/main_mcp.py",
        WriteEntry(data=MCP_SERVER_TEMPLATE.encode("utf-8")),
    )

    # 上传场景文件
    _emit("upload", "上传场景文件...")

    # env_scenario.json
    scenario_json = json.dumps(
        scene.scenario_content, ensure_ascii=False, indent=2
    )
    await sandbox.files.write(
        "/workspace/envscaler/data/env_scenario.json",
        WriteEntry(data=scenario_json.encode("utf-8")),
    )

    # filtered_env_metadata.json
    metadata_json = json.dumps(
        scene.metadata_content, ensure_ascii=False, indent=2
    )
    await sandbox.files.write(
        "/workspace/envscaler/data/filtered_env_metadata.json",
        WriteEntry(data=metadata_json.encode("utf-8")),
    )

    _emit("upload", "✅ 场景文件上传完成")


# ─── 依赖安装 ─────────────────────────────────────────────────────────────────

async def install_dependencies(
    sandbox: Sandbox,
    config: EnvScalerPipelineConfig,
    emit: Callable = None,
):
    """在沙箱中安装 MCP Server 运行依赖"""
    _emit = emit or (lambda t, m: None)

    _emit("install", "安装 Python 依赖...")

    # 按批次安装，避免超时
    dep_groups = [
        f"pip install fastmcp>={config.fastmcp_version}",
        "pip install openai httpx",
        "pip install zoneinfo-backport 2>/dev/null || true",  # 时区支持
    ]

    for cmd in dep_groups:
        _emit("install", f"  执行: {cmd}")
        result = await sandbox.commands.run(
            cmd,
            timeout=timedelta(seconds=120),
        )
        stderr = ""
        if result.logs.stderr:
            stderr = "\n".join([l.text for l in result.logs.stderr])
        if stderr and "error" in stderr.lower() and "warning" not in stderr.lower():
            _emit("install_warn", f"  ⚠ {stderr[:200]}")

    _emit("install", "✅ 依赖安装完成")


# ─── MCP Server 启动 ─────────────────────────────────────────────────────────

async def start_mcp_server(
    sandbox: Sandbox,
    config: EnvScalerPipelineConfig,
    emit: Callable = None,
) -> bool:
    """
    在沙箱中后台启动 MCP Server

    Returns:
        bool: 是否启动成功
    """
    _emit = emit or (lambda t, m: None)

    port = config.mcp_port
    transport = config.mcp_transport

    _emit("mcp_start", f"启动 MCP Server (port={port}, transport={transport})...")

    # 后台启动 MCP Server
    start_cmd = (
        f"cd /workspace/envscaler && "
        f"SCENE_DATA_DIR=/workspace/envscaler/data "
        f"MCP_PORT={port} "
        f"MCP_TRANSPORT={transport} "
        f"nohup python3 main_mcp.py > /tmp/mcp_server.log 2>&1 &"
    )

    await sandbox.commands.run(start_cmd, timeout=timedelta(seconds=10))

    # 等待 MCP Server 就绪
    _emit("mcp_start", "等待 MCP Server 就绪...")
    for attempt in range(15):
        await asyncio.sleep(2)

        # 检查进程是否存在
        check_result = await sandbox.commands.run(
            "pgrep -f 'main_mcp.py' && echo 'RUNNING' || echo 'NOT_RUNNING'",
            timeout=timedelta(seconds=5),
        )
        stdout = ""
        if check_result.logs.stdout:
            stdout = "\n".join([l.text for l in check_result.logs.stdout])

        if "RUNNING" in stdout:
            # 检查端口是否在监听
            port_check = await sandbox.commands.run(
                f"ss -tlnp | grep {port} && echo 'LISTENING' || echo 'NOT_LISTENING'",
                timeout=timedelta(seconds=5),
            )
            port_stdout = ""
            if port_check.logs.stdout:
                port_stdout = "\n".join([l.text for l in port_check.logs.stdout])

            if "LISTENING" in port_stdout:
                _emit("mcp_ready", f"✅ MCP Server 已就绪 (port={port})")
                return True

            _emit("mcp_start", f"  等待端口就绪... ({attempt + 1}/15)")
        else:
            # 查看日志排查问题
            log_result = await sandbox.commands.run(
                "tail -20 /tmp/mcp_server.log 2>/dev/null || echo '(无日志)'",
                timeout=timedelta(seconds=5),
            )
            log_stdout = ""
            if log_result.logs.stdout:
                log_stdout = "\n".join([l.text for l in log_result.logs.stdout])
            _emit("mcp_warn", f"  MCP 进程未运行, 日志: {log_stdout[:300]}")

            if attempt < 2:
                # 重试启动
                await sandbox.commands.run(start_cmd, timeout=timedelta(seconds=10))
            else:
                break

    _emit("mcp_error", "❌ MCP Server 启动失败")
    return False


# ─── MCP Server 验证 ─────────────────────────────────────────────────────────

async def verify_mcp_server(
    sandbox: Sandbox,
    config: EnvScalerPipelineConfig,
    emit: Callable = None,
) -> dict:
    """
    验证 MCP Server 可用性，获取工具列表

    Returns:
        {"available": bool, "tools": list, "log": str}
    """
    _emit = emit or (lambda t, m: None)

    port = config.mcp_port

    # 写一个测试脚本到沙箱
    test_script = f'''
import json
import httpx
import sys

try:
    # 尝试调用 MCP tools/list
    resp = httpx.post(
        "http://127.0.0.1:{port}/mcp",
        json={{"jsonrpc": "2.0", "method": "tools/list", "id": 1}},
        headers={{"Content-Type": "application/json"}},
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        tools = data.get("result", {{}}).get("tools", [])
        print(json.dumps({{"available": True, "tools": tools, "status": resp.status_code}}))
    else:
        print(json.dumps({{"available": False, "status": resp.status_code, "body": resp.text[:200]}}))
except Exception as e:
    # 回退: 检查进程和端口
    print(json.dumps({{"available": False, "error": str(e)}}))
'''

    await sandbox.files.write(
        "/workspace/envscaler/test_mcp.py",
        WriteEntry(data=test_script.encode("utf-8")),
    )

    result = await sandbox.commands.run(
        "cd /workspace/envscaler && python3 test_mcp.py",
        timeout=timedelta(seconds=15),
    )

    stdout = ""
    if result.logs.stdout:
        stdout = "\n".join([l.text for l in result.logs.stdout])

    try:
        verify_result = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        verify_result = {"available": False, "log": stdout[:300]}

    if verify_result.get("available"):
        tools = verify_result.get("tools", [])
        tool_names = [t.get("name", "") for t in tools]
        _emit("mcp_verify", f"✅ MCP 验证通过, 工具: {tool_names}")
    else:
        _emit("mcp_verify", f"⚠ MCP 验证未通过: {verify_result}")

    return verify_result


# ─── 整合: Step 1 完整流程 ───────────────────────────────────────────────────

async def run_step1(
    scene: SceneFile,
    config: EnvScalerPipelineConfig,
    event_callback: Callable = None,
) -> dict:
    """
    执行 Step 1: 沙箱 MCP Server 部署

    Args:
        scene: 场景文件
        config: Pipeline 配置
        event_callback: 事件回调

    Returns:
        {
            "sandbox_id": str,
            "sandbox": Sandbox,
            "mcp_ready": bool,
            "mcp_tools": list,
        }
    """
    def emit(t, m):
        if event_callback:
            event_callback(t, m)
        print(f"  [{t}] {m}")

    emit("step1_start", "Step 1: 沙箱 MCP Server 部署")

    # 1. 创建沙箱
    emit("sandbox_create", "创建沙箱实例...")
    sandbox_id = await create_sandbox()
    emit("sandbox_ready", f"沙箱已创建: {sandbox_id[:12]}...")
    await asyncio.sleep(3)

    # 2. 连接沙箱
    sandbox = await connect_sandbox(sandbox_id)

    try:
        async with sandbox:
            # 3. 上传文件
            await upload_scene_to_sandbox(sandbox, scene, emit)

            # 4. 安装依赖
            await install_dependencies(sandbox, config, emit)

            # 5. 启动 MCP Server
            mcp_ready = await start_mcp_server(sandbox, config, emit)

            # 6. 验证
            mcp_tools = []
            if mcp_ready:
                verify = await verify_mcp_server(sandbox, config, emit)
                mcp_tools = verify.get("tools", [])

            return {
                "sandbox_id": sandbox_id,
                "mcp_ready": mcp_ready,
                "mcp_tools": mcp_tools,
            }

    except Exception as e:
        emit("error", f"Step 1 执行失败: {e}")
        # 清理沙箱
        try:
            await delete_sandbox(sandbox_id)
        except Exception:
            pass
        raise
