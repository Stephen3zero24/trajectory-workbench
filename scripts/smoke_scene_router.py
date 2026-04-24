"""A-2-2 烟测:scene_router 在 8 条用例上的路由准确率。

前置:
  - VibeDataBot 平台跑在 http://127.0.0.1:3000
  - DEEPSEEK_API_KEY 已在平台侧 .env.local 配置

用法:
  python scripts/smoke_scene_router.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trajectory_agent.llm_client import LLMClient  # noqa: E402
from trajectory_agent.scene_router import RouterDecision, route_scene  # noqa: E402


TEST_CASES: list[tuple[str, str, str]] = [
    ("帮我基于'大语言模型对齐'这个主题合成一批搜索问答数据", "search2qa", "明确主题+QA"),
    ("我要生成些医疗领域的 QA 对用于模型训练", "search2qa", "明确 QA 对"),
    ("我想要用于训练 function calling 能力的多轮工具调用轨迹", "toolace", "明确 function calling"),
    ("需要多角色背景的工具调用训练数据", "toolace", "8 种角色特征"),
    ("基于 Smithery MCP Server 生成工具调用训练数据,要带质量评分", "toucan", "明确 MCP+Smithery"),
    ("我要做 MCP 生态里的工具调用数据合成", "toucan", "明确 MCP"),
    ("我要一些训练数据", "*low_confidence", "极度模糊应低置信度"),
    ("帮我写一份本周工作周报", "unknown", "完全跑题"),
]


def check_case(expected: str, decision: RouterDecision) -> bool:
    if expected == "*low_confidence":
        return decision.low_confidence is True
    return decision.scene == expected


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    client = LLMClient()
    print(f"-- target: {client.base_url}")
    print(f"-- total cases: {len(TEST_CASES)}")
    print()

    passed = 0
    results: list[tuple[int, str, bool, RouterDecision | None, BaseException | None]] = []
    for idx, (req, expected, note) in enumerate(TEST_CASES, start=1):
        try:
            decision = await route_scene(req, client)
        except Exception as e:
            print(f"[FAIL] case-{idx} ({note}): raised {type(e).__name__}: {e}")
            print(f"        user_request: {req}")
            results.append((idx, note, False, None, e))
            continue

        ok = check_case(expected, decision)
        tag = "[PASS]" if ok else "[FAIL]"
        if ok:
            passed += 1
        print(
            f"{tag} case-{idx} ({note}): expected={expected} "
            f"scene={decision.scene} conf={decision.confidence:.2f} "
            f"low_conf={decision.low_confidence}"
        )
        print(f"        user_request: {req}")
        print(f"        reasoning:    {decision.reasoning}")
        results.append((idx, note, ok, decision, None))

    total = len(TEST_CASES)
    accuracy = passed / total if total else 0.0
    print()
    print(f"-- total: {total}  passed: {passed}  accuracy: {accuracy:.0%}")

    if accuracy >= 0.9:
        print("✅ ALL PASSED (>= 90%)")
        return 0

    print("❌ ACCURACY BELOW THRESHOLD (< 90%)")
    print()
    print("Failed cases:")
    for idx, note, ok, decision, err in results:
        if ok:
            continue
        if err is not None:
            print(f"  - case-{idx} ({note}): {type(err).__name__}: {err}")
        elif decision is not None:
            print(
                f"  - case-{idx} ({note}): "
                f"scene={decision.scene} conf={decision.confidence:.2f} "
                f"low_conf={decision.low_confidence} reasoning={decision.reasoning!r}"
            )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
