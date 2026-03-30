"""
Step 2: 问题质量检查

子步骤:
  2.1 为每个问题生成质量评估 Prompt
  2.2 调用 LLM 进行多维度质量评分
  2.3 处理评分结果、过滤低质量问题
"""

import json
from dataclasses import dataclass, asdict
from typing import Optional

from openai import OpenAI

from .config import (
    ToucanPipelineConfig,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .step1_question_synthesis import GeneratedQuestion


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class QualityScore:
    """问题质量评分"""
    question_id: str
    difficulty_score: float = 0.0   # 工具选择难度 (0-1)
    quality_score: float = 0.0      # 问题质量 (0-1)
    realism_score: float = 0.0      # 场景真实性 (0-1)
    uniqueness_score: float = 0.0   # 独特性 (0-1)
    verifiability: float = 0.0      # 可验证性 (0-1)
    stability: float = 0.0          # 稳定性 (0-1)
    overall_score: float = 0.0      # 综合评分
    reasoning: str = ""             # 评分理由
    passed: bool = False            # 是否通过质量检查


@dataclass
class QualityCheckedQuestion:
    """通过质量检查的问题"""
    question: GeneratedQuestion
    quality: QualityScore


# ─── Step 2.1: 构建质量检查 Prompt ──────────────────────────────────────────────

def build_qc_prompt(question: GeneratedQuestion, criteria: list) -> str:
    """
    为单个问题构建质量评估 Prompt

    Args:
        question: 待评估的问题
        criteria: 评估维度列表

    Returns:
        str: 质量评估 Prompt
    """
    criteria_desc = {
        "difficulty": "tool_selection_difficulty: 工具选择的难度，是否需要从多个工具中做出正确选择 (0-1)",
        "quality": "question_quality: 问题的清晰度、完整性和表达质量 (0-1)",
        "realism": "scenario_realism: 场景是否真实可信，是否像真实用户会提出的 (0-1)",
        "uniqueness": "uniqueness: 问题是否独特、有创意，避免千篇一律 (0-1)",
        "verifiability": "verifiability: 问题的答案是否可以通过工具调用来验证 (0-1)",
        "stability": "stability: 多次执行是否会得到一致的结果 (0-1)",
    }

    selected = [criteria_desc.get(c, c) for c in criteria]
    criteria_text = "\n".join(f"  - {c}" for c in selected)

    prompt = f"""你是一个工具使用问题质量评估专家。请评估以下问题的质量。

## 待评估的问题

问题: {question.question}
目标服务器: {', '.join(question.target_servers)}
目标工具: {', '.join(question.target_tools)}
难度标注: {question.difficulty}
场景: {question.scenario}

## 评估维度

{criteria_text}

## 输出格式（严格 JSON）

{{
  "difficulty_score": 0.8,
  "quality_score": 0.7,
  "realism_score": 0.9,
  "uniqueness_score": 0.6,
  "verifiability": 0.8,
  "stability": 0.7,
  "overall_score": 0.75,
  "reasoning": "评分理由的简要说明"
}}

请只输出 JSON，不要有其他内容。
"""
    return prompt


# ─── Step 2.2: 批量质量评估 ────────────────────────────────────────────────────

def evaluate_questions_batch(
    questions: list,
    config: ToucanPipelineConfig,
) -> list:
    """
    批量评估问题质量

    Args:
        questions: 待评估的问题列表
        config: Pipeline 配置

    Returns:
        list[QualityScore]: 评分结果列表
    """
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    scores = []

    for i, q in enumerate(questions):
        print(f"    评估问题 {i + 1}/{len(questions)}: {q.question[:50]}...")

        prompt = build_qc_prompt(q, config.qc_criteria)

        try:
            response = client.chat.completions.create(
                model=config.qc_model,
                messages=[
                    {"role": "system", "content": "你是问题质量评估专家，请严格按 JSON 格式输出评分。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=config.qc_temperature,
                max_tokens=1024,
                stream=False,
            )

            raw = response.choices[0].message.content
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]

            data = json.loads(raw.strip())

            score = QualityScore(
                question_id=q.question_id,
                difficulty_score=float(data.get("difficulty_score", 0.5)),
                quality_score=float(data.get("quality_score", 0.5)),
                realism_score=float(data.get("realism_score", 0.5)),
                uniqueness_score=float(data.get("uniqueness_score", 0.5)),
                verifiability=float(data.get("verifiability", 0.5)),
                stability=float(data.get("stability", 0.5)),
                overall_score=float(data.get("overall_score", 0.5)),
                reasoning=data.get("reasoning", ""),
                passed=float(data.get("overall_score", 0.5)) >= config.qc_min_score,
            )
            scores.append(score)

        except Exception as e:
            print(f"    ⚠ 评估失败: {e}")
            scores.append(QualityScore(
                question_id=q.question_id,
                overall_score=0.5,
                reasoning=f"评估失败: {e}",
                passed=True,  # 评估失败时默认通过，避免丢失问题
            ))

    return scores


# ─── Step 2.3: 过滤 + 排序 ─────────────────────────────────────────────────────

def filter_and_rank(
    questions: list,
    scores: list,
    min_score: float = 0.6,
) -> list:
    """
    过滤低质量问题，按评分排序

    Args:
        questions: 问题列表
        scores: 评分列表
        min_score: 最低通过分数

    Returns:
        list[QualityCheckedQuestion]: 通过质检的问题（按分数降序）
    """
    score_map = {s.question_id: s for s in scores}
    checked = []

    for q in questions:
        s = score_map.get(q.question_id)
        if s and s.overall_score >= min_score:
            s.passed = True
            checked.append(QualityCheckedQuestion(question=q, quality=s))

    # 按评分降序排列
    checked.sort(key=lambda x: x.quality.overall_score, reverse=True)

    return checked


# ─── 整合: 执行 Step 2 完整流程 ────────────────────────────────────────────────

def run_step2(
    questions: list,
    config: ToucanPipelineConfig,
    event_callback=None,
) -> list:
    """
    执行 Step 2 完整流程: 问题质量检查

    Args:
        questions: Step 1 输出的问题列表
        config: Pipeline 配置
        event_callback: 事件回调

    Returns:
        list[QualityCheckedQuestion]: 通过质检的问题列表
    """
    def emit(msg):
        print(f"  {msg}")
        if event_callback:
            event_callback("quality_check", msg)

    emit(f"[Step 2.1-2.2] 对 {len(questions)} 个问题进行质量评估...")
    scores = evaluate_questions_batch(questions, config)

    passed_count = sum(1 for s in scores if s.overall_score >= config.qc_min_score)
    emit(f"  评估完成: {passed_count}/{len(scores)} 个通过阈值 ({config.qc_min_score})")

    emit("[Step 2.3] 过滤 + 排序...")
    checked = filter_and_rank(questions, scores, config.qc_min_score)
    emit(f"  最终保留 {len(checked)} 个高质量问题")

    # 打印 Top 3
    for i, c in enumerate(checked[:3]):
        emit(f"  Top {i+1}: [{c.quality.overall_score:.2f}] {c.question.question[:60]}")

    return checked
