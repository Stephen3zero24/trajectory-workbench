# EnvScaler 工具调用场景

基于 **EnvScaler** 环境骨架系统的状态化工具调用轨迹数据合成模块。

## 概述

EnvScaler 场景将外部生成的**状态化环境**（由 `skel_builder` + `scen_generator` 产出）部署为 MCP Server，让 Agent 通过 `scene_action` 工具与环境交互，采集真实的工具调用轨迹。

### 与 Toucan / ToolACE 的区别

| 维度 | Toucan | ToolACE | **EnvScaler** |
|------|--------|---------|---------------|
| 环境来源 | Smithery MCP 公开服务 | LLM 自进化合成 | 从指令数据集推断 + 代码生成 |
| 环境类型 | 无状态 API 调用 | 合成 API 定义 | **有状态的领域环境** |
| 交互方式 | 直接 MCP 调用 | Function Calling 模拟 | MCP Server 包装的环境实例 |
| 任务来源 | LLM 合成 | LLM 合成 | 场景生成器 (含检查函数) |
| 奖励信号 | 无 | 验证函数 | 环境 reward + check func |

### 整体流程

```
外部系统 (预处理)              本模块 (Trajectory Workbench)
┌─────────────┐               ┌──────────────────────────────────────┐
│ skel_builder│               │ Step 0: 加载场景文件                  │
│  (环境骨架) │               │   env_scenario.json                  │
│      ↓      │ ── 场景文件 ──→│   filtered_env_metadata.json         │
│scen_generator│              │      ↓                               │
│  (场景生成) │               │ Step 1: 沙箱部署 MCP Server           │
└─────────────┘               │   上传 → 安装依赖 → 启动 fastmcp     │
                              │      ↓                               │
                              │ Step 2: Agent 轨迹生成                │
                              │   DeepSeek → scene_action → 环境交互  │
                              │      ↓                               │
                              │ Review: 质量评估                      │
                              │      ↓                               │
                              │ Export: SFT / DPO / Raw              │
                              └──────────────────────────────────────┘
```

## 文件结构

```
envscaler/
├── __init__.py              # 模块说明
├── config.py                # 配置 + MCP Server 模板 + Agent Prompt
├── scene_manager.py         # Step 0: 场景文件加载/解析/提取
├── sandbox_runner.py        # Step 1: 沙箱部署 MCP Server
├── trajectory_gen.py        # Step 2: Agent 轨迹生成
├── envscaler_pipeline.py    # Pipeline 编排 + Review + Export
├── envscaler_api.py         # FastAPI 路由注册
└── README.md                # 本文档
```

## 使用方式

### 方式 1: 通过 Web UI

1. 在场景选择中选择 **🏗️ EnvScaler工具调用**
2. 上传两个场景文件:
   - `env_scenario.json` — 场景任务定义
   - `filtered_env_metadata.json` — 环境元数据（含代码）
3. 配置参数后启动 Pipeline

### 方式 2: 通过 API

```bash
# 1. 上传场景文件
curl -X POST http://localhost:3000/api/envscaler/upload-scene \
  -F "scenario_file=@env_scenario.json" \
  -F "metadata_file=@filtered_env_metadata.json"
# 返回: {"upload_id": "scene_xxxx", ...}

# 2. 创建任务
curl -X POST http://localhost:3000/api/envscaler/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "scene_source": "upload",
    "scene_upload_id": "scene_xxxx",
    "model": "deepseek-chat",
    "max_steps": 20
  }'
# 返回: {"task_id": "envscaler_xxxx", "status": "created"}

# 3. 查询进度
curl http://localhost:3000/api/envscaler/tasks/envscaler_xxxx/events
```

### 方式 3: 直接运行 Pipeline

```python
import asyncio
from envscaler.envscaler_pipeline import run_envscaler_pipeline
from envscaler.config import EnvScalerPipelineConfig

config = EnvScalerPipelineConfig(
    task_id="test_001",
    scene_source="local",
    scene_dir="/path/to/scene/files",
    agent_model="deepseek-chat",
    max_steps=20,
)

result = asyncio.run(run_envscaler_pipeline(config))
```

## 场景文件格式

### env_scenario.json

```json
[
  {
    "task_id": "task_001",
    "task_desc": "查询 PAT-002 的所有预约记录",
    "env_name": "ClinicScheduler",
    "init_config": {
      "patients": [...],
      "appointments": [...]
    },
    "check_func": "def check(env): return len(env.list_appointments('PAT-002')) > 0"
  }
]
```

### filtered_env_metadata.json

```json
[
  {
    "env_name": "ClinicScheduler",
    "code": "class ClinicScheduler:\\n  def __init__(self, ...): ...",
    "operations": [
      {
        "name": "list_patient_appointments",
        "description": "列出患者的所有预约",
        "parameters": {
          "type": "object",
          "properties": {
            "patient_id": {"type": "string", "description": "患者ID"}
          },
          "required": ["patient_id"]
        }
      }
    ]
  }
]
```

## MCP 工具接口

MCP Server 暴露两个工具:

### `scene_action`
执行环境中的操作。

```json
{
  "name": "list_patient_appointments",
  "arguments": {"patient_id": "PAT-002"}
}
```

返回:
```json
{
  "success": true,
  "observation": {
    "type": "tool",
    "content": "{'success': True, 'data': [...]}",
    "reward": 0.0,
    "terminated": false,
    "truncated": false
  }
}
```

### `get_current_time`
获取当前时间。

```json
{"timezone": "Asia/Shanghai"}
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/envscaler/upload-scene` | 上传场景文件 (multipart) |
| POST | `/api/envscaler/upload-scene-json` | 上传场景文件 (JSON body) |
| POST | `/api/envscaler/tasks` | 创建轨迹合成任务 |
| GET | `/api/envscaler/tasks` | 列出所有任务 |
| GET | `/api/envscaler/tasks/{id}` | 任务详情 |
| GET | `/api/envscaler/tasks/{id}/events` | 事件流 |
| POST | `/api/envscaler/tasks/{id}/export` | 导出数据集 |
| DELETE | `/api/envscaler/tasks/{id}` | 删除任务 |

## 依赖

- `fastmcp >= 3.1.1` — MCP Server 框架（沙箱内安装）
- `qwen-agent == 0.0.31` — (可选) Qwen Agent MCP 集成
- `openai >= 1.0.0` — DeepSeek API 调用
- `opensandbox >= 0.1.5` — 沙箱管理
