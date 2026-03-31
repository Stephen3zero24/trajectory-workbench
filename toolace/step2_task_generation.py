"""
Step 2: 任务生成 (Self-Guided Dialog Generation)

基于 ToolACE 论文中的 SDG 模块，增加了你提出的改进：
  2.1 格式转换 — 将 Step 1 的工具组转换为标准 function calling 格式
  2.2 使用流程生成 — 基于工具链生成使用流程
  2.3 任务生成 — 包含三种模式：
      a) 标准任务（单工具组内）
      b) 跨组交叉任务（相似 label 的工具组两两组合）
      c) 缺参任务（缺少参数，需要追问）

改进点：
  - 跨组交叉：按 label 分组 → 同 label 两两组合 → 排除名称过于相似的
  - 角色背景：可选加入用户角色设定
  - 缺参任务：生成缺少必要参数的任务
"""

import json
import random
import time
from itertools import combinations
from typing import Callable, Optional

from .config import ToolACEPipelineConfig, LLMConfig
from .llm_utils import call_llm, call_llm_json, parse_json_array_from_text, parse_json_from_text
from .prompts import (
    USAGE_FLOW_PROMPT,
    TASK_GENERATION_PROMPT,
    CROSS_GROUP_TASK_PROMPT,
    MISSING_PARAM_TASK_PROMPT,
    ROLE_BACKGROUNDS,
)


# ─── 2.1: 格式转换 ──────────────────────────────────────────────────────────

def convert_to_function_calling_format(tool: dict) -> dict:
    """将内部工具格式转换为标准 OpenAI function calling 格式"""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def convert_tools_group(group: dict) -> dict:
    """转换整个工具组"""
    tools = group.get("tools", [])
    return {
        "group_label": group.get("group_label", ""),
        "tools_internal": tools,
        "tools_fc": [convert_to_function_calling_format(t) for t in tools],
        "tool_chains": group.get("tool_chains", []),
    }


# ─── 2.2: 使用流程生成 ──────────────────────────────────────────────────────

def generate_usage_flows(
    group: dict,
    count: int,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    为一个工具组生成使用流程

    Returns: (flows, tokens)
    """
    tools_json = json.dumps(
        [{"name": t.get("name"), "description": t.get("description"), "parameters": t.get("parameters")}
         for t in group.get("tools_internal", group.get("tools", []))],
        ensure_ascii=False, indent=2,
    )
    if len(tools_json) > 4000:
        tools_json = tools_json[:4000] + "\n..."

    chains_json = json.dumps(group.get("tool_chains", []), ensure_ascii=False, indent=2)

    prompt = USAGE_FLOW_PROMPT.format(
        tools_json=tools_json,
        tool_chains_json=chains_json,
        count=count,
    )

    result = call_llm([{"role": "user", "content": prompt}], config, temperature=0.7)
    flows = parse_json_array_from_text(result["content"])

    if emit:
        emit("flows_generated", f"  ✅ 生成 {len(flows)} 个使用流程 ({group.get('group_label', '')})")

    return flows, result["tokens"]


# ─── 2.3a: 标准任务生成 ─────────────────────────────────────────────────────

def _make_role_section(enable_role: bool) -> str:
    """生成角色背景 section"""
    if not enable_role:
        return ""
    role = random.choice(ROLE_BACKGROUNDS)
    return f"## 角色背景\n{role}\n\n请在任务描述中融入此角色的视角和需求。"


def generate_standard_tasks(
    group: dict,
    flows: list,
    count: int,
    enable_role: bool,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    基于使用流程生成标准任务

    Returns: (tasks, tokens)
    """
    tasks = []
    total_tokens = 0

    # 简化工具列表用于 prompt
    tools_summary = json.dumps(
        [{"name": t.get("name"), "description": t.get("description")}
         for t in group.get("tools_internal", group.get("tools", []))],
        ensure_ascii=False, indent=2,
    )

    for flow in flows[:count]:
        role_section = _make_role_section(enable_role)

        prompt = TASK_GENERATION_PROMPT.format(
            tools_json=tools_summary,
            flow_json=json.dumps(flow, ensure_ascii=False, indent=2),
            role_background_section=role_section,
        )

        parsed, tokens = call_llm_json(
            [{"role": "user", "content": prompt}],
            config,
            temperature=0.7,
        )
        total_tokens += tokens

        if parsed and "description" in parsed:
            parsed["type"] = "standard"
            parsed["source_group"] = group.get("group_label", "")
            tasks.append(parsed)

    if emit:
        emit("standard_tasks_done",
             f"  ✅ 生成 {len(tasks)} 个标准任务 ({group.get('group_label', '')})")

    return tasks, total_tokens


# ─── 2.3b: 跨组交叉任务生成 ─────────────────────────────────────────────────

def _is_similar_label(label_a: str, label_b: str) -> bool:
    """判断两个 label 是否过于相似（排除同名/近似名称的组）"""
    a = label_a.strip().lower()
    b = label_b.strip().lower()
    if a == b:
        return True

    # 去除常见后缀后比较
    suffixes = ["服务", "工具", "模块", " service", " services", " tool", " tools",
                "server", "servers", " reference"]
    a_clean = a
    b_clean = b
    for s in suffixes:
        a_clean = a_clean.replace(s, "").strip()
        b_clean = b_clean.replace(s, "").strip()
    if a_clean == b_clean:
        return True

    # 编辑距离过小也排除
    if len(a) > 3 and len(b) > 3:
        common = sum(1 for ca, cb in zip(a, b) if ca == cb)
        if common / max(len(a), len(b)) > 0.8:
            return True

    return False


def generate_cross_group_tasks(
    groups: list,
    count_per_pair: int,
    enable_role: bool,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    跨工具组交叉生成任务

    策略：按 label 分组 → 同 label 或相关 label 的组两两组合 → 排除过于相似的

    Returns: (cross_tasks, tokens)
    """
    if len(groups) < 2:
        if emit:
            emit("cross_skip", "  ⏭ 工具组不足 2 个，跳过跨组任务")
        return [], 0

    if emit:
        emit("cross_group_start", f"  跨组交叉任务生成: {len(groups)} 个工具组...")

    cross_tasks = []
    total_tokens = 0

    # 生成所有有效的两两组合
    valid_pairs = []
    for ga, gb in combinations(groups, 2):
        label_a = ga.get("group_label", "")
        label_b = gb.get("group_label", "")
        if not _is_similar_label(label_a, label_b):
            valid_pairs.append((ga, gb))

    if not valid_pairs:
        if emit:
            emit("cross_no_valid_pairs", "  ⚠ 没有找到可组合的工具组对")
        return [], 0

    if emit:
        emit("cross_pairs_found", f"  找到 {len(valid_pairs)} 个有效组合对")

    for ga, gb in valid_pairs:
        label_a = ga.get("group_label", "")
        label_b = gb.get("group_label", "")

        # 简化工具列表
        tools_a = json.dumps(
            [{"name": t.get("name"), "description": t.get("description")}
             for t in (ga.get("tools_internal", ga.get("tools", [])))[:5]],
            ensure_ascii=False, indent=2,
        )
        tools_b = json.dumps(
            [{"name": t.get("name"), "description": t.get("description")}
             for t in (gb.get("tools_internal", gb.get("tools", [])))[:5]],
            ensure_ascii=False, indent=2,
        )

        role_section = _make_role_section(enable_role)

        prompt = CROSS_GROUP_TASK_PROMPT.format(
            label_a=label_a,
            tools_a_json=tools_a,
            label_b=label_b,
            tools_b_json=tools_b,
            role_background_section=role_section,
            count=count_per_pair,
        )

        result = call_llm([{"role": "user", "content": prompt}], config, temperature=0.8)
        total_tokens += result["tokens"]

        new_tasks = parse_json_array_from_text(result["content"])
        for task in new_tasks:
            task["type"] = "cross_group"
        cross_tasks.extend(new_tasks)

        if emit:
            emit("cross_pair_done",
                 f"  ✅ [{label_a}] × [{label_b}] → {len(new_tasks)} 个跨组任务")

    return cross_tasks, total_tokens


# ─── 2.3c: 缺参任务生成 ─────────────────────────────────────────────────────

def generate_missing_param_tasks(
    group: dict,
    count: int,
    enable_role: bool,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    生成缺少参数的任务

    Returns: (mp_tasks, tokens)
    """
    tools_summary = json.dumps(
        [{"name": t.get("name"), "description": t.get("description"),
          "parameters": t.get("parameters")}
         for t in group.get("tools_internal", group.get("tools", []))],
        ensure_ascii=False, indent=2,
    )
    if len(tools_summary) > 4000:
        tools_summary = tools_summary[:4000] + "\n..."

    role_section = _make_role_section(enable_role)

    prompt = MISSING_PARAM_TASK_PROMPT.format(
        tools_json=tools_summary,
        role_background_section=role_section,
        count=count,
    )

    result = call_llm([{"role": "user", "content": prompt}], config, temperature=0.7)
    mp_tasks = parse_json_array_from_text(result["content"])

    for task in mp_tasks:
        task["type"] = "missing_param"
        task["source_group"] = group.get("group_label", "")

    if emit:
        emit("missing_param_done",
             f"  ✅ 生成 {len(mp_tasks)} 个缺参任务 ({group.get('group_label', '')})")

    return mp_tasks, result["tokens"]


# ─── Step 2 主入口 ───────────────────────────────────────────────────────────

def run_step2(
    coupled_groups: list,
    config: ToolACEPipelineConfig,
    emit: Callable = None,
) -> tuple:
    """
    执行 Step 2: 任务生成

    Returns: (all_tasks, converted_groups, total_tokens)
        all_tasks: 所有生成的任务列表
        converted_groups: 格式转换后的工具组
    """
    if emit:
        emit("step2_start", f"═══ Step 2: 任务生成 ═══ (工具组: {len(coupled_groups)} 个)")

    total_tokens = 0
    all_tasks = []

    # 2.1 格式转换
    if emit:
        emit("step2_convert", "Step 2.1: 格式转换...")
    converted_groups = [convert_tools_group(g) for g in coupled_groups]

    # 2.2 使用流程生成
    if emit:
        emit("step2_flows", "Step 2.2: 生成使用流程...")

    all_flows = {}
    for group in converted_groups:
        label = group.get("group_label", "")
        flows_per_group = max(2, config.task_count // len(converted_groups))
        flows, tokens = generate_usage_flows(group, flows_per_group, config.llm, emit)
        all_flows[label] = flows
        total_tokens += tokens

    # 2.3a 标准任务
    if emit:
        emit("step2_standard", "Step 2.3a: 生成标准任务...")

    tasks_per_group = max(1, config.task_count // len(converted_groups))
    for group in converted_groups:
        label = group.get("group_label", "")
        flows = all_flows.get(label, [])
        if flows:
            tasks, tokens = generate_standard_tasks(
                group, flows, tasks_per_group,
                config.enable_role_background, config.llm, emit,
            )
            all_tasks.extend(tasks)
            total_tokens += tokens

    # 2.3b 跨组交叉任务
    if config.enable_cross_group and len(converted_groups) >= 2:
        if emit:
            emit("step2_cross", "Step 2.3b: 生成跨组交叉任务...")

        cross_count = max(1, int(config.task_count * 0.3))
        count_per_pair = max(1, cross_count // max(1, len(converted_groups) - 1))

        cross_tasks, tokens = generate_cross_group_tasks(
            converted_groups, count_per_pair,
            config.enable_role_background, config.llm, emit,
        )
        all_tasks.extend(cross_tasks)
        total_tokens += tokens

    # 2.3c 缺参任务
    if config.missing_param_ratio > 0:
        if emit:
            emit("step2_missing", "Step 2.3c: 生成缺参任务...")

        mp_count = max(1, int(config.task_count * config.missing_param_ratio))
        mp_per_group = max(1, mp_count // len(converted_groups))

        for group in converted_groups:
            mp_tasks, tokens = generate_missing_param_tasks(
                group, mp_per_group,
                config.enable_role_background, config.llm, emit,
            )
            all_tasks.extend(mp_tasks)
            total_tokens += tokens

    # 为每个任务分配唯一 ID（如果没有的话）
    for i, task in enumerate(all_tasks):
        if "task_id" not in task or not task["task_id"]:
            task["task_id"] = f"task_{i:04d}"

    if emit:
        standard_count = sum(1 for t in all_tasks if t.get("type") == "standard")
        cross_count = sum(1 for t in all_tasks if t.get("type") == "cross_group")
        mp_count = sum(1 for t in all_tasks if t.get("type") == "missing_param")
        emit("step2_complete",
             f"═══ Step 2 完成: {len(all_tasks)} 个任务 "
             f"(标准:{standard_count} 跨组:{cross_count} 缺参:{mp_count}), "
             f"{total_tokens:,} tokens ═══")

    return all_tasks, converted_groups, total_tokens
