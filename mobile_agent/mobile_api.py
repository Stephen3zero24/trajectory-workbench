"""
Mobile Agent API 路由 — 注册到 backend.py 的 FastAPI 应用

端点:
  POST /api/mobile/tasks            — 创建 Mobile Agent 轨迹合成任务
  GET  /api/mobile/tasks            — 列出所有任务
  GET  /api/mobile/tasks/{id}       — 任务详情
  GET  /api/mobile/tasks/{id}/events — 事件流
  POST /api/mobile/tasks/{id}/export — 导出数据集
  POST /api/mobile/upload-scenario  — 上传自定义场景
  GET  /api/mobile/builtin-scenarios — 获取内置场景列表
  DELETE /api/mobile/tasks/{id}     — 删除任务

使用:
    from mobile_agent.mobile_api import register_mobile_routes
    register_mobile_routes(app)
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import BackgroundTasks, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from .config import (
    MobileAgentPipelineConfig,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .mobile_pipeline import run_mobile_pipeline


# ─── 全局状态 ────────────────────────────────────────────────────────────────

mobile_tasks: dict = {}          # task_id -> MobileTaskState
mobile_events: dict = {}         # task_id -> list[event]
uploaded_scenarios: dict = {}     # upload_id -> scenario list


# ─── 请求模型 ────────────────────────────────────────────────────────────────

class MobileTaskRequest(BaseModel):
    """创建 Mobile Agent 任务的请求"""
    # 场景来源
    scenario_source: str = Field(
        default="builtin",
        description="场景来源: builtin(内置) | upload(上传) | local(本地文件)",
    )
    scenario_upload_id: Optional[str] = Field(
        default=None,
        description="上传场景的 ID (scenario_source=upload 时使用)",
    )
    scenario_path: Optional[str] = Field(
        default=None,
        description="本地场景文件路径 (scenario_source=local 时使用)",
    )
    scenario_filter_tags: Optional[List[str]] = Field(
        default=None,
        description="按标签筛选任务 (如 ['settings', 'easy'])",
    )

    # Agent 配置
    model: str = "deepseek-chat"
    temperature: float = Field(default=0.7, ge=0.0, le=1.5)
    max_steps: int = Field(default=20, ge=5, le=50)
    max_tasks: int = Field(default=0, ge=0, description="0=处理全部任务")
    enable_vision: bool = Field(default=True, description="是否发送截图给 VLM")
    enable_ui_tree: bool = Field(default=True, description="是否获取 UI hierarchy")

    # 沙箱配置
    wait_after_action: float = Field(default=1.5, ge=0.5, le=5.0)

    # 迭代配置
    max_iterations: int = Field(default=1, ge=1, le=5)
    quality_threshold: float = Field(default=0.70, ge=0.0, le=1.0)


# ─── 辅助函数 ────────────────────────────────────────────────────────────────

def _add_event(task_id: str, event_type: str, message: str, data: dict = None):
    if task_id not in mobile_events:
        mobile_events[task_id] = []
    mobile_events[task_id].append({
        "type": event_type,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    })


async def _run_mobile_task(task_id: str, config: MobileAgentPipelineConfig, scenario_content: list):
    """后台执行 Mobile Agent Pipeline"""
    mobile_tasks[task_id]["status"] = "running"

    def event_cb(t, m, d=None):
        _add_event(task_id, t, m, d)
        if t.endswith("_start"):
            mobile_tasks[task_id]["current_step"] = t.replace("_start", "")

    try:
        summary = await run_mobile_pipeline(
            config=config,
            scenario_content=scenario_content,
            event_callback=event_cb,
        )

        mobile_tasks[task_id]["status"] = summary.get("status", "completed")
        mobile_tasks[task_id]["summary"] = summary

    except Exception as e:
        mobile_tasks[task_id]["status"] = "failed"
        mobile_tasks[task_id]["error"] = str(e)
        _add_event(task_id, "error", f"Pipeline 失败: {str(e)}")


# ─── 路由注册 ────────────────────────────────────────────────────────────────

def register_mobile_routes(app):
    """注册 Mobile Agent API 路由到 FastAPI 应用"""

    # ── 获取内置场景 ──
    @app.get("/api/mobile/builtin-scenarios")
    async def get_builtin_scenarios():
        """获取内置场景列表"""
        builtin_path = os.path.join(
            os.path.dirname(__file__), "mobile_scenarios.json"
        )
        try:
            with open(builtin_path, "r", encoding="utf-8") as f:
                scenarios = json.load(f)
            # 提取摘要
            summary = []
            for s in scenarios:
                summary.append({
                    "task_id": s.get("task_id", ""),
                    "task_desc": s.get("task_desc", ""),
                    "app_package": s.get("app_package", ""),
                    "tags": s.get("tags", []),
                    "max_steps": s.get("max_steps", 0),
                    "check_type": s.get("check_type", "visual"),
                })
            return {
                "total": len(summary),
                "scenarios": summary,
                "tags": sorted(set(
                    tag for s in scenarios for tag in s.get("tags", [])
                )),
            }
        except FileNotFoundError:
            return {"total": 0, "scenarios": [], "tags": []}

    # ── 上传自定义场景 ──
    @app.post("/api/mobile/upload-scenario")
    async def upload_mobile_scenario(file: UploadFile = File(...)):
        """上传自定义场景 JSON 文件"""
        upload_id = f"mobile_scene_{uuid.uuid4().hex[:8]}"

        try:
            content = json.loads(await file.read())
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")

        # 验证格式
        if isinstance(content, dict):
            content = content.get("tasks", content.get("scenarios", [content]))
        if not isinstance(content, list):
            raise HTTPException(400, "场景文件应为任务列表 (JSON Array)")

        uploaded_scenarios[upload_id] = content

        return {
            "upload_id": upload_id,
            "task_count": len(content),
            "filename": file.filename,
            "tags": sorted(set(
                tag for s in content for tag in s.get("tags", [])
            )),
        }

    # ── 通过 JSON body 上传场景 ──
    @app.post("/api/mobile/upload-scenario-json")
    async def upload_mobile_scenario_json(body: dict):
        upload_id = f"mobile_scene_{uuid.uuid4().hex[:8]}"
        scenarios = body.get("scenarios", body.get("tasks", []))
        if not scenarios:
            raise HTTPException(400, "请提供 scenarios 或 tasks 列表")
        uploaded_scenarios[upload_id] = scenarios
        return {"upload_id": upload_id, "task_count": len(scenarios)}

    # ── 创建任务 ──
    @app.post("/api/mobile/tasks")
    async def create_mobile_task(
        req: MobileTaskRequest,
        bg: BackgroundTasks,
    ):
        """创建 Mobile Agent 轨迹合成任务"""
        task_id = f"mobile_{uuid.uuid4().hex[:8]}"

        # 获取场景内容
        scenario_content = None
        if req.scenario_source == "upload" and req.scenario_upload_id:
            scenario_content = uploaded_scenarios.get(req.scenario_upload_id)
            if not scenario_content:
                raise HTTPException(400, f"场景 {req.scenario_upload_id} 不存在或已过期")

        # 构建配置
        config = MobileAgentPipelineConfig(
            task_id=task_id,
            scenario_source=req.scenario_source,
            scenario_path=req.scenario_path or "",
            scenario_filter_tags=req.scenario_filter_tags or [],
            agent_model=req.model,
            agent_temperature=req.temperature,
            max_steps=req.max_steps,
            max_tasks=req.max_tasks,
            enable_vision=req.enable_vision,
            enable_ui_tree=req.enable_ui_tree,
            wait_after_action=req.wait_after_action,
            max_iterations=req.max_iterations,
            quality_threshold=req.quality_threshold,
            deepseek_api_key=DEEPSEEK_API_KEY,
            deepseek_base_url=DEEPSEEK_BASE_URL,
            output_dir=f"output/mobile_agent/{task_id}",
        )

        # 初始化任务状态
        mobile_tasks[task_id] = {
            "task_id": task_id,
            "status": "created",
            "config": config.to_dict(),
            "current_step": "init",
            "summary": {},
            "error": None,
            "created_at": datetime.now().isoformat(),
        }
        mobile_events[task_id] = []

        _add_event(task_id, "created", "Mobile Agent 任务已创建")

        # 后台启动
        bg.add_task(_run_mobile_task, task_id, config, scenario_content)

        return {"task_id": task_id, "status": "created"}

    # ── 列出任务 ──
    @app.get("/api/mobile/tasks")
    async def list_mobile_tasks():
        return {
            "tasks": [
                {
                    "task_id": k,
                    "status": v.get("status"),
                    "current_step": v.get("current_step", ""),
                    "created_at": v.get("created_at", ""),
                    "summary": v.get("summary", {}),
                }
                for k, v in mobile_tasks.items()
            ]
        }

    # ── 任务详情 ──
    @app.get("/api/mobile/tasks/{task_id}")
    async def get_mobile_task(task_id: str):
        state = mobile_tasks.get(task_id)
        if not state:
            raise HTTPException(404, "Task not found")
        return state

    # ── 事件流 ──
    @app.get("/api/mobile/tasks/{task_id}/events")
    async def get_mobile_events(task_id: str, since: int = 0):
        events = mobile_events.get(task_id, [])
        return {"events": events[since:], "total": len(events)}

    # ── 导出 ──
    @app.post("/api/mobile/tasks/{task_id}/export")
    async def export_mobile_task(task_id: str):
        state = mobile_tasks.get(task_id)
        if not state:
            raise HTTPException(404, "Task not found")
        summary = state.get("summary", {})
        if not summary or summary.get("status") != "completed":
            raise HTTPException(400, "任务尚未完成")
        return {
            "status": "exported",
            "export": summary.get("export", {}),
            "output_dir": summary.get("output_dir", ""),
        }

    # ── 删除任务 ──
    @app.delete("/api/mobile/tasks/{task_id}")
    async def delete_mobile_task(task_id: str):
        mobile_tasks.pop(task_id, None)
        mobile_events.pop(task_id, None)
        return {"status": "deleted"}
