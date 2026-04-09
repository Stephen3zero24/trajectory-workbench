# 📱 Mobile Agent — Android GUI 操控轨迹合成

基于 OpenSandbox + [Redroid](https://github.com/remote-android/redroid-doc) 的 Android GUI Agent 轨迹数据合成模块。

## 概述

Agent 在 OpenSandbox 管理的 Redroid 容器中执行真实 GUI 操控（点击、滑动、输入、按键），通过 VLM（视觉语言模型）理解屏幕截图和 UI 元素，自主规划并执行操作序列，最终产出高质量的 `screenshot → thought → action → result` 交互轨迹。

与其他引擎（envscaler / toolace / toucan）共用同一个 OpenSandbox 控制面，区别仅在于使用 Redroid 镜像，并通过 ADB 命令进行 GUI 操控。

## Pipeline

```
Step 0: 场景加载
  └→ 解析 mobile_scenarios.json → MobileScenarioTask 列表

Step 1: OpenSandbox 启动 Android 沙箱
  └→ OpenSandbox API 创建容器 (redroid/redroid:14.0.0-latest)
  └→ 等待 Android 模拟器启动 (sys.boot_completed)
  └→ 通过 ADB 获取屏幕分辨率

Step 2: Agent 轨迹生成 (核心循环)
  ┌→ 截图 (adb shell screencap) + UI Tree (uiautomator dump)
  │   ↓
  │  VLM 推理 (DeepSeek / Qwen-VL)
  │   ↓
  │  选择动作 (tap / swipe / input_text / key_event)
  │   ↓
  │  通过 ADB 执行动作 → 等待界面稳定
  │   ↓
  └← 循环直到 finish 或 max_steps

Review: 质量评估
  └→ 五维评分: UI理解 / 动作准确 / 推理清晰 / 完成度 / 效率

Export: 数据集导出
  └→ SFT / DPO / Raw (JSONL)
```

## 架构

```
                     ┌─────────────────────────────────────────┐
                     │          OpenSandbox Server              │
                     │          (统一沙箱控制面)                 │
                     │                                         │
  envscaler ────────▶│  opensandbox/code-interpreter ──→ MCP   │
  toolace  ─────────▶│  opensandbox/code-interpreter ──→ MCP   │
  mobile_agent ─────▶│  redroid/redroid ─────────→ ADB   │◀── 本模块
                     │                                         │
                     └─────────────────────────────────────────┘
                         ↑ 统一部署到 CCE / K8s 集群
```

## 环境准备

### 1. 前提条件

Mobile Agent **不需要额外的 Python 依赖**，只需：

- OpenSandbox Server 已启动（与其他引擎共用）
- Redroid 镜像已拉取

```bash
# 拉取 Redroid 镜像
docker pull redroid/redroid:14.0.0-latest
```

### 2. 宿主机 / CCE 节点要求

Android 模拟器需要 KVM 硬件虚拟化支持：

- **Linux**: 需要 `/dev/kvm` 存在，CCE 节点需选择支持嵌套虚拟化的机型
- **macOS (Apple Silicon)**: 需要 Rosetta 2 + Docker Desktop 或 Colima：
  ```bash
  colima start --vm-type=vz --vz-rosetta --memory 8 --cpu 4
  ```

### 3. 配置

```bash
export DEEPSEEK_API_KEY="your-key"

# 可选: 自定义镜像
export MOBILE_SANDBOX_IMAGE="redroid/redroid:14.0.0-latest"
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

通过 backend.py 启动服务后，在 Web UI 选择 "📱 Mobile Agent" 场景即可。

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
| settings_lang_006 | 查看系统语言设置 | Easy | settings, language |
| calculator_007 | 计算器运算 | Easy | calculator, math |
| settings_storage_008 | 查看存储空间 | Easy | settings, storage |
| notification_009 | 清除通知栏 | Easy | notification, system |
| multistep_010 | 跨应用多步任务 | Hard | multistep, cross_app |

## 自定义场景

创建 JSON 文件，格式如下：

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

所有动作通过 `sandbox.commands.run("adb shell ...")` 在沙箱内执行。

| 动作 | ADB 命令 | 参数 |
|------|---------|------|
| `tap` | `input tap X Y` | `coords: [x, y]` |
| `long_press` | `input swipe X Y X Y DURATION` | `coords, duration_ms` |
| `swipe` | `input swipe X1 Y1 X2 Y2 DURATION` | `start, end, duration_ms` |
| `input_text` | `input text "TEXT"` | `text` |
| `key_event` | `input keyevent CODE` | `keycode` (3=HOME, 4=BACK, 66=ENTER) |
| `wait` | *(sleep)* | `seconds` |
| `finish` | *(无)* | `summary` |

## Mock 模式

OpenSandbox Server 不可达时自动切换到 Mock 模式，可用于：
- 验证 Pipeline 逻辑
- 开发和调试 Agent Prompt
- CI/CD 集成测试

```bash
# 仅测试结构 (Mock 模式, 无需 OpenSandbox / API Key)
python -m mobile_agent.test_local --dry-run

# 完整测试 (需要 OpenSandbox + API Key)
python -m mobile_agent.test_local --max-tasks 2
```
