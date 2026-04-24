"""A-2-1 烟测:验证 trajectory_agent.llm_client 的三条路径。

前置:
  - VibeDataBot 平台跑在 http://127.0.0.1:3000
  - DEEPSEEK_API_KEY 已在平台侧 .env.local 配置

用法:
  python scripts/smoke_llm_client.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trajectory_agent.llm_client import LLMClient, LLMProxyUnavailable  # noqa: E402


def _ok(name: str, detail: str = "") -> None:
    print(f"✅ [{name}] {detail}")


def _ko(name: str, err: BaseException | str) -> None:
    if isinstance(err, BaseException):
        print(f"❌ [{name}] {type(err).__name__}: {err}")
    else:
        print(f"❌ [{name}] {err}")


async def case_1_chat_text(client: LLMClient) -> bool:
    try:
        result = await client.chat_text(
            [{"role": "user", "content": "Say 'pong' and nothing else."}]
        )
    except Exception as e:
        _ko("case-1 chat_text", e)
        return False
    if "pong" not in result.lower():
        _ko("case-1 chat_text", f"response does not contain 'pong': {result!r}")
        return False
    _ok("case-1 chat_text", f"content={result.strip()[:80]!r}")
    return True


async def case_2_chat_json(client: LLMClient) -> bool:
    try:
        result = await client.chat_json(
            [
                {
                    "role": "user",
                    "content": "Return a JSON object with key 'status' and value 'ok'.",
                }
            ]
        )
    except Exception as e:
        _ko("case-2 chat_json", e)
        return False
    if not isinstance(result, dict) or result.get("status") != "ok":
        _ko("case-2 chat_json", f"unexpected dict: {result!r}")
        return False
    _ok("case-2 chat_json", f"parsed={result}")
    return True


async def case_3_unavailable() -> bool:
    bad_client = LLMClient(
        base_url="http://127.0.0.1:59999/api/llm/chat", timeout=3.0
    )
    try:
        await bad_client.chat_text([{"role": "user", "content": "hi"}])
    except LLMProxyUnavailable as e:
        _ok("case-3 unavailable", f"raised LLMProxyUnavailable: {e}")
        return True
    except Exception as e:
        _ko("case-3 unavailable", e)
        return False
    _ko("case-3 unavailable", "expected LLMProxyUnavailable but call succeeded")
    return False


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    client = LLMClient()
    print(f"-- target: {client.base_url}")

    ok1 = await case_1_chat_text(client)
    ok2 = await case_2_chat_json(client)
    ok3 = await case_3_unavailable()

    all_ok = ok1 and ok2 and ok3
    print("-- all passed" if all_ok else "-- some failed")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
