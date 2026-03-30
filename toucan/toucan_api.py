"""
Toucan 后端 API 路由 — 集成到现有 FastAPI backend.py

使用: 在 backend.py 中添加
    from toucan.toucan_api import register_toucan_routes
    register_toucan_routes(app)
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .config import ToucanPipelineConfig, SmitheryConfig, LLMConfig, MCPServerRegistry
from .toucan_pipeline import run_toucan_pipeline


class ToucanTaskRequest(BaseModel):
    question_count: int = Field(default=10, ge=1, le=200)
    sampling_strategy: str = "uniform"
    server_mode: str = "single"
    max_tools_per_question: int = Field(default=3, ge=1, le=10)
    quality_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_steps: int = Field(default=15, ge=5, le=30)
    enable_multi_turn: bool = False
    multi_turn_max_rounds: int = Field(default=3, ge=1, le=10)
    model: str = "deepseek-chat"
    temperature: float = Field(default=0.7, ge=0.0, le=1.5)
    max_iterations: int = Field(default=1, ge=1, le=5)
    server_ids: Optional[list] = None


toucan_tasks: dict = {}
toucan_events: dict = {}


def _add_event(task_id, etype, msg, data=None):
    if task_id not in toucan_events:
        toucan_events[task_id] = []
    toucan_events[task_id].append({"type": etype, "message": msg, "data": data or {}, "timestamp": datetime.now().isoformat()})


async def _run_task(task_id, config):
    toucan_tasks[task_id]["status"] = "running"
    def cb(t, m, d=None):
        _add_event(task_id, t, m, d)
        if t.endswith("_start"): toucan_tasks[task_id]["current_step"] = t.replace("_start","")
    try:
        summary = await run_toucan_pipeline(config, cb)
        toucan_tasks[task_id]["status"] = summary.get("status", "completed")
        toucan_tasks[task_id]["summary"] = summary
    except Exception as e:
        toucan_tasks[task_id]["status"] = "failed"
        toucan_tasks[task_id]["error"] = str(e)
        _add_event(task_id, "error", str(e))


def register_toucan_routes(app):
    """注册 Toucan API 路由"""

    @app.post("/api/toucan/tasks")
    async def create_toucan_task(req: ToucanTaskRequest, bg: BackgroundTasks):
        task_id = f"toucan_{uuid.uuid4().hex[:8]}"
        llm = LLMConfig(model=req.model, temperature=req.temperature)
        config = ToucanPipelineConfig(
            task_id=task_id, smithery=SmitheryConfig(),
            question_count=req.question_count, sampling_strategy=req.sampling_strategy,
            server_mode=req.server_mode, max_tools_per_question=req.max_tools_per_question,
            quality_threshold=req.quality_threshold,
            question_llm=llm, quality_llm=LLMConfig(model=req.model, temperature=0.3),
            max_steps=req.max_steps, enable_multi_turn=req.enable_multi_turn,
            multi_turn_max_rounds=req.multi_turn_max_rounds,
            max_iterations=req.max_iterations,
            output_dir=f"output/toucan/{task_id}",
        )
        toucan_tasks[task_id] = {"task_id": task_id, "status": "created", "config": config.to_dict(), "current_step": "init", "summary": {}, "created_at": datetime.now().isoformat()}
        toucan_events[task_id] = []
        _add_event(task_id, "created", f"Toucan 任务已创建")
        bg.add_task(_run_task, task_id, config)
        return {"task_id": task_id, "status": "created"}

    @app.get("/api/toucan/tasks")
    async def list_toucan_tasks():
        return {"tasks": [{"task_id": k, "status": v.get("status"), "current_step": v.get("current_step",""), "created_at": v.get("created_at",""), "summary": v.get("summary",{})} for k, v in toucan_tasks.items()]}

    @app.get("/api/toucan/tasks/{task_id}")
    async def get_toucan_task(task_id: str):
        s = toucan_tasks.get(task_id)
        if not s: raise HTTPException(404)
        return s

    @app.get("/api/toucan/tasks/{task_id}/events")
    async def get_toucan_events(task_id: str, since: int = 0):
        evts = toucan_events.get(task_id, [])
        return {"events": evts[since:], "total": len(evts)}

    @app.get("/api/toucan/servers")
    async def list_mcp_servers():
        rp = "toucan/mcp_servers/registry.json"
        if os.path.exists(rp):
            reg = MCPServerRegistry.load(rp)
            return {"servers": [{"server_id": s.server_id, "name": s.name, "description": s.description, "category": s.category, "tool_count": len(s.tools), "tools": [{"name": t["name"], "description": t.get("description","")} for t in s.tools]} for s in reg.list_servers()]}
        return {"servers": [], "message": "注册表未初始化"}

    @app.post("/api/toucan/servers/refresh")
    async def refresh_servers(bg: BackgroundTasks):
        async def _r():
            from .step0_smithery_setup import SmitherySetup
            s = SmitherySetup(SmitheryConfig())
            reg = await s.build_registry()
            await s.save_registry(reg, "toucan/mcp_servers/registry.json")
        bg.add_task(_r)
        return {"status": "refreshing"}

    @app.delete("/api/toucan/tasks/{task_id}")
    async def delete_toucan_task(task_id: str):
        toucan_tasks.pop(task_id, None)
        toucan_events.pop(task_id, None)
        return {"status": "deleted"}

    # 覆盖场景列表，添加 Toucan
    @app.get("/api/scenes")
    async def scenes_with_toucan():
        return {"scenes": [
            {"id": "toucan_tool_call", "name": "Toucan-工具调用", "desc": "基于Toucan的MCP工具调用轨迹合成", "icon": "🦤"},
            {"id": "mcp_tool", "name": "MCP工具交互", "desc": "Agent Harness的MCP交互", "icon": "⚙️"},
            {"id": "gui", "name": "GUI操作", "desc": "浏览器/安卓GUI操控", "icon": "🖥️"},
            {"id": "deep_search", "name": "Deep Search", "desc": "搜索引擎检索", "icon": "🔍"},
            {"id": "multi_agent", "name": "多Agent协调", "desc": "多智能体协作", "icon": "🤖"},
            {"id": "code_exec", "name": "代码执行", "desc": "代码编写测试执行", "icon": "💻"},
        ]}
