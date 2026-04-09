# 📱 Mobile Agent — Android GUI 操控轨迹合成

基于 [AgentScope Runtime MobileSandbox](https://github.com/agentscope-ai/agentscope-runtime) 的 Android GUI Agent 轨迹数据合成模块。

## 概述

Agent 在 Docker 化的 Android 模拟器中执行真实 GUI 操控（点击、滑动、输入、按键），通过 VLM（视觉语言模型）理解屏幕截图和 UI 元素，自主规划并执行操作序列，最终产出高质量的 `screenshot → thought → action → result` 交互轨迹。

## Pipeline

```
Step 0: 场景加载
  └→ 解析 mobile_scenarios.json → MobileScenarioTask 列表

Step 1: MobileSandbox 启动
  └→ agentscope/runtime-sandbox-mobile:latest Docker 镜像
  └→ 获取屏幕分辨率、设备信息

Step 2: Agent 轨迹生成 (核心循环)
  ┌→ 截图 + UI Tree (uiautomator dump)
  │   ↓
  │  VLM 推理 (DeepSeek / Qwen-VL)
  │   ↓
  │  选择动作 (tap / swipe / input_text / key_event)
  │   ↓
  │  执行动作 → 等待界面稳定
  │   ↓
  └← 循环直到 finish 或 max_steps

Review: 质量评估
  └→ 五维评分: UI理解 / 动作准确 / 推理清晰 / 完成度 / 效率

Export: 数据集导出
  └→ SFT / DPO / Raw (JSONL)
```

## 环境准备

### 1. 安装依赖

```bash
# 安装 AgentScope Runtime
pip install agentscope-runtime

# 拉取 MobileSandbox 镜像
docker pull agentscope/runtime-sandbox-mobile:latest

# 或从阿里云 ACR 拉取 (国内更快)
docker pull agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/runtime-sandbox-mobile:latest
docker tag agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/runtime-sandbox-mobile:latest agentscope/runtime-sandbox-mobile:latest
```

### 2. 宿主机要求

- **Linux**: 需要 KVM 支持 (`/dev/kvm` 存在)
- **macOS (Apple Silicon)**: 需要 Rosetta 2 + Docker Desktop 或 Colima:
  ```bash
  colima start --vm-type=vz --vz-rosetta --memory 8 --cpu 4
  ```

### 3. 配置 API Key

```bash
export DEEPSEEK_API_KEY="your-key"
```

## 使用方式

### CLI 模式

```python
import asyncio
from mobile_agent.config import MobileAgentPipelineConfig
from mobile_agent.mobile_pipeline import run_mobile_pipeline

async def main():
    config = MobileAgentPipelineConfig(
        task_id="test_001",
        agent_model="deepseek-chat",
        max_steps=20,
        max_tasks=3,               # 只跑前 3 个任务
        enable_vision=True,
        enable_ui_tree=True,
    )
    result = await run_mobile_pipeline(config)
    print(result)

asyncio.run(main())
```

### Web UI 模式

通过 backend.py 启动服务后, 在 Web UI 选择 "Mobile Agent" 场景即可。

### API 模式

```bash
# 查看内置场景
curl http://localhost:3000/api/mobile/builtin-scenarios

# 创建任务 (使用内置场景)
curl -X POST http://localhost:3000/api/mobile/tasks \
  -H "Content-Type: application/json" \
  -d '{"scenario_source": "builtin", "max_tasks": 3}'

# 查看任务状态
curl http://localhost:3000/api/mobile/tasks/{task_id}

# 获取事件流
curl http://localhost:3000/api/mobile/tasks/{task_id}/events
```

## 内置场景

| Task ID | 描述 | 难度 | 标签 |
|---------|------|------|------|
| settings_wifi_001 | 查看已连接的 WiFi | Easy | settings, wifi |
| settings_display_002 | 调亮度 + 开深色模式 | Medium | settings, display |
| settings_bluetooth_003 | 打开蓝牙查看配对设备 | Easy | settings, bluetooth |
| settings_alarm_004 | 创建带重复的闹钟 | Medium | clock, alarm |
| contacts_add_005 | 新建联系人 | Medium | contacts, create |
| calculator_007 | 计算器运算 | Easy | calculator, math |
| multistep_010 | 跨应用多步任务 | Hard | multistep, cross_app |

## 自定义场景

创建 JSON 文件, 格式如下:

```json
[
  {
    "task_id": "my_task_001",
    "task_desc": "任务描述",
    "app_package": "com.example.app",
    "app_activity": "",
    "pre_install_apks": ["/path/to/app.apk"],
    "initial_actions": [
      {"type": "wait", "params": {"seconds": 3}}
    ],
    "check_type": "shell",
    "check_command": "adb shell ...",
    "max_steps": 20,
    "tags": ["custom"]
  }
]
```

## 动作空间

| 动作 | 参数 | 说明 |
|------|------|------|
| `tap` | `coords: [x, y]` | 点击坐标 |
| `long_press` | `coords, duration_ms` | 长按 |
| `swipe` | `start, end, duration_ms` | 滑动 |
| `input_text` | `text` | 输入文本 |
| `key_event` | `keycode` | 按键 (3=HOME, 4=BACK, 66=ENTER) |
| `wait` | `seconds` | 等待 |
| `finish` | `summary` | 任务完成 |

## Mock 模式

未安装 `agentscope-runtime` 时自动切换到 Mock 模式, 可用于:
- 验证 Pipeline 逻辑
- 开发和调试 Agent Prompt
- CI/CD 集成测试
