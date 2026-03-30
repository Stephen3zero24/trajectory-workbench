"""
Search2QA LLM Engine — 多轮 LLM-Tool 交互引擎

核心功能：
- 调用 DeepSeek API（OpenAI 兼容格式）
- 解析 function calling 响应
- 执行工具调用并将结果反馈给 LLM
- 循环直到 LLM 生成完整 QA 输出
"""

import json
import os
import uuid
from typing import Optional

from openai import OpenAI

from tools import execute_tool_call
from trace_manager import TraceManager
from prompts import TOOLS_SCHEMA


# ─── 配置 ─────────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

_llm_client: Optional[OpenAI] = None


def get_llm_client() -> OpenAI:
    """获取或创建 LLM 客户端"""
    global _llm_client
    if _llm_client is None:
        _llm_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    return _llm_client


# ─── LLM 调用 ─────────────────────────────────────────────────────────────────

def call_deepseek_with_tools(
    messages: list,
    model: str = "deepseek-chat",
    temperature: float = 0.7,
    tools: list = None,
) -> dict:
    """
    调用 DeepSeek API，支持 function calling。

    返回:
        {
            "content": str | None,         # 文本回复
            "tool_calls": list | None,     # 工具调用列表
            "tokens": int,                 # token 消耗
            "raw_message": object,         # 原始 message 对象
        }
    """
    client = get_llm_client()
    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 4096,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        tokens = response.usage.total_tokens if response.usage else 0

        # 解析工具调用
        tool_calls = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return {
            "content": message.content,
            "tool_calls": tool_calls,
            "tokens": tokens,
            "raw_message": message,
        }

    except Exception as e:
        return {
            "content": f"[LLM调用失败: {e}]",
            "tool_calls": None,
            "tokens": 0,
            "raw_message": None,
        }


# ─── 回退：手动解析工具调用（兼容不支持 function calling 的模型）──────────────

def parse_tool_calls_from_text(text: str) -> list:
    """
    从文本中手动解析工具调用（回退方案）。

    支持格式：
    <tool_call>{"name": "search", "arguments": {"query": "xxx"}}</tool_call>
    或 JSON 中的 tool_calls 字段
    """
    import re
    tool_calls = []

    # 方式1: <tool_call> 标签
    pattern = r'<tool_call>(.*?)</tool_call>'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match.strip())
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": data.get("name", ""),
                "arguments": data.get("arguments", {}),
            })
        except json.JSONDecodeError:
            continue

    # 方式2: 直接 JSON 格式的工具调用
    if not tool_calls:
        try:
            data = json.loads(text.strip())
            if isinstance(data, dict) and "tool_calls" in data:
                for tc in data["tool_calls"]:
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": tc.get("name", ""),
                        "arguments": tc.get("arguments", {}),
                    })
        except json.JSONDecodeError:
            pass

    return tool_calls


# ─── 核心交互循环 ─────────────────────────────────────────────────────────────

async def llm_with_tools(
    messages: list,
    trace_manager: TraceManager,
    model: str = "deepseek-chat",
    temperature: float = 0.7,
    max_iterations: int = 20,
    log_prefix: str = "",
) -> dict:
    """
    多轮 LLM-Tool 交互循环。

    流程：
    1. 调用 LLM
    2. 如果 LLM 返回工具调用 → 执行工具 → 将结果返回给 LLM → 继续循环
    3. 如果 LLM 返回纯文本 → 检查是否包含完整 QA → 结束

    参数:
        messages: 初始对话上下文
        trace_manager: 轨迹记录器
        model: 模型名称
        temperature: 温度参数
        max_iterations: 最大迭代次数
        log_prefix: 日志前缀

    返回:
        {
            "content": str,       # 最终输出（包含 QA 的文本）
            "qa": dict | None,    # 解析出的 QA 对
            "total_tokens": int,  # 总 token 消耗
            "iterations": int,    # 实际迭代次数
        }
    """
    total_tokens = 0
    final_content = ""

    for iteration in range(1, max_iterations + 1):
        prefix = f"{log_prefix}[Turn {iteration}/{max_iterations}]"
        print(f"  {prefix} 调用 LLM...", flush=True)

        # 1. 调用 LLM
        result = call_deepseek_with_tools(
            messages=messages,
            model=model,
            temperature=temperature,
            tools=TOOLS_SCHEMA,
        )
        total_tokens += result["tokens"]

        # 2. 处理工具调用
        if result["tool_calls"]:
            # 将 assistant 的 tool_calls 消息加入对话
            assistant_msg = {
                "role": "assistant",
                "content": result["content"] or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in result["tool_calls"]
                ],
            }
            messages.append(assistant_msg)

            # 记录 LLM 输出到轨迹
            if result["content"]:
                trace_manager.add_llm_output(result["content"])

            # 逐个执行工具调用
            for tc in result["tool_calls"]:
                tool_name = tc["name"]
                tool_args = tc["arguments"]
                tool_id = tc["id"]

                print(f"  {prefix} 🔧 {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:80]})")

                # 记录工具调用
                trace_manager.add_tool_call(tool_name, tool_args, tool_id)

                # 执行工具
                tool_result = await execute_tool_call(tool_name, tool_args)

                # 记录工具结果
                trace_manager.add_tool_result(tool_result, tool_id)

                # 将结果加入对话
                messages.append({
                    "role": "tool",
                    "content": tool_result[:4000],  # 截断过长结果
                    "tool_call_id": tool_id,
                })

            continue  # 继续下一轮 LLM 调用

        # 3. 没有工具调用 → 检查是否完成
        content = result["content"] or ""
        final_content = content

        # 记录到轨迹
        trace_manager.add_llm_output(content)

        # 将 assistant 消息加入对话
        messages.append({"role": "assistant", "content": content})

        # 检查是否包含有效的 QA
        qa = extract_qa_from_text(content)
        if qa:
            print(f"  {prefix} ✅ 生成 QA 完成")
            return {
                "content": content,
                "qa": qa,
                "total_tokens": total_tokens,
                "iterations": iteration,
            }

        # 如果没有 QA 也没有工具调用，提示 LLM 继续
        messages.append({
            "role": "user",
            "content": "请继续。如果你已经收集到足够的信息，请按照要求的 JSON 格式输出 QA 对。如果还需要更多信息，请调用搜索或爬取工具。",
        })

    # 达到最大迭代次数
    print(f"  {log_prefix} ⚠ 达到最大迭代次数 ({max_iterations})")
    return {
        "content": final_content,
        "qa": extract_qa_from_text(final_content),
        "total_tokens": total_tokens,
        "iterations": max_iterations,
    }


# ─── QA 解析 ──────────────────────────────────────────────────────────────────

def extract_qa_from_text(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 QA 对"""
    if not text:
        return None

    # 尝试从 JSON 代码块中提取
    import re

    # 方式1: ```json ... ```
    json_pattern = r'```json\s*(.*?)\s*```'
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        qa = _try_parse_qa(match)
        if qa:
            return qa

    # 方式2: ``` ... ```
    code_pattern = r'```\s*(.*?)\s*```'
    matches = re.findall(code_pattern, text, re.DOTALL)
    for match in matches:
        qa = _try_parse_qa(match)
        if qa:
            return qa

    # 方式3: 直接尝试解析整个文本
    qa = _try_parse_qa(text)
    if qa:
        return qa

    # 方式4: 从文本中搜索 JSON 对象
    brace_pattern = r'\{[^{}]*"question"[^{}]*"answer"[^{}]*\}'
    matches = re.findall(brace_pattern, text, re.DOTALL)
    for match in matches:
        qa = _try_parse_qa(match)
        if qa:
            return qa

    return None


def _try_parse_qa(text: str) -> Optional[dict]:
    """尝试将文本解析为 QA 对"""
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict) and "question" in data and "answer" in data:
            return {
                "question": data["question"],
                "answer": data["answer"],
                "reasoning": data.get("reasoning", ""),
                "sources": data.get("sources", []),
                "evolution_strategy": data.get("evolution_strategy", ""),
                "original_question": data.get("original_question", ""),
                "evolved_question": data.get("evolved_question", ""),
            }
    except json.JSONDecodeError:
        pass
    return None
