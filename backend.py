"""
轨迹合成工作台 - 后端 API 服务
基于 FastAPI，对接 pipeline.py 的核心逻辑，为前端 Web UI 提供接口。

【已集成 Search2QA 场景】

启动方式：
    cd ~/trajectory-workbench
    python3 backend.py
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn
import httpx
from openai import OpenAI
from opensandbox.sandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models import WriteEntry

# ─── Search2QA 集成 ───────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from search2qa.scene_handler import run_search2qa_in_sandbox

# ─── 配置 ─────────────────────────────────────────────────────────────────────

OPENSANDBOX_SERVER = os.environ.get("OPENSANDBOX_SERVER", "http://127.0.0.1:8080")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

llm_client = None

# ─── 全局状态存储（生产环境应替换为数据库）──────────────────────────────────────

tasks_store: dict = {}   # task_id -> TaskState
events_store: dict = {}  # task_id -> list[event]

# ─── 数据模型 ──────────────────────────────────────────────────────────────────

class TaskCreateRequest(BaseModel):
    task_desc: str
    scene_type: str = "code_exec"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_steps: int = 15
    max_iterations: int = 3
    quality_threshold: float = 0.80
    concurrent: int = 1
    # ─── search2qa 专用字段 ───
    seed: str = ""                    # 种子词（search2qa 模式下使用）
    qa_mode: str = "question"         # "question" 或 "answer"
    max_evolutions: int = 2           # 复杂化迭代次数
    max_turns: int = 20              # 每阶段最大交互轮次
    enable_evolution: bool = True     # 是否启用复杂化
    enable_rewrite: bool = True       # 是否启用轨迹改写


class ApprovalRequest(BaseModel):
    suggestion_index: int
    approved: bool
    selected_option: Optional[int] = None


class TaskState(BaseModel):
    task_id: str
    config: dict
    status: str = "created"  # created | executing | reviewing | waiting_approval | completed | failed
    current_iteration: int = 0
    max_iterations: int = 3
    quality_threshold: float = 0.80
    iterations: list = Field(default_factory=list)
    pending_suggestions: list = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ─── 沙箱管理 ──────────────────────────────────────────────────────────────────

async def create_sandbox_via_api() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes",
            json={
                "image": {"uri": "opensandbox/code-interpreter:v1.0.2"},
                "entrypoint": ["/opt/opensandbox/code-interpreter.sh"],
                "resourceLimits": {},
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def connect_sandbox(sandbox_id: str) -> Sandbox:
    config = ConnectionConfig(domain="127.0.0.1:8080", protocol="http")
    return await Sandbox.connect(sandbox_id, connection_config=config)


async def delete_sandbox_via_api(sandbox_id: str):
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes/{sandbox_id}",
            timeout=30,
        )


# ─── LLM 调用 ─────────────────────────────────────────────────────────────────

def call_deepseek(messages: list, model: str = "deepseek-chat", temperature: float = 0.7) -> dict:
    global llm_client
    if llm_client is None:
        llm_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    try:
        response = llm_client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=2048, stream=False,
        )
        return {
            "content": response.choices[0].message.content,
            "tokens": response.usage.total_tokens if response.usage else 0,
        }
    except Exception as e:
        return {"content": f"[LLM调用失败: {e}]", "tokens": 0}


# ─── 事件记录 ──────────────────────────────────────────────────────────────────

def add_event(task_id: str, event_type: str, message: str, data: dict = None):
    if task_id not in events_store:
        events_store[task_id] = []
    events_store[task_id].append({
        "type": event_type,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    })


def update_task_status(task_id: str, status: str):
    if task_id in tasks_store:
        tasks_store[task_id].status = status
        tasks_store[task_id].updated_at = datetime.now().isoformat()


# ─── 通用 Agent 执行逻辑（原有逻辑，用于非 search2qa 场景）───────────────────

async def execute_agent_in_sandbox(sandbox: Sandbox, config: dict, task_id: str) -> dict:
    """在沙箱内运行 Agent，返回轨迹"""
    task_desc = config["task_desc"]
    model = config.get("model", "deepseek-chat")
    temperature = config.get("temperature", 0.7)
    max_steps = config.get("max_steps", 15)
    system_prompt_extra = config.get("system_prompt_extra", "")

    system_prompt = f"""你是一个在沙箱环境中执行任务的AI Agent。

## 你的任务
{task_desc}

## 你的能力
你可以在Linux沙箱中执行shell命令、创建编辑文件、运行Python脚本、安装包。

## 输出格式（严格JSON，不要输出其他内容）
{{
    "thought": "你对当前状态的分析和推理",
    "action": "要执行的shell命令",
    "is_final": false
}}

任务完成时将 is_final 设为 true。

{f'## 额外指令' + chr(10) + system_prompt_extra if system_prompt_extra else ''}"""

    messages = [{"role": "system", "content": system_prompt}]

    # 初始环境状态
    init_result = await sandbox.commands.run("uname -a && pwd")
    initial_obs = init_result.logs.stdout[0].text if init_result.logs.stdout else "环境就绪"

    messages.append({"role": "user", "content": f"环境已就绪：\n{initial_obs}\n\n请开始执行任务。"})

    steps = []
    total_tokens = 0

    for step_id in range(1, max_steps + 1):
        add_event(task_id, "agent_step", f"Agent 执行第 {step_id} 步...")

        # Agent 决策
        llm_result = call_deepseek(messages, model, temperature)
        total_tokens += llm_result["tokens"]
        raw_response = llm_result["content"]

        # 解析输出
        try:
            json_str = raw_response
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
            agent_output = json.loads(json_str.strip())
            thought = agent_output.get("thought", "")
            action = agent_output.get("action", "echo 'no action'")
            is_final = agent_output.get("is_final", False)
        except (json.JSONDecodeError, IndexError):
            thought = raw_response[:200]
            action = "echo 'Agent输出解析失败'"
            is_final = False

        # 执行命令
        try:
            exec_result = await sandbox.commands.run(action, timeout=timedelta(seconds=30))
            stdout = "\n".join([l.text for l in exec_result.logs.stdout]) if exec_result.logs.stdout else ""
            stderr = "\n".join([l.text for l in exec_result.logs.stderr]) if exec_result.logs.stderr else ""
            observation = stdout if stdout else (stderr if stderr else "(无输出)")
            result = f"exit_code=0\n{observation}" if not stderr else f"exit_code=1\nstdout: {stdout}\nstderr: {stderr}"
        except Exception as e:
            observation = f"命令执行异常: {e}"
            result = observation

        step_data = {
            "step_id": step_id,
            "observation": observation[:1000],
            "thought": thought[:500],
            "action": action,
            "result": result[:1000],
            "timestamp": time.time(),
        }
        steps.append(step_data)
        add_event(task_id, "agent_action", f"Step {step_id}: {action[:60]}",
                  {"step": step_data})

        messages.append({"role": "assistant", "content": raw_response})
        messages.append({"role": "user", "content": f"命令执行结果：\n{observation[:800]}\n\n请继续。"})

        if is_final:
            add_event(task_id, "agent_complete", f"Agent 完成任务（共 {step_id} 步）")
            break

    return {
        "steps": steps,
        "total_tokens": total_tokens,
    }


def review_trajectory_llm(steps: list, config: dict) -> dict:
    """用 DeepSeek 评估轨迹"""
    trajectory_summary = json.dumps(steps, ensure_ascii=False, indent=2)
    if len(trajectory_summary) > 6000:
        trajectory_summary = trajectory_summary[:6000] + "\n...(截断)"

    review_prompt = f"""你是轨迹质量评估专家。评估以下Agent轨迹。

## 任务描述
{config['task_desc']}

## 轨迹
{trajectory_summary}

## 输出格式（严格JSON）
{{
    "overall_score": 0.75,
    "dimensions": {{
        "tool_usage": 0.8, "reasoning": 0.7,
        "error_handling": 0.6, "completeness": 0.9
    }},
    "fail_modes": ["失败模式描述"],
    "suggestions": [
        {{"level": "auto", "category": "类别", "desc": "描述", "field": "temperature", "from": "0.7", "to": "0.3"}},
        {{"level": "confirm", "category": "类别", "desc": "描述", "options": ["方案A", "方案B"]}},
        {{"level": "approve", "category": "类别", "desc": "描述", "impact": "影响评估"}}
    ],
    "reasoning": "评估推理"
}}

level: auto=低风险自动执行, confirm=中风险需人工选择, approve=高风险需人工审批
只输出JSON。"""

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
            "dimensions": {"tool_usage": 0.5, "reasoning": 0.5, "error_handling": 0.5, "completeness": 0.5},
            "fail_modes": ["Review Agent输出解析失败"],
            "suggestions": [],
            "reasoning": result["content"][:300],
        }


def apply_auto_fixes_to_config(config: dict, suggestions: list) -> tuple:
    """自动应用 auto 级别的修改"""
    applied = []
    for s in suggestions:
        if s.get("level") != "auto":
            continue
        field_name = s.get("field", "")
        new_value = s.get("to", "")

        if field_name == "temperature" and new_value:
            try:
                config["temperature"] = float(new_value)
                applied.append(s)
            except ValueError:
                pass
        elif field_name == "model" and new_value:
            config["model"] = new_value
            applied.append(s)
        elif "系统提示" in s.get("category", "") or field_name == "system_prompt_extra":
            config["system_prompt_extra"] = config.get("system_prompt_extra", "") + "\n" + s.get("to", s.get("desc", ""))
            applied.append(s)
        else:
            applied.append(s)

    return config, applied


# ═══════════════════════════════════════════════════════════════════════════════
# Search2QA 专用 Review 函数
# ═══════════════════════════════════════════════════════════════════════════════

def review_search2qa_quality(question: str, answer: str, trajectory_data: dict, config: dict) -> dict:
    """评估 Search2QA 生成的 QA 质量"""

    stages = trajectory_data.get("stages", {})
    evolution_history = stages.get("evolve", {}).get("evolution_history", [])
    rewrite_trace = stages.get("rewrite", {}).get("rewritten_trace", [])

    review_prompt = f"""你是 QA 质量评估专家。请评估以下通过搜索合成的 QA 对。

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
只输出JSON。"""

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


# ═══════════════════════════════════════════════════════════════════════════════
# Search2QA 专用执行函数
# ═══════════════════════════════════════════════════════════════════════════════

async def run_search2qa_iteration(task_id: str):
    """执行一轮 Search2QA Pipeline"""
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

        formatted_trajectory = {
            "steps": [],
            "total_tokens": result.get("total_tokens", 0),
            "search2qa_data": trajectory_data,
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


# ─── 后台任务：执行一轮迭代（统一入口，分发场景）─────────────────────────────

async def run_single_iteration(task_id: str):
    """执行单轮迭代：根据场景类型分发到不同的执行逻辑"""
    task = tasks_store.get(task_id)
    if not task:
        return

    # ═══ 场景分发 ═══
    if task.config.get("scene_type") == "search2qa":
        await run_search2qa_iteration(task_id)
        return

    # ═══ 以下是原有的通用 Agent 执行逻辑 ═══
    config = task.config
    iteration = task.current_iteration

    try:
        # 1. 创建沙箱
        update_task_status(task_id, "executing")
        add_event(task_id, "sandbox_create", "正在创建沙箱实例...")
        sandbox_id = await create_sandbox_via_api()
        add_event(task_id, "sandbox_ready", f"沙箱已创建: {sandbox_id[:12]}...")
        await asyncio.sleep(3)

        # 2. Agent 执行
        add_event(task_id, "agent_start", "Agent 开始在沙箱内执行任务")
        sandbox = await connect_sandbox(sandbox_id)
        try:
            async with sandbox:
                trajectory_data = await execute_agent_in_sandbox(sandbox, config, task_id)
        finally:
            await delete_sandbox_via_api(sandbox_id)
            add_event(task_id, "sandbox_cleanup", "沙箱已清理")

        # 3. Review Agent 评估
        update_task_status(task_id, "reviewing")
        add_event(task_id, "review_start", "Review Agent 正在评估轨迹质量...")
        review_result = review_trajectory_llm(trajectory_data["steps"], config)
        add_event(task_id, "review_complete", f"评估完成: {review_result.get('overall_score', 0):.2f}")

        # 4. 保存迭代结果
        iteration_result = {
            "iteration": iteration,
            "trajectory": trajectory_data,
            "review": review_result,
            "config_snapshot": dict(config),
            "timestamp": datetime.now().isoformat(),
        }
        task.iterations.append(iteration_result)

        # 5. 判断是否达标
        score = review_result.get("overall_score", 0)
        if score >= task.quality_threshold:
            update_task_status(task_id, "completed")
            add_event(task_id, "pipeline_complete",
                      f"质量达标 ({score:.2f} >= {task.quality_threshold})，流程完成")
            return

        # 6. 处理建议
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
                      f"有 {len(human_suggestions)} 项修改需要人工确认/审批")
            return

        # 没有人工建议，检查是否还有迭代次数
        task.current_iteration += 1
        if task.current_iteration >= task.max_iterations:
            update_task_status(task_id, "completed")
            add_event(task_id, "pipeline_complete",
                      f"达到最大迭代次数 ({task.max_iterations})，流程完成")
            return

        # 自动触发下一轮
        add_event(task_id, "next_iteration", f"自动触发第 {task.current_iteration + 1} 轮迭代")
        await run_single_iteration(task_id)

    except Exception as e:
        update_task_status(task_id, "failed")
        add_event(task_id, "error", f"执行失败: {str(e)}")


# ─── FastAPI 应用 ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 轨迹合成工作台后端启动")
    print(f"   OpenSandbox Server: {OPENSANDBOX_SERVER}")
    print(f"   DeepSeek API Key: {'已配置' if DEEPSEEK_API_KEY else '❌ 未配置'}")
    print(f"   Search2QA 场景: ✅ 已加载")
    yield
    print("👋 后端关闭")

app = FastAPI(title="轨迹合成工作台 API", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── API 端点 ──────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """健康检查"""
    sandbox_ok = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OPENSANDBOX_SERVER}/health", timeout=5)
            sandbox_ok = resp.status_code == 200
    except Exception:
        pass

    return {
        "status": "ok",
        "opensandbox": "connected" if sandbox_ok else "disconnected",
        "deepseek": "configured" if DEEPSEEK_API_KEY else "not_configured",
    }


@app.post("/api/tasks")
async def create_task(req: TaskCreateRequest, background_tasks: BackgroundTasks):
    """创建新的合成任务并启动执行"""
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    config = {
        "task_desc": req.task_desc,
        "scene_type": req.scene_type,
        "model": req.model,
        "temperature": req.temperature,
        "max_steps": req.max_steps,
        "system_prompt_extra": "",
    }

    # ═══ search2qa 专用配置 ═══
    if req.scene_type == "search2qa":
        config["seed"] = req.seed if req.seed else req.task_desc
        config["qa_mode"] = req.qa_mode
        config["max_turns"] = req.max_turns
        config["max_evolutions"] = req.max_evolutions
        config["enable_evolution"] = req.enable_evolution
        config["enable_rewrite"] = req.enable_rewrite

    task = TaskState(
        task_id=task_id,
        config=config,
        status="created",
        max_iterations=req.max_iterations,
        quality_threshold=req.quality_threshold,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )

    tasks_store[task_id] = task
    events_store[task_id] = []

    add_event(task_id, "task_created", f"任务已创建: {req.task_desc[:50]}...")

    # 在后台启动执行
    background_tasks.add_task(run_single_iteration, task_id)

    return {"task_id": task_id, "status": "created"}


@app.get("/api/tasks")
async def list_tasks():
    """获取所有任务列表"""
    result = []
    for tid, task in tasks_store.items():
        best_score = 0
        if task.iterations:
            scores = [it.get("review", {}).get("overall_score", 0) for it in task.iterations]
            best_score = max(scores) if scores else 0
        result.append({
            "task_id": tid,
            "status": task.status,
            "task_desc": task.config.get("task_desc", "")[:80],
            "scene_type": task.config.get("scene_type", ""),
            "current_iteration": task.current_iteration,
            "max_iterations": task.max_iterations,
            "best_score": best_score,
            "created_at": task.created_at,
        })
    return {"tasks": result}


@app.get("/api/tasks/{task_id}")
async def get_task_detail(task_id: str):
    """获取任务详情"""
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    return {
        "task_id": task.task_id,
        "config": task.config,
        "status": task.status,
        "current_iteration": task.current_iteration,
        "max_iterations": task.max_iterations,
        "quality_threshold": task.quality_threshold,
        "iterations": task.iterations,
        "pending_suggestions": task.pending_suggestions,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


@app.get("/api/tasks/{task_id}/events")
async def get_task_events(task_id: str, since: int = 0):
    """获取任务事件流（前端轮询用）"""
    events = events_store.get(task_id, [])
    return {"events": events[since:], "total": len(events)}


@app.get("/api/tasks/{task_id}/trajectory/{iteration}")
async def get_trajectory(task_id: str, iteration: int):
    """获取某一轮的轨迹详情"""
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if iteration >= len(task.iterations):
        raise HTTPException(404, "Iteration not found")

    it = task.iterations[iteration]
    return {
        "iteration": iteration,
        "steps": it.get("trajectory", {}).get("steps", []),
        "review": it.get("review", {}),
        "config_snapshot": it.get("config_snapshot", {}),
        "total_tokens": it.get("trajectory", {}).get("total_tokens", 0),
        # search2qa 额外数据
        "search2qa_result": it.get("search2qa_result", None),
        "search2qa_data": it.get("trajectory", {}).get("search2qa_data", None),
    }


@app.post("/api/tasks/{task_id}/approve")
async def approve_suggestion(task_id: str, req: ApprovalRequest, background_tasks: BackgroundTasks):
    """处理人工确认/审批"""
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    if task.status != "waiting_approval":
        raise HTTPException(400, "Task is not waiting for approval")

    suggestions = task.pending_suggestions
    if req.suggestion_index >= len(suggestions):
        raise HTTPException(400, "Invalid suggestion index")

    suggestion = suggestions[req.suggestion_index]

    if req.approved:
        if suggestion.get("level") == "confirm" and req.selected_option is not None:
            options = suggestion.get("options", [])
            if req.selected_option < len(options):
                selected = options[req.selected_option]
                add_event(task_id, "human_confirm",
                          f"🟡 人工确认: {suggestion.get('category', '')} → {selected}")
                task.config["system_prompt_extra"] = task.config.get("system_prompt_extra", "") + f"\n{selected}"

        elif suggestion.get("level") == "approve":
            add_event(task_id, "human_approve",
                      f"🔴 人工审批通过: {suggestion.get('category', '')}")
    else:
        add_event(task_id, "human_reject",
                  f"人工拒绝: {suggestion.get('category', '')}")

    # 移除已处理的建议
    task.pending_suggestions = [s for i, s in enumerate(suggestions) if i != req.suggestion_index]

    # 如果所有建议都已处理，触发下一轮
    if not task.pending_suggestions:
        task.current_iteration += 1
        if task.current_iteration < task.max_iterations:
            add_event(task_id, "next_iteration",
                      f"所有审批已完成，触发第 {task.current_iteration + 1} 轮迭代")
            background_tasks.add_task(run_single_iteration, task_id)
        else:
            update_task_status(task_id, "completed")
            add_event(task_id, "pipeline_complete", "达到最大迭代次数，流程完成")

    return {"status": "ok", "remaining": len(task.pending_suggestions)}


@app.post("/api/tasks/{task_id}/iterate")
async def force_iterate(task_id: str, background_tasks: BackgroundTasks):
    """手动触发下一轮迭代"""
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    task.pending_suggestions = []
    task.current_iteration += 1
    if task.current_iteration >= task.max_iterations:
        raise HTTPException(400, "Already at max iterations")

    add_event(task_id, "manual_iterate", f"手动触发第 {task.current_iteration + 1} 轮迭代")
    background_tasks.add_task(run_single_iteration, task_id)
    return {"status": "ok", "iteration": task.current_iteration}


@app.post("/api/tasks/{task_id}/export")
async def export_dataset(task_id: str):
    """导出最终数据集"""
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.iterations:
        raise HTTPException(400, "No trajectories to export")

    # 找最佳轨迹
    best_idx = 0
    best_score = 0
    for i, it in enumerate(task.iterations):
        score = it.get("review", {}).get("overall_score", 0)
        if score > best_score:
            best_score = score
            best_idx = i

    # 构建导出数据
    export_data = {
        "task_id": task_id,
        "task_desc": task.config.get("task_desc", ""),
        "scene_type": task.config.get("scene_type", ""),
        "total_iterations": len(task.iterations),
        "best_iteration": best_idx,
        "best_score": best_score,
        "quality_progression": [
            it.get("review", {}).get("overall_score", 0) for it in task.iterations
        ],
        "best_trajectory": task.iterations[best_idx].get("trajectory", {}).get("steps", []),
        "all_trajectories": [
            {
                "iteration": i,
                "score": it.get("review", {}).get("overall_score", 0),
                "steps": it.get("trajectory", {}).get("steps", []),
                "tokens": it.get("trajectory", {}).get("total_tokens", 0),
            }
            for i, it in enumerate(task.iterations)
        ],
    }

    # search2qa 额外导出
    if task.config.get("scene_type") == "search2qa":
        best_it = task.iterations[best_idx]
        export_data["search2qa"] = {
            "final_question": best_it.get("search2qa_result", {}).get("final_question", ""),
            "final_answer": best_it.get("search2qa_result", {}).get("final_answer", ""),
            "search2qa_data": best_it.get("trajectory", {}).get("search2qa_data", {}),
        }

    # 保存到文件
    os.makedirs("output", exist_ok=True)
    output_path = f"output/{task_id}_export.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    add_event(task_id, "export", f"数据集已导出: {output_path}")

    return {
        "status": "exported",
        "file": output_path,
        "best_score": best_score,
        "total_iterations": len(task.iterations),
        "formats": ["SFT", "DPO", "RLHF"],
    }


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务"""
    if task_id in tasks_store:
        del tasks_store[task_id]
    if task_id in events_store:
        del events_store[task_id]
    return {"status": "deleted"}


# ─── 预置场景 & 模型列表 ──────────────────────────────────────────────────────

@app.get("/api/scenes")
async def list_scenes():
    return {"scenes": [
        {"id": "mcp_tool",     "name": "MCP工具交互",    "desc": "Agent Harness的MCP交互、工具选择",     "icon": "⚙️"},
        {"id": "gui",          "name": "GUI操作",        "desc": "浏览器/安卓系统GUI操控",              "icon": "🖥️"},
        {"id": "search2qa",    "name": "Search2QA",      "desc": "基于WebExplorer的搜索轨迹驱动QA合成", "icon": "🔍"},
        {"id": "deep_search",  "name": "Deep Search",    "desc": "搜索引擎检索与信息整合",              "icon": "🌐"},
        {"id": "multi_agent",  "name": "多Agent协调",    "desc": "多智能体协作与交互",                  "icon": "🤖"},
        {"id": "code_exec",    "name": "代码执行",       "desc": "代码编写、测试与执行",                "icon": "💻"},
    ]}


@app.get("/api/models")
async def list_models():
    return {"models": [
        {"id": "deepseek-chat",     "name": "DeepSeek-Chat (V3.2)", "provider": "DeepSeek"},
        {"id": "deepseek-reasoner", "name": "DeepSeek-Reasoner (R1)", "provider": "DeepSeek"},
    ]}


# ─── 启动 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
