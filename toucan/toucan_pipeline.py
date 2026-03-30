"""
Toucan Pipeline 编排器 — 串联 Step 0~3 + Review + 导出
"""

import asyncio
import json
import os
import time
from dataclasses import asdict

from .config import ToucanPipelineConfig, MCPServerRegistry, SmitheryConfig, LLMConfig
from .step0_smithery_setup import SmitherySetup
from .step1_question_synthesis import run_step1
from .step2_quality_check import run_step2
from .step3_trajectory_gen import run_step3, ToucanTrajectory


# ─── Review Agent (工具调用场景专用) ──────────────────────────────────────────

def review_toucan_trajectory(traj, config):
    """评估 Toucan 轨迹质量"""
    from openai import OpenAI
    llm = OpenAI(api_key=config.question_llm.api_key, base_url=config.question_llm.base_url)

    turns_summary = []
    for turn in traj.turns:
        s = {"role": turn.role, "content": turn.content[:200]}
        if turn.tool_calls:
            s["tool_calls"] = [{"tool": tc.get("tool_name",""), "success": tc.get("success",False), "output": tc.get("tool_output","")[:100]} for tc in turn.tool_calls]
        turns_summary.append(s)

    traj_json = json.dumps(turns_summary, ensure_ascii=False, indent=2)[:6000]

    prompt = f"""你是工具调用轨迹评估专家。评估以下轨迹质量。

## 问题
{traj.question}

## 轨迹
{traj_json}

## 评估维度 (0-1): tool_selection, tool_execution, reasoning, completeness, multi_tool

输出严格 JSON:
{{"overall_score": 0.75, "dimensions": {{"tool_selection": 0.8, "tool_execution": 0.7, "reasoning": 0.8, "completeness": 0.9, "multi_tool": 0.6}}, "fail_modes": [], "suggestions": [], "reasoning": ""}}"""

    try:
        resp = llm.chat.completions.create(model=config.question_llm.model, messages=[{"role":"user","content":prompt}], temperature=0.3, max_tokens=1024)
        raw = resp.choices[0].message.content
        if "```json" in raw: raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw: raw = raw.split("```")[1].split("```")[0]
        return json.loads(raw.strip())
    except Exception as e:
        return {"overall_score": 0.5, "dimensions": {}, "fail_modes": [str(e)], "suggestions": [], "reasoning": ""}


# ─── 数据集导出 ───────────────────────────────────────────────────────────────

def export_toucan_dataset(trajectories, config, reviews=None):
    """导出 SFT / DPO / RLHF 格式数据"""
    os.makedirs(config.output_dir, exist_ok=True)

    # SFT
    sft_path = os.path.join(config.output_dir, f"{config.task_id}_sft.jsonl")
    with open(sft_path, "w", encoding="utf-8") as f:
        for traj in trajectories:
            messages = [{"role": "system", "content": "你是一个能够使用各种工具的智能助手。"}]
            for turn in traj.turns:
                msg = {"role": turn.role, "content": turn.content}
                if turn.tool_calls:
                    msg["tool_calls"] = [{"id": tc.get("call_id",""), "type": "function", "function": {"name": tc.get("tool_name",""), "arguments": json.dumps(tc.get("tool_input",{}), ensure_ascii=False)}} for tc in turn.tool_calls]
                messages.append(msg)
            f.write(json.dumps({"id": traj.trajectory_id, "messages": messages, "metadata": {"question_id": traj.question_id, "server_ids": traj.server_ids, "tool_calls": traj.total_tool_calls, "multi_turn": traj.is_multi_turn, "score": traj.quality_score}}, ensure_ascii=False) + "\n")
    print(f"  SFT: {sft_path}")

    # DPO
    if reviews and len(trajectories) >= 2:
        scored = sorted(zip(trajectories, reviews), key=lambda x: x[1].get("overall_score",0), reverse=True)
        dpo_path = os.path.join(config.output_dir, f"{config.task_id}_dpo.jsonl")
        with open(dpo_path, "w", encoding="utf-8") as f:
            for i in range(len(scored) // 2):
                ct, cr = scored[i]
                rt, rr = scored[-(i+1)]
                if cr.get("overall_score",0) > rr.get("overall_score",0):
                    chosen = "\n".join(t.content for t in ct.turns if t.role == "assistant" and t.content)[:1000]
                    rejected = "\n".join(t.content for t in rt.turns if t.role == "assistant" and t.content)[:1000]
                    f.write(json.dumps({"prompt": ct.question, "chosen": chosen, "rejected": rejected, "chosen_score": cr.get("overall_score",0), "rejected_score": rr.get("overall_score",0)}, ensure_ascii=False) + "\n")
        print(f"  DPO: {dpo_path}")

    # Raw
    raw_path = os.path.join(config.output_dir, f"{config.task_id}_raw.jsonl")
    with open(raw_path, "w", encoding="utf-8") as f:
        for traj in trajectories:
            f.write(json.dumps(asdict(traj), ensure_ascii=False) + "\n")
    print(f"  Raw: {raw_path}")


# ─── 主流程 ───────────────────────────────────────────────────────────────────

async def run_toucan_pipeline(config, event_callback=None):
    """执行完整的 Toucan 工具调用轨迹合成流水线"""
    def emit(t, m, d=None):
        if event_callback: event_callback(t, m, d)
        print(f"  [{t}] {m}")

    start = time.time()
    emit("pipeline_start", "Toucan Pipeline 启动")

    # Step 0
    emit("step0_start", "配置 MCP Server 注册表")
    setup = SmitherySetup(config.smithery)
    rpath = config.mcp_registry_path
    if os.path.exists(rpath):
        registry = MCPServerRegistry.load(rpath)
        emit("step0_cache", f"已加载: {len(registry.servers)} 个 Server")
    else:
        registry = await setup.build_registry(fetch_tools=config.smithery.is_configured)
        await setup.save_registry(registry, rpath)
    emit("step0_done", f"{len(registry.servers)} 个 Server, {sum(len(s.tools) for s in registry.list_servers())} 个工具")

    # Step 1
    emit("step1_start", "问题合成")
    questions = run_step1(config, registry)
    emit("step1_done", f"{len(questions)} 个问题")
    if not questions:
        return {"status": "failed", "error": "No questions"}

    # Step 2
    emit("step2_start", "质量检查")
    checked = run_step2(config, registry, questions)
    emit("step2_done", f"{len(checked)} 个通过")
    if not checked:
        return {"status": "failed", "error": "No questions passed QC"}

    # Step 3
    emit("step3_start", "轨迹生成")
    trajectories = await run_step3(config, registry, checked, event_callback)
    emit("step3_done", f"{len(trajectories)} 条轨迹")

    # Review
    emit("review_start", "质量评估")
    reviews = []
    for i, traj in enumerate(trajectories):
        rev = review_toucan_trajectory(traj, config)
        traj.quality_score = rev.get("overall_score", 0)
        reviews.append(rev)
        emit("review_progress", f"[{i+1}/{len(trajectories)}] score={traj.quality_score:.2f}")

    # Export
    emit("export_start", "导出数据集")
    export_toucan_dataset(trajectories, config, reviews)

    elapsed = time.time() - start
    avg_score = sum(r.get("overall_score",0) for r in reviews) / max(len(reviews),1)

    summary = {
        "status": "completed", "elapsed_seconds": round(elapsed, 1),
        "questions_generated": len(questions), "questions_passed_qc": len(checked),
        "trajectories": len(trajectories),
        "total_tool_calls": sum(t.total_tool_calls for t in trajectories),
        "total_tokens": sum(t.total_tokens for t in trajectories),
        "avg_quality": round(avg_score, 3),
        "multi_turn": sum(1 for t in trajectories if t.is_multi_turn),
        "output_dir": config.output_dir,
    }
    emit("pipeline_done", f"完成 | {elapsed:.1f}s | avg={avg_score:.3f}")

    print(f"\n{'='*60}\n📊 Toucan Pipeline 汇总\n{'='*60}")
    for k, v in summary.items(): print(f"  {k}: {v}")
    return summary


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = ToucanPipelineConfig(
        task_id="toucan_demo", question_count=10,
        sampling_strategy="uniform", server_mode="single",
        quality_threshold=0.6, max_steps=10,
        enable_multi_turn=False, max_iterations=1,
    )
    asyncio.run(run_toucan_pipeline(cfg))
