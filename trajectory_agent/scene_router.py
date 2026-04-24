"""Scene Router — 用 LLM 把用户自然语言需求路由到 search2qa / toolace / toucan。

A-2-2 设计选择:
- 词汇表只含 3 个已接通场景;envscaler / mobile_agent 本阶段不纳入,避免误导。
- 纯决策层:不做 dispatch、不做 clarify。LLM 层异常原样上抛,本层只做 schema 校验。
- 置信度低于阈值 或 scene==unknown 时 low_confidence=True,交由上层降级处理。
"""

import logging
import os
from dataclasses import dataclass

from trajectory_agent.llm_client import LLMClient

logger = logging.getLogger(__name__)


SUPPORTED_SCENES: tuple[str, ...] = ("search2qa", "toolace")

SCENE_CATALOG: dict[str, str] = {
    "search2qa": (
        "基于搜索引擎驱动的 QA 数据合成。适用场景是用户给定一个主题或领域"
        "(如'大语言模型对齐''医疗问诊'),需要合成【搜索问答对】类训练数据。"
        "支持 Query Evolution,产出形态是 QA pair。"
    ),
    "toolace": (
        "多轮 function calling 轨迹合成。适用场景是用户需要训练模型的【通用工具调用】"
        "能力,产出包含多轮工具调用 + 推理的轨迹,支持 8 种角色背景"
        "(开发者 / 产品经理 / 运营等)。工具集合偏通用 API,不依赖 MCP 生态。"
    ),
}

DEFAULT_CONFIDENCE_THRESHOLD = 0.7

_VALID_SCENES = set(SUPPORTED_SCENES) | {"unknown"}


class SceneRoutingInvalid(Exception):
    """LLM 返回的 JSON 不符合 schema:缺字段 / scene 非法枚举 / confidence 越界等。"""

    def __init__(self, message: str, *, raw: dict | None = None):
        super().__init__(message)
        self.raw = raw


@dataclass
class RouterDecision:
    scene: str
    confidence: float
    reasoning: str
    low_confidence: bool = False

    def to_dict(self) -> dict:
        return {
            "scene": self.scene,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "low_confidence": self.low_confidence,
        }


def _build_system_prompt() -> str:
    catalog_lines = [f"- {sid}: {desc}" for sid, desc in SCENE_CATALOG.items()]
    catalog_block = "\n".join(catalog_lines)

    return f"""你是一个场景路由器,负责把用户的数据合成需求路由到最合适的轨迹合成场景。

候选场景:
{catalog_block}

额外选项:
- unknown: 如果用户需求与上述三个场景都不沾边(比如完全不是数据合成任务,或者是其他领域的请求),返回 unknown。

输出要求(严格 JSON,不要任何额外文字):
{{
  "scene": "search2qa" | "toolace" | "toucan" | "unknown",
  "confidence": <0 到 1 之间的浮点数,表示你的决策确定性>,
  "reasoning": "<一句话中文说明为什么选这个场景>"
}}

判别指引:
- 用户若提到"搜索""主题""QA""问答"等关键词,倾向 search2qa。
- 用户若提到"工具调用""function calling""多轮工具"且没有特指 MCP,倾向 toolace。
- 用户需求极度模糊(比如只说"我要一些训练数据",没有任何关于数据形态的线索),confidence 应当 ≤ 0.5。
- 用户需求跑题(写周报 / 订机票 / 写普通代码等),返回 unknown 并给较高 confidence。

示例 1:
用户:我想合成关于量子计算领域的搜索问答数据用于训练
输出:{{"scene": "search2qa", "confidence": 0.95, "reasoning": "明确提到搜索问答数据合成,主题是量子计算,匹配 search2qa"}}

示例 2:
用户:帮我订一张明天去上海的机票
输出:{{"scene": "unknown", "confidence": 0.98, "reasoning": "订机票与任何数据合成场景都不相关"}}
"""


async def route_scene(
    user_request: str,
    client: LLMClient,
    *,
    threshold: float | None = None,
) -> RouterDecision:
    """调 LLM 把用户需求路由到场景。LLM 层异常原样上抛。"""
    if threshold is None:
        env_val = os.environ.get("SCENE_ROUTER_CONFIDENCE_THRESHOLD")
        if env_val:
            try:
                threshold = float(env_val)
            except ValueError:
                logger.warning(
                    "SCENE_ROUTER_CONFIDENCE_THRESHOLD=%r 无法转 float,回退默认 %s",
                    env_val,
                    DEFAULT_CONFIDENCE_THRESHOLD,
                )
                threshold = DEFAULT_CONFIDENCE_THRESHOLD
        else:
            threshold = DEFAULT_CONFIDENCE_THRESHOLD

    logger.info(
        "route_scene enter user_request=%r threshold=%.2f",
        user_request[:80],
        threshold,
    )

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": user_request},
    ]

    data = await client.chat_json(messages, temperature=0.1)

    decision = _parse_decision(data)
    decision.low_confidence = (decision.confidence < threshold) or (
        decision.scene == "unknown"
    )

    if decision.low_confidence:
        logger.warning(
            "route_scene low_confidence scene=%s confidence=%.2f threshold=%.2f",
            decision.scene,
            decision.confidence,
            threshold,
        )
    logger.info(
        "route_scene exit scene=%s confidence=%.2f low_confidence=%s",
        decision.scene,
        decision.confidence,
        decision.low_confidence,
    )
    return decision


def _parse_decision(raw: dict) -> RouterDecision:
    for key in ("scene", "confidence", "reasoning"):
        if key not in raw:
            raise SceneRoutingInvalid(
                f"LLM 响应缺少必需字段 '{key}',实际 keys={list(raw.keys())}",
                raw=raw,
            )

    scene = raw["scene"]
    if not isinstance(scene, str) or scene not in _VALID_SCENES:
        raise SceneRoutingInvalid(
            f"LLM 返回 scene={scene!r} 不在允许集合 {sorted(_VALID_SCENES)}",
            raw=raw,
        )

    raw_conf = raw["confidence"]
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        raise SceneRoutingInvalid(
            f"LLM 返回 confidence={raw_conf!r} 无法转 float",
            raw=raw,
        ) from None
    if not (0.0 <= confidence <= 1.0):
        raise SceneRoutingInvalid(
            f"LLM 返回 confidence={confidence} 不在 [0, 1]",
            raw=raw,
        )

    reasoning = raw["reasoning"]
    if not isinstance(reasoning, str):
        raise SceneRoutingInvalid(
            f"LLM 返回 reasoning 不是字符串,实际 {type(reasoning).__name__}",
            raw=raw,
        )

    return RouterDecision(scene=scene, confidence=confidence, reasoning=reasoning)
