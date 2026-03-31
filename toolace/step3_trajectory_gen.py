"""
Step 3: 轨迹生成 (Non-Autoregressive Trajectory Generation)

基于 ToolACE-MT 论文的三阶段方法：
  3.1 Initialization — 粗粒度初始化，生成对话骨架
  3.2 Iterative Refinement — 迭代精炼（mask-and-fill）
  3.3 Offline Verification — 离线验证

执行策略：
  - 将 tools + task 封装为 TrajectoryJob 对象
  - 在线程池中并发处理
  - 按轮次生成 role（user → assistant → tool → assistant）
  - 每完成一个子 task 则从队列中删除
"""

import json
import time
import uuid
import concurrent.futures
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from .config import ToolACEPipelineConfig, LLMConfig
from .llm_utils import call_llm, call_llm_json, parse_json_from_text
from .prompts import (
    TRAJECTORY_INIT_PROMPT,
    TRAJECTORY_REFINE_PROMPT,
    TRAJECTORY_VERIFY_PROMPT,
)


# ─── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass
class TrajectoryTurn:
    """轨迹中的单个轮次"""
    turn_id: int
    role: str               # user | assistant | tool
    content: str = ""
    thought: str = ""
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class TrajectoryJob:
    """轨迹生成任务"""
    job_id: str
    task: dict              # Step 2 生成的任务
    tools: list             # 对应的工具定义
    tools_fc: list          # function calling 格式的工具
    group_label: str = ""
    status: str = "pending"  # pending | generating | refining | verifying | done | failed


@dataclass
class GeneratedTrajectory:
    """生成的完整轨迹"""
    trajectory_id: str
    task_id: str
    task_description: str
    task_type: str          # standard | cross_group | missing_param
    turns: list = field(default_factory=list)
    tools_used: list = field(default_factory=list)
    total_turns: int = 0
    is_multi_turn: bool = False
    quality_score: float = 0.0
    verification: dict = field(default_factory=dict)
    total_tokens: int = 0
    metadata: dict = field(default_factory=dict)


# ─── 3.1: 粗粒度初始化 ──────────────────────────────────────────────────────

def generate_skeleton(
    job: TrajectoryJob,
    config: LLMConfig,
) -> tuple:
    """
    生成对话骨架

    Returns: (skeleton_dict, tokens)
    """
    task = job.task
    tools_summary = json.dumps(
        [{"name": t.get("name"), "description": t.get("description"),
          "parameters": t.get("parameters")}
         for t in job.tools[:8]],  # 最多 8 个工具
        ensure_ascii=False, indent=2,
    )
    if len(tools_summary) > 4000:
        tools_summary = tools_summary[:4000] + "\n..."

    # 处理缺参任务的特殊 section
    missing_param_section = ""
    if task.get("type") == "missing_param" and task.get("missing_params"):
        missing_param_section = f"""## 缺失参数信息
此任务缺少以下必要参数，assistant 需要先向 user 追问：
{json.dumps(task['missing_params'], ensure_ascii=False, indent=2)}

请在对话中体现 assistant 追问 → user 补充参数 → assistant 执行工具 的流程。"""

    prompt = TRAJECTORY_INIT_PROMPT.format(
        task_json=json.dumps(task, ensure_ascii=False, indent=2),
        tools_json=tools_summary,
        missing_param_section=missing_param_section,
    )

    parsed, tokens = call_llm_json(
        [{"role": "user", "content": prompt}],
        config,
        temperature=0.7,
    )

    return parsed, tokens


# ─── 3.2: 迭代精炼 ──────────────────────────────────────────────────────────

def refine_trajectory(
    skeleton: dict,
    config: LLMConfig,
) -> tuple:
    """
    对对话骨架进行精炼（mask-and-fill）

    Returns: (refined_dict, tokens)
    """
    traj_json = json.dumps(skeleton, ensure_ascii=False, indent=2)
    if len(traj_json) > 6000:
        traj_json = traj_json[:6000] + "\n..."

    prompt = TRAJECTORY_REFINE_PROMPT.format(trajectory_json=traj_json)

    parsed, tokens = call_llm_json(
        [{"role": "user", "content": prompt}],
        config,
        temperature=0.5,
    )

    # 如果精炼失败，返回原始骨架
    if not parsed or "turns" not in parsed:
        return skeleton, tokens

    return parsed, tokens


# ─── 3.3: 离线验证 ──────────────────────────────────────────────────────────

def verify_trajectory(
    trajectory: dict,
    task: dict,
    tools: list,
    config: LLMConfig,
) -> tuple:
    """
    验证对话轨迹质量

    Returns: (verification_result, tokens)
    """
    traj_json = json.dumps(trajectory, ensure_ascii=False, indent=2)
    if len(traj_json) > 5000:
        traj_json = traj_json[:5000] + "\n..."

    task_json = json.dumps(task, ensure_ascii=False, indent=2)
    tools_json = json.dumps(
        [{"name": t.get("name"), "description": t.get("description")}
         for t in tools[:8]],
        ensure_ascii=False, indent=2,
    )

    prompt = TRAJECTORY_VERIFY_PROMPT.format(
        task_json=task_json,
        tools_json=tools_json,
        trajectory_json=traj_json,
    )

    parsed, tokens = call_llm_json(
        [{"role": "user", "content": prompt}],
        config,
        temperature=0.3,
    )

    if not parsed:
        parsed = {
            "passed": False,
            "overall_score": 0.5,
            "dimensions": {},
            "issues": ["验证失败"],
            "fix_suggestions": [],
        }

    return parsed, tokens


# ─── 单条轨迹生成完整流程 ────────────────────────────────────────────────────

def process_single_job(
    job: TrajectoryJob,
    config: ToolACEPipelineConfig,
    emit: Callable = None,
) -> GeneratedTrajectory:
    """
    处理单个轨迹生成任务（三阶段流水线）

    This function is called in a thread pool.
    """
    task = job.task
    traj_id = f"traj_{uuid.uuid4().hex[:8]}"
    total_tokens = 0

    try:
        # ── 3.1 初始化 ──
        job.status = "generating"
        if emit:
            emit("traj_init", f"  [{job.job_id}] 初始化骨架: {task.get('description', '')[:50]}...")

        skeleton, tokens = generate_skeleton(job, config.llm)
        total_tokens += tokens

        if not skeleton or "turns" not in skeleton:
            raise ValueError("骨架生成失败")

        # ── 3.2 精炼 ──
        job.status = "refining"
        if emit:
            turns_count = len(skeleton.get("turns", []))
            emit("traj_refine", f"  [{job.job_id}] 精炼轨迹 ({turns_count} 轮)...")

        refined, tokens = refine_trajectory(skeleton, config.llm)
        total_tokens += tokens

        # ── 3.3 验证 ──
        job.status = "verifying"
        if emit:
            emit("traj_verify", f"  [{job.job_id}] 离线验证...")

        verification, tokens = verify_trajectory(refined, task, job.tools, config.llm)
        total_tokens += tokens

        # ── 组装结果 ──
        turns = refined.get("turns", [])
        tools_used = refined.get("tools_used", [])

        result = GeneratedTrajectory(
            trajectory_id=traj_id,
            task_id=task.get("task_id", ""),
            task_description=task.get("description", ""),
            task_type=task.get("type", "standard"),
            turns=turns,
            tools_used=tools_used,
            total_turns=len(turns),
            is_multi_turn=len(turns) > 4,
            quality_score=verification.get("overall_score", 0),
            verification=verification,
            total_tokens=total_tokens,
            metadata={
                "group_label": job.group_label,
                "cross_groups": task.get("cross_groups", []),
                "has_missing_params": task.get("type") == "missing_param",
                "role_background": task.get("role_background", ""),
            },
        )

        job.status = "done"
        if emit:
            emit("traj_done",
                 f"  ✅ [{job.job_id}] 完成: {len(turns)} 轮, "
                 f"score={verification.get('overall_score', 0):.2f}")

        return result

    except Exception as e:
        job.status = "failed"
        if emit:
            emit("traj_failed", f"  ❌ [{job.job_id}] 失败: {e}")

        return GeneratedTrajectory(
            trajectory_id=traj_id,
            task_id=task.get("task_id", ""),
            task_description=task.get("description", ""),
            task_type=task.get("type", "standard"),
            quality_score=0,
            total_tokens=total_tokens,
            metadata={"error": str(e)},
        )


# ─── Step 3 主入口 ───────────────────────────────────────────────────────────

def run_step3(
    tasks: list,
    converted_groups: list,
    config: ToolACEPipelineConfig,
    emit: Callable = None,
) -> tuple:
    """
    执行 Step 3: 轨迹生成

    策略：
      1. 将 tasks 和对应的 tools 封装为 TrajectoryJob
      2. 在线程池中并发执行
      3. 每完成一个 job 就从待处理队列中移除

    Returns: (trajectories, total_tokens)
    """
    if emit:
        emit("step3_start", f"═══ Step 3: 轨迹生成 ═══ (任务: {len(tasks)} 个, 并发: {config.max_workers})")

    # 构建 label → group 的映射
    group_map = {}
    for g in converted_groups:
        label = g.get("group_label", "")
        group_map[label] = g

    # 封装 TrajectoryJob
    jobs = []
    for i, task in enumerate(tasks):
        # 找到任务对应的工具组
        source_group = task.get("source_group", "")
        cross_groups = task.get("cross_groups", [])

        tools = []
        tools_fc = []

        if source_group and source_group in group_map:
            g = group_map[source_group]
            tools.extend(g.get("tools_internal", g.get("tools", [])))
            tools_fc.extend(g.get("tools_fc", []))
        elif cross_groups:
            for cg_label in cross_groups:
                if cg_label in group_map:
                    g = group_map[cg_label]
                    tools.extend(g.get("tools_internal", g.get("tools", [])))
                    tools_fc.extend(g.get("tools_fc", []))
        else:
            # 回退：使用所有工具
            for g in converted_groups:
                tools.extend(g.get("tools_internal", g.get("tools", []))[:3])
                tools_fc.extend(g.get("tools_fc", [])[:3])

        job = TrajectoryJob(
            job_id=f"job_{i:04d}",
            task=task,
            tools=tools,
            tools_fc=tools_fc,
            group_label=source_group or (cross_groups[0] if cross_groups else ""),
        )
        jobs.append(job)

    # 并发执行
    trajectories = []
    total_tokens = 0
    completed = 0

    if emit:
        emit("step3_pool_start", f"  启动线程池: {len(jobs)} 个任务...")

    # 使用 ThreadPoolExecutor 并发处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        future_to_job = {
            executor.submit(process_single_job, job, config, emit): job
            for job in jobs
        }

        for future in concurrent.futures.as_completed(future_to_job):
            job = future_to_job[future]
            try:
                result = future.result()
                trajectories.append(result)
                total_tokens += result.total_tokens
                completed += 1

                if emit:
                    emit("step3_progress",
                         f"  进度: {completed}/{len(jobs)} "
                         f"(成功: {sum(1 for t in trajectories if t.quality_score > 0)})")

            except Exception as e:
                completed += 1
                if emit:
                    emit("step3_job_error", f"  ❌ {job.job_id} 异常: {e}")

    # 统计
    successful = [t for t in trajectories if t.quality_score > 0]
    avg_score = (sum(t.quality_score for t in successful) / len(successful)) if successful else 0
    multi_turn = sum(1 for t in trajectories if t.is_multi_turn)

    if emit:
        emit("step3_complete",
             f"═══ Step 3 完成: {len(trajectories)} 条轨迹 "
             f"(成功: {len(successful)}, 多轮: {multi_turn}, "
             f"平均分: {avg_score:.2f}), {total_tokens:,} tokens ═══")

    return trajectories, total_tokens
