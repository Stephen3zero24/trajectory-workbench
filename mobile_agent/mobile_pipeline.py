"""
Mobile Agent Pipeline 编排器

串联 Step 0 → Step 1 → Step 2 + Review + 数据集导出

完整流程:
  Step 0: 场景加载 → 解析 mobile_scenarios.json → 任务列表
  Step 1: 通过 OpenSandbox 启动 Redroid 容器
  Step 2: VLM 驱动的 Agent 轨迹生成 (截图 → 推理 → ADB 动作)
  Review: 轨迹质量评估
  Export: 导出 SFT/DPO/Raw 格式数据
"""

import asyncio
import json
import os
import time
from dataclasses import asdict
from typing import Callable, Optional

from openai import OpenAI

from .config import (
    MobileAgentPipelineConfig,
    MobileScenarioTask,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .sandbox_runner import run_step1
from .trajectory_gen import run_step2, MobileTrajectory


# ─── Step 0: 场景加载 ────────────────────────────────────────────────────────

def run_step0(
    config: MobileAgentPipelineConfig,
    scenario_content: list = None,
    event_callback: Callable = None,
) -> list:
    """
    Step 0: 加载场景文件, 解析为 MobileScenarioTask 列表

    Args:
        config: Pipeline 配置
        scenario_content: 直接传入的场景 JSON (上传模式)
        event_callback: 事件回调

    Returns:
        list[MobileScenarioTask]
    """
    emit = event_callback or (lambda t, m: None)

    # 加载场景数据
    if scenario_content:
        raw_tasks = scenario_content
        emit("step0", "从上传内容加载场景")
    elif config.scenario_path and os.path.exists(config.scenario_path):
        with open(config.scenario_path, "r", encoding="utf-8") as f:
            raw_tasks = json.load(f)
        emit("step0", f"从文件加载场景: {config.scenario_path}")
    else:
        # 使用内置场景
        builtin_path = os.path.join(
            os.path.dirname(__file__), "mobile_scenarios.json"
        )
        if os.path.exists(builtin_path):
            with open(builtin_path, "r", encoding="utf-8") as f:
                raw_tasks = json.load(f)
            emit("step0", f"使用内置场景 ({len(raw_tasks)} 个任务)")
        else:
            emit("step0_error", "未找到场景文件")
            return []

    # 确保是列表
    if isinstance(raw_tasks, dict):
        raw_tasks = raw_tasks.get("tasks", raw_tasks.get("scenarios", [raw_tasks]))

    # 解析为 MobileScenarioTask
    tasks = []
    for item in raw_tasks:
        task = MobileScenarioTask(
            task_id=item.get("task_id", f"task_{len(tasks)}"),
            task_desc=item.get("task_desc", ""),
            app_package=item.get("app_package", ""),
            app_activity=item.get("app_activity", ""),
            pre_install_apks=item.get("pre_install_apks", []),
            initial_actions=item.get("initial_actions", []),
            check_description=item.get("check_description", ""),
            check_type=item.get("check_type", "visual"),
            check_command=item.get("check_command", ""),
            max_steps=item.get("max_steps", 0),
            tags=item.get("tags", []),
        )
        tasks.append(task)

    # 按标签筛选
    if config.scenario_filter_tags:
        tag_set = set(config.scenario_filter_tags)
        tasks = [t for t in tasks if tag_set & set(t.tags)]
        emit("step0", f"标签筛选后: {len(tasks)} 个任务")

    # 限制数量
    if config.max_tasks > 0:
        tasks = tasks[: config.max_tasks]

    emit("step0_done", f"✅ {len(tasks)} 个任务就绪")
    return tasks


# ─── Review Agent ────────────────────────────────────────────────────────────

def review_mobile_trajectory(
    traj: MobileTrajectory,
    config: MobileAgentPipelineConfig,
) -> dict:
    """
    评估单条 Mobile Agent 轨迹质量

    评估维度:
      - ui_understanding: Agent 对 UI 的理解能力
      - action_accuracy: 动作坐标和类型是否准确
      - reasoning: 推理过程是否清晰
      - completeness: 任务是否完成
      - efficiency: 步骤数是否合理
    """
    api_key = config.deepseek_api_key or DEEPSEEK_API_KEY
    base_url = config.deepseek_base_url or DEEPSEEK_BASE_URL
    client = OpenAI(api_key=api_key, base_url=base_url)

    # 构建轨迹摘要
    steps_summary = []
    for step in traj.steps:
        s = step if isinstance(step, dict) else asdict(step)
        steps_summary.append({
            "step": s.get("step_id"),
            "action": s.get("action_type", ""),
            "params": s.get("action_params", {}),
            "reasoning": s.get("action_reasoning", "")[:100],
            "success": s.get("action_result", {}).get("success", False),
            "ui_elements": s.get("observation", {}).get("ui_elements_count", 0),
        })

    traj_json = json.dumps(steps_summary, ensure_ascii=False, indent=2)
    if len(traj_json) > 5000:
        traj_json = traj_json[:5000] + "\n...(截断)"

    review_prompt = f"""你是 Android GUI Agent 轨迹评估专家。评估以下 Agent 操控手机完成任务的轨迹质量。

## 任务描述
{traj.task_desc}

## 目标应用
{traj.app_package or '(系统级操作)'}

## 屏幕分辨率
{traj.screen_resolution}

## Agent 轨迹
{traj_json}

## 统计
- 总动作数: {traj.total_actions}
- 成功动作: {traj.successful_actions}
- 任务完成: {'是' if traj.task_completed else '否'}
- 完成总结: {traj.finish_summary[:200]}
- 任务验证: {json.dumps(traj.task_check_result, ensure_ascii=False)[:200]}

## 评估维度 (0-1)
1. ui_understanding: Agent 是否正确理解了屏幕元素和布局
2. action_accuracy: 点击坐标、滑动方向等是否准确
3. reasoning: 每步推理是否清晰、逻辑是否正确
4. completeness: 任务是否完整完成
5. efficiency: 操作步骤数是否合理（无冗余操作）

## 输出（严格 JSON）
{{
    "overall_score": 0.75,
    "dimensions": {{
        "ui_understanding": 0.8,
        "action_accuracy": 0.7,
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
    config: MobileAgentPipelineConfig,
) -> dict:
    """综合评估所有轨迹"""
    if not trajectories:
        return {
            "overall_score": 0,
            "dimensions": {},
            "fail_modes": ["无轨迹数据"],
            "suggestions": [],
            "reasoning": "没有生成任何轨迹",
        }

    individual_reviews = []
    for traj in trajectories:
        rev = review_mobile_trajectory(traj, config)
        traj.quality_score = rev.get("overall_score", 0)
        individual_reviews.append(rev)

    scores = [r.get("overall_score", 0) for r in individual_reviews]
    avg_score = sum(scores) / len(scores) if scores else 0

    dim_names = ["ui_understanding", "action_accuracy", "reasoning", "completeness", "efficiency"]
    avg_dims = {}
    for dim in dim_names:
        vals = [r.get("dimensions", {}).get(dim, 0) for r in individual_reviews]
        avg_dims[dim] = sum(vals) / len(vals) if vals else 0

    all_fail_modes = []
    all_suggestions = []
    for r in individual_reviews:
        all_fail_modes.extend(r.get("fail_modes", []))
        all_suggestions.extend(r.get("suggestions", []))

    return {
        "overall_score": avg_score,
        "dimensions": avg_dims,
        "fail_modes": list(set(all_fail_modes))[:5],
        "suggestions": all_suggestions[:6],
        "reasoning": f"平均评分 {avg_score:.2f}, 共 {len(trajectories)} 条轨迹",
        "individual_reviews": individual_reviews,
    }


# ─── 数据集导出 ──────────────────────────────────────────────────────────────

def export_mobile_dataset(
    trajectories: list,
    config: MobileAgentPipelineConfig,
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
            sft_turns = []
            for step in traj.steps:
                s = step if isinstance(step, dict) else asdict(step)
                # Observation turn
                sft_turns.append({
                    "role": "user",
                    "content": f"[Screenshot] UI元素数: {s.get('observation', {}).get('ui_elements_count', 0)}",
                })
                # Agent action turn
                sft_turns.append({
                    "role": "assistant",
                    "content": s.get("thought", ""),
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": "mobile_action",
                            "arguments": json.dumps({
                                "action_type": s.get("action_type", ""),
                                "params": s.get("action_params", {}),
                                "reasoning": s.get("action_reasoning", ""),
                            }, ensure_ascii=False),
                        },
                    }] if s.get("action_type") else [],
                })

            f.write(json.dumps({
                "id": traj.trajectory_id,
                "messages": [
                    {"role": "system", "content": "你是一个 Android 手机操控 AI 助手。"},
                    {"role": "user", "content": traj.task_desc},
                    *sft_turns,
                ],
                "metadata": {
                    "task_id": traj.task_id,
                    "app_package": traj.app_package,
                    "screen_resolution": traj.screen_resolution,
                    "total_actions": traj.total_actions,
                    "task_completed": traj.task_completed,
                    "score": traj.quality_score,
                    "tags": traj.tags,
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
                    f.write(json.dumps({
                        "prompt": chosen_traj.task_desc,
                        "chosen_steps": len(chosen_traj.steps),
                        "rejected_steps": len(rejected_traj.steps),
                        "chosen_score": cs,
                        "rejected_score": rs,
                        "chosen_trajectory_id": chosen_traj.trajectory_id,
                        "rejected_trajectory_id": rejected_traj.trajectory_id,
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
                "app_package": traj.app_package,
                "screen_resolution": traj.screen_resolution,
                "steps": traj.steps,
                "total_actions": traj.total_actions,
                "successful_actions": traj.successful_actions,
                "total_tokens": traj.total_tokens,
                "quality_score": traj.quality_score,
                "task_completed": traj.task_completed,
                "task_check_result": traj.task_check_result,
                "finish_summary": traj.finish_summary,
                "tags": traj.tags,
            }
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    result["raw_path"] = raw_path
    print(f"  Raw: {raw_path}")

    return result


# ─── 主流程 ──────────────────────────────────────────────────────────────────

async def run_mobile_pipeline(
    config: MobileAgentPipelineConfig,
    scenario_content: list = None,
    event_callback: Callable = None,
) -> dict:
    """
    执行完整的 Mobile Agent 轨迹合成 Pipeline

    Args:
        config: Pipeline 配置
        scenario_content: 直接传入的场景 JSON (Web UI 上传)
        event_callback: 事件回调

    Returns:
        Pipeline 执行结果摘要
    """
    def emit(t, m, d=None):
        if event_callback:
            event_callback(t, m, d)
        print(f"  [{t}] {m}")

    start = time.time()
    emit("pipeline_start", "Mobile Agent Pipeline 启动")

    runner = None

    try:
        # ════ Step 0: 场景加载 ════
        emit("step0_start", "加载场景文件")
        tasks = run_step0(
            config,
            scenario_content=scenario_content,
            event_callback=lambda t, m: emit(t, m),
        )

        if not tasks:
            emit("pipeline_error", "未找到可执行的任务")
            return {"status": "failed", "error": "No tasks found"}

        emit("step0_done", f"✅ {len(tasks)} 个任务就绪")

        # ════ Step 1: 启动 Android 沙箱 ════
        emit("step1_start", "启动 Android 沙箱")
        step1_result = await run_step1(
            config=config,
            event_callback=lambda t, m: emit(t, m),
        )

        runner = step1_result["runner"]
        emit("step1_done",
             f"✅ Android 沙箱就绪 ({step1_result['backend']}): "
             f"{step1_result['screen_width']}x{step1_result['screen_height']}")

        # ════ Step 2: Agent 轨迹生成 ════
        emit("step2_start", "Agent 轨迹生成")
        trajectories = await run_step2(
            tasks=tasks,
            config=config,
            runner=runner,
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
        export_result = export_mobile_dataset(
            trajectories, config, individual_reviews,
        )
        emit("export_done", f"✅ 已导出到 {config.output_dir}")

        elapsed = time.time() - start

        summary = {
            "status": "completed",
            "elapsed_seconds": round(elapsed, 1),
            "tasks_count": len(tasks),
            "trajectories_count": len(trajectories),
            "total_actions": sum(t.total_actions for t in trajectories),
            "successful_actions": sum(t.successful_actions for t in trajectories),
            "tasks_completed": sum(1 for t in trajectories if t.task_completed),
            "total_tokens": sum(t.total_tokens for t in trajectories),
            "avg_quality": round(avg_score, 3),
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
        # 清理沙箱
        if runner:
            try:
                await runner.stop(emit=lambda t, m: emit(t, m))
            except Exception:
                pass
