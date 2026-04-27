"""Unit tests for clarifier (T2-2 manifest-driven 重构后).

覆盖 9 类 case:
- enum 匹配成功
- enum 值非法 → 视为缺失
- 全 must_clarify 缺失 → questions
- 部分缺失 → 部分 questions
- maps_to 展开正确性(_scale_preset 不在最终 params)
- required_params 缺失 → ClarifierInvalid
- defaults 全量合并
- extracted 覆盖 defaults
- search2qa + toucan 双 scene mock 验证
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from trajectory_agent import clarifier
from trajectory_agent import skill_manifest_loader as loader
from trajectory_agent.clarifier import ClarifierInvalid, ClarifyOutcome, clarify
from trajectory_agent.skill_manifest_loader import SceneCatalogEntry


# ─── 测试 fixture ───────────────────────────────────────────────────────────


def _scale_preset_item(*, with_maps_to: bool = True) -> dict:
    def _opt(value, label, hint, maps_to_v):
        out = {"value": value, "label": label, "hint": hint}
        if with_maps_to:
            out["maps_to"] = {"max_samples": maps_to_v}
        return out

    return {
        "param": "_scale_preset",
        "question": "合成规模？",
        "options": (
            _opt("fast", "快速验证", "1 样本", 1),
            _opt("standard", "标准", "5 样本", 5),
            _opt("batch", "批量", "20 样本", 20),
        ),
    }


def _qa_mode_item() -> dict:
    return {
        "param": "qa_mode",
        "question": "你希望的合成模式是？",
        "options": (
            {"value": "question", "label": "Q 模式", "hint": "种子词→问题"},
            {"value": "answer", "label": "A 模式", "hint": "答案→问题"},
        ),
    }


@pytest.fixture
def search2qa_entry():
    """覆盖 conftest 默认 catalog,模拟 search2qa 完整 manifest 形态。"""
    entry = SceneCatalogEntry(
        scene_id="search2qa",
        description="搜索问答数据合成",
        must_clarify=(_qa_mode_item(), _scale_preset_item()),
        defaults={
            "max_turns": 20,
            "model": "deepseek-chat",
            "temperature": 0.7,
            "max_samples": 1,
        },
        required_params=("qa_mode",),  # qa_mode 在 must_clarify 里,可达
    )
    loader._CATALOG = {"search2qa": entry}
    yield entry


@pytest.fixture
def search2qa_with_unmet_required():
    """seed 在 required_params 但既不在 must_clarify 也不在 defaults。"""
    entry = SceneCatalogEntry(
        scene_id="search2qa",
        description="搜索问答数据合成",
        must_clarify=(_qa_mode_item(), _scale_preset_item()),
        defaults={"max_turns": 20},
        required_params=("seed", "qa_mode"),  # seed 没人喂
    )
    loader._CATALOG = {"search2qa": entry}
    yield entry


@pytest.fixture
def toucan_entry():
    entry = SceneCatalogEntry(
        scene_id="toucan",
        description="MCP 工具调用轨迹合成",
        must_clarify=(
            {
                "param": "_scale_preset",
                "question": "合成规模？",
                "options": (
                    {
                        "value": "fast",
                        "label": "快速",
                        "hint": "1 样本",
                        "maps_to": {"question_count": 1},
                    },
                    {
                        "value": "standard",
                        "label": "标准",
                        "hint": "5 样本",
                        "maps_to": {"question_count": 5},
                    },
                ),
            },
        ),
        defaults={"model": "deepseek-chat", "max_steps": 15},
        required_params=(),
    )
    loader._CATALOG = {"toucan": entry}
    yield entry


@pytest.fixture
def dual_scene_catalog():
    s2qa = SceneCatalogEntry(
        scene_id="search2qa",
        description="搜索问答",
        must_clarify=(_qa_mode_item(), _scale_preset_item()),
        defaults={"max_samples": 1},
        required_params=("qa_mode",),
    )
    toucan = SceneCatalogEntry(
        scene_id="toucan",
        description="MCP",
        must_clarify=(
            {
                "param": "_scale_preset",
                "question": "合成规模？",
                "options": (
                    {
                        "value": "fast",
                        "label": "快速",
                        "hint": "",
                        "maps_to": {"question_count": 1},
                    },
                ),
            },
        ),
        defaults={},
        required_params=(),
    )
    loader._CATALOG = {"search2qa": s2qa, "toucan": toucan}
    yield {"search2qa": s2qa, "toucan": toucan}


def _llm_returning(payload: Any) -> AsyncMock:
    client = AsyncMock()
    client.chat_json = AsyncMock(return_value=payload)
    return client


# ─── enum 匹配 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enum_match_success_returns_ready(search2qa_entry):
    """LLM 返回合法 enum value → ready,params 含 defaults + extracted + maps_to 展开。"""
    client = _llm_returning(
        {"qa_mode": "question", "_scale_preset": "fast"}
    )

    outcome = await clarify("我要快速验证医疗 QA", "search2qa", client)

    assert outcome.status == "ready"
    assert outcome.scene == "search2qa"
    # qa_mode 进 params(无 maps_to,直接保留)
    assert outcome.params["qa_mode"] == "question"
    # _scale_preset=fast 触发 maps_to 展开 → max_samples 被覆盖到 1
    assert outcome.params["max_samples"] == 1
    # _scale_preset 自身从 params 中移除(下划线前缀)
    assert "_scale_preset" not in outcome.params


@pytest.mark.asyncio
async def test_enum_value_invalid_treated_as_missing(search2qa_entry):
    """LLM 返回不在 options 集合的 value → 视为缺失,产生 questions。"""
    client = _llm_returning(
        {"qa_mode": "FAKE_MODE", "_scale_preset": "fast"}
    )

    outcome = await clarify("test", "search2qa", client)

    assert outcome.status == "questions"
    field_names = [q["field_name"] for q in outcome.questions]
    # qa_mode 因非法值视为缺失,出现在 questions
    assert "qa_mode" in field_names
    # _scale_preset 合法 → 不在 questions
    assert "_scale_preset" not in field_names


# ─── questions 输出 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_missing_returns_full_questions(search2qa_entry):
    """LLM 全返回 null → 全部 must_clarify 进 questions。"""
    client = _llm_returning({"qa_mode": None, "_scale_preset": None})

    outcome = await clarify("含糊不清的需求", "search2qa", client)

    assert outcome.status == "questions"
    assert len(outcome.questions) == 2
    field_names = [q["field_name"] for q in outcome.questions]
    assert set(field_names) == {"qa_mode", "_scale_preset"}


@pytest.mark.asyncio
async def test_questions_schema_contains_options(search2qa_entry):
    """questions 字段形态:{field_name, question, options:[{value,label,hint}]}。"""
    client = _llm_returning({"qa_mode": None, "_scale_preset": None})

    outcome = await clarify("foo", "search2qa", client)

    qa_mode_q = next(q for q in outcome.questions if q["field_name"] == "qa_mode")
    assert qa_mode_q["question"] == "你希望的合成模式是？"
    assert isinstance(qa_mode_q["options"], list)
    assert {"value": "question", "label": "Q 模式", "hint": "种子词→问题"} in qa_mode_q["options"]
    assert {"value": "answer", "label": "A 模式", "hint": "答案→问题"} in qa_mode_q["options"]


@pytest.mark.asyncio
async def test_partial_missing_returns_partial_questions(search2qa_entry):
    """qa_mode 抽到,_scale_preset 没抽到 → 只追问 _scale_preset。"""
    client = _llm_returning({"qa_mode": "question", "_scale_preset": None})

    outcome = await clarify("基于关键词出题", "search2qa", client)

    assert outcome.status == "questions"
    field_names = [q["field_name"] for q in outcome.questions]
    assert field_names == ["_scale_preset"]


@pytest.mark.asyncio
async def test_empty_user_request_returns_all_questions(search2qa_entry):
    """空 user_request 不调 LLM,直接产生全部 questions。"""
    client = AsyncMock()  # chat_json 不应被调用
    client.chat_json = AsyncMock(side_effect=AssertionError("LLM should not be called"))

    outcome = await clarify("", "search2qa", client)

    assert outcome.status == "questions"
    assert len(outcome.questions) == 2
    client.chat_json.assert_not_awaited()


# ─── maps_to 展开 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maps_to_expansion_overrides_defaults(search2qa_entry):
    """defaults.max_samples=1,_scale_preset=batch 应当展开为 max_samples=20。"""
    client = _llm_returning({"qa_mode": "question", "_scale_preset": "batch"})

    outcome = await clarify("批量合成", "search2qa", client)

    assert outcome.status == "ready"
    assert outcome.params["max_samples"] == 20  # 不是 defaults 的 1
    assert "_scale_preset" not in outcome.params


@pytest.mark.asyncio
async def test_maps_to_does_not_remove_non_underscore_fields(search2qa_entry):
    """qa_mode 没有下划线前缀,maps_to 展开不应把它从 params 中移除。"""
    client = _llm_returning({"qa_mode": "answer", "_scale_preset": "fast"})

    outcome = await clarify("反推答案", "search2qa", client)

    assert outcome.status == "ready"
    assert outcome.params["qa_mode"] == "answer"


# ─── defaults 合并 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_defaults_full_merge(search2qa_entry):
    """params 应包含 manifest defaults 的所有字段。"""
    client = _llm_returning({"qa_mode": "question", "_scale_preset": "fast"})

    outcome = await clarify("test", "search2qa", client)

    assert outcome.status == "ready"
    # search2qa_entry.defaults 含 max_turns / model / temperature / max_samples
    assert outcome.params["max_turns"] == 20
    assert outcome.params["model"] == "deepseek-chat"
    assert outcome.params["temperature"] == 0.7


@pytest.mark.asyncio
async def test_extracted_overrides_defaults_for_same_key(search2qa_entry):
    """defaults.max_samples=1,extracted maps_to 把它覆写成 20(batch)。"""
    client = _llm_returning({"qa_mode": "question", "_scale_preset": "batch"})

    outcome = await clarify("batch", "search2qa", client)

    assert outcome.params["max_samples"] == 20


# ─── required_params 校验 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_required_params_missing_raises_clarifier_invalid(
    search2qa_with_unmet_required,
):
    """seed 在 required_params 但无人产生 → ClarifierInvalid。"""
    client = _llm_returning({"qa_mode": "question", "_scale_preset": "fast"})

    with pytest.raises(ClarifierInvalid) as exc_info:
        await clarify("test", "search2qa", client)
    assert "required by manifest" in str(exc_info.value)
    assert "seed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_required_params_satisfied_when_in_must_clarify(search2qa_entry):
    """qa_mode 同时在 required_params 和 must_clarify → 抽到值后校验通过。"""
    client = _llm_returning({"qa_mode": "question", "_scale_preset": "fast"})

    outcome = await clarify("test", "search2qa", client)

    assert outcome.status == "ready"
    assert outcome.params["qa_mode"] == "question"


# ─── 双 scene ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search2qa_via_dual_catalog(dual_scene_catalog):
    client = _llm_returning({"qa_mode": "question", "_scale_preset": "fast"})

    outcome = await clarify("快速验证 search2qa", "search2qa", client)

    assert outcome.status == "ready"
    assert outcome.scene == "search2qa"
    assert outcome.params["qa_mode"] == "question"
    assert outcome.params["max_samples"] == 1


@pytest.mark.asyncio
async def test_toucan_via_dual_catalog(dual_scene_catalog):
    """toucan 的 maps_to 是 question_count 而不是 max_samples,验证不同 scene 字段隔离。"""
    client = _llm_returning({"_scale_preset": "fast"})

    outcome = await clarify("快速 toucan", "toucan", client)

    assert outcome.status == "ready"
    assert outcome.scene == "toucan"
    assert outcome.params["question_count"] == 1
    assert "max_samples" not in outcome.params  # 不与 search2qa 串
    assert "_scale_preset" not in outcome.params


@pytest.mark.asyncio
async def test_unknown_scene_raises_keyerror(dual_scene_catalog):
    client = _llm_returning({})

    with pytest.raises(KeyError) as exc_info:
        await clarify("test", "envscaler", client)
    assert "envscaler" in str(exc_info.value)


# ─── LLM 边界响应 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_returns_non_dict_raises_clarifier_invalid(search2qa_entry):
    """LLM 返回 list / str → ClarifierInvalid。"""
    client = _llm_returning(["not", "a", "dict"])

    with pytest.raises(ClarifierInvalid) as exc_info:
        await clarify("foo", "search2qa", client)
    assert "不是 dict" in str(exc_info.value)


@pytest.mark.asyncio
async def test_llm_returns_partial_keys(search2qa_entry):
    """LLM 漏返某字段 key → 该字段视为 None,产生 questions。"""
    client = _llm_returning({"qa_mode": "question"})  # 漏 _scale_preset

    outcome = await clarify("foo", "search2qa", client)

    assert outcome.status == "questions"
    field_names = [q["field_name"] for q in outcome.questions]
    assert field_names == ["_scale_preset"]


@pytest.mark.asyncio
async def test_no_must_clarify_short_circuits_to_ready(toucan_entry, monkeypatch):
    """must_clarify 为空时直接 ready,跳过 LLM 调用。"""
    # 替换 toucan 为无 must_clarify 的形态
    empty_entry = SceneCatalogEntry(
        scene_id="toucan",
        description="x",
        must_clarify=(),
        defaults={"key": "val"},
        required_params=(),
    )
    loader._CATALOG = {"toucan": empty_entry}

    client = AsyncMock()
    client.chat_json = AsyncMock(side_effect=AssertionError("LLM should not be called"))

    outcome = await clarify("anything", "toucan", client)

    assert outcome.status == "ready"
    assert outcome.params == {"key": "val"}
    client.chat_json.assert_not_awaited()


# ─── prompt 内容回归 ──────────────────────────────────────────────────────


def test_prompt_lists_options_for_each_must_clarify(search2qa_entry):
    """prompt 应当列出 qa_mode + _scale_preset 各自的 options(value/label/hint)。"""
    prompt = clarifier._build_extract_prompt(search2qa_entry)

    # qa_mode options
    assert '"question"' in prompt
    assert '"answer"' in prompt
    assert "Q 模式" in prompt or "A 模式" in prompt

    # _scale_preset options
    assert '"fast"' in prompt
    assert '"standard"' in prompt
    assert '"batch"' in prompt

    # schema enum 段
    assert '"qa_mode"' in prompt
    assert '"_scale_preset"' in prompt


def test_prompt_no_residual_old_type_descriptions(search2qa_entry):
    """旧版 _TYPE_DESCRIPTIONS 已删除,prompt 不应再出现 type=str/int/bool 描述。"""
    prompt = clarifier._build_extract_prompt(search2qa_entry)
    # 旧版 prompt 含 "(str / 字符串)" "(int / 整数)" 这种类型标注
    assert "/ 字符串)" not in prompt
    assert "/ 整数)" not in prompt
    assert "/ 布尔" not in prompt
