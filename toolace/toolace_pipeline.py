"""
ToolACE Pipeline 编排器

串联 Step 1 → Step 2 → Step 3 + Review + 数据集导出
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict
from typing import Callable, Optional

from .config import ToolACEPipelineConfig, LLMConfig, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from .step1_tool_evolution import run_step1
from .step2_task_generation import run_step2
from .step3_trajectory_gen import run_step3, GeneratedTrajectory


# ─── Review Agent ────────────────────────────────────────────────────────────

def review_toolace_trajectories(
    trajectories: list,
    config: ToolACEPipelineConfig,
) -> dict:
    """
    综合评估所有轨迹的质量

    Returns: review_result dict (compatible with backend.py format)
    """
    if not trajectories:
        return {
            "overall_score": 0,
            "dimensions": {},
            "fail_modes": ["无轨迹数据"],
            "suggestions": [],
            "reasoning": "没有生成任何轨迹",
        }

    # 聚合所有轨迹的验证分数
    scores = [t.quality_score for t in trajectories if t.quality_score > 0]
    if not scores:
        return {
            "overall_score": 0,
            "dimensions": {},
            "fail_modes": ["所有轨迹验证失败"],
            "suggestions": [],
            "reasoning": "所有轨迹的质量验证均未通过",
        }

    overall = sum(scores) / len(scores)

    # 聚合各维度分数
    dim_scores = {}
    for traj in trajectories:
        v = traj.verification if isinstance(traj.verification, dict) else {}
        dims = v.get("dimensions", {})
        for k, v in dims.items():
            if k not in dim_scores:
                dim_scores[k] = []
            dim_scores[k].append(v)

    dimensions = {k: sum(vs) / len(vs) for k, vs in dim_scores.items() if vs}

    # 收集失败模式
    fail_modes = []
    for traj in trajectories:
        v = traj.verification if isinstance(traj.verification, dict) else {}
        for issue in v.get("issues", []):
            if issue not in fail_modes:
                fail_modes.append(issue)

    # 生成建议
    suggestions = []
    if overall < config.quality_threshold:
        if dimensions.get("tool_selection", 1) < 0.7:
            suggestions.append({
                "level": "auto",
                "category": "工具选择",
                "desc": "提高工具选择准确度",
                "field": "temperature",
                "from": str(config.llm.temperature),
                "to": "0.3",
            })
        if dimensions.get("naturalness", 1) < 0.7:
            suggestions.append({
                "level": "confirm",
                "category": "自然度",
                "desc": "对话表达不够自然",
                "options": [
                    "增加更多日常用语和语气词",
                    "减少技术术语使用",
                    "添加更多上下文描述",
                ],
            })

    return {
        "overall_score": overall,
        "dimensions": dimensions,
        "fail_modes": fail_modes[:5],
        "suggestions": suggestions,
        "reasoning": (
            f"共 {len(trajectories)} 条轨迹, {len(scores)} 条通过验证. "
            f"平均分: {overall:.2f}. "
            f"多轮对话: {sum(1 for t in trajectories if t.is_multi_turn)}, "
            f"缺参任务: {sum(1 for t in trajectories if t.metadata.get('has_missing_params'))}."
        ),
    }


# ─── 数据集导出 ───────────────────────────────────────────────────────────────

def export_toolace_dataset(
    trajectories: list,
    config: ToolACEPipelineConfig,
    coupled_groups: list = None,
    review: dict = None,
) -> dict:
    """
    导出 SFT / DPO / RLHF 格式数据集

    Returns: export summary dict
    """
    os.makedirs(config.output_dir, exist_ok=True)

    # ── SFT 格式 ──
    sft_path = os.path.join(config.output_dir, f"{config.task_id}_sft.jsonl")
    with open(sft_path, "w", encoding="utf-8") as f:
        for traj in trajectories:
            # 转换为 SFT messages 格式
            messages = [{"role": "system", "content": "你是一个能够使用各种工具完成任务的智能助手。"}]
            for turn in traj.turns:
                if isinstance(turn, dict):
                    role = turn.get("role", "")
                    msg = {"role": role}
                    if turn.get("content"):
                        msg["content"] = turn["content"]
                    if turn.get("thought"):
                        msg["thought"] = turn["thought"]
                    if turn.get("tool_calls"):
                        msg["tool_calls"] = turn["tool_calls"]
                    if turn.get("tool_results"):
                        msg["tool_results"] = turn["tool_results"]
                    messages.append(msg)

            entry = {
                "id": traj.trajectory_id,
                "task_id": traj.task_id,
                "messages": messages,
                "metadata": {
                    "task_type": traj.task_type,
                    "tools_used": traj.tools_used,
                    "total_turns": traj.total_turns,
                    "is_multi_turn": traj.is_multi_turn,
                    "quality_score": traj.quality_score,
                    **traj.metadata,
                },
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── DPO 格式（如果有足够数据）──
    dpo_path = None
    scored = sorted(
        [t for t in trajectories if t.quality_score > 0],
        key=lambda t: t.quality_score,
        reverse=True,
    )
    if len(scored) >= 4:
        dpo_path = os.path.join(config.output_dir, f"{config.task_id}_dpo.jsonl")
        with open(dpo_path, "w", encoding="utf-8") as f:
            half = len(scored) // 2
            for i in range(min(half, len(scored) - half)):
                chosen = scored[i]
                rejected = scored[-(i + 1)]
                if chosen.quality_score > rejected.quality_score:
                    chosen_text = "\n".join(
                        t.get("content", "") for t in chosen.turns
                        if isinstance(t, dict) and t.get("role") == "assistant" and t.get("content")
                    )[:1500]
                    rejected_text = "\n".join(
                        t.get("content", "") for t in rejected.turns
                        if isinstance(t, dict) and t.get("role") == "assistant" and t.get("content")
                    )[:1500]
                    f.write(json.dumps({
                        "prompt": chosen.task_description,
                        "chosen": chosen_text,
                        "rejected": rejected_text,
                        "chosen_score": chosen.quality_score,
                        "rejected_score": rejected.quality_score,
                    }, ensure_ascii=False) + "\n")

    # ── Raw 完整数据 ──
    raw_path = os.path.join(config.output_dir, f"{config.task_id}_raw.jsonl")
    with open(raw_path, "w", encoding="utf-8") as f:
        for traj in trajectories:
            data = {
                "trajectory_id": traj.trajectory_id,
                "task_id": traj.task_id,
                "task_description": traj.task_description,
                "task_type": traj.task_type,
                "turns": traj.turns,
                "tools_used": traj.tools_used,
                "total_turns": traj.total_turns,
                "is_multi_turn": traj.is_multi_turn,
                "quality_score": traj.quality_score,
                "verification": traj.verification,
                "total_tokens": traj.total_tokens,
                "metadata": traj.metadata,
            }
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    # ── 工具组导出 ──
    tools_path = None
    if coupled_groups:
        tools_path = os.path.join(config.output_dir, f"{config.task_id}_tools.json")
        with open(tools_path, "w", encoding="utf-8") as f:
            json.dump(coupled_groups, f, ensure_ascii=False, indent=2)

    return {
        "sft_path": sft_path,
        "dpo_path": dpo_path,
        "raw_path": raw_path,
        "tools_path": tools_path,
        "total_trajectories": len(trajectories),
        "successful": sum(1 for t in trajectories if t.quality_score > 0),
    }


# ─── 主流程 ───────────────────────────────────────────────────────────────────

async def run_toolace_pipeline(
    config: ToolACEPipelineConfig,
    emit: Callable = None,
) -> dict:
    """
    执行完整的 ToolACE Pipeline

    Returns: pipeline_result dict
    """
    start_time = time.time()
    total_tokens = 0

    if not config.task_id:
        config.task_id = f"toolace_{uuid.uuid4().hex[:8]}"

    if not config.llm.api_key:
        config.llm.api_key = DEEPSEEK_API_KEY
    if not config.llm.base_url:
        config.llm.base_url = DEEPSEEK_BASE_URL

    if emit:
        emit("pipeline_start", f"🚀 ToolACE Pipeline 启动 (task_id={config.task_id})")
        emit("pipeline_config", f"   源工具: {len(config.source_tools)} | "
                                f"扩展: ×{config.expansion_count} | "
                                f"任务: {config.task_count} | "
                                f"并发: {config.max_workers}")

    # ═══ Step 1: 工具自进化合成 ═══
    coupled_groups, tokens = run_step1(config, emit)
    total_tokens += tokens

    # ═══ Step 2: 任务生成 ═══
    all_tasks, converted_groups, tokens = run_step2(coupled_groups, config, emit)
    total_tokens += tokens

    # ═══ Step 3: 轨迹生成 ═══
    trajectories, tokens = run_step3(all_tasks, converted_groups, config, emit)
    total_tokens += tokens

    # ═══ Review ═══
    if emit:
        emit("review_start", "Review Agent 评估轨迹质量...")

    review = review_toolace_trajectories(trajectories, config)

    if emit:
        emit("review_done", f"评估完成: overall_score={review['overall_score']:.2f}")

    # ═══ 导出 ═══
    if emit:
        emit("export_start", "导出数据集...")

    export_result = export_toolace_dataset(trajectories, config, coupled_groups, review)

    if emit:
        emit("export_done", f"数据集已导出: {export_result['sft_path']}")

    # ═══ 汇总 ═══
    elapsed = time.time() - start_time

    pipeline_result = {
        "task_id": config.task_id,
        "status": "completed",
        "elapsed_seconds": elapsed,
        "total_tokens": total_tokens,
        "step1": {
            "tool_groups": len(coupled_groups),
            "total_tools": sum(len(g.get("tools", [])) for g in coupled_groups),
            "total_chains": sum(len(g.get("tool_chains", [])) for g in coupled_groups),
        },
        "step2": {
            "total_tasks": len(all_tasks),
            "standard": sum(1 for t in all_tasks if t.get("type") == "standard"),
            "cross_group": sum(1 for t in all_tasks if t.get("type") == "cross_group"),
            "missing_param": sum(1 for t in all_tasks if t.get("type") == "missing_param"),
        },
        "step3": {
            "total_trajectories": len(trajectories),
            "successful": sum(1 for t in trajectories if t.quality_score > 0),
            "multi_turn": sum(1 for t in trajectories if t.is_multi_turn),
            "avg_score": review.get("overall_score", 0),
        },
        "review": review,
        "export": export_result,
    }

    if emit:
        emit("pipeline_complete",
             f"✅ ToolACE Pipeline 完成! "
             f"({elapsed:.1f}s, {total_tokens:,} tokens, "
             f"{len(trajectories)} 条轨迹, "
             f"avg_score={review.get('overall_score', 0):.2f})")

    return pipeline_result


# ─── CLI 入口 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ToolACE Pipeline")
    parser.add_argument("--task-count", type=int, default=5, help="生成任务数量")
    parser.add_argument("--expansion-count", type=int, default=2, help="每个工具扩展数量")
    parser.add_argument("--max-workers", type=int, default=2, help="并发线程数")
    parser.add_argument("--output-dir", type=str, default="output/toolace", help="输出目录")
    parser.add_argument("--model", type=str, default="deepseek-chat", help="LLM 模型")
    parser.add_argument("--temperature", type=float, default=0.7, help="LLM 温度")
    parser.add_argument("--no-cross-group", action="store_true", help="禁用跨组任务")
    parser.add_argument("--no-role", action="store_true", help="禁用角色背景")
    parser.add_argument("--missing-ratio", type=float, default=0.3, help="缺参任务比例")
    args = parser.parse_args()

    from .config import PRESET_SOURCE_TOOLS

    config = ToolACEPipelineConfig(
        task_id=f"toolace_{uuid.uuid4().hex[:8]}",
        source_tools=PRESET_SOURCE_TOOLS,
        llm=LLMConfig(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            model=args.model,
            temperature=args.temperature,
        ),
        expansion_count=args.expansion_count,
        task_count=args.task_count,
        max_workers=args.max_workers,
        enable_cross_group=not args.no_cross_group,
        enable_role_background=not args.no_role,
        missing_param_ratio=args.missing_ratio,
        output_dir=args.output_dir,
    )

    def cli_emit(event_type, message, data=None):
        print(f"[{event_type}] {message}")

    result = asyncio.run(run_toolace_pipeline(config, cli_emit))
    print(json.dumps(result, ensure_ascii=False, indent=2))
