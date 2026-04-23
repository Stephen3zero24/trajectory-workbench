"""TrajectoryAgent — 壳层门面:生成 agent_run_id,透传底层 task_id 与状态。"""

import asyncio
import logging
import uuid

from trajectory_agent.dispatcher import SceneDispatcher
from trajectory_agent.schemas import SubmitRequest, SubmitResponse

logger = logging.getLogger(__name__)


class TrajectoryAgent:
    def __init__(self, dispatcher: SceneDispatcher):
        self.dispatcher = dispatcher
        self._runs: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def submit(self, req: SubmitRequest) -> SubmitResponse:
        # router 层已校验 scene_hint 非空且为合法枚举值,此处不再兜底
        assert req.scene_hint, "scene_hint must be validated by router before reaching agent"
        upstream = await self.dispatcher.dispatch(req.scene_hint, req.params or {})

        scene_task_id = upstream.get("task_id") or upstream.get("scene_task_id") or ""
        if not scene_task_id:
            raise RuntimeError(f"upstream response missing task_id: {upstream!r}")

        agent_run_id = str(uuid.uuid4())
        async with self._lock:
            self._runs[agent_run_id] = {
                "scene": req.scene_hint,
                "scene_task_id": scene_task_id,
            }

        logger.info(
            "submit scene=%s agent_run_id=%s scene_task_id=%s",
            req.scene_hint, agent_run_id, scene_task_id,
        )
        return SubmitResponse(
            agent_run_id=agent_run_id,
            scene=req.scene_hint,
            scene_task_id=scene_task_id,
            status=upstream.get("status", "created"),
            message=upstream.get("message"),
        )

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
