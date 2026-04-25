"""Clarify 规则硬编码 —— A-2-3b 阶段的场景澄清规则。

本文件硬编码 clarify 规则;B 阶段会搬到平台侧 Skill manifest。

仅支持 search2qa 和 toolace 两个场景;toucan 端点未注册(import 失败),
A-2-3b 阶段不纳入 router / clarifier 词汇表。
"""

from dataclasses import dataclass


@dataclass
class ClarifyRule:
    scene: str
    must_fields: list[str]
    soft_fields: list[str]
    defaults: dict
    field_specs: dict[str, dict]


CLARIFY_RULES: dict[str, ClarifyRule] = {
    "search2qa": ClarifyRule(
        scene="search2qa",
        must_fields=["task_desc"],
        soft_fields=[],
        defaults={
            "max_evolutions": 2,
            "max_turns": 20,
            "enable_evolution": True,
            "enable_rewrite": True,
        },
        field_specs={
            "task_desc": {
                "question": "您希望合成哪个领域或主题的搜索问答数据?",
                "type": "str",
                "example": "例如:大语言模型对齐、临床医学问诊、金融风控",
                "default_hint": None,
            },
        },
    ),
    "toolace": ClarifyRule(
        scene="toolace",
        must_fields=[],
        soft_fields=["task_count"],
        defaults={
            "enable_role_background": True,
            "enable_cross_group": True,
            "source_tools": [],
        },
        field_specs={
            "task_count": {
                "question": "需要合成多少条工具调用轨迹?",
                "type": "int",
                "example": "例如:50、100、200",
                "default_hint": "默认 10 条,可直接回复\"默认\"跳过",
            },
        },
    ),
}


def get_rule(scene: str) -> ClarifyRule:
    """根据 scene 返回规则;scene 不在 CLARIFY_RULES 抛 KeyError。"""
    if scene not in CLARIFY_RULES:
        raise KeyError(
            f"scene {scene!r} 不在 CLARIFY_RULES,"
            f"本阶段只支持 {sorted(CLARIFY_RULES.keys())}"
        )
    return CLARIFY_RULES[scene]
