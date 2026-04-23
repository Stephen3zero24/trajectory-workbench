"""FastAPI 路由 — /api/trajectory-agent/*。"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from trajectory_agent.agent import TrajectoryAgent
from trajectory_agent.dispatcher import (
    SUPPORTED_SCENES,
    DispatcherHTTPError,
    SceneDispatcher,
    SceneNotImplementedError,
)
from trajectory_agent.schemas import SubmitRequest, SubmitResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trajectory-agent", tags=["trajectory-agent"])

_agent_singleton: TrajectoryAgent | None = None


def get_agent() -> TrajectoryAgent:
    global _agent_singleton
    if _agent_singleton is None:
        _agent_singleton = TrajectoryAgent(SceneDispatcher())
    return _agent_singleton


@router.post("/submit", response_model=SubmitResponse)
async def submit(
    req: SubmitRequest, agent: TrajectoryAgent = Depends(get_agent)
) -> SubmitResponse:
    if not req.scene_hint:
        raise HTTPException(
            status_code=400,
            detail="scene_hint is required (one of: "
            + ", ".join(sorted(SUPPORTED_SCENES))
            + ")",
        )
    if req.scene_hint not in SUPPORTED_SCENES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"scene_hint '{req.scene_hint}' is not a valid scene "
                f"(expected one of: {', '.join(sorted(SUPPORTED_SCENES))})"
            ),
        )
    try:
        return await agent.submit(req)
    except SceneNotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except DispatcherHTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"upstream returned {e.status_code}: {e.detail[:200]}",
        )


@router.get("/runs/{agent_run_id}")
async def get_run(
    agent_run_id: str, agent: TrajectoryAgent = Depends(get_agent)
) -> dict:
    try:
        result = await agent.get_run(agent_run_id)
    except SceneNotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except DispatcherHTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"upstream returned {e.status_code}: {e.detail[:200]}",
        )
    if result is None:
        raise HTTPException(status_code=404, detail="agent_run_id not found")
    return result
