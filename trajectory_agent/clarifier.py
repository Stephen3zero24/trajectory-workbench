"""Clarifier — A-2-3b 参数澄清层。

给定 user_request 和 scene,用 LLM 从自然语言里抽取 rule 里定义的字段值;
- 若 must 或 soft 字段有任何一个为空,返回 status=questions,附追问清单
- 若全部齐备,返回 status=ready 并把 rule.defaults 和抽到的值合并成 dispatch 参数

本层不做 dispatch,不接 submit 端点。A-2-4 再把本层整合进 submit。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from trajectory_agent.clarify_rules import ClarifyRule, get_rule
from trajectory_agent.llm_client import LLMClient

logger = logging.getLogger(__name__)


class ClarifierInvalid(Exception):
    """LLM 返回的抽参 JSON 不符合 schema,或字段类型无法转换。"""

    def __init__(self, message: str, *, raw: dict | None = None):
        super().__init__(message)
        self.raw = raw


@dataclass
class ClarifyOutcome:
    status: Literal["ready", "questions"]
    scene: str
    params: dict = field(default_factory=dict)
    questions: list[dict] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "scene": self.scene,
            "params": self.params,
            "questions": self.questions,
            "extracted": self.extracted,
        }


_TYPE_DESCRIPTIONS = {
    "str": "字符串",
    "int": "整数",
    "bool": "布尔 true/false",
}


def _build_extract_prompt(rule: ClarifyRule) -> str:
    all_fields = list(rule.must_fields) + list(rule.soft_fields)
    if not all_fields:
        # 极端情况:规则没任何待抽字段,LLM 直接返回空 dict
        return (
            "你是参数抽取助手。本次无字段需要抽取,请直接返回空 JSON 对象 {}。"
        )

    lines = []
    for name in all_fields:
        spec = rule.field_specs.get(name, {})
        ftype = spec.get("type", "str")
        type_desc = _TYPE_DESCRIPTIONS.get(ftype, ftype)
        question = spec.get("question", "")
        lines.append(f"- {name} ({ftype} / {type_desc}) —— {question}")
    fields_block = "\n".join(lines)

    schema_entries = ",\n  ".join(
        f'"{name}": <value_or_null>' for name in all_fields
    )

    return f"""你是参数抽取助手,从用户的数据合成需求里抽取指定字段的值。

要抽取的字段:
{fields_block}

抽取规则:
- 若用户**明确**提到某字段的值,返回该值(数值返回 JSON number,字符串返回 string,布尔返回 true/false)
- 若用户未明确提到该字段,返回 null(JSON 的 null,不是字符串 "null")
- 不要猜测、不要编造、不要用常识兜底
- 若用户提到了字段清单之外的其他信息,忽略即可

输出要求(严格 JSON,不要任何额外文字):
{{
  {schema_entries}
}}

示例 1:
字段清单:task_desc (str) —— 数据合成的主题/领域
用户:我要合成关于量子计算的搜索问答数据
输出:{{"task_desc": "量子计算"}}

示例 2:
字段清单:task_count (int) —— 合成轨迹条数
用户:给我来一批工具调用的训练数据
输出:{{"task_count": null}}
"""


def _coerce(value: Any, field_name: str, field_type: str) -> Any:
    """把 LLM 返回的值按字段类型强转;转不动抛 ClarifierInvalid;null/空串 → None。"""
    if value is None:
        return None

    if field_type == "str":
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        # 其他类型强转为 str
        return str(value)

    if field_type == "int":
        # 注意 bool 是 int 的子类,要先排除
        if isinstance(value, bool):
            raise ClarifierInvalid(
                f"字段 {field_name!r} 期望 int,LLM 返回了 bool {value!r}"
            )
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise ClarifierInvalid(
                f"字段 {field_name!r} 期望 int,LLM 返回了非整数 float {value!r}"
            )
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return int(stripped)
            except ValueError as e:
                raise ClarifierInvalid(
                    f"字段 {field_name!r} 期望 int,LLM 返回了无法解析的字符串 {value!r}: {e}"
                ) from None
        raise ClarifierInvalid(
            f"字段 {field_name!r} 期望 int,LLM 返回类型 {type(value).__name__}: {value!r}"
        )

    if field_type == "bool":
        if isinstance(value, bool):
            return value
        raise ClarifierInvalid(
            f"字段 {field_name!r} 期望 bool,LLM 返回类型 {type(value).__name__}: {value!r}"
        )

    # 未知 type:原样返回,交给上层自行处理
    return value


def _build_question(field_name: str, rule: ClarifyRule) -> dict:
    spec = rule.field_specs.get(field_name, {})
    return {
        "field": field_name,
        "question": spec.get("question", f"请提供 {field_name} 的值。"),
        "example": spec.get("example"),
        "default_hint": spec.get("default_hint"),
    }


async def clarify(
    user_request: str,
    scene: str,
    client: LLMClient,
) -> ClarifyOutcome:
    """按 rule 抽参并决定 ready / questions。LLM/schema 异常上抛。"""
    rule = get_rule(scene)
    all_fields = list(rule.must_fields) + list(rule.soft_fields)

    logger.info(
        "clarify enter scene=%s user_request=%r fields=%s",
        scene,
        user_request[:80],
        all_fields,
    )

    # ─── 抽参 ───────────────────────────────────────────────────────────
    if not all_fields:
        extracted: dict[str, Any] = {}
    elif not user_request or not user_request.strip():
        extracted = {name: None for name in all_fields}
    else:
        raw = await client.chat_json(
            [
                {"role": "system", "content": _build_extract_prompt(rule)},
                {"role": "user", "content": user_request},
            ],
            temperature=0.1,
        )
        if not isinstance(raw, dict):
            raise ClarifierInvalid(
                f"LLM 抽参响应不是 dict,实际 {type(raw).__name__}",
                raw=raw if isinstance(raw, dict) else None,
            )
        extracted = {}
        for name in all_fields:
            ftype = rule.field_specs.get(name, {}).get("type", "str")
            extracted[name] = _coerce(raw.get(name), name, ftype)

    # ─── 判齐 ───────────────────────────────────────────────────────────
    missing_must = [n for n in rule.must_fields if _is_missing(extracted.get(n))]
    missing_soft = [n for n in rule.soft_fields if _is_missing(extracted.get(n))]

    if missing_must or missing_soft:
        questions = [_build_question(n, rule) for n in missing_must]
        questions += [_build_question(n, rule) for n in missing_soft]
        outcome = ClarifyOutcome(
            status="questions",
            scene=scene,
            questions=questions,
            extracted=extracted,
        )
        logger.info(
            "clarify exit scene=%s status=questions missing_must=%s missing_soft=%s",
            scene,
            missing_must,
            missing_soft,
        )
        return outcome

    # ─── ready:合并 defaults + 抽到的值(抽到的优先)──────────────────
    params: dict[str, Any] = dict(rule.defaults)
    for name in all_fields:
        value = extracted.get(name)
        if value is not None:
            params[name] = value

    outcome = ClarifyOutcome(
        status="ready",
        scene=scene,
        params=params,
        extracted=extracted,
    )
    logger.info("clarify exit scene=%s status=ready param_keys=%s", scene, sorted(params.keys()))
    return outcome


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False
