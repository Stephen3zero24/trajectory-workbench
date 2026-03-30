"""
Search2QA Backend Integration — backend.py 集成补丁

本文件说明需要在 backend.py 中进行的修改，以支持 search2qa 场景。

=== 使用方式 ===
将本文件中的代码片段按标注位置插入到 backend.py 中。
或者直接使用下方的完整集成模块，通过 import 方式引入。
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 1. 在 backend.py 顶部添加 import
# ═══════════════════════════════════════════════════════════════════════════════

# import sys
# sys.path.insert(0, os.path.dirname(__file__))
# from search2qa.scene_handler import run_search2qa_in_sandbox


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 修改 TaskCreateRequest，添加 search2qa 专用字段
# ═══════════════════════════════════════════════════════════════════════════════

# class TaskCreateRequest(BaseModel):
#     task_desc: str
#     scene_type: str = "code_exec"
#     model: str = "deepseek-chat"
#     temperature: float = 0.7
#     max_steps: int = 15
#     max_iterations: int = 3
#     quality_threshold: float = 0.80
#     concurrent: int = 1
#     # ─── search2qa 专用字段 ───
#     seed: str = ""                    # 种子词
#     qa_mode: str = "question"         # "question" 或 "answer"
#     max_evolutions: int = 2           # 复杂化迭代次数
#     max_turns: int = 20              # 每阶段最大交互轮次
#     enable_evolution: bool = True     # 是否启用复杂化
#     enable_rewrite: bool = True       # 是否启用轨迹改写


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 修改 run_single_iteration 函数，分发 search2qa 场景
# ═══════════════════════════════════════════════════════════════════════════════

# 在 run_single_iteration 函数的开头添加场景分发逻辑：
#
#   async def run_single_iteration(task_id: str):
#       task = tasks_store.get(task_id)
#       if not task:
#           return
#
#       # ─── 场景分发 ───
#       if task.config.get("scene_type") == "search2qa":
#           await run_search2qa_iteration(task_id)
#           return
#
#       # ... 原有的通用 Agent 执行逻辑 ...


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 新增 search2qa 专用执行函数
# ═══════════════════════════════════════════════════════════════════════════════

"""
async def run_search2qa_iteration(task_id: str):
    '''执行一轮 Search2QA Pipeline'''
    task = tasks_store.get(task_id)
    if not task:
        return

    config = task.config
    iteration = task.current_iteration

    try:
        update_task_status(task_id, "executing")
        add_event(task_id, "search2qa_start",
                  f"Search2QA Pipeline 启动 (seed={config.get('seed', '')}, mode={config.get('qa_mode', 'question')})")

        # 事件回调
        def emit(event_type, message, data=None):
            add_event(task_id, event_type, message, data or {})

        # 执行 Search2QA Pipeline
        result = await run_search2qa_in_sandbox(
            config={
                "seed": config.get("seed", config.get("task_desc", "")),
                "mode": config.get("qa_mode", "question"),
                "model": config.get("model", "deepseek-chat"),
                "temperature": config.get("temperature", 0.7),
                "max_turns": config.get("max_turns", 20),
                "max_evolutions": config.get("max_evolutions", 2),
                "enable_evolution": config.get("enable_evolution", True),
                "enable_rewrite": config.get("enable_rewrite", True),
                "deepseek_api_key": DEEPSEEK_API_KEY,
                "deepseek_base_url": DEEPSEEK_BASE_URL,
                "timeout_minutes": config.get("timeout_minutes", 15),
            },
            emit=emit,
        )

        # 构建轨迹数据（兼容通用格式）
        trajectory_data = result.get("trajectory_data", {})
        stages = trajectory_data.get("stages", {})

        # 从 final_output 中提取步骤作为通用轨迹格式
        steps = []
        # 从 init 阶段的 trace 提取
        if "init" in stages:
            init_trace_file = stages["init"].get("trace_file", "")
            # 轨迹已在 trajectory_data 中

        # 将 search2qa 的结果包装为通用格式
        formatted_trajectory = {
            "steps": steps,
            "total_tokens": result.get("total_tokens", 0),
            "search2qa_data": trajectory_data,  # 保留完整的 search2qa 数据
        }

        # Review Agent 评估
        update_task_status(task_id, "reviewing")
        add_event(task_id, "review_start", "Review Agent 评估 QA 质量...")

        review_result = review_search2qa_quality(
            question=result.get("final_question", ""),
            answer=result.get("final_answer", ""),
            trajectory_data=trajectory_data,
            config=config,
        )
        add_event(task_id, "review_complete",
                  f"评估完成: {review_result.get('overall_score', 0):.2f}")

        # 保存迭代结果
        iteration_result = {
            "iteration": iteration,
            "trajectory": formatted_trajectory,
            "review": review_result,
            "config_snapshot": dict(config),
            "search2qa_result": {
                "final_question": result.get("final_question", ""),
                "final_answer": result.get("final_answer", ""),
                "status": result.get("status", ""),
            },
            "timestamp": datetime.now().isoformat(),
        }
        task.iterations.append(iteration_result)

        # 判断质量
        score = review_result.get("overall_score", 0)
        if score >= task.quality_threshold:
            update_task_status(task_id, "completed")
            add_event(task_id, "pipeline_complete",
                      f"QA 质量达标 ({score:.2f} >= {task.quality_threshold})")
            return

        # 处理建议
        suggestions = review_result.get("suggestions", [])
        auto_suggestions = [s for s in suggestions if s.get("level") == "auto"]
        human_suggestions = [s for s in suggestions if s.get("level") in ("confirm", "approve")]

        if auto_suggestions:
            task.config, applied = apply_auto_fixes_to_config(config, auto_suggestions)
            for a in applied:
                add_event(task_id, "auto_fix", f"🟢 自动应用: {a.get('category', '')}")

        if human_suggestions:
            task.pending_suggestions = human_suggestions
            update_task_status(task_id, "waiting_approval")
            add_event(task_id, "waiting_approval",
                      f"有 {len(human_suggestions)} 项修改需要确认")
            return

        # 触发下一轮
        task.current_iteration += 1
        if task.current_iteration >= task.max_iterations:
            update_task_status(task_id, "completed")
            add_event(task_id, "pipeline_complete", "达到最大迭代次数")
            return

        add_event(task_id, "next_iteration",
                  f"触发第 {task.current_iteration + 1} 轮迭代")
        await run_search2qa_iteration(task_id)

    except Exception as e:
        update_task_status(task_id, "failed")
        add_event(task_id, "error", f"Search2QA 执行失败: {str(e)}")
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Search2QA 专用 Review 函数
# ═══════════════════════════════════════════════════════════════════════════════

"""
def review_search2qa_quality(question, answer, trajectory_data, config):
    '''评估 Search2QA 生成的 QA 质量'''

    stages = trajectory_data.get("stages", {})
    evolution_history = stages.get("evolve", {}).get("evolution_history", [])
    rewrite_trace = stages.get("rewrite", {}).get("rewritten_trace", [])

    review_prompt = f'''你是 QA 质量评估专家。请评估以下通过搜索合成的 QA 对。

## 最终问题
{question}

## 最终答案
{answer}

## 演化历史
{json.dumps(evolution_history, ensure_ascii=False, indent=2) if evolution_history else "无（未经演化）"}

## 搜索轨迹步数
{len(rewrite_trace)} 步

## 评估维度（0-1分）
1. question_quality: 问题是否具有挑战性，需要多步推理
2. answer_accuracy: 答案是否准确、有搜索证据支撑
3. trace_quality: 搜索轨迹是否合理、完整
4. evolution_effectiveness: 问题演化是否有效提高了难度
5. cross_source_reasoning: 是否需要跨多个来源的信息才能回答

## 输出（严格 JSON）
{{
    "overall_score": 0.75,
    "dimensions": {{
        "question_quality": 0.8,
        "answer_accuracy": 0.9,
        "trace_quality": 0.7,
        "evolution_effectiveness": 0.6,
        "cross_source_reasoning": 0.7
    }},
    "fail_modes": ["问题描述"],
    "suggestions": [
        {{"level": "auto", "category": "类别", "desc": "描述", "field": "字段", "from": "原值", "to": "新值"}},
        {{"level": "confirm", "category": "类别", "desc": "描述", "options": ["方案A", "方案B"]}}
    ],
    "reasoning": "评估推理过程"
}}
只输出JSON。'''

    result = call_deepseek(
        [{"role": "user", "content": review_prompt}],
        model="deepseek-chat", temperature=0.3,
    )

    try:
        raw = result["content"]
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
        return json.loads(raw.strip())
    except Exception:
        return {
            "overall_score": 0.5,
            "dimensions": {},
            "fail_modes": ["Review解析失败"],
            "suggestions": [],
            "reasoning": result.get("content", "")[:300],
        }
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 修改场景列表，添加 search2qa
# ═══════════════════════════════════════════════════════════════════════════════

# 在 /api/scenes 端点中添加：
#
# {"id": "search2qa", "name": "Search2QA", "desc": "基于WebExplorer的搜索轨迹驱动QA合成", "icon": "🔍"},


# ═══════════════════════════════════════════════════════════════════════════════
# 7. 修改 create_task 端点，处理 search2qa 专用参数
# ═══════════════════════════════════════════════════════════════════════════════

# 在 create_task 函数中，config 字典需要额外添加 search2qa 字段：
#
# if req.scene_type == "search2qa":
#     config["seed"] = req.seed or req.task_desc
#     config["qa_mode"] = req.qa_mode
#     config["max_turns"] = req.max_turns
#     config["max_evolutions"] = req.max_evolutions
#     config["enable_evolution"] = req.enable_evolution
#     config["enable_rewrite"] = req.enable_rewrite
