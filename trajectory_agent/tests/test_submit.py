"""A-1 壳层门面的 pytest 用例:覆盖 scene_hint 校验 / 成功分发 / 未实现场景。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class MockResponse:
    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class MockAsyncClient:
    posts: list = []
    gets: list = []
    post_response = MockResponse(200, {"task_id": "task_xyz", "status": "created"})
    get_response = MockResponse(200, {"task_id": "task_xyz", "status": "running"})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def post(self, url, **kwargs):
        MockAsyncClient.posts.append({"url": url, "kwargs": kwargs})
        return MockAsyncClient.post_response

    async def get(self, url, **kwargs):
        MockAsyncClient.gets.append({"url": url, "kwargs": kwargs})
        return MockAsyncClient.get_response


@pytest.fixture
def client(monkeypatch):
    MockAsyncClient.posts = []
    MockAsyncClient.gets = []

    import trajectory_agent.dispatcher as disp_module
    import trajectory_agent.router as router_module

    monkeypatch.setattr(disp_module.httpx, "AsyncClient", MockAsyncClient)
    # 重置 agent 单例,避免跨测试串流
    monkeypatch.setattr(router_module, "_agent_singleton", None)

    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app)


def test_missing_scene_hint_returns_400(client):
    resp = client.post("/api/trajectory-agent/submit", json={"params": {}})
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "scene_hint" in detail
    assert "required" in detail


def test_invalid_scene_hint_returns_400(client):
    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"scene_hint": "unknown", "params": {}},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "unknown" in detail
    assert "not a valid scene" in detail


def test_search2qa_dispatch_success(client):
    resp = client.post(
        "/api/trajectory-agent/submit",
        json={
            "scene_hint": "search2qa",
            "params": {"task_desc": "test query", "seed": "test"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scene"] == "search2qa"
    assert body["scene_task_id"] == "task_xyz"
    assert body["agent_run_id"]
    assert body["status"] == "created"

    assert len(MockAsyncClient.posts) == 1
    call = MockAsyncClient.posts[0]
    assert call["url"].endswith("/api/tasks")
    sent_body = call["kwargs"]["json"]
    assert sent_body["scene_type"] == "search2qa"
    assert sent_body["task_desc"] == "test query"


def test_envscaler_returns_501(client):
    resp = client.post(
        "/api/trajectory-agent/submit",
        json={"scene_hint": "envscaler", "params": {}},
    )
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "envscaler" in detail
    assert "not implemented" in detail
