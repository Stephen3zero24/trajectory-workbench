"""共享 fixture:给非 loader 测试预填一个最小 catalog。

T2-1 之后,scene_router.get_supported_scenes() / _valid_scenes() 在 catalog
未加载时抛 SkillManifestNotLoaded,会污染除 loader 自身测试以外的所有测试。
本 fixture autouse 启用,在每个测试前把 catalog 设到一个稳定的两-scene 状态。

skill_manifest_loader 自己的测试通过其内部 _reset_catalog autouse fixture
在 setup 阶段把 catalog 重新置 None(autouse 顺序:外层 conftest 先 setup,
内层模块 fixture 后 setup),不互相干扰。
"""

from __future__ import annotations

import pytest

from trajectory_agent import skill_manifest_loader as loader


@pytest.fixture(autouse=True)
def _default_catalog():
    """默认只放 search2qa,使 test_submit.py 里 scene_hint='toucan' 仍当作 invalid。

    需要更宽 catalog 的测试(如 test_scene_router.py 需要 toucan 进合法集合)
    在自己的测试 fixture 里覆盖 loader._CATALOG。
    """
    loader._CATALOG = {
        "search2qa": loader.SceneCatalogEntry(
            scene_id="search2qa",
            description="搜索问答数据合成 (test fixture)",
        ),
    }
    yield
    loader._reset_for_testing()
