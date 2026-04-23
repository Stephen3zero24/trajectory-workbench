"""Pydantic 模型 — trajectory_agent 的请求/响应体。"""

from pydantic import BaseModel


class SubmitRequest(BaseModel):
    user_request: str | None = None
    scene_hint: str | None = None
    params: dict = {}
    attachments: list | None = None


class SubmitResponse(BaseModel):
    agent_run_id: str
    scene: str
    scene_task_id: str
    status: str
    message: str | None = None
