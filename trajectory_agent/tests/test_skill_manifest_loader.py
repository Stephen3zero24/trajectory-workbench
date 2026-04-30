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
    must_clarify: list[dict] | None = None,
    defaults: dict | None = None,
    required_params: list[str] | None = None,
) -> dict:
    out = {
        "skill_id": skill_id,
        "sig_owner": sig_owner,
        "status": status,
        "description": description,
        "trigger_keywords": ["dummy"],
    }
    if must_clarify is not None:
        out["must_clarify"] = must_clarify
    if defaults is not None:
        out["defaults"] = defaults
    if required_params is not None:
        out["required_params"] = required_params
    return out


# ---------- 成功路径 ----------


@pytest.mark.asyncio
async def test_load_manifests_filters_to_active_trajectory_skills(monkeypatch):
    """2 active(search2qa, toucan)+ 3 stub → catalog 只含 2 个 active,key 是裸 scene 名。

    T2-2 扩展:同时验证 must_clarify / defaults / required_params 字段进缓存。
    """
    search2qa_must_clarify = [
        {
            "param": "qa_mode",
            "question": "你希望的合成模式是？",
            "options": [
                {"value": "question", "label": "Q 模式", "hint": "种子词 → 问题"},
                {"value": "answer", "label": "A 模式", "hint": "答案 → 问题"},
            ],
        },
        {
            "param": "_scale_preset",
            "question": "合成规模？",
            "options": [
                {
                    "value": "fast",
                    "label": "快速",
                    "hint": "1 样本",
                    "maps_to": {"max_samples": 1},
                },
            ],
        },
    ]
    search2qa_defaults = {
        "max_turns": 20,
        "model": "deepseek-chat",
        "temperature": 0.7,
    }
    search2qa_required = ["seed", "qa_mode"]

    payload = _platform_payload(
        [
            _skill(
                skill_id="trajectory-search2qa",
                description="搜索问答数据合成",
                must_clarify=search2qa_must_clarify,
                defaults=search2qa_defaults,
                required_params=search2qa_required,
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

    # T2-2 新字段断言 — search2qa
    s2qa = catalog["search2qa"]
    assert isinstance(s2qa.must_clarify, tuple)
    assert len(s2qa.must_clarify) == 2
    assert s2qa.must_clarify[0]["param"] == "qa_mode"
    assert isinstance(s2qa.must_clarify[0]["options"], tuple)
    assert s2qa.must_clarify[0]["options"][0]["value"] == "question"
    # _scale_preset 第二个 must_clarify 项,maps_to 必须保留
    assert s2qa.must_clarify[1]["param"] == "_scale_preset"
    assert s2qa.must_clarify[1]["options"][0]["maps_to"] == {"max_samples": 1}

    assert s2qa.defaults == search2qa_defaults
    assert s2qa.defaults is not search2qa_defaults  # loader 内部 dict() 复制,不共享引用
    assert s2qa.required_params == ("seed", "qa_mode")
    assert isinstance(s2qa.required_params, tuple)

    # toucan 没在 mock 里给三字段 → 应当 graceful 默认空
    toucan = catalog["toucan"]
    assert toucan.must_clarify == ()
    assert toucan.defaults == {}
    assert toucan.required_params == ()


@pytest.mark.asyncio
async def test_load_manifests_handles_must_clarify_missing(monkeypatch):
    """manifest 完全缺失 must_clarify / defaults / required_params 三字段 → graceful 默认。"""
    payload = _platform_payload(
        [_skill(skill_id="trajectory-search2qa", description="x")]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    entry = loader.get_loaded_catalog()["search2qa"]
    assert entry.must_clarify == ()
    assert entry.defaults == {}
    assert entry.required_params == ()


@pytest.mark.asyncio
async def test_load_manifests_handles_must_clarify_malformed(monkeypatch):
    """manifest must_clarify 元素非 dict / options 非 list → graceful 跳过 / 转空。"""
    payload = _platform_payload(
        [
            _skill(
                skill_id="trajectory-search2qa",
                description="x",
                must_clarify=[
                    "not_a_dict",
                    {"param": "valid", "question": "Q?", "options": "not_a_list"},
                    {"param": "good", "question": "Q?", "options": [{"value": "v"}]},
                ],
            )
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    must_clarify = loader.get_loaded_catalog()["search2qa"].must_clarify
    # 字符串元素跳过,剩两个 dict 项
    assert len(must_clarify) == 2
    # options 非 list 的项,options 转为空 tuple
    assert must_clarify[0]["param"] == "valid"
    assert must_clarify[0]["options"] == ()
    # 正常项 options 转 tuple
    assert must_clarify[1]["param"] == "good"
    assert isinstance(must_clarify[1]["options"], tuple)
    assert must_clarify[1]["options"][0]["value"] == "v"


@pytest.mark.asyncio
async def test_load_manifests_preserves_complex_nested_defaults(monkeypatch):
    """defaults 含嵌套 dict / list / 各种 primitive,必须原样透传不丢精度。"""
    complex_defaults = {
        "max_turns": 20,
        "model": "deepseek-chat",
        "temperature": 0.7,
        "enable_evolution": True,
        "max_samples": 1,
        "quality_threshold": 0.75,
        "nested_config": {
            "sub_a": [1, 2, 3],
            "sub_b": {"deep": "value"},
            "sub_c": None,
        },
        "tags": ["a", "b", "c"],
    }
    payload = _platform_payload(
        [
            _skill(
                skill_id="trajectory-search2qa",
                description="x",
                defaults=complex_defaults,
            )
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    defaults = loader.get_loaded_catalog()["search2qa"].defaults
    assert defaults == complex_defaults
    # 嵌套也要全等(同时不丢精度,float / bool / None / list 都正确)
    assert defaults["temperature"] == 0.7
    assert defaults["enable_evolution"] is True
    assert defaults["nested_config"]["sub_b"]["deep"] == "value"
    assert defaults["nested_config"]["sub_c"] is None
    assert defaults["tags"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_load_manifests_required_params_filters_non_str(monkeypatch):
    """required_params 中混入非 str(int / dict)→ 仅 str 元素保留。"""
    payload = _platform_payload(
        [
            _skill(
                skill_id="trajectory-search2qa",
                description="x",
                required_params=["seed", 42, {"x": 1}, "qa_mode"],
            )
        ]
    )
    _patch_httpx(monkeypatch, _FakeResponse(body=payload))

    await loader.load_manifests()

    rp = loader.get_loaded_catalog()["search2qa"].required_params
    assert rp == ("seed", "qa_mode")


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
