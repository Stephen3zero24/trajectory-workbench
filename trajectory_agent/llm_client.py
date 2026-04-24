"""LLMClient — 调用 VibeDataBot 平台侧 /api/llm/chat 代理的异步客户端。

A-2-1 设计选择:
- HTTP 调用平台代理(服务端持有 DEEPSEEK_API_KEY),非流式。
- 错误分三类:代理不可达(可重试)/ 请求构造错(不可重试)/ 响应体不符合预期。
- 只对"代理不可达"做一次重试(间隔 1s),400 与解析失败都不重试。
- httpx 客户端每次调用新建,和 trajectory_agent/dispatcher.py 保持一致。
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


DEFAULT_LLM_CHAT_URL = "http://127.0.0.1:3000/api/llm/chat"

_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE
)


class LLMClientError(Exception):
    """LLMClient 所有错误的共同基类。"""


class LLMProxyUnavailable(LLMClientError):
    """/api/llm/chat 代理不可达:网络错误 / 连接或读超时 / HTTP 5xx。可重试。"""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
        body: str | None = None,
    ):
        super().__init__(message)
        self.url = url
        self.status = status
        self.body = body


class LLMProxyBadRequest(LLMClientError):
    """/api/llm/chat 返回 HTTP 400:请求构造有误。不可重试。"""

    def __init__(self, message: str, *, url: str | None = None, body: str | None = None):
        super().__init__(message)
        self.url = url
        self.body = body


class LLMResponseInvalid(LLMClientError):
    """HTTP 200 但响应体不符合预期,例如 json_mode 下 content 非合法 JSON。"""

    def __init__(self, message: str, *, content: str | None = None):
        super().__init__(message)
        self.content = content


def _strip_json_fence(content: str) -> str:
    """剥除 ```json ... ``` / ``` ... ``` 整体包裹;其他情况原样返回。"""
    m = _JSON_FENCE_RE.match(content)
    if m:
        return m.group(1)
    return content


class LLMClient:
    """异步 LLM 代理客户端。

    base_url 读取优先级:构造参数 > 环境变量 LLM_CHAT_URL > DEFAULT_LLM_CHAT_URL。
    """

    def __init__(self, base_url: str | None = None, timeout: float = 60.0):
        self.base_url = (
            base_url or os.environ.get("LLM_CHAT_URL") or DEFAULT_LLM_CHAT_URL
        )
        self.timeout = timeout

    async def chat_text(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        """发一次非 JSON 模式 chat,返回原始 content 字符串。"""
        data = await self._post_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )
        return data.get("content", "")

    async def chat_json(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> dict:
        """发一次 JSON 模式 chat,返回解析后的 dict。content 若被 ```json``` 包裹会先剥除。"""
        data = await self._post_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        content = data.get("content", "") or ""
        cleaned = _strip_json_fence(content)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise LLMResponseInvalid(
                f"chat_json content 不是合法 JSON: {e}",
                content=content[:500],
            ) from e
        if not isinstance(parsed, dict):
            raise LLMResponseInvalid(
                f"chat_json content 解析后不是 JSON 对象,而是 {type(parsed).__name__}",
                content=content[:500],
            )
        return parsed

    async def _post_with_retry(
        self,
        *,
        messages: list[dict],
        model: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> dict:
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
        }
        if model:
            payload["model"] = model

        max_attempts = 2
        retry_delay = 1.0
        last_err: LLMProxyUnavailable | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._post_once(payload, attempt=attempt)
            except LLMProxyUnavailable as e:
                last_err = e
                if attempt >= max_attempts:
                    break
                logger.warning(
                    "llm chat proxy unavailable (attempt %d/%d), retry in %.1fs: %s",
                    attempt,
                    max_attempts,
                    retry_delay,
                    e,
                )
                await asyncio.sleep(retry_delay)

        assert last_err is not None
        logger.error("llm chat failed after %d attempts: %s", max_attempts, last_err)
        raise last_err

    async def _post_once(self, payload: dict, *, attempt: int) -> dict:
        url = self.base_url
        logger.info(
            "llm chat request url=%s attempt=%d model=%s json_mode=%s msgs=%d",
            url,
            attempt,
            payload.get("model", "<default>"),
            payload.get("json_mode"),
            len(payload["messages"]),
        )
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
        except httpx.TransportError as e:
            raise LLMProxyUnavailable(
                f"无法连接 /api/llm/chat ({type(e).__name__}): {e}",
                url=url,
            ) from e

        elapsed = time.monotonic() - start
        status = resp.status_code
        body_text = resp.text

        if status == 400:
            raise LLMProxyBadRequest(
                f"/api/llm/chat 返回 400: {body_text[:300]}",
                url=url,
                body=body_text[:500],
            )
        if status >= 500:
            raise LLMProxyUnavailable(
                f"/api/llm/chat 返回 {status}: {body_text[:300]}",
                url=url,
                status=status,
                body=body_text[:500],
            )
        if status != 200:
            raise LLMProxyUnavailable(
                f"/api/llm/chat 非预期状态码 {status}: {body_text[:300]}",
                url=url,
                status=status,
                body=body_text[:500],
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMResponseInvalid(
                f"/api/llm/chat 响应 body 不是合法 JSON: {e}",
                content=body_text[:500],
            ) from e

        if not isinstance(data, dict):
            raise LLMResponseInvalid(
                f"/api/llm/chat 响应 body 不是对象,而是 {type(data).__name__}",
                content=body_text[:500],
            )

        if data.get("error"):
            raise LLMResponseInvalid(
                f"/api/llm/chat 返回 200 但 body 携带 error: {data.get('message')}",
                content=body_text[:500],
            )

        logger.info(
            "llm chat response elapsed=%.2fs tokens=%s model=%s",
            elapsed,
            data.get("tokens"),
            data.get("model"),
        )
        return data
