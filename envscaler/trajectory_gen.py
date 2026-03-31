"""
Step 2: Agent 轨迹生成

核心模块: Agent 通过 MCP 工具与环境交互, 采集完整轨迹。

两种模式:
  - openai 模式: 使用 DeepSeek (OpenAI 兼容) function calling
  - qwen 模式: 使用 qwen-agent 的 MCP 集成

子步骤:
  2.1 构建 Agent（tools schema + system prompt）
  2.2 Agent 循环: LLM 决策 → scene_action 调用 → 结果反馈
  2.3 采集并结构化轨迹
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from typing import Callable, Optional

from openai import OpenAI
from opensandbox.sandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models import WriteEntry

from .config import (
    EnvScalerPipelineConfig,
    SceneTask,
    AGENT_SYSTEM_PROMPT,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .scene_manager import format_tools_for_prompt


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class TrajectoryTurn:
    """轨迹中的单个轮次"""
    turn_id: int
    role: str                   # user | assistant | tool
    content: str = ""
    thought: str = ""           # Agent 推理过程
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class EnvScalerTrajectory:
    """一条完整的 EnvScaler 轨迹"""
    trajectory_id: str = ""
    task_id: str = ""
    task_desc: str = ""
    env_name: str = ""
    turns: list = field(default_factory=list)        # list[TrajectoryTurn]
    messages: list = field(default_factory=list)      # 原始 chat messages
    tools_schema: list = field(default_factory=list)  # 使用的工具定义
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    total_tokens: int = 0
    quality_score: float = 0.0
    task_completed: bool = False
    task_reward: float = 0.0


# ─── 工具定义构建 ─────────────────────────────────────────────────────────────

def build_tools_schema_for_scene(mcp_tools: list = None) -> list:
    """
    构建 OpenAI function calling 格式的工具定义

    无论 MCP Server 暴露了什么工具, Agent 主要通过 scene_action 与环境交互。
    同时也暴露 get_current_time 辅助工具。
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "scene_action",
                "description": (
                    "执行场景指令。通过 name 指定要调用的环境操作名称, "
                    "通过 arguments 传入操作参数。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "要调用的环境操作名称",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "操作参数",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_current_time",
                "description": "获取指定时区的当前时间",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "时区名称, 如 'UTC', 'Asia/Shanghai'",
                            "default": "UTC",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]

    return tools


# ─── MCP 工具调用（通过沙箱中转） ─────────────────────────────────────────────

async def call_mcp_tool_via_sandbox(
    sandbox: Sandbox,
    tool_name: str,
    arguments: dict,
    mcp_port: int = 8888,
) -> dict:
    """
    在沙箱内通过 HTTP 调用 MCP Server 的工具

    Args:
        sandbox: 沙箱实例
        tool_name: 工具名称 (scene_action / get_current_time)
        arguments: 工具参数

    Returns:
        {"success": bool, "result": str, "duration_ms": int}
    """
    start = time.time()

    # 构建调用脚本
    call_script = f'''
import json
import httpx

try:
    resp = httpx.post(
        "http://127.0.0.1:{mcp_port}/mcp",
        json={{
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {{
                "name": "{tool_name}",
                "arguments": {json.dumps(arguments, ensure_ascii=False)},
            }},
            "id": 1,
        }},
        headers={{"Content-Type": "application/json"}},
        timeout=30,
    )
    data = resp.json()
    result = data.get("result", {{}})
    # MCP result 的 content 通常是列表
    content_parts = result.get("content", [])
    text_parts = []
    for part in content_parts:
        if isinstance(part, dict) and part.get("type") == "text":
            text_parts.append(part.get("text", ""))
        elif isinstance(part, str):
            text_parts.append(part)
    output = "\\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False)
    print(json.dumps({{"success": True, "result": output}}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({{"success": False, "result": str(e)}}, ensure_ascii=False))
'''

    # 写入并执行
    await sandbox.files.write(
        "/workspace/envscaler/_call_tool.py",
        WriteEntry(data=call_script.encode("utf-8")),
    )

    exec_result = await sandbox.commands.run(
        "cd /workspace/envscaler && python3 _call_tool.py",
        timeout=timedelta(seconds=45),
    )

    duration = int((time.time() - start) * 1000)

    stdout = ""
    if exec_result.logs.stdout:
        stdout = "\n".join([l.text for l in exec_result.logs.stdout])

    try:
        result = json.loads(stdout.strip())
        return {
            "success": result.get("success", False),
            "result": result.get("result", stdout)[:3000],
            "duration_ms": duration,
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "success": False,
            "result": stdout[:1000] if stdout else "(无输出)",
            "duration_ms": duration,
        }


# ─── 直接 HTTP 调用（不经过沙箱, 当 MCP Server 在宿主机时使用） ───────────────

async def call_mcp_tool_direct(
    tool_name: str,
    arguments: dict,
    mcp_host: str = "127.0.0.1",
    mcp_port: int = 8888,
) -> dict:
    """
    直接通过 HTTP 调用 MCP Server（宿主机模式）
    """
    start = time.time()

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"http://{mcp_host}:{mcp_port}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                    "id": 1,
                },
                headers={"Content-Type": "application/json"},
            )

            duration = int((time.time() - start) * 1000)
            data = resp.json()
            result = data.get("result", {})

            content_parts = result.get("content", [])
            text_parts = []
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            output = "\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False)

            return {
                "success": not result.get("isError", False),
                "result": output[:3000],
                "duration_ms": duration,
            }

    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return {
            "success": False,
            "result": f"MCP 调用失败: {e}",
            "duration_ms": duration,
        }


# ─── 单任务轨迹生成 ──────────────────────────────────────────────────────────

async def generate_trajectory_for_task(
    task: SceneTask,
    config: EnvScalerPipelineConfig,
    sandbox: Sandbox = None,
    mcp_tools: list = None,
    event_callback: Callable = None,
) -> EnvScalerTrajectory:
    """
    为单个任务生成 Agent 轨迹

    流程:
      1. 构建 tools schema + system prompt
      2. Agent 循环: LLM → tool call → MCP 执行 → 反馈
      3. 采集完整轨迹

    Args:
        task: 场景任务
        config: Pipeline 配置
        sandbox: 沙箱实例（用于 MCP 调用中转）
        mcp_tools: MCP Server 暴露的工具列表
        event_callback: 事件回调

    Returns:
        EnvScalerTrajectory
    """
    def emit(msg):
        if event_callback:
            event_callback("trajectory_gen", msg)

    traj_id = f"envscaler_{task.task_id}_{int(time.time())}"

    # ── 1. 构建工具和 Prompt ──
    tools_schema = build_tools_schema_for_scene(mcp_tools)
    tools_desc = format_tools_for_prompt(task.available_tools)

    system_prompt = AGENT_SYSTEM_PROMPT.format(
        available_tools=tools_desc,
        task_desc=task.task_desc,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task.task_desc},
    ]

    trajectory = EnvScalerTrajectory(
        trajectory_id=traj_id,
        task_id=task.task_id,
        task_desc=task.task_desc,
        env_name=task.env_name,
        tools_schema=tools_schema,
    )

    # ── 2. Agent 循环 ──
    api_key = config.deepseek_api_key or DEEPSEEK_API_KEY
    base_url = config.deepseek_base_url or DEEPSEEK_BASE_URL
    client = OpenAI(api_key=api_key, base_url=base_url)

    for step_id in range(1, config.max_steps + 1):
        emit(f"  [{task.task_id}] Step {step_id}: Agent 推理中...")

        try:
            response = client.chat.completions.create(
                model=config.agent_model,
                messages=messages,
                tools=tools_schema,
                tool_choice="auto",
                temperature=config.agent_temperature,
                max_tokens=2048,
                stream=False,
            )

            trajectory.total_tokens += (
                response.usage.total_tokens if response.usage else 0
            )
            choice = response.choices[0]
            assistant_msg = choice.message

            # 构建 assistant message dict
            msg_dict = {"role": "assistant", "content": assistant_msg.content or ""}
            if assistant_msg.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_msg.tool_calls
                ]
            messages.append(msg_dict)

            # ── 处理工具调用 ──
            if assistant_msg.tool_calls:
                step_tool_calls = []
                step_tool_results = []

                for tc in assistant_msg.tool_calls:
                    func_name = tc.function.name
                    try:
                        func_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        func_args = {}

                    emit(f"    🔧 {func_name}({json.dumps(func_args, ensure_ascii=False)[:80]})")

                    # 调用 MCP 工具
                    if sandbox:
                        result = await call_mcp_tool_via_sandbox(
                            sandbox, func_name, func_args, config.mcp_port,
                        )
                    else:
                        result = await call_mcp_tool_direct(
                            func_name, func_args, mcp_port=config.mcp_port,
                        )

                    step_tool_calls.append({
                        "tool_name": func_name,
                        "tool_input": func_args,
                        "tool_output": result["result"],
                        "success": result["success"],
                        "duration_ms": result["duration_ms"],
                        "call_id": tc.id,
                    })

                    trajectory.total_tool_calls += 1
                    if result["success"]:
                        trajectory.successful_tool_calls += 1

                    # 反馈给 Agent
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result["result"][:2000],
                    })

                    # 检查是否有 reward 和 terminated 信号
                    _check_env_signals(result, trajectory)

                    emit(f"      {'✓' if result['success'] else '✗'} "
                         f"({result['duration_ms']}ms)")

                    step_tool_results.append(result["result"][:500])

                # 记录轨迹轮次
                trajectory.turns.append(asdict(TrajectoryTurn(
                    turn_id=step_id,
                    role="assistant",
                    content=assistant_msg.content or "",
                    thought=assistant_msg.content or "",
                    tool_calls=step_tool_calls,
                    tool_results=step_tool_results,
                )))

            else:
                # 没有工具调用 — Agent 给出最终回答
                trajectory.turns.append(asdict(TrajectoryTurn(
                    turn_id=step_id,
                    role="assistant",
                    content=assistant_msg.content or "",
                )))

                if choice.finish_reason == "stop":
                    emit(f"  [{task.task_id}] ✓ Agent 完成 (共 {step_id} 步)")
                    trajectory.task_completed = True
                    break

        except Exception as e:
            emit(f"  [{task.task_id}] ⚠ Step {step_id} 异常: {e}")
            trajectory.turns.append(asdict(TrajectoryTurn(
                turn_id=step_id,
                role="error",
                content=f"执行异常: {str(e)[:500]}",
            )))
            break

    # 保存完整 messages
    trajectory.messages = messages

    return trajectory


def _check_env_signals(result: dict, trajectory: EnvScalerTrajectory):
    """从工具结果中检查环境信号（reward, terminated）"""
    try:
        result_text = result.get("result", "")
        if isinstance(result_text, str):
            # 尝试解析 JSON
            data = json.loads(result_text)
        elif isinstance(result_text, dict):
            data = result_text
        else:
            return

        obs = data.get("observation", data)
        if isinstance(obs, dict):
            reward = obs.get("reward", 0.0)
            if isinstance(reward, (int, float)):
                trajectory.task_reward = max(trajectory.task_reward, reward)
            if obs.get("terminated", False):
                trajectory.task_completed = True
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass


# ─── 批量轨迹生成 ────────────────────────────────────────────────────────────

async def run_step2(
    tasks: list,
    config: EnvScalerPipelineConfig,
    sandbox_id: str = None,
    mcp_tools: list = None,
    event_callback: Callable = None,
) -> list:
    """
    执行 Step 2: 为所有任务生成 Agent 轨迹

    Args:
        tasks: 任务列表
        config: Pipeline 配置
        sandbox_id: 沙箱 ID
        mcp_tools: MCP 工具列表
        event_callback: 事件回调

    Returns:
        list[EnvScalerTrajectory]
    """
    def emit(t, m):
        if event_callback:
            event_callback(t, m)
        print(f"  [{t}] {m}")

    emit("step2_start", f"Step 2: 为 {len(tasks)} 个任务生成轨迹")

    trajectories = []
    sandbox = None

    # 连接沙箱（需要 async with 来激活连接）
    if sandbox_id:
        try:
            config_conn = ConnectionConfig(domain="127.0.0.1:8080", protocol="http")
            sandbox = await Sandbox.connect(sandbox_id, connection_config=config_conn)
        except Exception as e:
            emit("warning", f"沙箱连接失败, 使用直接 HTTP 模式: {e}")
            sandbox = None

    async def _generate_all(sb):
        for i, task in enumerate(tasks):
            emit("trajectory_progress", f"任务 {i + 1}/{len(tasks)}: {task.task_desc[:50]}...")

            traj = await generate_trajectory_for_task(
                task=task,
                config=config,
                sandbox=sb,
                mcp_tools=mcp_tools,
                event_callback=event_callback,
            )

            trajectories.append(traj)
            emit("trajectory_done",
                 f"  ✓ {traj.total_tool_calls} 次工具调用, "
                 f"{traj.successful_tool_calls} 次成功, "
                 f"reward={traj.task_reward:.2f}")

    if sandbox:
        async with sandbox:
            await _generate_all(sandbox)
    else:
        await _generate_all(None)

    return trajectories
