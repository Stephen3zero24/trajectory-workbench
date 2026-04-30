"""pytest 用例 — /api/trajectory-agent/ 的 A-2-4 四态契约。

Mock 策略(不依赖 VibeDataBot 平台):
- 在 trajectory_agent.router 模块上 monkeypatch `route_scene` / `clarify`,
  让它们直接返回预设 RouterDecision / ClarifyOutcome
- 用 FastAPI dependency_overrides 注入一个 TrajectoryAgent,
  它持有一个 AsyncMock 假 dispatcher(dispatch / get_status 均可配置)

这样 pytest 可以在 VibeDataBot :3000 没起的情况下跑绿。
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trajectory_agent.router as router_module
from trajectory_agent.agent import TrajectoryAgent
from trajectory_agent.clarifier import ClarifyOutcome
from trajectory_agent.dispatcher import DispatcherHTTPError
from trajectory_agent.scene_router import RouterDecision


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_dispatcher():
    d = AsyncMock()
    d.dispatch = AsyncMock(return_value={"task_id": "task_abc", "status": "created"})
    d.get_status = AsyncMock(
        return_value={"task_id": "task_abc", "status": "running"}
    )
    return d


@pytest.fixture
def test_agent(fake_dispatcher):
    return TrajectoryAgent(fake_dispatcher)


@pytest.fixture
def client(test_agent, monkeypatch):
    # 重置模块级 singleton,避免跨测试串流
    monkeypatch.setattr(router_module, "_agent_singleton", None)
    monkeypatch.setattr(router_module, "_llm_client_singleton", None)

    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module.get_agent] = lambda: test_agent
    # LLMClient 不需要真实注入:route_scene / clarify 都被 patch 掉,不会触达
    app.dependency_overrides[router_module.get_llm_client] = lambda: object()
    return TestClient(app)


# ─── Mock 工厂 ─────────────────────────────────────────────────────────────


def _fake_route_scene(scene, confidence, *, low_confidence=False, reasoning="mock reasoning"):
    async def _f(user_request, client, *, threshold=None):
        return RouterDecision(
            scene=scene,
            confidence=confidence,
            reasoning=reasoning,
            low_confidence=low_confidence,
        )
    return _f


def _fake_clarify_ready(scene, params):
    async def _f(user_request, scene_arg, client):
        return ClarifyOutcome(status="ready", scene=scene_arg, params=dict(params))
    return _f


def _fake_clarify_questions(scene, fields):
    """T2-2 questions 形态:{field_name, question, options:[{value,label,hint}]}。"""
    async def _f(user_request, scene_arg, client):
        questions = [
            {
                "field_name": name,
                "question": f"请提供 {name}",
                "options": [
                    {"value": "opt_a", "label": f"{name} A 选项", "hint": "示例 A"},
                    {"value": "opt_b", "label": f"{name} B 选项", "hint": "示例 B"},
                ],
            }
            for name in fields
        ]
        return ClarifyOutcome(status="questions", scene=scene_arg, questions=questions)
    return _f


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_submit_dispatched(client, monkeypatch):
    """scene_hint + clarify ready → dispatched,含 agent_run_id / scene_task_id。"""
    monkeypatch.setattr(
        router_module,
        "clarify",
        _fake_clarify_ready("search2qa", {"task_desc": "量子计算", "max_evolutions": 2}),
    )

    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "基于量子计算合成 QA", "scene_hint": "search2qa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dispatched"
    assert body["scene"] == "search2qa"
    assert body["scene_task_id"] == "task_abc"
    assert body["agent_run_id"]
    assert body["upstream_status"] == "created"


def test_submit_clarify_needed(client, monkeypatch):
    """scene_hint + clarify questions → clarify_needed,questions 含 qa_mode 字段(T2-2 形态)。"""
    monkeypatch.setattr(
        router_module,
        "clarify",
        _fake_clarify_questions("search2qa", ["qa_mode"]),
    )

    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "数据", "scene_hint": "search2qa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clarify_needed"
    assert body["scene"] == "search2qa"
    fields = [q["field_name"] for q in body["questions"]]
    assert "qa_mode" in fields
    # T2-2 形态:每个 question 必须含 options 列表,每个 option 含 value/label/hint
    qa_mode_q = next(q for q in body["questions"] if q["field_name"] == "qa_mode")
    assert isinstance(qa_mode_q["options"], list)
    assert qa_mode_q["options"][0]["value"] == "opt_a"


def test_submit_scene_unrecognized_via_low_confidence(client, monkeypatch):
    """无 scene_hint + router 低置信度 → scene_unrecognized,带 reasoning。"""
    monkeypatch.setattr(
        router_module,
        "route_scene",
        _fake_route_scene(
            "unknown", 0.3, low_confidence=True, reasoning="需求极度模糊"
        ),
    )

    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "来点数据"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scene_unrecognized"
    assert "模糊" in body["reasoning"]
    assert body["confidence"] == 0.3


def test_submit_scene_unrecognized_via_invalid_hint(client):
    """scene_hint 不在 SUPPORTED_SCENES → scene_unrecognized,message 提示非法场景。"""
    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "数据", "scene_hint": "toucan"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scene_unrecognized"
    assert "toucan" in body["message"]


def test_submit_error_dispatch_failed(client, fake_dispatcher, monkeypatch):
    """dispatcher 抛 DispatcherHTTPError → status=error, error_code=DISPATCH_FAILED,HTTP 500。"""
    monkeypatch.setattr(
        router_module,
        "clarify",
        _fake_clarify_ready("search2qa", {"task_desc": "foo"}),
    )
    fake_dispatcher.dispatch.side_effect = DispatcherHTTPError(502, "upstream boom")

    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "foo", "scene_hint": "search2qa"},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "error"
    assert body["error_code"] == "DISPATCH_FAILED"
    assert body["scene"] == "search2qa"
    assert "502" in body["message"]


def test_submit_user_request_empty_returns_400(client):
    """空 user_request → 400,不进四态。"""
    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "   ", "scene_hint": "search2qa"},
    )
    assert resp.status_code == 400


def test_get_runs_not_found(client):
    """未知 agent_run_id → 404。"""
    resp = client.get("/api/trajectory-agent/runs/nonexistent")
    assert resp.status_code == 404


def test_get_runs_after_dispatch(client, monkeypatch):
    """先 dispatch,再 GET runs/{id} 可查到 scene / scene_task_id / upstream。"""
    monkeypatch.setattr(
        router_module,
        "clarify",
        _fake_clarify_ready("search2qa", {"task_desc": "foo"}),
    )

    submit_resp = client.post(
        "/api/trajectory-agent/submit",
        json={"user_request": "foo", "scene_hint": "search2qa"},
    )
    assert submit_resp.status_code == 200
    run_id = submit_resp.json()["agent_run_id"]

    resp = client.get(f"/api/trajectory-agent/runs/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_run_id"] == run_id
    assert data["scene"] == "search2qa"
    assert data["scene_task_id"] == "task_abc"
    assert data["upstream"]["status"] == "running"
