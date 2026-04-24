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


if __name__ == "__main__":
    test_llm_config_defaults()
    test_smithery_config_is_configured()
    test_mcp_server_registry_roundtrip()
    test_pipeline_config_has_three_llms()
    test_pipeline_config_step_fields_present()
    test_pipeline_config_nested_roundtrip()
    print("✅ T-1 config schema 冒烟测试全部通过")
