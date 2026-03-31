"""
EnvScaler Pipeline 编排器

串联 Step 0 → Step 1 → Step 2 + Review + 数据集导出

完整流程:
  Step 0: 场景文件加载 → 任务解析
  Step 1: 沙箱部署 MCP Server
  Step 2: Agent 轨迹生成（通过 MCP 工具调用）
  Review: 轨迹质量评估
  Export: 导出 SFT/DPO/RLHF 格式数据
"""

import asyncio
import json
import os
import time
from dataclasses import asdict
from typing import Callable, Optional

from openai import OpenAI

from .config import (
    EnvScalerPipelineConfig,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .scene_manager import run_step0
from .sandbox_runner import run_step1, delete_sandbox
from .trajectory_gen import run_step2, EnvScalerTrajectory


# ─── Review Agent ────────────────────────────────────────────────────────────

def review_envscaler_trajectory(
    traj: EnvScalerTrajectory,
    config: EnvScalerPipelineConfig,
) -> dict:
    """
    评估单条 EnvScaler 轨迹质量

    评估维度:
      - tool_selection: 工具选择是否正确
      - tool_execution: 工具执行是否成功
      - reasoning: 推理过程是否清晰
      - completeness: 任务是否完成
      - efficiency: 操作是否高效（步骤数合理）

    Returns:
        Review result dict (兼容 backend.py 格式)
    """
    api_key = config.deepseek_api_key or DEEPSEEK_API_KEY
    base_url = config.deepseek_base_url or DEEPSEEK_BASE_URL
    client = OpenAI(api_key=api_key, base_url=base_url)

    # 构建轨迹摘要
    turns_summary = []
    for turn in traj.turns:
        if isinstance(turn, dict):
            s = {"role": turn.get("role", ""), "content": turn.get("content", "")[:200]}
            if turn.get("tool_calls"):
                s["tool_calls"] = [
                    {
                        "tool": tc.get("tool_name", ""),
                        "success": tc.get("success", False),
                        "output": tc.get("tool_output", "")[:100],
                    }
                    for tc in turn["tool_calls"]
                ]
            turns_summary.append(s)

    traj_json = json.dumps(turns_summary, ensure_ascii=False, indent=2)
    if len(traj_json) > 6000:
        traj_json = traj_json[:6000] + "\n...(截断)"

    review_prompt = f"""你是工具调用轨迹评估专家。评估以下 Agent 与环境交互的轨迹质量。

## 任务描述
{traj.task_desc}

## 环境
{traj.env_name}

## Agent 轨迹
{traj_json}

## 统计
- 总工具调用: {traj.total_tool_calls} 次
- 成功调用: {traj.successful_tool_calls} 次
- 任务完成: {'是' if traj.task_completed else '否'}
- 环境奖励: {traj.task_reward}

## 评估维度 (0-1)
1. tool_selection: Agent 是否选择了正确的工具和参数
2. tool_execution: 工具执行成功率和结果利用
3. reasoning: 推理链是否清晰、逻辑是否正确
4. completeness: 任务是否完整完成
5. efficiency: 操作是否高效，步骤数是否合理

## 输出（严格 JSON）
{{
    "overall_score": 0.75,
    "dimensions": {{
        "tool_selection": 0.8,
        "tool_execution": 0.7,
        "reasoning": 0.8,
        "completeness": 0.9,
        "efficiency": 0.6
    }},
    "fail_modes": [],
    "suggestions": [
        {{"level": "auto", "category": "类别", "desc": "描述", "field": "字段", "from": "原值", "to": "新值"}},
        {{"level": "confirm", "category": "类别", "desc": "描述", "options": ["方案A", "方案B"]}}
    ],
    "reasoning": "评估理由"
}}
只输出 JSON。"""

    try:
        resp = client.chat.completions.create(
            model=config.agent_model,
            messages=[{"role": "user", "content": review_prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        raw = resp.choices[0].message.content
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
        return json.loads(raw.strip())
    except Exception as e:
        return {
            "overall_score": 0.5,
            "dimensions": {},
            "fail_modes": [str(e)],
            "suggestions": [],
            "reasoning": f"Review 失败: {e}",
        }


def review_all_trajectories(
    trajectories: list,
    config: EnvScalerPipelineConfig,
) -> dict:
    """
    综合评估所有轨迹

    Returns:
        review_result dict (兼容 backend.py format)
    """
    if not trajectories:
        return {
            "overall_score": 0,
            "dimensions": {},
            "fail_modes": ["无轨迹数据"],
            "suggestions": [],
            "reasoning": "没有生成任何轨迹",
        }

    # 逐条评估
    individual_reviews = []
    for traj in trajectories:
        rev = review_envscaler_trajectory(traj, config)
        traj.quality_score = rev.get("overall_score", 0)
        individual_reviews.append(rev)

    # 聚合评分
    scores = [r.get("overall_score", 0) for r in individual_reviews]
    avg_score = sum(scores) / len(scores) if scores else 0

    # 聚合维度
    dim_names = ["tool_selection", "tool_execution", "reasoning", "completeness", "efficiency"]
    avg_dims = {}
    for dim in dim_names:
        vals = [r.get("dimensions", {}).get(dim, 0) for r in individual_reviews]
        avg_dims[dim] = sum(vals) / len(vals) if vals else 0

    # 收集失败模式和建议
    all_fail_modes = []
    all_suggestions = []
    for r in individual_reviews:
        all_fail_modes.extend(r.get("fail_modes", []))
        all_suggestions.extend(r.get("suggestions", []))

    # 去重失败模式
    unique_fails = list(set(all_fail_modes))[:5]

    return {
        "overall_score": avg_score,
        "dimensions": avg_dims,
        "fail_modes": unique_fails,
        "suggestions": all_suggestions[:6],
        "reasoning": f"平均评分 {avg_score:.2f}, 共 {len(trajectories)} 条轨迹",
        "individual_reviews": individual_reviews,
    }


# ─── 数据集导出 ───────────────────────────────────────────────────────────────

def export_envscaler_dataset(
    trajectories: list,
    config: EnvScalerPipelineConfig,
    reviews: list = None,
) -> dict:
    """
    导出 SFT / DPO / Raw 格式数据

    Returns:
        {"sft_path": str, "raw_path": str, "dpo_path": str | None}
    """
    os.makedirs(config.output_dir, exist_ok=True)

    result = {}

    # ─── SFT 格式 ───
    sft_path = os.path.join(config.output_dir, f"{config.task_id}_sft.jsonl")
    with open(sft_path, "w", encoding="utf-8") as f:
        for traj in trajectories:
            # 构建 SFT 训练数据（system + user + assistant turns with tool calls）
            sft_messages = [
                {"role": "system", "content": "你是一个能够使用环境工具完成任务的 AI 助手。"},
            ]
            for turn in traj.turns:
                t = turn if isinstance(turn, dict) else asdict(turn)
                msg = {"role": t["role"], "content": t["content"]}
                if t.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": tc.get("call_id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("tool_name", ""),
                                "arguments": json.dumps(
                                    tc.get("tool_input", {}), ensure_ascii=False
                                ),
                            },
                        }
                        for tc in t["tool_calls"]
                    ]
                sft_messages.append(msg)

            f.write(json.dumps({
                "id": traj.trajectory_id,
                "messages": sft_messages,
                "metadata": {
                    "task_id": traj.task_id,
                    "env_name": traj.env_name,
                    "tool_calls": traj.total_tool_calls,
                    "task_completed": traj.task_completed,
                    "reward": traj.task_reward,
                    "score": traj.quality_score,
                },
            }, ensure_ascii=False) + "\n")

    result["sft_path"] = sft_path
    print(f"  SFT: {sft_path}")

    # ─── DPO 格式 ───
    if reviews and len(trajectories) >= 2:
        scored = sorted(
            zip(trajectories, reviews),
            key=lambda x: x[1].get("overall_score", 0),
            reverse=True,
        )
        dpo_path = os.path.join(config.output_dir, f"{config.task_id}_dpo.jsonl")
        dpo_count = 0
        with open(dpo_path, "w", encoding="utf-8") as f:
            for i in range(len(scored) // 2):
                chosen_traj, chosen_rev = scored[i]
                rejected_traj, rejected_rev = scored[-(i + 1)]
                cs = chosen_rev.get("overall_score", 0)
                rs = rejected_rev.get("overall_score", 0)
                if cs > rs:
                    chosen_text = "\n".join(
                        t.get("content", "") if isinstance(t, dict) else t.content
                        for t in chosen_traj.turns
                        if (t.get("role") if isinstance(t, dict) else t.role) == "assistant"
                    )[:1500]
                    rejected_text = "\n".join(
                        t.get("content", "") if isinstance(t, dict) else t.content
                        for t in rejected_traj.turns
                        if (t.get("role") if isinstance(t, dict) else t.role) == "assistant"
                    )[:1500]
                    f.write(json.dumps({
                        "prompt": chosen_traj.task_desc,
                        "chosen": chosen_text,
                        "rejected": rejected_text,
                        "chosen_score": cs,
                        "rejected_score": rs,
                    }, ensure_ascii=False) + "\n")
                    dpo_count += 1

        if dpo_count > 0:
            result["dpo_path"] = dpo_path
            print(f"  DPO: {dpo_path} ({dpo_count} pairs)")

    # ─── Raw 格式 ───
    raw_path = os.path.join(config.output_dir, f"{config.task_id}_raw.jsonl")
    with open(raw_path, "w", encoding="utf-8") as f:
        for traj in trajectories:
            data = {
                "trajectory_id": traj.trajectory_id,
                "task_id": traj.task_id,
                "task_desc": traj.task_desc,
                "env_name": traj.env_name,
                "turns": traj.turns,
                "total_tool_calls": traj.total_tool_calls,
                "successful_tool_calls": traj.successful_tool_calls,
                "total_tokens": traj.total_tokens,
                "quality_score": traj.quality_score,
                "task_completed": traj.task_completed,
                "task_reward": traj.task_reward,
            }
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    result["raw_path"] = raw_path
    print(f"  Raw: {raw_path}")

    return result


# ─── 主流程 ──────────────────────────────────────────────────────────────────

async def run_envscaler_pipeline(
    config: EnvScalerPipelineConfig,
    scene_files_content: dict = None,
    event_callback: Callable = None,
) -> dict:
    """
    执行完整的 EnvScaler 工具调用轨迹合成流水线

    Args:
        config: Pipeline 配置
        scene_files_content: 直接传入的场景文件内容（Web UI 上传）
        event_callback: 事件回调

    Returns:
        Pipeline 执行结果摘要
    """
    def emit(t, m, d=None):
        if event_callback:
            event_callback(t, m, d)
        print(f"  [{t}] {m}")

    start = time.time()
    emit("pipeline_start", "EnvScaler Pipeline 启动")

    sandbox_id = None

    try:
        # ════ Step 0: 场景文件管理 ════
        emit("step0_start", "加载场景文件")
        scene, tasks = run_step0(
            config,
            scene_files_content=scene_files_content,
            event_callback=lambda t, m: emit(t, m),
        )

        if not tasks:
            emit("pipeline_error", "未找到可执行的任务")
            return {"status": "failed", "error": "No tasks found in scene files"}

        emit("step0_done", f"✅ {len(tasks)} 个任务就绪, 环境: {scene.env_name}")

        # ════ Step 1: 沙箱 MCP Server 部署 ════
        emit("step1_start", "部署沙箱 MCP Server")
        step1_result = await run_step1(
            scene=scene,
            config=config,
            event_callback=lambda t, m: emit(t, m),
        )

        sandbox_id = step1_result["sandbox_id"]
        mcp_ready = step1_result["mcp_ready"]
        mcp_tools = step1_result["mcp_tools"]

        if not mcp_ready:
            emit("pipeline_warn", "⚠ MCP Server 未就绪, 轨迹生成可能失败")

        emit("step1_done", f"✅ 沙箱就绪, MCP 工具: {len(mcp_tools)} 个")

        # ════ Step 2: Agent 轨迹生成 ════
        emit("step2_start", "Agent 轨迹生成")
        trajectories = await run_step2(
            tasks=tasks,
            config=config,
            sandbox_id=sandbox_id,
            mcp_tools=mcp_tools,
            event_callback=lambda t, m: emit(t, m),
        )
        emit("step2_done", f"✅ 生成 {len(trajectories)} 条轨迹")

        # ════ Review ════
        emit("review_start", "质量评估")
        review_result = review_all_trajectories(trajectories, config)
        avg_score = review_result.get("overall_score", 0)
        individual_reviews = review_result.pop("individual_reviews", [])
        emit("review_done", f"✅ 平均评分: {avg_score:.3f}")

        # ════ Export ════
        emit("export_start", "导出数据集")
        export_result = export_envscaler_dataset(
            trajectories, config, individual_reviews,
        )
        emit("export_done", f"✅ 已导出到 {config.output_dir}")

        # ════ 清理沙箱 ════
        if sandbox_id:
            try:
                await delete_sandbox(sandbox_id)
                emit("sandbox_cleanup", "沙箱已清理")
            except Exception:
                pass
            sandbox_id = None

        elapsed = time.time() - start

        summary = {
            "status": "completed",
            "elapsed_seconds": round(elapsed, 1),
            "env_name": scene.env_name,
            "tasks_count": len(tasks),
            "trajectories_count": len(trajectories),
            "total_tool_calls": sum(t.total_tool_calls for t in trajectories),
            "successful_tool_calls": sum(t.successful_tool_calls for t in trajectories),
            "tasks_completed": sum(1 for t in trajectories if t.task_completed),
            "total_tokens": sum(t.total_tokens for t in trajectories),
            "avg_quality": round(avg_score, 3),
            "avg_reward": round(
                sum(t.task_reward for t in trajectories) / max(len(trajectories), 1), 3
            ),
            "review": review_result,
            "export": export_result,
            "output_dir": config.output_dir,
        }

        emit("pipeline_done",
             f"完成 | {elapsed:.1f}s | {len(trajectories)} 条轨迹 | "
             f"avg_score={avg_score:.3f}")

        return summary

    except Exception as e:
        emit("pipeline_error", f"Pipeline 执行失败: {e}")
        return {"status": "failed", "error": str(e)}

    finally:
        # 确保清理沙箱
        if sandbox_id:
            try:
                await delete_sandbox(sandbox_id)
            except Exception:
                pass
