"""SceneDispatcher — 通过 HTTP 自调用 localhost 现有端点路由到场景 Pipeline。

A-1 设计选择:HTTP 自调用(非直调场景内部函数),保持模块边界清晰,
避免与现有 backend.py 路由层耦合。
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


SUPPORTED_SCENES = {"search2qa", "toolace", "toucan", "envscaler", "mobile_agent"}
IMPLEMENTED_SCENES = {"search2qa", "toolace", "toucan"}


class SceneNotImplementedError(Exception):
    """scene_hint 合法但在 A-1 阶段未接通(envscaler / mobile_agent)。"""


class DispatcherHTTPError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class SceneDispatcher:
    """把请求分发给场景 Pipeline 的现有 HTTP 端点。"""

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (
            base_url
            or os.environ.get("TRAJECTORY_BACKEND_URL", "http://127.0.0.1:3100")
        ).rstrip("/")
        self.timeout = timeout

    def _submit_path(self, scene: str) -> str:
        if scene == "search2qa":
            return "/api/tasks"
        if scene == "toolace":
            return "/api/toolace/tasks"
        if scene == "toucan":
            return "/api/toucan/tasks"
        raise SceneNotImplementedError(
            f"scene_hint '{scene}' is not implemented in A-1, "
            "will be supported in later stages"
        )

    def _status_path(self, scene: str, scene_task_id: str) -> str:
        if scene == "search2qa":
            return f"/api/tasks/{scene_task_id}"
        if scene in ("toolace", "toucan"):
            return f"/api/{scene}/tasks/{scene_task_id}"
        raise SceneNotImplementedError(
            f"scene_hint '{scene}' is not implemented in A-1, "
            "will be supported in later stages"
        )

    async def dispatch(self, scene: str, params: dict) -> dict:
        path = self._submit_path(scene)
        body = dict(params or {})
        if scene == "search2qa":
            body.setdefault("scene_type", "search2qa")

        url = f"{self.base_url}{path}"
        logger.info("dispatch scene=%s url=%s", scene, url)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=body)
        return _unwrap(resp)

    async def get_status(self, scene: str, scene_task_id: str) -> dict:
        path = self._status_path(scene, scene_task_id)
        url = f"{self.base_url}{path}"
        logger.info("get_status scene=%s url=%s", scene, url)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url)
        return _unwrap(resp)


def _unwrap(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        raise DispatcherHTTPError(resp.status_code, resp.text[:500])
    data = resp.json()
    if not isinstance(data, dict):
        raise DispatcherHTTPError(502, f"unexpected upstream body: {data!r}")
    return data
