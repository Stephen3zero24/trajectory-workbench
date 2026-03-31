"""
ToolACE API 路由 — 注册到 backend.py 的 FastAPI 应用

提供 ToolACE 场景的 HTTP 端点：
  POST /api/toolace/tasks       — 创建 ToolACE 任务
  GET  /api/toolace/tasks       — 列出所有 ToolACE 任务
  GET  /api/toolace/tasks/{id}  — 任务详情
  GET  /api/toolace/tasks/{id}/events — 事件流
  GET  /api/toolace/presets      — 获取预置源工具
"""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from .config import (
    ToolACEPipelineConfig,
    LLMConfig,
    PRESET_SOURCE_TOOLS,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .toolace_pipeline import run_toolace_pipeline


# ─── 全局状态 ─────────────────────────────────────────────────────────────────

toolace_tasks: dict = {}   # task_id -> ToolACETaskState
toolace_events: dict = {}  # task_id -> list[event]


# ─── 数据模型 ─────────────────────────────────────────────────────────────────

class ToolACETaskRequest(BaseModel):
    """创建 ToolACE 任务的请求"""
    source_tools: list = Field(default_factory=list, description="源工具列表（空则使用预置）")
    model: str = "deepseek-chat"
    temperature: float = 0.7
    expansion_count: int = 3
    coupling_rounds: int = 2
    task_count: int = 10
    enable_cross_group: bool = True
    enable_role_background: bool = True
    missing_param_ratio: float = 0.3
    max_turns: int = 15
    max_workers: int = 3
    quality_threshold: float = 0.80
    max_iterations: int = 3
    output_dir: str = "output/toolace"


class ToolACETaskState(BaseModel):
    """ToolACE 任务状态"""
    task_id: str
    config: dict
    status: str = "created"
    pipeline_result: Optional[dict] = None
    created_at: str = ""
    updated_at: str = ""


# ─── 事件辅助 ─────────────────────────────────────────────────────────────────

def add_toolace_event(task_id: str, event_type: str, message: str, data: dict = None):
    """记录事件"""
    if task_id not in toolace_events:
        toolace_events[task_id] = []
    toolace_events[task_id].append({
        "type": event_type,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    })


# ─── 后台执行 ─────────────────────────────────────────────────────────────────

async def run_toolace_task(task_id: str):
    """在后台执行 ToolACE Pipeline"""
    task_state = toolace_tasks.get(task_id)
    if not task_state:
        return

    task_state.status = "executing"
    task_state.updated_at = datetime.now().isoformat()

    cfg = task_state.config

    pipeline_config = ToolACEPipelineConfig(
        task_id=task_id,
        source_tools=cfg.get("source_tools") or PRESET_SOURCE_TOOLS,
        llm=LLMConfig(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            model=cfg.get("model", "deepseek-chat"),
            temperature=cfg.get("temperature", 0.7),
        ),
        expansion_count=cfg.get("expansion_count", 3),
        coupling_rounds=cfg.get("coupling_rounds", 2),
        task_count=cfg.get("task_count", 10),
        enable_cross_group=cfg.get("enable_cross_group", True),
        enable_role_background=cfg.get("enable_role_background", True),
        missing_param_ratio=cfg.get("missing_param_ratio", 0.3),
        max_turns=cfg.get("max_turns", 15),
        max_workers=cfg.get("max_workers", 3),
        quality_threshold=cfg.get("quality_threshold", 0.80),
        max_iterations=cfg.get("max_iterations", 3),
        output_dir=cfg.get("output_dir", "output/toolace"),
    )

    def emit(event_type, message, data=None):
        add_toolace_event(task_id, event_type, message, data)

    try:
        result = await run_toolace_pipeline(pipeline_config, emit)
        task_state.pipeline_result = result
        task_state.status = "completed"
        add_toolace_event(task_id, "completed", "ToolACE Pipeline 完成")
    except Exception as e:
        task_state.status = "failed"
        add_toolace_event(task_id, "error", f"Pipeline 执行失败: {e}")

    task_state.updated_at = datetime.now().isoformat()


# ─── 路由注册 ─────────────────────────────────────────────────────────────────

def register_toolace_routes(app):
    """注册 ToolACE 路由到 FastAPI 应用"""

    @app.post("/api/toolace/tasks")
    async def create_toolace_task(req: ToolACETaskRequest, background_tasks: BackgroundTasks):
        """创建 ToolACE 任务"""
        task_id = f"toolace_{uuid.uuid4().hex[:8]}"

        config = {
            "source_tools": req.source_tools,
            "model": req.model,
            "temperature": req.temperature,
            "expansion_count": req.expansion_count,
            "coupling_rounds": req.coupling_rounds,
            "task_count": req.task_count,
            "enable_cross_group": req.enable_cross_group,
            "enable_role_background": req.enable_role_background,
            "missing_param_ratio": req.missing_param_ratio,
            "max_turns": req.max_turns,
            "max_workers": req.max_workers,
            "quality_threshold": req.quality_threshold,
            "max_iterations": req.max_iterations,
            "output_dir": req.output_dir,
        }

        state = ToolACETaskState(
            task_id=task_id,
            config=config,
            status="created",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

        toolace_tasks[task_id] = state
        toolace_events[task_id] = []

        add_toolace_event(task_id, "task_created", f"ToolACE 任务已创建")

        background_tasks.add_task(run_toolace_task, task_id)

        return {"task_id": task_id, "status": "created"}

    @app.get("/api/toolace/tasks")
    async def list_toolace_tasks():
        """列出所有 ToolACE 任务"""
        result = []
        for tid, task in toolace_tasks.items():
            pr = task.pipeline_result or {}
            result.append({
                "task_id": tid,
                "status": task.status,
                "model": task.config.get("model", ""),
                "task_count": task.config.get("task_count", 0),
                "trajectories": pr.get("step3", {}).get("total_trajectories", 0),
                "avg_score": pr.get("step3", {}).get("avg_score", 0),
                "created_at": task.created_at,
            })
        return {"tasks": result}

    @app.get("/api/toolace/tasks/{task_id}")
    async def get_toolace_task(task_id: str):
        """获取 ToolACE 任务详情"""
        task = toolace_tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return {
            "task_id": task.task_id,
            "config": task.config,
            "status": task.status,
            "pipeline_result": task.pipeline_result,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    @app.get("/api/toolace/tasks/{task_id}/events")
    async def get_toolace_events(task_id: str, since: int = 0):
        """获取事件流"""
        events = toolace_events.get(task_id, [])
        return {"events": events[since:], "total": len(events)}

    @app.get("/api/toolace/presets")
    async def get_presets():
        """获取预置源工具列表"""
        return {
            "source_tools": PRESET_SOURCE_TOOLS,
            "role_backgrounds": [
                "技术运维工程师", "数据分析师", "产品经理",
                "客户服务主管", "市场营销专员", "研发负责人",
                "财务人员", "内容运营",
            ],
        }

    print("  ToolACE 场景: ✅ 路由已注册")
