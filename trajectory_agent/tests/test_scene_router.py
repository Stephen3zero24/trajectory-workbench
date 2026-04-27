"""Unit tests for scene_router (T2-1 manifest-driven 改造后).

覆盖:
- _build_system_prompt 完全由 catalog 驱动,不再硬编码 toucan / toolace
- _parse_decision 对 LLM 返回的 scene 用 _valid_scenes() 校验,catalog 内的 scene 通过、catalog 外的拒绝
- 偏差 1 已消除:prompt schema enum 与 _valid_scenes() 集合一致
"""

from __future__ import annotations

import pytest

from trajectory_agent import scene_router
from trajectory_agent import skill_manifest_loader as loader


@pytest.fixture
def two_scene_catalog():
    """覆盖 conftest 默认 catalog,加入 toucan,使 {search2qa, toucan} 同时合法。"""
    loader._CATALOG = {
        "search2qa": loader.SceneCatalogEntry(
            scene_id="search2qa",
            description="搜索问答数据合成 (test catalog)",
        ),
        "toucan": loader.SceneCatalogEntry(
            scene_id="toucan",
            description="MCP 工具调用轨迹合成 (test catalog)",
        ),
    }
    yield
    # conftest 的 _default_catalog teardown 会做 reset,这里不重复


# ---------- _valid_scenes ----------


def test_valid_scenes_includes_catalog_plus_unknown(two_scene_catalog):
    valid = scene_router._valid_scenes()
    assert valid == {"search2qa", "toucan", "unknown"}


def test_valid_scenes_uses_default_catalog_from_conftest():
    """conftest 默认只放 search2qa → _valid_scenes = {search2qa, unknown}。"""
    valid = scene_router._valid_scenes()
    assert valid == {"search2qa", "unknown"}


# ---------- _build_system_prompt ----------


def test_prompt_lists_only_catalog_scenes(two_scene_catalog):
    """prompt 候选场景段只包含 catalog 里的 scene,不含老硬编码 toolace。"""
    prompt = scene_router._build_system_prompt()
    assert "- search2qa: 搜索问答数据合成 (test catalog)" in prompt
    assert "- toucan: MCP 工具调用轨迹合成 (test catalog)" in prompt
    # 老的硬编码 toolace 必须消失
    assert "toolace" not in prompt


def test_prompt_schema_enum_matches_valid_scenes(two_scene_catalog):
    """prompt 里 \"scene\": ... 那一行的枚举集合必须与 _valid_scenes 完全一致。

    这是偏差 1 的回归测试:旧版本 prompt 模板硬编码 'toucan',但
    _VALID_SCENES 不含 toucan,LLM 真返回 toucan 时会被自身代码当作非法。
    新版本两侧都从同一 catalog 派生,不可能再分歧。
    """
    prompt = scene_router._build_system_prompt()
    valid = scene_router._valid_scenes()

    for s in valid:
        assert f'"{s}"' in prompt, f"scene {s!r} 在 _valid_scenes 但不在 prompt schema"

    # prompt 里不该出现 catalog 外的 scene 字面量(检查老硬编码残留)
    assert '"toolace"' not in prompt


def test_prompt_scene_count_in_unknown_hint(two_scene_catalog):
    """unknown 选项的描述里数字应跟 catalog 大小一致(本例 2)。"""
    prompt = scene_router._build_system_prompt()
    assert "上述 2 个场景" in prompt


def test_prompt_uses_default_catalog_size():
    """conftest 默认 catalog 只 1 个 scene → prompt 应说\"上述 1 个场景\"。"""
    prompt = scene_router._build_system_prompt()
    assert "上述 1 个场景" in prompt


# ---------- _parse_decision ----------


def _decision_payload(scene: str, confidence: float = 0.9, reasoning: str = "ok") -> dict:
    return {"scene": scene, "confidence": confidence, "reasoning": reasoning}


def test_parse_decision_accepts_scene_in_catalog(two_scene_catalog):
    """toucan 在 catalog 里 → 通过(消除偏差 1 后的核心保证)。"""
    decision = scene_router._parse_decision(_decision_payload("toucan", 0.85))
    assert decision.scene == "toucan"
    assert decision.confidence == 0.85


def test_parse_decision_accepts_unknown(two_scene_catalog):
    decision = scene_router._parse_decision(_decision_payload("unknown", 0.95))
    assert decision.scene == "unknown"


def test_parse_decision_rejects_scene_not_in_catalog(two_scene_catalog):
    """foobar 不在 catalog ∪ {unknown} → SceneRoutingInvalid。"""
    with pytest.raises(scene_router.SceneRoutingInvalid) as exc_info:
        scene_router._parse_decision(_decision_payload("foobar"))
    assert "foobar" in str(exc_info.value)


def test_parse_decision_rejects_old_hardcoded_toolace_when_not_in_catalog(two_scene_catalog):
    """toolace 不在测试 catalog → 必须被拒(对照旧硬编码集合的回归)。"""
    with pytest.raises(scene_router.SceneRoutingInvalid):
        scene_router._parse_decision(_decision_payload("toolace"))


def test_parse_decision_uses_runtime_catalog_not_import_time_snapshot():
    """_parse_decision 每次调用都查最新 catalog,不缓存 import 期快照。

    conftest 默认只放 search2qa;这里把 catalog 改成只放 toucan,
    然后验证 search2qa 反而被拒,toucan 通过。
    """
    loader._CATALOG = {
        "toucan": loader.SceneCatalogEntry(
            scene_id="toucan", description="only toucan"
        ),
    }

    decision = scene_router._parse_decision(_decision_payload("toucan"))
    assert decision.scene == "toucan"

    with pytest.raises(scene_router.SceneRoutingInvalid):
        scene_router._parse_decision(_decision_payload("search2qa"))


# ---------- 偏差 1 回归(整体) ----------


def test_no_hardcoded_scene_id_remains_in_module_source():
    """模块源码里不应再出现 'SUPPORTED_SCENES' 元组或 'SCENE_CATALOG' 字典字面量。"""
    import inspect
    src = inspect.getsource(scene_router)
    # 旧符号已删除
    assert "SUPPORTED_SCENES: tuple" not in src
    assert "SCENE_CATALOG: dict" not in src
    # 新数据入口存在
    assert "get_loaded_catalog" in src
    assert "_valid_scenes" in src
