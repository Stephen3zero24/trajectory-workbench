"""
Step 3: Agent 轨迹生成

核心集成模块：将 Qwen-Agent + Smithery MCP + OpenSandbox 组合
在沙箱环境中通过真实 MCP 工具调用采集 Agent 轨迹

子步骤:
  3.1 单轮轨迹生成 — Qwen-Agent 驱动的 MCP 工具调用
  3.2 多轮轨迹扩展 — 将单轮对话扩展为多轮对话
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from typing import Optional

import httpx
from openai import OpenAI

from .config import (
    ToucanPipelineConfig,
    MCPServerInfo,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    SMITHERY_API_KEY,
    SMITHERY_SERVER_BASE,
    OPENSANDBOX_SERVER,
)
from .step2_quality_check import QualityCheckedQuestion


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """单次工具调用记录"""
    tool_name: str
    tool_input: dict
    tool_output: str
    server_id: str = ""
    success: bool = True
    duration_ms: int = 0


@dataclass
class TrajectoryStep:
    """轨迹中的单步"""
    step_id: int
    role: str               # "assistant" | "tool" | "user"
    content: str             # 文本内容
    thought: str = ""        # Agent 的推理过程
    tool_calls: list = field(default_factory=list)  # 工具调用列表
    timestamp: float = field(default_factory=time.time)


@dataclass
class ToucanTrajectory:
    """完整的 Toucan 轨迹"""
    trajectory_id: str
    question: str
    question_id: str
    target_servers: list
    target_tools: list
    steps: list = field(default_factory=list)
    messages: list = field(default_factory=list)      # 原始 chat messages
    tools_schema: list = field(default_factory=list)   # 使用的工具定义
    quality_score: float = 0.0
    total_tool_calls: int = 0
    successful_tool_calls: int = 0
    total_tokens: int = 0
    iteration: int = 0
    subset: str = "single-turn-original"  # 对齐 Toucan 格式


# ─── MCP 工具调用核心 ──────────────────────────────────────────────────────────

async def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict,
    api_key: str = "",
) -> dict:
    """
    通过 Smithery Connect REST API 调用 MCP 工具

    Args:
        server_url: MCP Server 的 Smithery URL
        tool_name: 工具名称
        arguments: 工具参数
        api_key: Smithery API Key

    Returns:
        dict: {"success": bool, "result": str, "duration_ms": int}
    """
    key = api_key or SMITHERY_API_KEY
    start = time.time()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 通过 Streamable HTTP 调用 MCP 工具
            resp = await client.post(
                server_url,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                },
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                    "id": int(time.time() * 1000),
                },
            )

            duration = int((time.time() - start) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                result = data.get("result", {})
                # MCP tool result 通常在 content 数组中
                content_parts = result.get("content", [])
                text_parts = []
                for part in content_parts:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)

                result_text = "\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False)

                return {
                    "success": not result.get("isError", False),
                    "result": result_text[:2000],
                    "duration_ms": duration,
                }
            else:
                return {
                    "success": False,
                    "result": f"HTTP {resp.status_code}: {resp.text[:500]}",
                    "duration_ms": duration,
                }

    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return {
            "success": False,
            "result": f"MCP 调用异常: {str(e)[:300]}",
            "duration_ms": duration,
        }


# ─── 沙箱管理 ──────────────────────────────────────────────────────────────────

async def create_sandbox() -> str:
    """创建 OpenSandbox 沙箱"""
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


async def delete_sandbox(sandbox_id: str):
    """删除沙箱"""
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes/{sandbox_id}",
            timeout=30,
        )


async def exec_in_sandbox(sandbox_id: str, command: str) -> str:
    """在沙箱中执行命令（通过 HTTP API）"""
    try:
        from opensandbox.sandbox import Sandbox
        from opensandbox.config import ConnectionConfig

        config = ConnectionConfig(domain="127.0.0.1:8080", protocol="http")
        sandbox = await Sandbox.connect(sandbox_id, connection_config=config)
        async with sandbox:
            result = await sandbox.commands.run(command, timeout=timedelta(seconds=30))
            stdout = "\n".join([l.text for l in result.logs.stdout]) if result.logs.stdout else ""
            stderr = "\n".join([l.text for l in result.logs.stderr]) if result.logs.stderr else ""
            return stdout if stdout else (stderr if stderr else "(无输出)")
    except Exception as e:
        return f"沙箱执行异常: {e}"


# ─── Step 3.1: 单轮轨迹生成 ────────────────────────────────────────────────────

def build_tools_schema(servers: list) -> list:
    """从 MCP Server 列表构建 OpenAI 格式的 tools schema"""
    tools = []
    for server in servers:
        for tool_def in server.tools:
            if isinstance(tool_def, dict):
                func = {
                    "type": "function",
                    "function": {
                        "name": f"{server.server_id}__{tool_def.get('name', 'unknown')}",
                        "description": tool_def.get("description", ""),
                        "parameters": tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                    },
                }
                tools.append(func)

    # 如果没有从 server 获取到 tools，添加通用工具定义
    if not tools:
        for server in servers:
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{server.server_id}__query",
                    "description": f"调用 {server.name} 服务: {server.description}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "查询内容"},
                        },
                        "required": ["query"],
                    },
                },
            })

    return tools


async def generate_single_turn_trajectory(
    checked_question: QualityCheckedQuestion,
    servers: list,
    config: ToucanPipelineConfig,
    sandbox_id: Optional[str] = None,
    event_callback=None,
) -> ToucanTrajectory:
    """
    为单个问题生成单轮 Agent 轨迹

    流程:
      1. 构建 tools schema + system prompt
      2. Agent 循环: LLM 决策 → 工具调用 → 结果反馈
      3. 沙箱辅助执行（代码等需要隔离环境的操作）
      4. 采集完整轨迹

    Args:
        checked_question: 通过质检的问题
        servers: 相关的 MCP Server 列表
        config: Pipeline 配置
        sandbox_id: 沙箱 ID（可选，用于代码执行）
        event_callback: 事件回调

    Returns:
        ToucanTrajectory: 采集的轨迹
    """
    q = checked_question.question
    traj_id = f"traj_{q.question_id}_{int(time.time())}"

    def emit(msg):
        if event_callback:
            event_callback("trajectory_gen", msg)

    # ── 1. 构建 tools 和 prompt ──
    tools_schema = build_tools_schema(servers)

    system_prompt = f"""你是一个能够使用各种工具来完成用户任务的AI助手。

## 可用工具
你可以通过 function calling 来调用以下工具。每个工具的名称格式为: server_id__tool_name

## 工具调用规范
- 仔细分析用户需求，选择最合适的工具
- 可以连续调用多个工具来完成复杂任务
- 调用工具时提供完整、准确的参数
- 根据工具返回结果进行下一步推理
- 任务完成后给出清晰的总结

## 注意
- 如果工具调用失败，分析原因并尝试替代方案
- 优先使用最直接相关的工具
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": q.question})

    trajectory = ToucanTrajectory(
        trajectory_id=traj_id,
        question=q.question,
        question_id=q.question_id,
        target_servers=q.target_servers,
        target_tools=q.target_tools,
        tools_schema=tools_schema,
    )

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    # ── 2. Agent 循环 ──
    for step_id in range(1, config.max_steps + 1):
        emit(f"  Step {step_id}: Agent 推理中...")

        try:
            response = client.chat.completions.create(
                model=config.agent_model,
                messages=messages,
                tools=tools_schema if tools_schema else None,
                temperature=config.agent_temperature,
                max_tokens=2048,
                stream=False,
            )

            trajectory.total_tokens += response.usage.total_tokens if response.usage else 0
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

                for tc in assistant_msg.tool_calls:
                    func_name = tc.function.name
                    try:
                        func_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        func_args = {"raw": tc.function.arguments}

                    emit(f"    🔧 调用工具: {func_name}")

                    # 解析 server_id 和 tool_name
                    parts = func_name.split("__", 1)
                    server_id = parts[0] if len(parts) > 1 else ""
                    tool_name = parts[1] if len(parts) > 1 else func_name

                    # 找到对应的 server URL
                    server_url = None
                    for s in servers:
                        if s.server_id == server_id:
                            server_url = s.url
                            break
                    if not server_url:
                        server_url = f"{SMITHERY_SERVER_BASE}/{server_id}"

                    # 调用 MCP 工具
                    result = await call_mcp_tool(
                        server_url=server_url,
                        tool_name=tool_name,
                        arguments=func_args,
                        api_key=config.smithery_api_key,
                    )

                    tool_call_record = ToolCall(
                        tool_name=func_name,
                        tool_input=func_args,
                        tool_output=result["result"],
                        server_id=server_id,
                        success=result["success"],
                        duration_ms=result["duration_ms"],
                    )
                    step_tool_calls.append(tool_call_record)
                    trajectory.total_tool_calls += 1
                    if result["success"]:
                        trajectory.successful_tool_calls += 1

                    # 将工具结果反馈给 Agent
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result["result"],
                    })

                    emit(f"      {'✓' if result['success'] else '✗'} "
                         f"({result['duration_ms']}ms)")

                # 记录轨迹步骤
                trajectory.steps.append(asdict(TrajectoryStep(
                    step_id=step_id,
                    role="assistant",
                    content=assistant_msg.content or "",
                    thought=assistant_msg.content or "",
                    tool_calls=[asdict(tc) for tc in step_tool_calls],
                )))

            else:
                # 没有工具调用 — Agent 给出最终回答
                trajectory.steps.append(asdict(TrajectoryStep(
                    step_id=step_id,
                    role="assistant",
                    content=assistant_msg.content or "",
                    thought="",
                )))

                # 检查是否完成
                if choice.finish_reason == "stop":
                    emit(f"  ✓ Agent 完成回答（共 {step_id} 步）")
                    break

        except Exception as e:
            emit(f"  ⚠ Step {step_id} 异常: {e}")
            trajectory.steps.append(asdict(TrajectoryStep(
                step_id=step_id,
                role="error",
                content=f"执行异常: {str(e)[:500]}",
            )))
            break

    # 保存完整 messages
    trajectory.messages = messages

    return trajectory


# ─── Step 3.2: 多轮轨迹扩展 ────────────────────────────────────────────────────

async def expand_to_multi_turn(
    trajectory: ToucanTrajectory,
    config: ToucanPipelineConfig,
    servers: list,
    event_callback=None,
) -> ToucanTrajectory:
    """
    将单轮轨迹扩展为多轮对话

    方法:
      1. 基于已有对话上下文，用 LLM 生成后续问题
      2. 继续 Agent 循环处理后续问题
      3. 合并为多轮轨迹

    Args:
        trajectory: 单轮轨迹
        config: Pipeline 配置
        servers: MCP Server 列表
        event_callback: 事件回调

    Returns:
        ToucanTrajectory: 扩展后的多轮轨迹
    """
    def emit(msg):
        if event_callback:
            event_callback("multi_turn", msg)

    emit("[Step 3.2] 生成后续问题...")

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    # 生成后续问题
    followup_prompt = f"""基于以下对话历史，生成1-2个自然的后续问题。
后续问题应该：
1. 与原始对话主题相关但有延伸
2. 可能需要使用新的工具或组合使用工具
3. 是用户自然会继续追问的

对话历史:
用户: {trajectory.question}
助手: {trajectory.steps[-1].get('content', '') if trajectory.steps else '(无回答)'}

请输出 JSON 数组，每个元素是一个后续问题字符串:
["后续问题1", "后续问题2"]
"""

    try:
        resp = client.chat.completions.create(
            model=config.agent_model,
            messages=[{"role": "user", "content": followup_prompt}],
            temperature=0.8,
            max_tokens=512,
        )

        raw = resp.choices[0].message.content
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]

        followup_questions = json.loads(raw.strip())

    except Exception as e:
        emit(f"  ⚠ 后续问题生成失败: {e}")
        return trajectory

    # 继续 Agent 循环
    messages = list(trajectory.messages)  # 复制原始对话
    tools_schema = trajectory.tools_schema
    current_step = len(trajectory.steps)

    for fq in followup_questions[:2]:  # 最多2个后续问题
        emit(f"  追问: {fq[:60]}...")
        messages.append({"role": "user", "content": fq})

        trajectory.steps.append(asdict(TrajectoryStep(
            step_id=current_step + 1,
            role="user",
            content=fq,
        )))
        current_step += 1

        # Agent 回答后续问题（简化版，最多3步）
        for sub_step in range(3):
            try:
                response = client.chat.completions.create(
                    model=config.agent_model,
                    messages=messages,
                    tools=tools_schema if tools_schema else None,
                    temperature=config.agent_temperature,
                    max_tokens=2048,
                )

                trajectory.total_tokens += response.usage.total_tokens if response.usage else 0
                choice = response.choices[0]
                assistant_msg = choice.message

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

                if assistant_msg.tool_calls:
                    step_tool_calls = []
                    for tc in assistant_msg.tool_calls:
                        func_name = tc.function.name
                        try:
                            func_args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            func_args = {}

                        parts = func_name.split("__", 1)
                        server_id = parts[0] if len(parts) > 1 else ""
                        tool_name = parts[1] if len(parts) > 1 else func_name

                        server_url = f"{SMITHERY_SERVER_BASE}/{server_id}"
                        for s in servers:
                            if s.server_id == server_id:
                                server_url = s.url
                                break

                        result = await call_mcp_tool(
                            server_url=server_url,
                            tool_name=tool_name,
                            arguments=func_args,
                            api_key=config.smithery_api_key,
                        )

                        step_tool_calls.append(ToolCall(
                            tool_name=func_name,
                            tool_input=func_args,
                            tool_output=result["result"],
                            server_id=server_id,
                            success=result["success"],
                            duration_ms=result["duration_ms"],
                        ))
                        trajectory.total_tool_calls += 1
                        if result["success"]:
                            trajectory.successful_tool_calls += 1

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result["result"],
                        })

                    current_step += 1
                    trajectory.steps.append(asdict(TrajectoryStep(
                        step_id=current_step,
                        role="assistant",
                        content=assistant_msg.content or "",
                        tool_calls=[asdict(tc) for tc in step_tool_calls],
                    )))
                else:
                    current_step += 1
                    trajectory.steps.append(asdict(TrajectoryStep(
                        step_id=current_step,
                        role="assistant",
                        content=assistant_msg.content or "",
                    )))
                    break

            except Exception as e:
                emit(f"  ⚠ 多轮续写异常: {e}")
                break

    trajectory.subset = "multi-turn"
    trajectory.messages = messages
    return trajectory


# ─── 整合: 执行 Step 3 完整流程 ────────────────────────────────────────────────

async def run_step3(
    checked_questions: list,
    servers: list,
    config: ToucanPipelineConfig,
    event_callback=None,
) -> list:
    """
    执行 Step 3 完整流程: Agent 轨迹生成

    Args:
        checked_questions: 通过质检的问题列表
        servers: MCP Server 列表
        config: Pipeline 配置
        event_callback: 事件回调

    Returns:
        list[ToucanTrajectory]: 生成的轨迹列表
    """
    def emit(msg):
        print(f"  {msg}")
        if event_callback:
            event_callback("trajectory_gen", msg)

    trajectories = []
    sandbox_id = None

    # 创建沙箱（如果需要代码执行环境）
    try:
        emit("[Step 3.0] 创建沙箱实例...")
        sandbox_id = await create_sandbox()
        emit(f"  沙箱已创建: {sandbox_id[:12]}...")
        await asyncio.sleep(3)
    except Exception as e:
        emit(f"  ⚠ 沙箱创建失败（将跳过沙箱功能）: {e}")

    for i, cq in enumerate(checked_questions):
        emit(f"\n[Step 3.1] 生成轨迹 {i+1}/{len(checked_questions)}: {cq.question.question[:50]}...")

        # 找到相关的 servers
        relevant_servers = []
        for sid in cq.question.target_servers:
            for s in servers:
                if s.server_id == sid:
                    relevant_servers.append(s)
                    break
        if not relevant_servers:
            relevant_servers = servers[:3]  # 默认使用前3个

        # 生成单轮轨迹
        traj = await generate_single_turn_trajectory(
            checked_question=cq,
            servers=relevant_servers,
            config=config,
            sandbox_id=sandbox_id,
            event_callback=event_callback,
        )

        # 可选: 扩展为多轮
        if config.enable_multi_turn:
            emit("[Step 3.2] 扩展为多轮对话...")
            traj = await expand_to_multi_turn(
                trajectory=traj,
                config=config,
                servers=relevant_servers,
                event_callback=event_callback,
            )

        trajectories.append(traj)
        emit(f"  ✓ 轨迹完成: {traj.total_tool_calls} 次工具调用, "
             f"{traj.successful_tool_calls} 次成功")

    # 清理沙箱
    if sandbox_id:
        try:
            await delete_sandbox(sandbox_id)
            emit("  沙箱已清理")
        except Exception:
            pass

    return trajectories
