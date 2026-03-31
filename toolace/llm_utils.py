"""
ToolACE LLM 工具函数
封装 DeepSeek API 调用，统一 JSON 解析和错误处理
"""

import json
import re
from typing import Optional

from openai import OpenAI

from .config import LLMConfig, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL


_client: Optional[OpenAI] = None


def get_client(config: LLMConfig = None) -> OpenAI:
    """获取或创建 LLM 客户端"""
    global _client
    api_key = config.api_key if config and config.api_key else DEEPSEEK_API_KEY
    base_url = config.base_url if config and config.base_url else DEEPSEEK_BASE_URL
    if _client is None or (config and config.api_key):
        _client = OpenAI(api_key=api_key, base_url=base_url)
    return _client


def call_llm(
    messages: list,
    config: LLMConfig = None,
    model: str = None,
    temperature: float = None,
    max_tokens: int = None,
    tools: list = None,
) -> dict:
    """
    调用 LLM，返回标准化结果

    Returns:
        {
            "content": str,
            "tool_calls": list | None,
            "tokens": int,
        }
    """
    config = config or LLMConfig()
    client = get_client(config)

    kwargs = {
        "model": model or config.model,
        "messages": messages,
        "temperature": temperature if temperature is not None else config.temperature,
        "max_tokens": max_tokens or config.max_tokens,
        "stream": False,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    try:
        response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        tokens = response.usage.total_tokens if response.usage else 0

        tool_calls = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return {
            "content": message.content or "",
            "tool_calls": tool_calls,
            "tokens": tokens,
        }
    except Exception as e:
        return {
            "content": f"[LLM调用失败: {e}]",
            "tool_calls": None,
            "tokens": 0,
        }


def call_llm_json(
    messages: list,
    config: LLMConfig = None,
    temperature: float = None,
) -> tuple:
    """
    调用 LLM 并解析 JSON 结果

    Returns:
        (parsed_dict, tokens)
    """
    result = call_llm(messages, config, temperature=temperature)
    content = result["content"]
    tokens = result["tokens"]

    parsed = parse_json_from_text(content)
    return parsed, tokens


def parse_json_from_text(text: str) -> dict:
    """从 LLM 输出中提取 JSON"""
    if not text:
        return {}

    # 尝试 ```json ... ``` 格式
    json_pattern = r'```json\s*(.*?)\s*```'
    matches = re.findall(json_pattern, text, re.DOTALL)
    if matches:
        try:
            return json.loads(matches[0])
        except json.JSONDecodeError:
            pass

    # 尝试 ``` ... ``` 格式
    code_pattern = r'```\s*(.*?)\s*```'
    matches = re.findall(code_pattern, text, re.DOTALL)
    if matches:
        try:
            return json.loads(matches[0])
        except json.JSONDecodeError:
            pass

    # 尝试直接解析整个文本
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 尝试找到第一个 { 和最后一个 }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # 尝试找 JSON 数组
    start = text.find('[')
    end = text.rfind(']')
    if start != -1 and end != -1 and end > start:
        try:
            return {"items": json.loads(text[start:end + 1])}
        except json.JSONDecodeError:
            pass

    return {}


def parse_json_array_from_text(text: str) -> list:
    """从 LLM 输出中提取 JSON 数组"""
    if not text:
        return []

    # 尝试 ```json ... ``` 格式
    json_pattern = r'```json\s*(.*?)\s*```'
    matches = re.findall(json_pattern, text, re.DOTALL)
    if matches:
        try:
            result = json.loads(matches[0])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 尝试直接解析
    try:
        result = json.loads(text.strip())
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # 尝试找 [ ... ]
    start = text.find('[')
    end = text.rfind(']')
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []
