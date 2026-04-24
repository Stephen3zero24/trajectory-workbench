"""A-2-4 E2E 烟测:/api/trajectory-agent/submit 四态响应。

前置:
  - 专项后端跑在 http://127.0.0.1:3100(即 backend.py)
  - VibeDataBot 平台跑在 http://127.0.0.1:3000(提供 /api/llm/chat)
  - 后端要重启一次才能吃到 A-2-4 的 router.py 变更(uvicorn 未开 --reload)

用法:
  python scripts/smoke_submit_e2e.py
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


BACKEND_URL = "http://127.0.0.1:3100"
SUBMIT_URL = f"{BACKEND_URL}/api/trajectory-agent/submit"
RUNS_URL = f"{BACKEND_URL}/api/trajectory-agent/runs"


def _tag(ok: bool) -> str:
    return "[PASS]" if ok else "[FAIL]"


def _dump(body: dict, keys: list[str] | None = None) -> str:
    if keys is None:
        return str(body)
    return "{" + ", ".join(f"{k}={body.get(k)!r}" for k in keys) + "}"


async def _post_submit(
    client: httpx.AsyncClient, body: dict
) -> tuple[int, dict]:
    resp = await client.post(SUBMIT_URL, json=body, timeout=60.0)
    try:
        payload = resp.json()
    except Exception:
        payload = {"__raw__": resp.text}
    return resp.status_code, payload


async def case_1(client: httpx.AsyncClient) -> bool:
    body = {"user_request": "帮我基于'大语言模型对齐'这个主题合成问答数据"}
    status, data = await _post_submit(client, body)
    fail_reason = None
    if status != 200:
        fail_reason = f"expected HTTP 200, got {status}"
    elif data.get("status") != "dispatched":
        fail_reason = f"expected status=dispatched, got {data.get('status')!r}"
    elif data.get("scene") != "search2qa":
        fail_reason = f"expected scene=search2qa, got {data.get('scene')!r}"
    elif not data.get("agent_run_id"):
        fail_reason = f"missing agent_run_id"

    ok = fail_reason is None
    print(
        f"{_tag(ok)} case-1 (dispatched 一把过) "
        f"| http={status} "
        f"| {_dump(data, ['status', 'scene', 'agent_run_id', 'scene_task_id'])}"
    )
    if fail_reason:
        print(f"        reason: {fail_reason}")
        print(f"        body:   {body}")
        return False

    # 再 GET runs/{id} 验证映射已落库
    run_id = data["agent_run_id"]
    resp = await client.get(f"{RUNS_URL}/{run_id}", timeout=30.0)
    if resp.status_code != 200:
        print(
            f"        [WARN] GET runs/{run_id} returned {resp.status_code}:"
            f" {resp.text[:200]}"
        )
    else:
        info = resp.json()
        print(f"        GET runs/{run_id} → scene={info.get('scene')!r}")
    return True


async def case_2(client: httpx.AsyncClient) -> tuple[bool, str]:
    """返回 (ok, actual_status)。actual_status ∈ {clarify_needed, scene_unrecognized, other}。"""
    body = {"user_request": "帮我合成一批数据"}
    status, data = await _post_submit(client, body)
    actual = data.get("status", "other")
    ok = status == 200 and actual in ("clarify_needed", "scene_unrecognized")
    print(
        f"{_tag(ok)} case-2 (clarify_needed 或 scene_unrecognized) "
        f"| http={status} | status={actual!r}"
    )
    if ok:
        if actual == "clarify_needed":
            qs = [q.get("field") for q in (data.get("questions") or [])]
            print(f"        scene={data.get('scene')!r} questions={qs}")
        else:
            print(
                f"        reasoning={data.get('reasoning')!r}"
                f" confidence={data.get('confidence')}"
            )
    else:
        print(f"        raw: {data}")
    return ok, actual


async def case_3(client: httpx.AsyncClient) -> bool:
    """无状态拼接第二轮:复用 Case 5 同款"scene_hint 固定 + user_request 极简"稳定触发 clarify。

    Round 1 用 user_request='数据' + scene_hint='search2qa' 稳定拿到 clarify_needed;
    Round 2 把对 task_desc 的回答拼到 user_request 末尾,验证整条链路闭合。
    """
    round1_body = {"user_request": "数据", "scene_hint": "search2qa"}
    status1, data1 = await _post_submit(client, round1_body)
    round1_fields = [q.get("field") for q in (data1.get("questions") or [])]
    round1_ok = (
        status1 == 200
        and data1.get("status") == "clarify_needed"
        and data1.get("scene") == "search2qa"
        and "task_desc" in round1_fields
    )
    if not round1_ok:
        print(
            f"[FAIL] case-3 (无状态拼接 第二轮) "
            f"| round1 http={status1} status={data1.get('status')!r} "
            f"expected clarify_needed + task_desc question"
        )
        print(f"        round1 body:     {round1_body}")
        print(f"        round1 response: {data1}")
        return False

    round2_body = {
        "user_request": "数据。主题:医疗问诊",
        "scene_hint": "search2qa",
    }
    status2, data2 = await _post_submit(client, round2_body)
    actual = data2.get("status")
    round2_ok = (
        status2 == 200
        and actual == "dispatched"
        and data2.get("scene") == "search2qa"
        and bool(data2.get("agent_run_id"))
    )
    print(
        f"{_tag(round2_ok)} case-3 (无状态拼接 第二轮) "
        f"| round1 status={data1.get('status')!r} questions={round1_fields} "
        f"| round2 http={status2} status={actual!r}"
    )
    print(f"        round1 body:     {round1_body}")
    print(f"        round1 response: {data1}")
    print(f"        round2 body:     {round2_body}")
    print(f"        round2 response: {data2}")

    if round2_ok:
        scene_task_id = data2.get("scene_task_id")
        try:
            r = await client.get(
                f"{BACKEND_URL}/api/tasks/{scene_task_id}", timeout=10.0
            )
            if r.status_code == 200:
                cfg = r.json().get("config", {})
                print(
                    f"        下游 task config.task_desc={cfg.get('task_desc')!r} "
                    f"config.seed={cfg.get('seed')!r}"
                )
            else:
                print(
                    f"        GET /api/tasks/{scene_task_id} returned "
                    f"{r.status_code}: {r.text[:200]}"
                )
        except Exception as e:
            print(
                f"        GET /api/tasks/{scene_task_id} failed: "
                f"{type(e).__name__}: {e}"
            )

    return round2_ok


async def case_4(client: httpx.AsyncClient) -> bool:
    body = {"user_request": "帮我写一份本周工作周报"}
    status, data = await _post_submit(client, body)
    reasoning = data.get("reasoning")
    ok = (
        status == 200
        and data.get("status") == "scene_unrecognized"
        and isinstance(reasoning, str)
        and reasoning.strip() != ""
    )
    print(
        f"{_tag(ok)} case-4 (scene_unrecognized) "
        f"| http={status} | status={data.get('status')!r}"
    )
    if ok:
        print(
            f"        reasoning={reasoning!r} confidence={data.get('confidence')}"
        )
    else:
        print(f"        raw: {data}")
    return ok


async def case_5(client: httpx.AsyncClient) -> bool:
    body = {"user_request": "数据", "scene_hint": "search2qa"}
    status, data = await _post_submit(client, body)
    qs = data.get("questions") or []
    fields = [q.get("field") for q in qs]
    ok = (
        status == 200
        and data.get("status") == "clarify_needed"
        and data.get("scene") == "search2qa"
        and "task_desc" in fields
    )
    print(
        f"{_tag(ok)} case-5 (scene_hint override → clarify) "
        f"| http={status} | status={data.get('status')!r} "
        f"scene={data.get('scene')!r} questions={fields}"
    )
    if not ok:
        print(f"        raw: {data}")
    return ok


async def case_6(client: httpx.AsyncClient) -> bool:
    body = {"user_request": "数据", "scene_hint": "toucan"}
    status, data = await _post_submit(client, body)
    msg = data.get("message") or ""
    ok = (
        status == 200
        and data.get("status") == "scene_unrecognized"
        and "toucan" in msg
    )
    print(
        f"{_tag(ok)} case-6 (scene_hint 非法值) "
        f"| http={status} | status={data.get('status')!r}"
    )
    if ok:
        print(f"        message={msg!r}")
    else:
        print(f"        raw: {data}")
    return ok


async def main() -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    print(f"-- backend: {BACKEND_URL}")

    # 先探活
    async with httpx.AsyncClient() as probe:
        try:
            r = await probe.get(f"{BACKEND_URL}/docs", timeout=3.0)
        except Exception as e:
            print(f"❌ 后端不可达: {type(e).__name__}: {e}")
            print("   请先 cd trajectory-workbench && python backend.py")
            return 2
        if r.status_code != 200:
            print(f"❌ GET /docs 返回 {r.status_code}")
            return 2

    results: list[bool] = []
    async with httpx.AsyncClient() as client:
        results.append(await case_1(client))
        ok2, _actual2 = await case_2(client)
        results.append(ok2)
        results.append(await case_3(client))
        results.append(await case_4(client))
        results.append(await case_5(client))
        results.append(await case_6(client))

    total = len(results)
    passed = sum(results)
    print()
    print(f"-- total: {total}  passed: {passed}")
    if passed == total:
        print("✅ ALL PASSED")
        return 0
    print("❌ SOME FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
