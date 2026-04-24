"""T-1 config schema 冒烟测试。直接 python toucan/test_config_schema.py 运行。"""
import os
import tempfile

from toucan.config import (
    LLMConfig, SmitheryConfig, MCPServerRegistry,
    MCPServerInfo, ToucanPipelineConfig,
)


def test_llm_config_defaults():
    c = LLMConfig()
    assert c.model == "deepseek-chat"
    assert c.temperature == 0.7
    assert hasattr(c, "api_key")
    assert hasattr(c, "base_url")


def test_smithery_config_is_configured():
    assert SmitheryConfig(api_key="").is_configured is False
    assert SmitheryConfig(api_key="x").is_configured is True


def test_mcp_server_registry_roundtrip():
    reg = MCPServerRegistry(servers=[
        MCPServerInfo(server_id="s1", name="S1", url="http://x"),
    ])
    assert len(reg.list_servers()) == 1
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r.json")
        reg.save(p)
        reg2 = MCPServerRegistry.load(p)
        assert len(reg2.servers) == 1
        assert reg2.servers[0].server_id == "s1"


def test_pipeline_config_has_three_llms():
    cfg = ToucanPipelineConfig()
    # 三组 LLM 各自独立、默认 temperature 不同
    assert cfg.question_llm.temperature == 0.8
    assert cfg.quality_llm.temperature == 0.3
    assert cfg.agent_llm.temperature == 0.7


def test_pipeline_config_step_fields_present():
    """确认 T-1 扩容把 step 脚本所需字段补齐了"""
    cfg = ToucanPipelineConfig()
    # Step 1 专属
    assert hasattr(cfg, "multi_server")
    assert hasattr(cfg, "dedup_threshold")
    # Step 2 专属
    assert hasattr(cfg, "qc_criteria")
    assert isinstance(cfg.qc_criteria, list) and len(cfg.qc_criteria) > 0
    assert hasattr(cfg, "qc_min_score")
    # Step 3 所需的 agent_llm
    assert hasattr(cfg, "agent_llm")
    # 两阈值独立存在
    assert cfg.qc_min_score != cfg.quality_threshold


def test_pipeline_config_nested_roundtrip():
    cfg = ToucanPipelineConfig(
        task_id="t1",
        smithery=SmitheryConfig(api_key="sk"),
        question_llm=LLMConfig(model="deepseek-chat", temperature=0.9),
        agent_llm=LLMConfig(model="qwen-max", temperature=0.5),
    )
    assert cfg.smithery.is_configured is True
    assert cfg.question_llm.temperature == 0.9
    assert cfg.agent_llm.model == "qwen-max"

    # to_dict / from_dict roundtrip
    d = cfg.to_dict()
    assert isinstance(d["smithery"], dict)
    assert isinstance(d["agent_llm"], dict)
    cfg2 = ToucanPipelineConfig.from_dict(d)
    assert isinstance(cfg2.smithery, SmitheryConfig)
    assert isinstance(cfg2.question_llm, LLMConfig)
    assert isinstance(cfg2.agent_llm, LLMConfig)
    assert cfg2.smithery.api_key == "sk"
    assert cfg2.agent_llm.model == "qwen-max"


import asyncio


def test_smithery_setup_fetch_tools_false():
    """build_registry(fetch_tools=False) 走兜底路径,不触发网络"""
    from toucan.step0_smithery_setup import SmitherySetup
    setup = SmitherySetup(SmitheryConfig(api_key=""))
    registry = asyncio.run(setup.build_registry(fetch_tools=False))
    assert isinstance(registry, MCPServerRegistry)
    assert len(registry.servers) > 0


def test_smithery_setup_save_registry():
    """save_registry 能写入文件"""
    import os
    import tempfile
    from toucan.step0_smithery_setup import SmitherySetup
    setup = SmitherySetup(SmitheryConfig())
    registry = MCPServerRegistry(servers=[
        MCPServerInfo(server_id="t", name="T", url="http://x"),
    ])
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "reg.json")
        asyncio.run(setup.save_registry(registry, p))
        assert os.path.exists(p)
        loaded = MCPServerRegistry.load(p)
        assert len(loaded.servers) == 1
        assert loaded.servers[0].server_id == "t"


def test_toucan_api_create_task():
    """POST /api/toucan/tasks 构造 config 不再 TypeError,
    且 server_ids 正确映射到 mcp_server_ids"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from toucan.toucan_api import register_toucan_routes, toucan_tasks

    app = FastAPI()
    register_toucan_routes(app)
    client = TestClient(app)

    with patch("toucan.toucan_api._run_task"):
        resp = client.post("/api/toucan/tasks", json={
            "question_count": 3,
            "server_ids": ["exa", "brave-search"],
            "model": "deepseek-chat",
            "temperature": 0.8,
        })
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "task_id" in data
    assert data["status"] == "created"

    task_id = data["task_id"]
    stored = toucan_tasks[task_id]
    cfg_dict = stored["config"]
    assert cfg_dict["mcp_server_ids"] == ["exa", "brave-search"], \
        f"mcp_server_ids not mapped: {cfg_dict.get('mcp_server_ids')}"
    assert cfg_dict["question_llm"]["model"] == "deepseek-chat"
    assert cfg_dict["quality_llm"]["model"] == "deepseek-chat"
    assert cfg_dict["agent_llm"]["model"] == "deepseek-chat"
    assert cfg_dict["question_llm"]["temperature"] == 0.8
    assert cfg_dict["quality_llm"]["temperature"] == 0.3
    assert cfg_dict["agent_llm"]["temperature"] == 0.7

    toucan_tasks.pop(task_id, None)


def test_toucan_api_server_ids_none():
    """不传 server_ids 时,mcp_server_ids 应为空列表"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from toucan.toucan_api import register_toucan_routes, toucan_tasks

    app = FastAPI()
    register_toucan_routes(app)
    client = TestClient(app)

    with patch("toucan.toucan_api._run_task"):
        resp = client.post("/api/toucan/tasks", json={"question_count": 3})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    cfg_dict = toucan_tasks[task_id]["config"]
    assert cfg_dict["mcp_server_ids"] == []
    toucan_tasks.pop(task_id, None)


if __name__ == "__main__":
    test_llm_config_defaults()
    test_smithery_config_is_configured()
    test_mcp_server_registry_roundtrip()
    test_pipeline_config_has_three_llms()
    test_pipeline_config_step_fields_present()
    test_pipeline_config_nested_roundtrip()
    test_smithery_setup_fetch_tools_false()
    test_smithery_setup_save_registry()
    test_toucan_api_create_task()
    test_toucan_api_server_ids_none()
    print("✅ T-1 config schema 冒烟测试全部通过")
