"""Clarifier — T2-2 manifest 驱动的参数澄清层。

数据流:
    skill_manifest_loader.get_loaded_catalog()[scene]
        ↓  SceneCatalogEntry.must_clarify (enum 字段定义)
        ↓  LLM 抽参(从给定 enum options 中匹配,失败返 null)
        ↓  _validate_value 校验(value 必须在 options 集合内)
        ↓  ready / questions 二分
        ↓  ready 时:defaults 全量合并 → maps_to 展开 → required_params 校验

与 T2-1 形态对比:
- 字段不再是自由文本(str/int/bool 三类型),而是 enum + options
- 不再有 must_fields / soft_fields 双层,只有 must_clarify 一层
- 新增 _scale_preset 这种"展开字段":选 fast → maps_to 把
  {max_samples: 1} 注入实际 params,原始 _scale_preset 字段不进 dispatcher
- 新增 required_params 后置校验:manifest 声明的必填若不在最终 params,
  fail-fast 抛 ClarifierInvalid

questions 字段形态(breaking change vs T2-1):
    [{field_name, question, options: [{value, label, hint}]}]

LLM 调用形态与 scene_router 一致:单次 chat_json,system+user 双消息,
temperature 0.1。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from trajectory_agent.llm_client import LLMClient
from trajectory_agent.skill_manifest_loader import (
    SceneCatalogEntry,
    get_loaded_catalog,
)

logger = logging.getLogger(__name__)


class ClarifierInvalid(Exception):
    """LLM 返回不是 dict / required_params 后置校验失败。"""

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


# ─── prompt ─────────────────────────────────────────────────────────────────


def _build_extract_prompt(entry: SceneCatalogEntry) -> str:
    field_blocks: list[str] = []
    schema_keys: list[str] = []

    for item in entry.must_clarify:
        param = item["param"]
        question = item.get("question", "")
        options = item.get("options", ())
        schema_keys.append(param)

        if options:
            opt_lines: list[str] = []
            for opt in options:
                value = opt.get("value", "")
                label = opt.get("label", "")
                hint = opt.get("hint", "")
                line = f'    * "{value}" ({label})'
                if hint:
                    line += f": {hint}"
                opt_lines.append(line)
            opts_block = "\n".join(opt_lines)
            field_blocks.append(f"- [enum] {param}: {question}\n{opts_block}")
        else:
            field_blocks.append(
                f"- [free_text] {param}: {question}\n"
                f"  (从用户需求中抽取领域关键词或主题词,无法识别返 null)"
            )

    fields_section = "\n\n".join(field_blocks)
    schema_entries = ",\n  ".join(f'"{k}": <value_or_null>' for k in schema_keys)

    return f"""你是参数抽取助手,根据用户的数据合成需求,从用户表述中抽取每个字段的取值——枚举字段从给定选项中匹配,自由文本字段从用户表述中提取关键词。无法判断的字段返回 null。

字段:
{fields_section}

匹配规则:
- 用户**明确**或**强烈暗示**某选项 → 返回该选项的 value 字面量(不是 label,不是 hint)
- 用户没暗示 → 返回 null,系统会再追问

判别指引(适用规模相关字段,如 _scale_preset):
- 用户说"快速试一下"/"先跑通"/"快速验证"/"快速看看效果" → 选 fast
- 用户说"批量"/"大量"/"多一点"/"全量"/"上规模" → 选 batch
- 用户说"标准"/"正常量级"/"差不多就行" → 选 standard
- 用户没提规模 → 选 standard(中位默认)

判别指引(适用 qa_mode):
- 用户提到"问题"/"提问"/"出题"/"基于关键词出题" → 选 question
- 用户提到"答案"/"反推"/"已有答案"/"反向" → 选 answer
- 不能确定 → 返回 null

判别指引(适用 seed 等 free_text 字段):
- 用户提到具体领域/主题词("医疗问诊"/"法律咨询"/"电商客服"等)→ 抽取该词
- 用户只说"合成数据"未提领域 → 返回 null
- 抽取结果保留中文/英文原词,不翻译,不缩写

输出要求(严格 JSON,不要任何额外文字):
{{
  {schema_entries}
}}

示例 1:
用户:我想快速验证 search2qa,基于关键词出题
输出:{{"qa_mode": "question", "_scale_preset": "fast"}}

示例 2:
用户:给我合成一些工具调用数据
输出:{{"_scale_preset": "standard"}}

示例 3:
用户:我想做点法律咨询方向的训练数据,数量不用太多
输出:{{"seed": "法律咨询", "qa_mode": null, "_scale_preset": "standard"}}

示例 4:
用户:给我合成一些数据
输出:{{"seed": null, "qa_mode": null, "_scale_preset": "standard"}}
"""


# ─── helpers ────────────────────────────────────────────────────────────────


def _validate_value(value: Any, options: tuple) -> str | None:
    """value 校验:
    - enum 字段(options 非空):value 必须等于某 option 的 value 字面量,否则视为缺失返 None。
    - free_text 字段(options 为空):value 是非空字符串则接受(返回 strip 后的值),
      否则(None / 空字符串 / 非字符串)视为缺失返 None。
    """
    if value is None:
        return None
    if not options:
        # free_text 旁路:无 options 约束,只检查非空字符串
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    valid = {opt.get("value") for opt in options if isinstance(opt, dict)}
    if value in valid:
        return value
    return None


def _expand_maps_to(params: dict, entry: SceneCatalogEntry) -> dict:
    """对 must_clarify 中带 maps_to 的选项做展开,以 _ 开头的字段从 params 移除。

    例:_scale_preset=fast → 找到 fast 选项的 maps_to={max_samples:1} → 合入 params,
    然后从 params 中移除 _scale_preset,因为下划线前缀字段不应进 dispatcher。
    """
    out = dict(params)
    for item in entry.must_clarify:
        param_name = item.get("param")
        if not isinstance(param_name, str) or param_name not in out:
            continue
        chosen = out[param_name]
        for opt in item.get("options", ()):
            if not isinstance(opt, dict) or opt.get("value") != chosen:
                continue
            maps_to = opt.get("maps_to")
            if isinstance(maps_to, dict):
                for k, v in maps_to.items():
                    out[k] = v
            break
        if param_name.startswith("_"):
            out.pop(param_name, None)
    return out


def _check_required_params(params: dict, entry: SceneCatalogEntry) -> None:
    missing = [p for p in entry.required_params if p not in params]
    if missing:
        raise ClarifierInvalid(
            f"required by manifest but not produced by must_clarify or defaults: "
            f"{missing}"
        )


def _build_question(item: dict) -> dict:
    """questions 输出条目:{field_name, question, type, options: [{value,label,hint}]}。"""
    raw_options = item.get("options", ())
    options_out = []
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        options_out.append(
            {
                "value": opt.get("value"),
                "label": opt.get("label"),
                "hint": opt.get("hint"),
            }
        )
    return {
        "field_name": item.get("param"),
        "question": item.get("question", ""),
        "type": "free_text" if not raw_options else "enum",
        "options": options_out,
    }


# ─── main ───────────────────────────────────────────────────────────────────


async def clarify(
    user_request: str,
    scene: str,
    client: LLMClient,
) -> ClarifyOutcome:
    """根据 manifest must_clarify 对用户请求做 enum 抽取并决定 ready / questions。"""
    catalog = get_loaded_catalog()
    if scene not in catalog:
        raise KeyError(
            f"scene {scene!r} 不在 manifest catalog,"
            f"可用场景:{sorted(catalog.keys())}"
        )
    entry = catalog[scene]

    must_clarify = entry.must_clarify
    field_names = [item["param"] for item in must_clarify]

    logger.info(
        "clarify enter scene=%s user_request=%r fields=%s",
        scene,
        user_request[:80],
        field_names,
    )

    # ─── 抽参 ───────────────────────────────────────────────────────────
    if not must_clarify:
        extracted: dict[str, Any] = {}
    elif not user_request or not user_request.strip():
        extracted = {name: None for name in field_names}
    else:
        raw = await client.chat_json(
            [
                {"role": "system", "content": _build_extract_prompt(entry)},
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
        for item in must_clarify:
            param = item["param"]
            extracted[param] = _validate_value(
                raw.get(param), item.get("options", ())
            )

    # ─── 判齐 ───────────────────────────────────────────────────────────
    missing_items = [item for item in must_clarify if extracted.get(item["param"]) is None]

    if missing_items:
        questions = [_build_question(item) for item in missing_items]
        outcome = ClarifyOutcome(
            status="questions",
            scene=scene,
            questions=questions,
            extracted=extracted,
        )
        logger.info(
            "clarify exit scene=%s status=questions missing=%s",
            scene,
            [item["param"] for item in missing_items],
        )
        return outcome

    # ─── ready:defaults 全量合并 → maps_to 展开 → required_params 校验 ─
    params: dict[str, Any] = dict(entry.defaults)
    params.update(extracted)
    params = _expand_maps_to(params, entry)
    _check_required_params(params, entry)

    outcome = ClarifyOutcome(
        status="ready",
        scene=scene,
        params=params,
        extracted=extracted,
    )
    logger.info(
        "clarify exit scene=%s status=ready param_keys=%s",
        scene,
        sorted(params.keys()),
    )
    return outcome
