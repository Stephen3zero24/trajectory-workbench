"""
Mobile Agent 模块集成测试

在 Mock 模式下测试完整 Pipeline 流程:
  Step 0: 场景加载 (内置场景)
  Step 1: MobileSandbox 启动 (mock)
  Step 2: Agent 轨迹生成 (需 DEEPSEEK_API_KEY, 否则仅测试非 LLM 部分)
  Review + Export

运行:
  # 仅测试结构 (无需 API Key)
  python -m mobile_agent.test_local --dry-run

  # 完整测试 (需要 DEEPSEEK_API_KEY)
  DEEPSEEK_API_KEY=your-key python -m mobile_agent.test_local

  # 指定任务数量
  DEEPSEEK_API_KEY=your-key python -m mobile_agent.test_local --max-tasks 2
"""

import argparse
import asyncio
import json
import os
import sys
import time

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── 处理可能缺失的依赖 (离线/CI 环境) ─────────────────────────────────────
_MOCK_MODULES = {
    "openai": {"OpenAI": type("OpenAI", (), {"__init__": lambda *a, **k: None})},
    "httpx": {},
    "pydantic": {
        "BaseModel": type("BaseModel", (), {}),
        "Field": lambda **k: None,
    },
    "fastapi": {
        "BackgroundTasks": type("BackgroundTasks", (), {}),
        "HTTPException": Exception,
        "UploadFile": type("UploadFile", (), {}),
        "File": lambda **k: None,
    },
    "opensandbox": {},
    "opensandbox.sandbox": {"Sandbox": type("Sandbox", (), {})},
    "opensandbox.config": {"ConnectionConfig": type("ConnectionConfig", (), {})},
    "opensandbox.models": {"WriteEntry": type("WriteEntry", (), {})},
}

import types
for mod_name, attrs in _MOCK_MODULES.items():
    if mod_name not in sys.modules:
        m = types.ModuleType(mod_name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[mod_name] = m


def test_step0_scene_loading():
    """测试 Step 0: 场景加载"""
    print("\n═══ Test Step 0: 场景加载 ═══")

    from mobile_agent.mobile_pipeline import run_step0
    from mobile_agent.config import MobileAgentPipelineConfig

    # 测试加载内置场景
    config = MobileAgentPipelineConfig()
    tasks = run_step0(config, event_callback=lambda t, m: print(f"  [{t}] {m}"))

    assert len(tasks) > 0, "应至少加载一个任务"
    print(f"  ✅ 加载了 {len(tasks)} 个内置任务")

    # 验证任务结构
    t = tasks[0]
    assert t.task_id, "task_id 不能为空"
    assert t.task_desc, "task_desc 不能为空"
    print(f"  ✅ 第一个任务: {t.task_id} — {t.task_desc[:50]}")

    # 测试标签筛选
    config2 = MobileAgentPipelineConfig(scenario_filter_tags=["easy"])
    tasks2 = run_step0(config2, event_callback=lambda t, m: print(f"  [{t}] {m}"))
    assert len(tasks2) <= len(tasks), "筛选后任务数应 <= 全部"
    print(f"  ✅ 标签筛选 [easy]: {len(tasks2)} 个任务")

    # 测试数量限制
    config3 = MobileAgentPipelineConfig(max_tasks=2)
    tasks3 = run_step0(config3, event_callback=lambda t, m: print(f"  [{t}] {m}"))
    assert len(tasks3) <= 2, "限制后任务数应 <= 2"
    print(f"  ✅ 数量限制 max_tasks=2: {len(tasks3)} 个任务")

    # 测试上传场景
    custom_scenario = [
        {
            "task_id": "custom_001",
            "task_desc": "自定义测试任务",
            "app_package": "com.test",
            "tags": ["custom"],
        }
    ]
    tasks4 = run_step0(config, scenario_content=custom_scenario,
                       event_callback=lambda t, m: print(f"  [{t}] {m}"))
    assert len(tasks4) == 1, "上传场景应有 1 个任务"
    assert tasks4[0].task_id == "custom_001"
    print(f"  ✅ 上传场景: {tasks4[0].task_id}")

    return tasks


def test_step1_sandbox_runner():
    """测试 Step 1: MobileSandbox (Mock 模式)"""
    print("\n═══ Test Step 1: MobileSandbox 启动 ═══")

    from mobile_agent.sandbox_runner import MobileSandboxRunner
    from mobile_agent.config import MobileAgentPipelineConfig

    config = MobileAgentPipelineConfig()
    runner = MobileSandboxRunner(config, backend="mock")

    async def _test():
        await runner.start(emit=lambda t, m: print(f"  [{t}] {m}"))
        assert runner.is_running, "Runner 应已启动"
        print(f"  ✅ Runner 已启动: {runner.screen_width}x{runner.screen_height}")

        # 测试动作执行
        result = await runner.execute_action("tap", {"coords": [540, 1000]})
        assert result["success"], "Mock tap 应成功"
        print(f"  ✅ tap: {result}")

        result = await runner.execute_action("input_text", {"text": "hello"})
        assert result["success"], "Mock input 应成功"
        print(f"  ✅ input_text: {result}")

        result = await runner.execute_action("key_event", {"keycode": 3})
        assert result["success"], "Mock key_event 应成功"
        print(f"  ✅ key_event(HOME): {result}")

        result = await runner.execute_action("swipe", {
            "start": [540, 1500], "end": [540, 500], "duration_ms": 300,
        })
        assert result["success"], "Mock swipe 应成功"
        print(f"  ✅ swipe: {result}")

        # 测试截图
        ss = await runner.get_screenshot()
        assert ss["success"], "Mock screenshot 应成功"
        assert ss["image_base64"], "应有 base64 数据"
        print(f"  ✅ screenshot: format={ss['format']}, len={len(ss['image_base64'])}")

        # 测试 UI 树
        ui = await runner.get_ui_tree()
        print(f"  ✅ ui_tree: success={ui['success']}")

        await runner.stop(emit=lambda t, m: print(f"  [{t}] {m}"))
        assert not runner.is_running, "Runner 应已停止"
        print(f"  ✅ Runner 已停止")

    asyncio.run(_test())


def test_config():
    """测试配置模块"""
    print("\n═══ Test Config ═══")

    from mobile_agent.config import (
        MobileAgentPipelineConfig,
        MobileScenarioTask,
        ACTION_TYPES,
        MOBILE_TOOLS_SCHEMA,
        MOBILE_AGENT_SYSTEM_PROMPT,
    )

    # 测试 Pipeline Config
    config = MobileAgentPipelineConfig(task_id="test", max_steps=15)
    d = config.to_dict()
    assert d["task_id"] == "test"
    assert d["max_steps"] == 15

    config2 = MobileAgentPipelineConfig.from_dict(d)
    assert config2.task_id == "test"
    print(f"  ✅ PipelineConfig 序列化/反序列化正常")

    # 测试动作空间
    assert "tap" in ACTION_TYPES
    assert "finish" in ACTION_TYPES
    print(f"  ✅ ACTION_TYPES: {list(ACTION_TYPES.keys())}")

    # 测试 Tools Schema
    assert len(MOBILE_TOOLS_SCHEMA) == 1
    assert MOBILE_TOOLS_SCHEMA[0]["function"]["name"] == "mobile_action"
    print(f"  ✅ MOBILE_TOOLS_SCHEMA 格式正确")

    # 测试 System Prompt 模板
    prompt = MOBILE_AGENT_SYSTEM_PROMPT.format(
        screen_width=1080, screen_height=2340,
        ui_tree_info="test", task_desc="test task",
    )
    assert "1080" in prompt
    assert "test task" in prompt
    print(f"  ✅ System Prompt 模板渲染正常 (len={len(prompt)})")


def test_ui_tree_parser():
    """测试 UI 树解析"""
    print("\n═══ Test UI Tree Parser ═══")

    from mobile_agent.sandbox_runner import _parse_ui_tree_simple, format_ui_elements_for_prompt

    sample_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="设置" resource-id="com.android.settings:id/title" class="android.widget.TextView" clickable="true" bounds="[0,0][200,100]" />
  <node text="" resource-id="com.android.settings:id/icon" class="android.widget.ImageView" clickable="false" bounds="[200,0][300,100]" />
  <node text="WLAN" resource-id="com.android.settings:id/wifi" class="android.widget.TextView" clickable="true" bounds="[0,100][1080,200]" />
</hierarchy>'''

    elements = _parse_ui_tree_simple(sample_xml)
    assert len(elements) == 2, f"应解析出 2 个可交互元素, 实际 {len(elements)}"
    assert elements[0]["text"] == "设置"
    assert elements[0]["clickable"] is True
    assert elements[0]["center"] == [100, 50]
    print(f"  ✅ 解析出 {len(elements)} 个元素: {[e['text'] for e in elements]}")

    prompt_text = format_ui_elements_for_prompt(elements)
    assert "设置" in prompt_text
    assert "WLAN" in prompt_text
    print(f"  ✅ Prompt 格式化正常:\n{prompt_text}")

    # 空输入
    empty = format_ui_elements_for_prompt([])
    assert "无" in empty
    print(f"  ✅ 空元素: {empty}")


async def test_full_pipeline_mock(max_tasks: int = 1):
    """测试完整 Pipeline (Mock 模式, 需要 DEEPSEEK_API_KEY)"""
    print("\n═══ Test Full Pipeline (Mock) ═══")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("  ⏭ 跳过: 未设置 DEEPSEEK_API_KEY")
        return

    from mobile_agent.config import MobileAgentPipelineConfig
    from mobile_agent.mobile_pipeline import run_mobile_pipeline

    config = MobileAgentPipelineConfig(
        task_id=f"test_{int(time.time())}",
        agent_model="deepseek-chat",
        max_steps=5,       # 减少步数加速测试
        max_tasks=max_tasks,
        enable_vision=False,  # Mock 模式下截图无意义, 关闭 vision
        enable_ui_tree=False,
        output_dir=f"/tmp/mobile_agent_test_{int(time.time())}",
        deepseek_api_key=api_key,
    )

    events = []

    def event_cb(t, m, d=None):
        events.append((t, m))
        print(f"  [{t}] {m}")

    result = await run_mobile_pipeline(config, event_callback=event_cb)

    print(f"\n  Pipeline 结果:")
    print(f"    status: {result.get('status')}")
    print(f"    tasks: {result.get('tasks_count')}")
    print(f"    trajectories: {result.get('trajectories_count')}")
    print(f"    actions: {result.get('total_actions')}")
    print(f"    avg_quality: {result.get('avg_quality')}")
    print(f"    elapsed: {result.get('elapsed_seconds')}s")

    if result.get("status") == "completed":
        export = result.get("export", {})
        for fmt, path in export.items():
            if os.path.exists(path):
                lines = sum(1 for _ in open(path))
                print(f"    {fmt}: {path} ({lines} lines)")
        print(f"  ✅ Pipeline 完成")
    else:
        print(f"  ⚠ Pipeline 状态: {result.get('status')}, error: {result.get('error', '')[:200]}")


def test_api_models():
    """测试 API 请求/响应模型"""
    print("\n═══ Test API Models ═══")

    try:
        from pydantic import BaseModel as _RealBaseModel
        if not hasattr(_RealBaseModel, '__fields__') and not hasattr(_RealBaseModel, 'model_fields'):
            print("  ⏭ 跳过: pydantic 未真实安装")
            return
    except Exception:
        print("  ⏭ 跳过: pydantic 未真实安装")
        return

    from mobile_agent.mobile_api import MobileTaskRequest

    # 默认值
    req = MobileTaskRequest()
    assert req.scenario_source == "builtin"
    assert req.model == "deepseek-chat"
    assert req.enable_vision is True
    print(f"  ✅ 默认请求: source={req.scenario_source}, model={req.model}")

    # 自定义值
    req2 = MobileTaskRequest(
        scenario_source="upload",
        scenario_upload_id="test_123",
        max_tasks=5,
        scenario_filter_tags=["easy", "settings"],
        enable_vision=False,
    )
    assert req2.scenario_filter_tags == ["easy", "settings"]
    assert req2.enable_vision is False
    print(f"  ✅ 自定义请求: tags={req2.scenario_filter_tags}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Mobile Agent 模块测试")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅测试结构, 不调用 LLM")
    parser.add_argument("--max-tasks", type=int, default=1,
                        help="完整测试时使用的任务数")
    args = parser.parse_args()

    print("🧪 Mobile Agent 模块集成测试\n")
    start = time.time()

    # 基础测试 (不需要 API Key)
    test_config()
    test_ui_tree_parser()
    test_step0_scene_loading()
    test_step1_sandbox_runner()
    test_api_models()

    # 完整 Pipeline 测试 (需要 API Key)
    if not args.dry_run:
        asyncio.run(test_full_pipeline_mock(max_tasks=args.max_tasks))

    elapsed = time.time() - start
    print(f"\n{'─' * 50}")
    print(f"✅ 所有测试通过 ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
