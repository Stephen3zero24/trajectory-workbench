"""
EnvScaler API 路由 — 注册到 backend.py 的 FastAPI 应用

端点:
  POST /api/envscaler/tasks           — 创建 EnvScaler 轨迹合成任务
  GET  /api/envscaler/tasks           — 列出所有 EnvScaler 任务
  GET  /api/envscaler/tasks/{id}      — 任务详情
  GET  /api/envscaler/tasks/{id}/events — 事件流
  POST /api/envscaler/tasks/{id}/export — 导出数据集
  POST /api/envscaler/upload-scene    — 上传场景文件
  DELETE /api/envscaler/tasks/{id}    — 删除任务

使用:
    from envscaler.envscaler_api import register_envscaler_routes
    register_envscaler_routes(app)
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from .config import EnvScalerPipelineConfig, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from .envscaler_pipeline import run_envscaler_pipeline


# ─── 全局状态 ────────────────────────────────────────────────────────────────

envscaler_tasks: dict = {}    # task_id -> EnvScalerTaskState
envscaler_events: dict = {}   # task_id -> list[event]
uploaded_scenes: dict = {}     # upload_id -> {"scenario": dict, "metadata": dict}


# ─── 请求模型 ────────────────────────────────────────────────────────────────

class EnvScalerTaskRequest(BaseModel):
    """创建 EnvScaler 任务的请求"""
    # 场景来源
    scene_source: str = Field(
        default="upload",
        description="场景来源: upload(上传) | local(本地目录)",
    )
    scene_upload_id: Optional[str] = Field(
        default=None,
        description="上传场景的 ID（scene_source=upload 时使用）",
    )
    scene_dir: Optional[str] = Field(
        default=None,
        description="本地场景目录（scene_source=local 时使用）",
    )

    # Agent 配置
    model: str = "deepseek-chat"
    temperature: float = Field(default=0.7, ge=0.0, le=1.5)
    max_steps: int = Field(default=20, ge=5, le=50)
    max_tasks: int = Field(default=0, ge=0, description="0=处理全部任务")

    # MCP 配置
    mcp_port: int = Field(default=8888, ge=1024, le=65535)

    # 迭代配置
    max_iterations: int = Field(default=1, ge=1, le=5)
    quality_threshold: float = Field(default=0.70, ge=0.0, le=1.0)


# ─── 辅助函数 ────────────────────────────────────────────────────────────────

def _add_event(task_id: str, event_type: str, message: str, data: dict = None):
    """添加事件"""
    if task_id not in envscaler_events:
        envscaler_events[task_id] = []
    envscaler_events[task_id].append({
        "type": event_type,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    })


async def _run_envscaler_task(task_id: str, config: EnvScalerPipelineConfig, scene_content: dict):
    """后台执行 EnvScaler Pipeline"""
    envscaler_tasks[task_id]["status"] = "running"

    def event_cb(t, m, d=None):
        _add_event(task_id, t, m, d)
        # 更新 current_step
        if t.endswith("_start"):
            envscaler_tasks[task_id]["current_step"] = t.replace("_start", "")

    try:
        summary = await run_envscaler_pipeline(
            config=config,
            scene_files_content=scene_content if scene_content else None,
            event_callback=event_cb,
        )

        envscaler_tasks[task_id]["status"] = summary.get("status", "completed")
        envscaler_tasks[task_id]["summary"] = summary

    except Exception as e:
        envscaler_tasks[task_id]["status"] = "failed"
        envscaler_tasks[task_id]["error"] = str(e)
        _add_event(task_id, "error", f"Pipeline 失败: {str(e)}")


# ─── 路由注册 ────────────────────────────────────────────────────────────────

def register_envscaler_routes(app):
    """注册 EnvScaler API 路由到 FastAPI 应用"""

    # ── 上传场景文件 ──
    @app.post("/api/envscaler/upload-scene")
    async def upload_scene_files(
        scenario_file: UploadFile = File(..., description="env_scenario.json"),
        metadata_file: UploadFile = File(..., description="filtered_env_metadata.json"),
    ):
        """上传场景文件（两个 JSON 文件）"""
        upload_id = f"scene_{uuid.uuid4().hex[:8]}"

        try:
            scenario_content = json.loads(await scenario_file.read())
            metadata_content = json.loads(await metadata_file.read())
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")

        uploaded_scenes[upload_id] = {
            "scenario": scenario_content,
            "metadata": metadata_content,
            "uploaded_at": datetime.now().isoformat(),
        }

        # 提取摘要信息
        env_name = "unknown"
        if isinstance(metadata_content, list) and metadata_content:
            env_name = metadata_content[0].get("env_name", metadata_content[0].get("name", "unknown"))
        elif isinstance(metadata_content, dict):
            env_name = metadata_content.get("env_name", metadata_content.get("name", "unknown"))

        task_count = 0
        if isinstance(scenario_content, list):
            task_count = len(scenario_content)
        elif isinstance(scenario_content, dict):
            tasks = scenario_content.get("tasks", scenario_content.get("scenarios", []))
            task_count = len(tasks) if isinstance(tasks, list) else 1

        return {
            "upload_id": upload_id,
            "env_name": env_name,
            "task_count": task_count,
            "scenario_file": scenario_file.filename,
            "metadata_file": metadata_file.filename,
        }

    # ── 通过 JSON body 上传场景（前端 AJAX 友好） ──
    @app.post("/api/envscaler/upload-scene-json")
    async def upload_scene_json(body: dict):
        """通过 JSON body 上传场景数据"""
        upload_id = f"scene_{uuid.uuid4().hex[:8]}"

        scenario = body.get("scenario", {})
        metadata = body.get("metadata", {})

        if not scenario or not metadata:
            raise HTTPException(400, "请提供 scenario 和 metadata 数据")

        uploaded_scenes[upload_id] = {
            "scenario": scenario,
            "metadata": metadata,
            "uploaded_at": datetime.now().isoformat(),
        }

        return {"upload_id": upload_id}

    # ── 创建任务 ──
    @app.post("/api/envscaler/tasks")
    async def create_envscaler_task(
        req: EnvScalerTaskRequest,
        bg: BackgroundTasks,
    ):
        """创建 EnvScaler 轨迹合成任务"""
        task_id = f"envscaler_{uuid.uuid4().hex[:8]}"

        # 获取场景内容
        scene_content = None
        if req.scene_source == "upload" and req.scene_upload_id:
            scene_data = uploaded_scenes.get(req.scene_upload_id)
            if not scene_data:
                raise HTTPException(400, f"场景 {req.scene_upload_id} 不存在或已过期")
            scene_content = scene_data

        # 构建配置
        config = EnvScalerPipelineConfig(
            task_id=task_id,
            scene_source=req.scene_source,
            scene_dir=req.scene_dir or "",
            agent_model=req.model,
            agent_temperature=req.temperature,
            max_steps=req.max_steps,
            max_tasks=req.max_tasks,
            mcp_port=req.mcp_port,
            max_iterations=req.max_iterations,
            quality_threshold=req.quality_threshold,
            deepseek_api_key=DEEPSEEK_API_KEY,
            deepseek_base_url=DEEPSEEK_BASE_URL,
            output_dir=f"output/envscaler/{task_id}",
        )

        # 初始化任务状态
        envscaler_tasks[task_id] = {
            "task_id": task_id,
            "status": "created",
            "config": config.to_dict(),
            "current_step": "init",
            "summary": {},
            "error": None,
            "created_at": datetime.now().isoformat(),
        }
        envscaler_events[task_id] = []

        _add_event(task_id, "created", "EnvScaler 任务已创建")

        # 后台启动
        bg.add_task(_run_envscaler_task, task_id, config, scene_content)

        return {"task_id": task_id, "status": "created"}

    # ── 列出任务 ──
    @app.get("/api/envscaler/tasks")
    async def list_envscaler_tasks():
        """获取所有 EnvScaler 任务列表"""
        return {
            "tasks": [
                {
                    "task_id": k,
                    "status": v.get("status"),
                    "current_step": v.get("current_step", ""),
                    "created_at": v.get("created_at", ""),
                    "summary": v.get("summary", {}),
                }
                for k, v in envscaler_tasks.items()
            ]
        }

    # ── 任务详情 ──
    @app.get("/api/envscaler/tasks/{task_id}")
    async def get_envscaler_task(task_id: str):
        """获取 EnvScaler 任务详情"""
        state = envscaler_tasks.get(task_id)
        if not state:
            raise HTTPException(404, "Task not found")
        return state

    # ── 事件流 ──
    @app.get("/api/envscaler/tasks/{task_id}/events")
    async def get_envscaler_events(task_id: str, since: int = 0):
        """获取任务事件流（前端轮询用）"""
        events = envscaler_events.get(task_id, [])
        return {"events": events[since:], "total": len(events)}

    # ── 导出 ──
    @app.post("/api/envscaler/tasks/{task_id}/export")
    async def export_envscaler_task(task_id: str):
        """导出任务数据"""
        state = envscaler_tasks.get(task_id)
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
    @app.delete("/api/envscaler/tasks/{task_id}")
    async def delete_envscaler_task(task_id: str):
        """删除 EnvScaler 任务"""
        envscaler_tasks.pop(task_id, None)
        envscaler_events.pop(task_id, None)
        return {"status": "deleted"}
