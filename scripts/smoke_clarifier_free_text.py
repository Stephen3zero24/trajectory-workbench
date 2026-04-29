#!/usr/bin/env python3
"""Free-text path live LLM smoke (M0-T2).

Calls real DeepSeek via VibeDataBot proxy at http://127.0.0.1:3000/api/llm/chat
to verify the clarifier prompt guides correct free_text + enum extraction.

5 cases:
  1. explicit domain+scale          -> seed="医疗问诊", _scale_preset="batch"
  2. variant phrasing+ambiguous     -> seed="法律咨询", _scale_preset="standard"
  3. fully ambiguous                -> seed=null, qa_mode=null
  4. enum+free_text mix            -> seed="医疗问诊", qa_mode="question"
  5. robustness probe (observation) -> seed=null preferred, "医疗问诊" if extracted is OK
"""

import asyncio
import json
import sys
from pathlib import Path

# add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trajectory_agent.clarifier import _build_extract_prompt
from trajectory_agent.llm_client import LLMClient
from trajectory_agent.skill_manifest_loader import SceneCatalogEntry


def make_entry() -> SceneCatalogEntry:
    def seed_item():
        return {
            "param": "seed",
            "question": "请提供搜索种子词——通常是一个领域或主题(例如:医疗问诊)",
            "options": (),
        }

    def qa_mode_item():
        return {
            "param": "qa_mode",
            "question": "你希望的合成模式是？",
            "options": (
                {"value": "question", "label": "Q 模式", "hint": "种子词→问题"},
                {"value": "answer", "label": "A 模式", "hint": "答案→问题"},
            ),
        }

    def scale_preset_item():
        return {
            "param": "_scale_preset",
            "question": "合成规模？",
            "options": (
                {"value": "fast", "label": "快速验证", "hint": "1 样本"},
                {"value": "standard", "label": "标准", "hint": "5 样本"},
                {"value": "batch", "label": "批量", "hint": "20 样本"},
            ),
        }

    return SceneCatalogEntry(
        scene_id="search2qa",
        description="smoke test scene",
        must_clarify=(seed_item(), qa_mode_item(), scale_preset_item()),
        defaults={},
        required_params=(),
    )


CASES = [
    {
        "name": "1. explicit domain+scale",
        "input": "我想合成医疗问诊领域的批量数据",
        "expect": {
            "seed": "医疗问诊",
            "_scale_preset": "batch",
            "qa_mode": "ANY",
        },
    },
    {
        "name": "2. variant phrasing+ambiguous",
        "input": "做点法律咨询方向的训练数据,数量不用太多",
        "expect": {
            "seed": "法律咨询",
            "_scale_preset": "standard",
            "qa_mode": "ANY",
        },
    },
    {
        "name": "3. fully ambiguous",
        "input": "给我合成一些数据",
        "expect": {
            "seed": None,
            "qa_mode": None,
            "_scale_preset": "ANY",
        },
    },
    {
        "name": "4. enum+free_text mix",
        "input": "用 Q 模式做医疗问诊数据",
        "expect": {
            "seed": "医疗问诊",
            "qa_mode": "question",
            "_scale_preset": "ANY",
        },
    },
    {
        "name": "5. robustness probe (observation only)",
        "input": "我有一份医疗问诊的客户名单",
        "expect": {
            "seed": "ANY",
            "qa_mode": "ANY",
            "_scale_preset": "ANY",
        },
        "observation_only": True,
    },
]


async def run_case(client: LLMClient, entry: SceneCatalogEntry, case: dict) -> dict:
    prompt = _build_extract_prompt(entry)
    # truncated prompt for display
    prompt_lines = prompt.split("\n")
    prompt_head_tail = (
        "\n".join(prompt_lines[:3]) + "\n  ... [truncated] ...\n" + "\n".join(prompt_lines[-3:])
    )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": case["input"]},
    ]

    raw_content = await client.chat_text(messages, temperature=0.1, max_tokens=512)

    # parse JSON from content
    raw_content_stripped = raw_content.strip()
    # try to find JSON in the content
    json_str = raw_content_stripped
    # strip markdown fences
    if json_str.startswith("```"):
        lines = json_str.split("\n")
        json_str = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        params = json.loads(json_str)
    except json.JSONDecodeError as e:
        return {
            "name": case["name"],
            "input": case["input"],
            "raw": raw_content_stripped,
            "params": None,
            "parse_error": str(e),
            "status": "FAIL",
            "prompt_head_tail": prompt_head_tail,
        }

    # evaluate
    expect = case["expect"]
    observation_only = case.get("observation_only", False)

    seed_ok = expect["seed"] == "ANY" or params.get("seed") == expect["seed"]
    qa_ok = expect["qa_mode"] == "ANY" or params.get("qa_mode") == expect["qa_mode"]
    scale_ok = expect["_scale_preset"] == "ANY" or params.get("_scale_preset") == expect["_scale_preset"]

    if observation_only:
        status = "OBSERVE"
    elif seed_ok and qa_ok and scale_ok:
        status = "PASS"
    else:
        status = "FAIL"

    return {
        "name": case["name"],
        "input": case["input"],
        "raw": raw_content_stripped,
        "params": params,
        "status": status,
        "seed_ok": seed_ok,
        "qa_ok": qa_ok,
        "scale_ok": scale_ok,
        "prompt_head_tail": prompt_head_tail,
        "observation_only": observation_only,
    }


async def main():
    entry = make_entry()
    client = LLMClient(timeout=60.0)

    print("=" * 70)
    print("SMOKE: free_text path live LLM (M0-T2)")
    print("=" * 70)

    results = []
    for case in CASES:
        print(f"\n{'='*70}")
        print(f"CASE: {case['name']}")
        print(f"INPUT: {case['input']}")
        print(f"EXPECT: {case['expect']}")
        print("-" * 70)

        result = await run_case(client, entry, case)

        print(f"PROMPT (head+tail):\n{result['prompt_head_tail']}")
        print(f"\nLLM RAW RESPONSE:\n{result['raw']}")
        print(f"\nPARSED PARAMS: {result['params']}")

        if result.get("parse_error"):
            print(f"\nJSON PARSE ERROR: {result['parse_error']}")
            print(f"\nSTATUS: FAIL (JSON parse failed)")
        else:
            print(f"\nseed_ok={result.get('seed_ok')}, qa_ok={result.get('qa_ok')}, scale_ok={result.get('scale_ok')}")
            print(f"\nSTATUS: {result['status']}")

        results.append(result)

    # summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        print(f"  {r['name']}: {r['status']}")

    critical_failures = [r for r in results if r["status"] == "FAIL" and not r.get("observation_only")]
    if critical_failures:
        print(f"\nCRITICAL FAILURES: {len(critical_failures)} (case 1-4 FAIL — see above)")
        sys.exit(1)
    else:
        print("\nAll critical cases PASSED.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
