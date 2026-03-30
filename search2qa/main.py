"""
Search2QA Main — 三阶段 QA 合成主流程

在沙箱内运行，完成：
  Stage 1: 初始化 QA 生成（question/answer 模式）
  Stage 2: 迭代复杂化（Query Evolution）
  Stage 3: 轨迹改写（造题轨迹 → 答题轨迹）

使用方式：
  python3 main.py --seed "种子词" --mode question --iterations 2

输出：
  output/trace/{seed}_{timestamp}/
    ├── trace_init.json          # Stage 1 简化轨迹
    ├── tool_results_init.json   # Stage 1 完整工具结果
    ├── trace_evolve_1.json      # Stage 2 第1轮轨迹
    ├── trace_evolve_2.json      # Stage 2 第2轮轨迹
    ├── trace_rewrite.json       # Stage 3 改写后的轨迹
    └── final_output.json        # 最终汇总输出
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime

from trace_manager import TraceManager, create_run_folder
from llm_engine import llm_with_tools, call_deepseek_with_tools, extract_qa_from_text
from prompts import (
    QUESTION_MODE_SYSTEM_PROMPT,
    QUESTION_MODE_USER_PROMPT,
    ANSWER_MODE_SYSTEM_PROMPT,
    ANSWER_MODE_USER_PROMPT,
    QUERY_EVOLUTION_SYSTEM_PROMPT,
    QUERY_EVOLUTION_USER_PROMPT,
    TRACE_REWRITE_SYSTEM_PROMPT,
    TRACE_REWRITE_USER_PROMPT,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1: 初始化 QA 生成
# ═══════════════════════════════════════════════════════════════════════════════

async def stage_init_qa(
    seed: str,
    mode: str,
    run_folder: str,
    model: str = "deepseek-chat",
    temperature: float = 0.7,
    max_turns: int = 20,
) -> dict:
    """
    Stage 1: 从 seed 出发生成初始 QA 对

    Args:
        seed: 种子词（question 模式）或已知答案（answer 模式）
        mode: "question" 或 "answer"
        run_folder: 输出文件夹
        model: LLM 模型
        temperature: 温度
        max_turns: 最大交互轮次

    Returns:
        {
            "question": str,
            "answer": str,
            "reasoning": str,
            "sources": list,
            "trace_file": str,
            "tool_results_file": str,
            "total_tokens": int,
        }
    """
    print(f"\n{'='*60}")
    print(f"Stage 1: 初始化 QA 生成 (mode={mode}, seed={seed})")
    print(f"{'='*60}")

    trace_manager = TraceManager(run_folder)
    trace_manager.add_stage_marker("init", f"mode={mode}, seed={seed}")

    # 构建初始消息
    if mode == "question":
        system_prompt = QUESTION_MODE_SYSTEM_PROMPT
        user_prompt = QUESTION_MODE_USER_PROMPT.format(seed=seed)
    elif mode == "answer":
        system_prompt = ANSWER_MODE_SYSTEM_PROMPT
        user_prompt = ANSWER_MODE_USER_PROMPT.format(seed=seed)
    else:
        raise ValueError(f"Unknown mode: {mode}, expected 'question' or 'answer'")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # 执行多轮交互
    result = await llm_with_tools(
        messages=messages,
        trace_manager=trace_manager,
        model=model,
        temperature=temperature,
        max_iterations=max_turns,
        log_prefix="[Init]",
    )

    # 提取 QA
    qa = result.get("qa")
    if not qa:
        print("  ⚠ 未能从 LLM 输出中提取 QA，使用原始输出")
        qa = {
            "question": f"[未提取到问题] seed={seed}",
            "answer": f"[未提取到答案]",
            "reasoning": result.get("content", "")[:500],
            "sources": [],
        }

    # 保存轨迹
    trace_file, results_file = trace_manager.save(
        question=qa["question"],
        answer=qa["answer"],
        iterations=result.get("iterations", 0),
        stage="init",
    )

    summary = trace_manager.get_trace_summary()
    print(f"\n  📊 Stage 1 完成:")
    print(f"     Q: {qa['question'][:80]}...")
    print(f"     A: {qa['answer'][:80]}")
    print(f"     搜索次数: {summary['search_calls']}, 爬取次数: {summary['crawl_calls']}")
    print(f"     Token 消耗: {result['total_tokens']:,}")

    return {
        **qa,
        "trace_file": trace_file,
        "tool_results_file": results_file,
        "total_tokens": result["total_tokens"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2: 迭代复杂化
# ═══════════════════════════════════════════════════════════════════════════════

async def stage_evolve_qa(
    question: str,
    answer: str,
    run_folder: str,
    max_evolutions: int = 2,
    model: str = "deepseek-chat",
    temperature: float = 0.7,
    max_turns_per_round: int = 15,
) -> dict:
    """
    Stage 2: 迭代复杂化问题（Query Evolution）

    Args:
        question: 初始问题
        answer: 答案（不变）
        run_folder: 输出文件夹
        max_evolutions: 复杂化迭代次数
        model: LLM 模型
        temperature: 温度

    Returns:
        {
            "original_question": str,
            "evolved_question": str,
            "answer": str,
            "evolution_history": list,
            "total_tokens": int,
        }
    """
    print(f"\n{'='*60}")
    print(f"Stage 2: 迭代复杂化 (共 {max_evolutions} 轮)")
    print(f"{'='*60}")

    current_question = question
    evolution_history = []
    total_tokens = 0

    for evo_round in range(1, max_evolutions + 1):
        print(f"\n  --- 第 {evo_round}/{max_evolutions} 轮复杂化 ---")

        trace_manager = TraceManager(run_folder)
        trace_manager.add_stage_marker("evolve", f"round={evo_round}")

        messages = [
            {"role": "system", "content": QUERY_EVOLUTION_SYSTEM_PROMPT},
            {"role": "user", "content": QUERY_EVOLUTION_USER_PROMPT.format(
                question=current_question,
                answer=answer,
            )},
        ]

        result = await llm_with_tools(
            messages=messages,
            trace_manager=trace_manager,
            model=model,
            temperature=temperature,
            max_iterations=max_turns_per_round,
            log_prefix=f"[Evolve-{evo_round}]",
        )
        total_tokens += result["total_tokens"]

        # 提取演化后的 QA
        qa = result.get("qa")
        if qa:
            # 优先使用 evolved_question 字段
            evolved_q = qa.get("evolved_question") or qa.get("question", current_question)
            evolution_history.append({
                "round": evo_round,
                "before": current_question,
                "after": evolved_q,
                "strategy": qa.get("evolution_strategy", ""),
            })
            current_question = evolved_q
            print(f"  ✅ 问题已演化: {current_question[:80]}...")
        else:
            print(f"  ⚠ 第 {evo_round} 轮未提取到演化结果，保持原问题")

        # 保存本轮轨迹
        trace_manager.save(
            question=current_question,
            answer=answer,
            iterations=result.get("iterations", 0),
            stage=f"evolve_{evo_round}",
        )

    print(f"\n  📊 Stage 2 完成:")
    print(f"     原始问题: {question[:60]}...")
    print(f"     最终问题: {current_question[:60]}...")
    print(f"     演化轮次: {len(evolution_history)}")

    return {
        "original_question": question,
        "evolved_question": current_question,
        "answer": answer,
        "evolution_history": evolution_history,
        "total_tokens": total_tokens,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3: 轨迹改写
# ═══════════════════════════════════════════════════════════════════════════════

async def stage_rewrite_trace(
    question: str,
    answer: str,
    run_folder: str,
    model: str = "deepseek-chat",
    temperature: float = 0.3,
) -> dict:
    """
    Stage 3: 将造题轨迹改写为答题轨迹

    收集所有阶段的轨迹，让 LLM 改写为合理的搜索回答轨迹，
    然后用实际的工具结果替换 PLACEHOLDER。

    Returns:
        {
            "question": str,
            "answer": str,
            "rewritten_trace": list,
            "trace_file": str,
        }
    """
    print(f"\n{'='*60}")
    print(f"Stage 3: 轨迹改写")
    print(f"{'='*60}")

    # 1. 收集所有阶段的轨迹
    all_traces = []
    all_tool_results = {}

    for fname in sorted(os.listdir(run_folder)):
        if fname.startswith("trace_") and fname.endswith(".json") and "rewrite" not in fname:
            with open(os.path.join(run_folder, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
                all_traces.extend(data.get("trace", []))

        if fname.startswith("tool_results_") and fname.endswith(".json"):
            with open(os.path.join(run_folder, fname), "r", encoding="utf-8") as f:
                results = json.load(f)
                all_tool_results.update(results)

    # 简化轨迹用于发给 LLM（避免上下文过长）
    simplified_trace = []
    for entry in all_traces:
        if entry["type"] == "tool_call":
            simplified_trace.append({
                "type": "tool_call",
                "tool_name": entry["tool_name"],
                "tool_args": entry["tool_args"],
                "tool_call_id": entry.get("tool_call_id", ""),
            })
        elif entry["type"] == "tool_result":
            simplified_trace.append({
                "type": "tool_result",
                "content": entry["content"][:200],  # 进一步截断
                "tool_call_id": entry.get("tool_call_id", ""),
            })
        elif entry["type"] == "llm_output":
            simplified_trace.append({
                "type": "thought",
                "content": entry["content"][:300],
            })

    trace_json = json.dumps(simplified_trace, ensure_ascii=False, indent=2)
    # 截断避免上下文过长
    if len(trace_json) > 8000:
        trace_json = trace_json[:8000] + "\n...(截断)"

    # 2. 调用 LLM 改写
    print("  调用 LLM 改写轨迹...")
    messages = [
        {"role": "system", "content": TRACE_REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": TRACE_REWRITE_USER_PROMPT.format(
            question=question,
            answer=answer,
            trace_json=trace_json,
        )},
    ]

    result = call_deepseek_with_tools(
        messages=messages,
        model=model,
        temperature=temperature,
        tools=None,  # 改写阶段不需要工具
    )

    # 3. 解析改写结果
    rewritten_trace = []
    try:
        content = result["content"] or ""
        # 提取 JSON
        import re
        json_pattern = r'```json\s*(.*?)\s*```'
        matches = re.findall(json_pattern, content, re.DOTALL)
        if matches:
            data = json.loads(matches[0])
        else:
            data = json.loads(content.strip())

        rewritten_trace = data.get("rewritten_trace", [])

        # 4. 用实际工具结果替换 PLACEHOLDER
        for entry in rewritten_trace:
            if entry.get("type") == "tool_result":
                tool_call_id = entry.get("tool_call_id", "")
                if tool_call_id in all_tool_results:
                    entry["content"] = all_tool_results[tool_call_id]
                # 如果没有匹配的工具结果，尝试重新执行工具调用
                elif entry.get("content") == "PLACEHOLDER":
                    # 查找对应的 tool_call
                    for prev in rewritten_trace:
                        if (prev.get("type") == "tool_call"
                                and prev.get("tool_call_id") == tool_call_id):
                            tool_name = prev.get("tool_name", "")
                            tool_args = prev.get("tool_args", {})
                            if isinstance(tool_args, str):
                                tool_args = json.loads(tool_args)
                            print(f"  🔄 重新执行工具: {tool_name}({tool_args})")
                            from tools import execute_tool_call
                            entry["content"] = await execute_tool_call(tool_name, tool_args)
                            break

    except Exception as e:
        print(f"  ⚠ 轨迹改写解析失败: {e}")
        print(f"  原始输出: {result.get('content', '')[:200]}")

    # 5. 保存改写轨迹
    rewrite_output = {
        "question": question,
        "answer": answer,
        "rewritten_trace": rewritten_trace,
        "metadata": {
            "original_trace_entries": len(all_traces),
            "rewritten_trace_entries": len(rewritten_trace),
            "timestamp": datetime.now().isoformat(),
        },
    }

    rewrite_path = os.path.join(run_folder, "trace_rewrite.json")
    with open(rewrite_path, "w", encoding="utf-8") as f:
        json.dump(rewrite_output, f, ensure_ascii=False, indent=2)

    print(f"  ✅ 轨迹改写完成: {len(rewritten_trace)} 步")
    print(f"  📄 保存到: {rewrite_path}")

    return {
        "question": question,
        "answer": answer,
        "rewritten_trace": rewritten_trace,
        "trace_file": rewrite_path,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程编排
# ═══════════════════════════════════════════════════════════════════════════════

async def run_search2qa_pipeline(
    seed: str,
    mode: str = "question",
    max_evolutions: int = 2,
    model: str = "deepseek-chat",
    temperature: float = 0.7,
    max_turns: int = 20,
    enable_evolution: bool = True,
    enable_rewrite: bool = True,
    output_dir: str = "output/trace",
) -> dict:
    """
    完整的 Search2QA 流水线

    Args:
        seed: 种子词或已知答案
        mode: "question" 或 "answer"
        max_evolutions: 复杂化迭代次数
        model: LLM 模型
        temperature: 温度
        max_turns: 每阶段最大交互轮次
        enable_evolution: 是否启用迭代复杂化
        enable_rewrite: 是否启用轨迹改写
        output_dir: 输出目录

    Returns:
        完整的流水线结果字典
    """
    print(f"\n{'#'*60}")
    print(f"  Search2QA Pipeline")
    print(f"  Seed: {seed}")
    print(f"  Mode: {mode}")
    print(f"  Model: {model}")
    print(f"{'#'*60}")

    # 创建运行文件夹
    run_folder = create_run_folder(seed, output_dir)
    print(f"\n  📁 输出文件夹: {run_folder}")

    total_tokens = 0
    pipeline_result = {
        "seed": seed,
        "mode": mode,
        "run_folder": run_folder,
        "stages": {},
    }

    # ── Stage 1: 初始化 QA ──
    init_result = await stage_init_qa(
        seed=seed,
        mode=mode,
        run_folder=run_folder,
        model=model,
        temperature=temperature,
        max_turns=max_turns,
    )
    total_tokens += init_result.get("total_tokens", 0)
    pipeline_result["stages"]["init"] = init_result

    current_question = init_result["question"]
    current_answer = init_result["answer"]

    # ── Stage 2: 迭代复杂化（可选）──
    if enable_evolution and max_evolutions > 0:
        evolve_result = await stage_evolve_qa(
            question=current_question,
            answer=current_answer,
            run_folder=run_folder,
            max_evolutions=max_evolutions,
            model=model,
            temperature=temperature,
            max_turns_per_round=max_turns,
        )
        total_tokens += evolve_result.get("total_tokens", 0)
        pipeline_result["stages"]["evolve"] = evolve_result
        current_question = evolve_result["evolved_question"]
    else:
        print("\n  ⏭ 跳过 Stage 2 (迭代复杂化)")

    # ── Stage 3: 轨迹改写（可选）──
    if enable_rewrite:
        rewrite_result = await stage_rewrite_trace(
            question=current_question,
            answer=current_answer,
            run_folder=run_folder,
            model=model,
            temperature=0.3,  # 改写用低温度
        )
        pipeline_result["stages"]["rewrite"] = rewrite_result
    else:
        print("\n  ⏭ 跳过 Stage 3 (轨迹改写)")

    # ── 保存最终汇总 ──
    pipeline_result["final_question"] = current_question
    pipeline_result["final_answer"] = current_answer
    pipeline_result["total_tokens"] = total_tokens
    pipeline_result["timestamp"] = datetime.now().isoformat()

    final_path = os.path.join(run_folder, "final_output.json")
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(pipeline_result, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'#'*60}")
    print(f"  ✅ Pipeline 完成!")
    print(f"  最终问题: {current_question[:80]}...")
    print(f"  最终答案: {current_answer[:80]}")
    print(f"  总 Token 消耗: {total_tokens:,}")
    print(f"  输出目录: {run_folder}")
    print(f"{'#'*60}\n")

    return pipeline_result


# ═══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Search2QA: 搜索轨迹驱动的 QA 合成")
    parser.add_argument("--seed", type=str, required=True, help="种子词或已知答案")
    parser.add_argument("--mode", type=str, default="question",
                        choices=["question", "answer"],
                        help="生成模式: question(种子→QA) 或 answer(答案→问题)")
    parser.add_argument("--evolutions", type=int, default=2,
                        help="复杂化迭代次数 (0=跳过)")
    parser.add_argument("--model", type=str, default="deepseek-chat",
                        help="LLM 模型名称")
    parser.add_argument("--temperature", type=float, default=0.7,
                        help="LLM 温度")
    parser.add_argument("--max-turns", type=int, default=20,
                        help="每阶段最大交互轮次")
    parser.add_argument("--no-evolution", action="store_true",
                        help="跳过迭代复杂化阶段")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="跳过轨迹改写阶段")
    parser.add_argument("--output-dir", type=str, default="output/trace",
                        help="输出目录")

    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("❌ 请设置环境变量 DEEPSEEK_API_KEY")
        sys.exit(1)

    result = asyncio.run(run_search2qa_pipeline(
        seed=args.seed,
        mode=args.mode,
        max_evolutions=args.evolutions,
        model=args.model,
        temperature=args.temperature,
        max_turns=args.max_turns,
        enable_evolution=not args.no_evolution,
        enable_rewrite=not args.no_rewrite,
        output_dir=args.output_dir,
    ))

    # 输出结果路径
    print(json.dumps({
        "status": "success",
        "run_folder": result["run_folder"],
        "final_question": result["final_question"],
        "final_answer": result["final_answer"],
        "total_tokens": result["total_tokens"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
