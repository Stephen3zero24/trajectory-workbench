"""Unit tests for skill_manifest_loader.

不发起真实 HTTP,所有网络调用通过 monkeypatch 替换 httpx.AsyncClient。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from trajectory_agent import skill_manifest_loader as loader


# ---------- 测试夹具 ----------


@pytest.fixture(autouse=True)
def _reset_catalog():
    """每个用例前后重置 module-level 缓存,避免污染。"""
    loader._reset_for_testing()
    yield
    loader._reset_for_testing()


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: Any = None,
        raw_text: str | None = None,
    ):
        self.status_code = status_code
        self._body = body
        self._raw_text = raw_text
        self.text = raw_text if raw_text is not None else json.dumps(body or {})

    def json(self) -> Any:
        if self._raw_text is not None:
            # 模拟非 JSON 响应:json() 抛 ValueError
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class _FakeAsyncClient:
    """替换 httpx.AsyncClient,按构造时给定的策略响应 GET。"""

    def __init__(self, response: _FakeResponse | Exception):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_httpx(monkeypatch, response: _FakeResponse | Exception):
    def _factory(*args, **kwargs):
        return _FakeAsyncClient(response)

    monkeypatch.setattr(loader.httpx, "AsyncClient", _factory)


def _platform_payload(skills: list[dict]) -> dict:
    return {"skills": skills}


def _skill(
    *,
    skill_id: str,
    sig_owner: str = "trajectory-synthesis",
    status: str = "active",
    description: str = "(无描述)",
) -> dict:
    return {
        "skill_id": skill_id,
        "sig_owner": sig_owner,
        "status": status,
        "description": description,
        "trigger_keywords": ["dummy"],
    }


# ---------- 成功路径 ----------


@pytest.mark.asyncio
async def test_load_manifests_filters_to_active_trajectory_skills(monkeypatch):
    """2 active(search2qa, toucan)+ 3 stub → catalog 只含 2 个 active,key 是裸 scene 名。"""
    payload = _platform_payload(
        [
            _skill(
                skill_id="trajectory-search2qa",
                description="搜索问答数据合成",
            ),
            _skill(
                skill_id="trajectory-toucan",
                description="MCP 工具调用轨迹合成",
            ),
            _skill(
                skill_id="trajectory-envscaler",
                status="stub",
                description="env scaler stub",
            ),
            _skill(
                skill_id="trajectory-mobile_agent",
                status="stub",
                description="mobile agent stub",
            ),
            _skill(
                skill_id="trajectory-toolace",
                status="stub",
                description="toolace stub",
            ),
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    catalog = loader.get_loaded_catalog()
    assert set(catalog.keys()) == {"search2qa", "toucan"}
    assert catalog["search2qa"].scene_id == "search2qa"
    assert catalog["search2qa"].description == "搜索问答数据合成"
    assert catalog["toucan"].description == "MCP 工具调用轨迹合成"


@pytest.mark.asyncio
async def test_load_manifests_excludes_other_owners(monkeypatch):
    """sig_owner 非 trajectory-synthesis 的 skill 必须被过滤。"""
    payload = _platform_payload(
        [
            _skill(skill_id="trajectory-search2qa"),
            _skill(
                skill_id="trajectory-foreign",
                sig_owner="some-other-team",
            ),
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    assert set(loader.get_loaded_catalog().keys()) == {"search2qa"}


@pytest.mark.asyncio
async def test_get_supported_scenes_returns_tuple(monkeypatch):
    payload = _platform_payload(
        [
            _skill(skill_id="trajectory-search2qa"),
            _skill(skill_id="trajectory-toucan"),
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    scenes = loader.get_supported_scenes()
    assert isinstance(scenes, tuple)
    assert set(scenes) == {"search2qa", "toucan"}


@pytest.mark.asyncio
async def test_load_manifests_respects_env_url(monkeypatch):
    """PLATFORM_SKILLS_URL env var 应当被读取并拼到 /api/skills。"""
    captured: dict[str, str] = {}

    class _CapturingClient(_FakeAsyncClient):
        async def get(self, url: str):
            captured["url"] = url
            return await super().get(url)

    payload = _platform_payload([_skill(skill_id="trajectory-search2qa")])

    def _factory(*args, **kwargs):
        return _CapturingClient(_FakeResponse(body=payload))

    monkeypatch.setattr(loader.httpx, "AsyncClient", _factory)
    monkeypatch.setenv("PLATFORM_SKILLS_URL", "http://platform.local:9999")

    await loader.load_manifests()

    assert captured["url"] == "http://platform.local:9999/api/skills"


# ---------- 失败路径 ----------


@pytest.mark.asyncio
async def test_load_manifests_raises_on_connect_error(monkeypatch):
    _patch_httpx(monkeypatch, httpx.ConnectError("connection refused"))

    with pytest.raises(loader.SkillManifestUnavailable) as exc_info:
        await loader.load_manifests()
    assert "ConnectError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_load_manifests_raises_on_5xx(monkeypatch):
    _patch_httpx(
        monkeypatch,
        _FakeResponse(status_code=500, raw_text="Internal Server Error"),
    )

    with pytest.raises(loader.SkillManifestUnavailable) as exc_info:
        await loader.load_manifests()
    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_load_manifests_raises_on_non_json(monkeypatch):
    _patch_httpx(
        monkeypatch,
        _FakeResponse(status_code=200, raw_text="<html>not json</html>"),
    )

    with pytest.raises(loader.SkillManifestUnavailable) as exc_info:
        await loader.load_manifests()
    assert "non-JSON" in str(exc_info.value)


@pytest.mark.asyncio
async def test_load_manifests_raises_on_empty_skills_list(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(body={"skills": []}))

    with pytest.raises(loader.SkillManifestUnavailable) as exc_info:
        await loader.load_manifests()
    assert "0 active" in str(exc_info.value)


@pytest.mark.asyncio
async def test_load_manifests_raises_when_all_stub(monkeypatch):
    """全是 stub → 0 个 active → fail-fast。"""
    payload = _platform_payload(
        [
            _skill(skill_id="trajectory-envscaler", status="stub"),
            _skill(skill_id="trajectory-mobile_agent", status="stub"),
            _skill(skill_id="trajectory-toolace", status="stub"),
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    with pytest.raises(loader.SkillManifestUnavailable) as exc_info:
        await loader.load_manifests()
    assert "0 active" in str(exc_info.value)


@pytest.mark.asyncio
async def test_load_manifests_raises_on_missing_skills_key(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(body={"unexpected": "shape"}))

    with pytest.raises(loader.SkillManifestUnavailable):
        await loader.load_manifests()


def test_get_loaded_catalog_raises_when_not_loaded():
    with pytest.raises(loader.SkillManifestNotLoaded):
        loader.get_loaded_catalog()


def test_get_supported_scenes_raises_when_not_loaded():
    with pytest.raises(loader.SkillManifestNotLoaded):
        loader.get_supported_scenes()
