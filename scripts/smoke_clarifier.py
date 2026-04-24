"""A-2-3b 烟测:Clarifier 在 6 条用例上的行为。

前置:
  - VibeDataBot 平台跑在 http://127.0.0.1:3000
  - DEEPSEEK_API_KEY 已在平台侧 .env.local 配置

用法:
  python scripts/smoke_clarifier.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trajectory_agent.clarifier import ClarifyOutcome, clarify  # noqa: E402
from trajectory_agent.llm_client import LLMClient  # noqa: E402


# (scene, user_request, expected_status, expected_check_key, note)
TEST_CASES: list[tuple[str, str, str, str, str]] = [
    ("search2qa", "帮我基于'大语言模型对齐'这个主题合成问答数据",
     "ready", "task_desc_has_alignment", "search2qa must 齐"),
    ("search2qa", "帮我合成一批训练数据",
     "questions", "asks_task_desc", "search2qa must 缺"),
    ("toolace", "给我合成 100 条工具调用轨迹",
     "ready", "task_count_eq_100_int", "toolace soft 齐"),
    ("toolace", "给我合成一批工具调用训练数据",
     "questions", "asks_task_count", "toolace soft 缺"),
    ("toolace", "",
     "questions", "asks_task_count", "空输入"),
    ("search2qa", "基于医疗问诊合成 200 条数据",
     "ready", "task_desc_has_yiliao_no_task_count", "search2qa 带数量但 rule 不收"),
]


def _asks_for(outcome: ClarifyOutcome, field_name: str) -> bool:
    return any(q.get("field") == field_name for q in outcome.questions)


def check(outcome: ClarifyOutcome, expected_status: str, key: str) -> tuple[bool, str]:
    if outcome.status != expected_status:
        return False, f"status={outcome.status!r} want={expected_status!r}"

    if key == "task_desc_has_alignment":
        val = outcome.params.get("task_desc", "")
        if not isinstance(val, str) or not val:
            return False, f"task_desc missing/empty: {val!r}"
        if not any(s in val for s in ("对齐", "alignment", "Alignment")):
            return False, f"task_desc={val!r} does not mention 对齐/alignment"
        return True, f"task_desc={val!r}"

    if key == "asks_task_desc":
        if not _asks_for(outcome, "task_desc"):
            return False, f"no question for task_desc; got {outcome.questions}"
        return True, f"asked for task_desc"

    if key == "task_count_eq_100_int":
        tc = outcome.params.get("task_count")
        if not isinstance(tc, int) or isinstance(tc, bool):
            return False, f"task_count type={type(tc).__name__} value={tc!r}"
        if tc != 100:
            return False, f"task_count={tc!r} want=100"
        return True, f"task_count={tc} (int)"

    if key == "asks_task_count":
        if not _asks_for(outcome, "task_count"):
            return False, f"no question for task_count; got {outcome.questions}"
        return True, f"asked for task_count"

    if key == "task_desc_has_yiliao_no_task_count":
        val = outcome.params.get("task_desc", "")
        if not isinstance(val, str) or not val:
            return False, f"task_desc missing/empty: {val!r}"
        if "医疗" not in val:
            return False, f"task_desc={val!r} does not mention 医疗"
        if "task_count" in outcome.params:
            return False, f"unexpected task_count in params: {outcome.params['task_count']!r}"
        return True, f"task_desc={val!r}, no task_count"

    return False, f"unknown check key {key!r}"


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    client = LLMClient()
    print(f"-- target: {client.base_url}")
    print(f"-- total cases: {len(TEST_CASES)}")
    print()

    passed = 0
    for idx, (scene, req, expected, key, note) in enumerate(TEST_CASES, start=1):
        try:
            outcome = await clarify(req, scene, client)
        except Exception as e:
            print(f"[FAIL] case-{idx} ({note}): raised {type(e).__name__}: {e}")
            print(f"        scene={scene!r} user_request={req!r}")
            continue

        ok, detail = check(outcome, expected, key)
        tag = "[PASS]" if ok else "[FAIL]"
        if ok:
            passed += 1
        print(f"{tag} case-{idx} ({note}): expected={expected} | {detail}")
        print(f"        scene={scene!r} user_request={req!r}")
        print(f"        outcome.status={outcome.status} extracted={outcome.extracted}")
        if outcome.status == "ready":
            print(f"        params={outcome.params}")
        else:
            print(f"        questions={[q['field'] for q in outcome.questions]}")

    total = len(TEST_CASES)
    accuracy = passed / total if total else 0.0
    print()
    print(f"-- total: {total}  passed: {passed}  accuracy: {accuracy:.0%}")
    if passed == total:
        print("✅ ALL PASSED")
        return 0
    print("❌ SOME FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
