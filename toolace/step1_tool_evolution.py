"""
Step 1: 工具自进化合成 (Tool Self-Evolution Synthesis)

基于 ToolACE 论文中的 TSS 模块：
  1.1 Refinement — 完善源工具定义
  1.2 Expansion  — 扩展生成同领域新工具（Speciation + Adaptation）
  1.3 Coupling   — 优化工具组之间的耦合关系（Evolution）

输入：源 tools 列表
输出：多个耦合优化后的工具组
"""

import json
import time
from typing import Callable, Optional

from .config import ToolACEPipelineConfig, LLMConfig
from .llm_utils import call_llm_json, parse_json_from_text, parse_json_array_from_text, call_llm
from .prompts import (
    TOOL_REFINEMENT_PROMPT,
    TOOL_EXPANSION_PROMPT,
    TOOL_COUPLING_PROMPT,
)


# ─── Step 1.1: 工具完善 ──────────────────────────────────────────────────────

def refine_tool(
    tool: dict,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    完善单个工具定义

    Returns: (refined_tool, tokens)
    """
    prompt = TOOL_REFINEMENT_PROMPT.format(
        tool_json=json.dumps(tool, ensure_ascii=False, indent=2)
    )
    messages = [{"role": "user", "content": prompt}]

    parsed, tokens = call_llm_json(messages, config, temperature=0.5)

    if parsed and "name" in parsed:
        # 保留原始 label
        if "label" not in parsed and "label" in tool:
            parsed["label"] = tool["label"]
        if emit:
            emit("tool_refined", f"  ✅ 完善工具: {parsed.get('name', tool.get('name', ''))}")
        return parsed, tokens

    # 失败回退：返回原始工具
    if emit:
        emit("tool_refine_fallback", f"  ⚠ 工具完善失败，保留原始: {tool.get('name', '')}")
    return tool, tokens


def refine_all_tools(
    tools: list,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    批量完善所有源工具

    Returns: (refined_tools, total_tokens)
    """
    if emit:
        emit("step1_refine_start", f"Step 1.1: 完善 {len(tools)} 个源工具...")

    refined = []
    total_tokens = 0

    for i, tool in enumerate(tools):
        if emit:
            emit("tool_refining", f"  [{i+1}/{len(tools)}] 完善 {tool.get('name', 'unknown')}...")
        refined_tool, tokens = refine_tool(tool, config, emit)
        refined.append(refined_tool)
        total_tokens += tokens

    if emit:
        emit("step1_refine_done", f"Step 1.1 完成: {len(refined)} 个工具已完善, tokens={total_tokens}")

    return refined, total_tokens


# ─── Step 1.2: 工具扩展 ──────────────────────────────────────────────────────

def expand_tool(
    source_tool: dict,
    count: int,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    从源工具扩展生成新工具

    Returns: (new_tools, tokens)
    """
    prompt = TOOL_EXPANSION_PROMPT.format(
        source_tool_json=json.dumps(source_tool, ensure_ascii=False, indent=2),
        count=count,
    )
    messages = [{"role": "user", "content": prompt}]

    result = call_llm(messages, config, temperature=0.8)
    tokens = result["tokens"]
    content = result["content"]

    new_tools = parse_json_array_from_text(content)

    if not new_tools:
        # 尝试从 dict 中提取
        parsed = parse_json_from_text(content)
        if "items" in parsed:
            new_tools = parsed["items"]
        elif isinstance(parsed, dict) and "tools" in parsed:
            new_tools = parsed["tools"]

    # 确保 label 一致
    for tool in new_tools:
        if "label" not in tool and "label" in source_tool:
            tool["label"] = source_tool["label"]

    if emit:
        emit("tool_expanded", f"  ✅ 从 {source_tool.get('name', '')} 扩展出 {len(new_tools)} 个新工具")

    return new_tools, tokens


def expand_all_tools(
    tools: list,
    expansion_count: int,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    批量扩展所有工具

    Returns: (tool_groups, total_tokens)
        tool_groups: {label: [tools]} 按 label 分组
    """
    if emit:
        emit("step1_expand_start", f"Step 1.2: 扩展工具 (每个×{expansion_count})...")

    tool_groups = {}
    total_tokens = 0

    for i, tool in enumerate(tools):
        label = tool.get("label", "default")

        if emit:
            emit("tool_expanding", f"  [{i+1}/{len(tools)}] 扩展 {tool.get('name', '')} ({label})...")

        new_tools, tokens = expand_tool(tool, expansion_count, config, emit)
        total_tokens += tokens

        if label not in tool_groups:
            tool_groups[label] = [tool]  # 源工具也放入组中
        else:
            tool_groups[label].append(tool)

        tool_groups[label].extend(new_tools)

    if emit:
        group_summary = ", ".join(f"{k}: {len(v)}个" for k, v in tool_groups.items())
        emit("step1_expand_done", f"Step 1.2 完成: {group_summary}")

    return tool_groups, total_tokens


# ─── Step 1.3: 工具组耦合优化 ────────────────────────────────────────────────

def couple_tool_group(
    label: str,
    tools: list,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    优化单个工具组的耦合关系

    Returns: (coupled_group, tokens)
        coupled_group: {"group_label": ..., "tools": [...], "tool_chains": [...]}
    """
    # 截断过长的工具列表
    tools_json = json.dumps(tools, ensure_ascii=False, indent=2)
    if len(tools_json) > 6000:
        tools_json = tools_json[:6000] + "\n...(截断)"

    prompt = TOOL_COUPLING_PROMPT.format(tools_group_json=tools_json)
    messages = [{"role": "user", "content": prompt}]

    parsed, tokens = call_llm_json(messages, config, temperature=0.5)

    if parsed and "tools" in parsed:
        if emit:
            chains = parsed.get("tool_chains", [])
            emit("group_coupled", f"  ✅ 工具组 [{label}] 耦合完成: "
                                  f"{len(parsed['tools'])} 工具, {len(chains)} 条调用链")
        return parsed, tokens

    # 回退：构建基础结构
    fallback = {
        "group_label": label,
        "tools": tools,
        "tool_chains": [],
    }
    if emit:
        emit("group_couple_fallback", f"  ⚠ 工具组 [{label}] 耦合失败，使用原始分组")
    return fallback, tokens


def couple_all_groups(
    tool_groups: dict,
    coupling_rounds: int,
    config: LLMConfig,
    emit: Callable = None,
) -> tuple:
    """
    对所有工具组执行耦合优化

    Returns: (coupled_groups, total_tokens)
        coupled_groups: [{"group_label": ..., "tools": [...], "tool_chains": [...]}]
    """
    if emit:
        emit("step1_couple_start", f"Step 1.3: 耦合优化 ({coupling_rounds} 轮)...")

    coupled_groups = []
    total_tokens = 0

    for label, tools in tool_groups.items():
        current_tools = tools
        for round_idx in range(coupling_rounds):
            if emit and coupling_rounds > 1:
                emit("coupling_round", f"  [{label}] 第 {round_idx + 1}/{coupling_rounds} 轮耦合...")

            result, tokens = couple_tool_group(label, current_tools, config, emit)
            total_tokens += tokens
            current_tools = result.get("tools", current_tools)

        # 最终结果
        final_group = result if isinstance(result, dict) and "tools" in result else {
            "group_label": label,
            "tools": current_tools,
            "tool_chains": [],
        }
        coupled_groups.append(final_group)

    if emit:
        total_tools = sum(len(g.get("tools", [])) for g in coupled_groups)
        total_chains = sum(len(g.get("tool_chains", [])) for g in coupled_groups)
        emit("step1_couple_done",
             f"Step 1.3 完成: {len(coupled_groups)} 组, {total_tools} 工具, {total_chains} 调用链")

    return coupled_groups, total_tokens


# ─── Step 1 主入口 ───────────────────────────────────────────────────────────

def run_step1(
    config: ToolACEPipelineConfig,
    emit: Callable = None,
) -> tuple:
    """
    执行 Step 1: 工具自进化合成

    Returns: (coupled_groups, total_tokens)
    """
    if emit:
        emit("step1_start", f"═══ Step 1: 工具自进化合成 ═══ (源工具: {len(config.source_tools)} 个)")

    total_tokens = 0

    # 1.1 完善源工具
    refined_tools, tokens = refine_all_tools(config.source_tools, config.llm, emit)
    total_tokens += tokens

    # 1.2 扩展工具
    tool_groups, tokens = expand_all_tools(refined_tools, config.expansion_count, config.llm, emit)
    total_tokens += tokens

    # 1.3 耦合优化
    coupled_groups, tokens = couple_all_groups(tool_groups, config.coupling_rounds, config.llm, emit)
    total_tokens += tokens

    if emit:
        emit("step1_complete", f"═══ Step 1 完成: {total_tokens:,} tokens ═══")

    return coupled_groups, total_tokens
