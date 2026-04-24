"""FastAPI 路由 — /api/trajectory-agent/*。

A-2-4 改造:submit 端点整合 scene_router + clarifier,返回四态响应:
- dispatched / clarify_needed / scene_unrecognized 返回 200
- error 返回 500,带 error_code 枚举

A-1 的 GET /runs/{agent_run_id} 端点不变。
"""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from trajectory_agent.agent import TrajectoryAgent
from trajectory_agent.clarifier import ClarifierInvalid, clarify
from trajectory_agent.dispatcher import (
    DispatcherHTTPError,
    SceneDispatcher,
    SceneNotImplementedError,
)
from trajectory_agent.llm_client import (
    LLMClient,
    LLMProxyBadRequest,
    LLMProxyUnavailable,
    LLMResponseInvalid,
)
from trajectory_agent.scene_router import (
    SUPPORTED_SCENES,
    SceneRoutingInvalid,
    route_scene,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/trajectory-agent", tags=["trajectory-agent"])

_agent_singleton: TrajectoryAgent | None = None
_llm_client_singleton: LLMClient | None = None


def get_agent() -> TrajectoryAgent:
    global _agent_singleton
    if _agent_singleton is None:
        _agent_singleton = TrajectoryAgent(SceneDispatcher())
    return _agent_singleton


def get_llm_client() -> LLMClient:
    global _llm_client_singleton
    if _llm_client_singleton is None:
        _llm_client_singleton = LLMClient()
    return _llm_client_singleton


class SubmitRequest(BaseModel):
    user_request: str = Field(..., description="用户自然语言需求(必填)")
    scene_hint: str | None = Field(
        default=None,
        description="显式场景;传了则跳过 scene_router,但仍走 clarifier",
    )


class SubmitResponse(BaseModel):
    status: Literal["dispatched", "clarify_needed", "scene_unrecognized", "error"]
    scene: str | None = None
    agent_run_id: str | None = None
    scene_task_id: str | None = None
    upstream_status: str | None = None
    questions: list[dict] | None = None
    reasoning: str | None = None
    confidence: float | None = None
    error_code: str | None = None
    message: str | None = None


def _error_response(code: str, message: str, *, scene: str | None = None) -> JSONResponse:
    body = SubmitResponse(
        status="error", error_code=code, message=message, scene=scene
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=500, content=body)


@router.post(
    "/submit",
    response_model=SubmitResponse,
    response_model_exclude_none=True,
)
async def submit(
    req: SubmitRequest,
    agent: TrajectoryAgent = Depends(get_agent),
    llm: LLMClient = Depends(get_llm_client),
) -> Any:
    if not req.user_request or not req.user_request.strip():
        raise HTTPException(
            status_code=400, detail="user_request 不能为空"
        )

    logger.info(
        "submit enter user_request=%r scene_hint=%s",
        req.user_request[:80],
        req.scene_hint,
    )

    try:
        # ─── Step 1: 场景决定 ───────────────────────────────────────────
        if req.scene_hint is not None:
            if req.scene_hint not in SUPPORTED_SCENES:
                logger.info(
                    "submit exit status=scene_unrecognized reason=invalid_scene_hint hint=%s",
                    req.scene_hint,
                )
                return SubmitResponse(
                    status="scene_unrecognized",
                    message=(
                        f"指定的场景 {req.scene_hint!r} 不受支持,"
                        f"当前可用场景:{sorted(SUPPORTED_SCENES)}"
                    ),
                )
            scene = req.scene_hint
            logger.info("submit step1 scene=%s source=scene_hint", scene)
        else:
            decision = await route_scene(req.user_request, llm)
            if decision.low_confidence:
                logger.info(
                    "submit exit status=scene_unrecognized low_confidence scene=%s conf=%.2f",
                    decision.scene,
                    decision.confidence,
                )
                return SubmitResponse(
                    status="scene_unrecognized",
                    reasoning=decision.reasoning,
                    confidence=decision.confidence,
                )
            scene = decision.scene
            logger.info(
                "submit step1 scene=%s source=router conf=%.2f",
                scene,
                decision.confidence,
            )

        # ─── Step 2: 参数澄清 ───────────────────────────────────────────
        outcome = await clarify(req.user_request, scene, llm)
        if outcome.status == "questions":
            logger.info(
                "submit exit status=clarify_needed scene=%s missing=%s",
                scene,
                [q.get("field") for q in outcome.questions],
            )
            return SubmitResponse(
                status="clarify_needed",
                scene=scene,
                questions=outcome.questions,
            )

        # ─── Step 3: dispatch ───────────────────────────────────────────
        try:
            result = await agent.dispatch_and_record(scene, outcome.params)
        except (DispatcherHTTPError, SceneNotImplementedError, RuntimeError) as e:
            logger.exception("dispatch failed scene=%s", scene)
            return _error_response(
                "DISPATCH_FAILED", str(e), scene=scene
            )

        logger.info(
            "submit exit status=dispatched scene=%s agent_run_id=%s",
            scene,
            result["agent_run_id"],
        )
        return SubmitResponse(
            status="dispatched",
            scene=result["scene"],
            agent_run_id=result["agent_run_id"],
            scene_task_id=result["scene_task_id"],
            upstream_status=result.get("status"),
            message=result.get("message"),
        )

    except LLMProxyUnavailable as e:
        logger.exception("LLMProxyUnavailable")
        return _error_response("LLM_PROXY_UNAVAILABLE", str(e))
    except LLMProxyBadRequest as e:
        logger.exception("LLMProxyBadRequest")
        return _error_response("LLM_BAD_REQUEST", str(e))
    except LLMResponseInvalid as e:
        logger.exception("LLMResponseInvalid")
        return _error_response("LLM_RESPONSE_INVALID", str(e))
    except SceneRoutingInvalid as e:
        logger.exception("SceneRoutingInvalid")
        return _error_response("SCENE_ROUTING_INVALID", str(e))
    except ClarifierInvalid as e:
        logger.exception("ClarifierInvalid")
        return _error_response("CLARIFIER_INVALID", str(e))
    except HTTPException:
        # FastAPI 的 HTTPException 走默认处理,不吞
        raise
    except Exception as e:
        logger.exception("internal error in submit")
        return _error_response("INTERNAL_ERROR", str(e))


@router.get("/runs/{agent_run_id}")
async def get_run(
    agent_run_id: str, agent: TrajectoryAgent = Depends(get_agent)
) -> dict:
    """A-1 遗留端点,未改动。"""
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
