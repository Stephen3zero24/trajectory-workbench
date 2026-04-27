"""Skill Manifest Loader — 启动期从平台 /api/skills 拉 manifest 喂给 scene_router / clarifier。

数据流(T2-1 落地形态):
    平台 skills/<dir>/manifest.json
        ↓  Next.js GET /api/skills 扫盘拼装(顶层 {"skills": [...]} dict)
        ↓  本模块 load_manifests() 启动期一次性 HTTP 拉取
        ↓  过滤 sig_owner=="trajectory-synthesis" + status=="active"
        ↓  strip "trajectory-" 前缀 → 裸 scene_id
        ↓  module-level 缓存 _CATALOG: dict[scene_id, SceneCatalogEntry]
        ↓  scene_router.get_loaded_catalog() / get_supported_scenes() 统一入口

哲学:fail-fast。
- 启动期拉不到 manifest(平台离线 / 5xx / 非 JSON / 0 个 active)→ 抛异常,
  uvicorn lifespan 不捕获即进程退出。**不留半启动状态**。
- 运行期任何模块在 catalog 未加载时调 get_loaded_catalog → 抛 SkillManifestNotLoaded,
  这只在测试或异常代码路径里可能出现。

缓存策略:启动期一次,manifest 变更需重启进程。本阶段不做热重载、不做 TTL、
不做后台轮询 —— 平台 manifest 是低频变更资产,重启成本可接受。

加载时机:FastAPI lifespan startup 阶段调 load_manifests(),不在 import 期触发,
保证无 :3000 也能 import 本模块(测试场景必要)。
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


DEFAULT_PLATFORM_URL = "http://127.0.0.1:3000"
SKILL_OWNER = "trajectory-synthesis"
SKILL_ID_PREFIX = "trajectory-"
HTTP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class SceneCatalogEntry:
    scene_id: str
    description: str


class SkillManifestUnavailable(Exception):
    """启动期拉 manifest 失败:网络不通 / 5xx / 非 JSON / 0 个 active skill。"""


class SkillManifestNotLoaded(Exception):
    """运行期访问 catalog 但 load_manifests 还没跑过(测试或异常代码路径)。"""


_CATALOG: dict[str, SceneCatalogEntry] | None = None


async def load_manifests() -> None:
    """启动期一次性拉平台 manifest 并填充 module-level 缓存。fail-fast。"""
    global _CATALOG

    base_url = os.environ.get("PLATFORM_SKILLS_URL", DEFAULT_PLATFORM_URL).rstrip("/")
    url = f"{base_url}/api/skills"

    logger.info("load_manifests enter url=%s", url)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise SkillManifestUnavailable(
            f"failed to reach platform manifest endpoint {url}: "
            f"{type(e).__name__}: {e}"
        ) from e

    if resp.status_code != 200:
        raise SkillManifestUnavailable(
            f"platform manifest endpoint {url} returned "
            f"HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        payload = resp.json()
    except ValueError as e:
        raise SkillManifestUnavailable(
            f"platform manifest endpoint {url} returned non-JSON body: {e}"
        ) from e

    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, list):
        raise SkillManifestUnavailable(
            f"platform manifest payload missing 'skills' list, got: {payload!r}"
        )

    catalog: dict[str, SceneCatalogEntry] = {}
    for s in skills:
        if not isinstance(s, dict):
            continue
        if s.get("sig_owner") != SKILL_OWNER:
            continue
        if s.get("status") != "active":
            continue
        skill_id = s.get("skill_id", "")
        if not isinstance(skill_id, str) or not skill_id.startswith(SKILL_ID_PREFIX):
            continue
        scene_id = skill_id[len(SKILL_ID_PREFIX):]
        description = s.get("description", "")
        if not isinstance(description, str):
            continue
        catalog[scene_id] = SceneCatalogEntry(
            scene_id=scene_id, description=description
        )

    if not catalog:
        raise SkillManifestUnavailable(
            f"platform returned 0 active {SKILL_OWNER!r} skills, "
            f"refusing to start with empty catalog"
        )

    _CATALOG = catalog
    logger.info(
        "load_manifests success scene_count=%d scenes=%s",
        len(_CATALOG),
        sorted(_CATALOG.keys()),
    )


def get_loaded_catalog() -> dict[str, SceneCatalogEntry]:
    """scene_router / clarifier 唯一数据入口。未加载抛 SkillManifestNotLoaded。"""
    if _CATALOG is None:
        raise SkillManifestNotLoaded(
            "skill manifest catalog not loaded; "
            "call load_manifests() in FastAPI lifespan startup first"
        )
    return _CATALOG


def get_supported_scenes() -> tuple[str, ...]:
    """router.py 的 scene_hint 校验入口。返回 catalog keys 的 tuple。"""
    return tuple(get_loaded_catalog().keys())


def _reset_for_testing() -> None:
    """仅供测试使用:重置 module-level 缓存到未加载状态。"""
    global _CATALOG
    _CATALOG = None
