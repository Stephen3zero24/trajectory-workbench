"""TrajectoryAgent — 壳层门面:生成 agent_run_id,透传底层 task_id 与状态。"""

import asyncio
import logging
import uuid

from trajectory_agent.dispatcher import SceneDispatcher

logger = logging.getLogger(__name__)


class TrajectoryAgent:
    def __init__(self, dispatcher: SceneDispatcher):
        self.dispatcher = dispatcher
        self._runs: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def dispatch_and_record(self, scene: str, params: dict) -> dict:
        """dispatch → 记录 agent_run_id 映射 → 返回结构化结果。

        A-2-4 submit 直接调用本方法;A-1 时代的 `submit(req)` 旧方法已废弃删除。
        """
        upstream = await self.dispatcher.dispatch(scene, params)
        scene_task_id = upstream.get("task_id") or upstream.get("scene_task_id") or ""
        if not scene_task_id:
            raise RuntimeError(f"upstream response missing task_id: {upstream!r}")

        agent_run_id = str(uuid.uuid4())
        async with self._lock:
            self._runs[agent_run_id] = {
                "scene": scene,
                "scene_task_id": scene_task_id,
            }

        logger.info(
            "dispatch_and_record scene=%s agent_run_id=%s scene_task_id=%s",
            scene, agent_run_id, scene_task_id,
        )
        return {
            "agent_run_id": agent_run_id,
            "scene": scene,
            "scene_task_id": scene_task_id,
            "status": upstream.get("status", "created"),
            "message": upstream.get("message"),
        }

    async def get_run(self, agent_run_id: str) -> dict | None:
        async with self._lock:
            mapping = self._runs.get(agent_run_id)
        if mapping is None:
            return None
        upstream = await self.dispatcher.get_status(
            mapping["scene"], mapping["scene_task_id"]
        )
        return {
            "agent_run_id": agent_run_id,
            "scene": mapping["scene"],
            "scene_task_id": mapping["scene_task_id"],
            "upstream": upstream,
        }
