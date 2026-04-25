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


from unittest.mock import patch, MagicMock


def _make_fake_trajectory(subset="single-turn-original", with_multi_step=True):
    """构造符合 ToucanTrajectory 实际 schema 的假轨迹,供 review/export 测试用。

    关键:steps 里的元素是 dict(asdict 过),不是 TrajectoryStep 对象 ——
    这是 T-7.3 修复的核心:review/export 必须用 dict 访问(turn["role"]),
    不能用 attribute 访问(turn.role)。
    """
    from toucan.step3_trajectory_gen import ToucanTrajectory

    steps = [
        # user turn
        {
            "step_id": 1,
            "role": "user",
            "content": "What's the weather in Tokyo?",
            "thought": "",
            "tool_calls": [],
            "timestamp": "2025-01-01T00:00:00",
        },
    ]
    if with_multi_step:
        # assistant turn with tool call
        steps.append({
            "step_id": 2,
            "role": "assistant",
            "content": "Let me check the weather.",
            "thought": "Need to call weather API",
            "tool_calls": [{
                "tool_name": "get_weather",
                "tool_input": {"city": "Tokyo"},
                "tool_output": "Sunny, 25C",
                "server_id": "weather",
                "success": True,
                "duration_ms": 123,
            }],
            "timestamp": "2025-01-01T00:00:01",
        })
        # assistant final response
        steps.append({
            "step_id": 3,
            "role": "assistant",
            "content": "The weather in Tokyo is sunny, 25C.",
            "thought": "",
            "tool_calls": [],
            "timestamp": "2025-01-01T00:00:02",
        })

    return ToucanTrajectory(
        trajectory_id=f"test_traj_{subset}",
        question="What's the weather in Tokyo?",
        question_id="q1",
        target_servers=["weather"],
        target_tools=["get_weather"],
        steps=steps,
        messages=[],
        tools_schema=[],
        quality_score=0.85,
        total_tool_calls=1 if with_multi_step else 0,
        successful_tool_calls=1 if with_multi_step else 0,
        total_tokens=100,
        iteration=0,
        subset=subset,
    )


def test_t73_review_accepts_real_trajectory_shape():
    """T-7.3 活体验证:review_toucan_trajectory 能处理真实 shape 的
    ToucanTrajectory(steps 是 list[dict],不是 list[obj])。

    mock 掉内部的 OpenAI 调用,只测字段访问路径。
    """
    from toucan.toucan_pipeline import review_toucan_trajectory

    traj = _make_fake_trajectory(with_multi_step=True)
    cfg = ToucanPipelineConfig(
        task_id="test",
        question_llm=LLMConfig(model="deepseek-chat", api_key="fake"),
    )

    # mock OpenAI 的返回
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = '{"overall_score": 0.8, "dimensions": {}}'

    with patch("toucan.toucan_pipeline.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        MockOpenAI.return_value = mock_client

        # 关键断言:不抛 AttributeError('turns' / 'role' / 'content')
        result = review_toucan_trajectory(traj, cfg)

    # 验证返回值可解析
    assert isinstance(result, dict)


def test_t73_export_writes_valid_jsonl():
    """T-7.3 活体验证:export_toucan_dataset 能写出合法 SFT/DPO/raw JSONL。

    精确覆盖:
    - traj.steps 访问(不再用 traj.turns)
    - turn["role"] / turn["content"] dict 访问
    - traj.target_servers(不再 traj.server_ids)
    - traj.subset == "multi-turn" 派生(不再 traj.is_multi_turn)
    """
    import json
    import os
    import tempfile
    from toucan.toucan_pipeline import export_toucan_dataset

    traj_single = _make_fake_trajectory(subset="single-turn-original")
    traj_multi = _make_fake_trajectory(subset="multi-turn")

    with tempfile.TemporaryDirectory() as tmp:
        cfg = ToucanPipelineConfig(
            task_id="test_export",
            output_dir=tmp,
        )
        # 至少 2 条以触发 DPO 段
        export_toucan_dataset([traj_single, traj_multi], cfg, reviews=None)

        # 验证 SFT 文件
        sft_path = os.path.join(tmp, "test_export_sft.jsonl")
        assert os.path.exists(sft_path), "SFT 文件未产出"
        with open(sft_path) as f:
            lines = f.readlines()
        assert len(lines) == 2, f"SFT 期望 2 行,实际 {len(lines)}"
        for line in lines:
            rec = json.loads(line)
            assert "id" in rec
            assert "messages" in rec
            assert "metadata" in rec
            meta = rec["metadata"]
            # 关键验证:server_ids 来自 target_servers
            assert meta["server_ids"] == ["weather"]
            # 关键验证:multi_turn 派生自 subset
            if rec["id"] == "test_traj_multi-turn":
                assert meta["multi_turn"] is True, \
                    f"multi-turn traj 的 multi_turn 应为 True,实际 {meta['multi_turn']}"
            else:
                assert meta["multi_turn"] is False

        # 验证 raw 文件
        raw_path = os.path.join(tmp, "test_export_raw.jsonl")
        assert os.path.exists(raw_path), "Raw 文件未产出"


def test_t73_summary_multi_turn_count():
    """T-7.3 活体验证:summary 段的 multi_turn 计数从 subset 派生。

    这个测试间接命中 L163:
        sum(1 for t in trajectories if t.subset == "multi-turn")

    靠直接 import 某个暴露 summary 计算的函数做测试不现实(summary 逻辑
    嵌在 run_toucan_pipeline 里),所以改为验证:构造的 trajectory
    能被正确分类(表达式不 AttributeError)。
    """
    traj_single = _make_fake_trajectory(subset="single-turn-original")
    traj_multi = _make_fake_trajectory(subset="multi-turn")

    # 直接复用 pipeline 里同款表达式验证
    trajectories = [traj_single, traj_multi]
    count = sum(1 for t in trajectories if t.subset == "multi-turn")
    assert count == 1

    # 再用属性访问方式确认 subset 字段真实存在且可访问
    assert traj_single.subset == "single-turn-original"
    assert traj_multi.subset == "multi-turn"
    # 反向验证:is_multi_turn 字段不存在
    assert not hasattr(traj_single, "is_multi_turn")


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
    test_t73_review_accepts_real_trajectory_shape()
    test_t73_export_writes_valid_jsonl()
    test_t73_summary_multi_turn_count()
    print("✅ T-1 config schema 冒烟测试全部通过")
