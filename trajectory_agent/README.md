# Trajectory Agent (A-1 壳层)

## 模块职责

`trajectory_agent/` 是 trajectory-workbench 专项 Agent 的**薄门面层**:为后续阶段(A-2 参数澄清 / A-3 聚合产物 / C 阶段编排)提供统一入口,把上游调用方和具体场景 Pipeline 解耦。A-1 阶段只实现"分发 + ID 映射",不接 LLM、不做澄清、不做聚合。

## 目录结构

```
trajectory_agent/
├── __init__.py
├── schemas.py         # Pydantic 模型
├── dispatcher.py      # SceneDispatcher 类(HTTP 自调用)
├── agent.py           # TrajectoryAgent 类(生成 agent_run_id + 内存字典)
├── router.py          # FastAPI APIRouter
├── README.md
└── tests/
    ├── __init__.py
    ├── pytest.ini     # asyncio_mode = auto
    └── test_submit.py
```

## 对外端点

### POST `/api/trajectory-agent/submit`

**Request(JSON)**:
| 字段 | 类型 | 说明 |
|---|---|---|
| `user_request` | `str \| null` | A-1 不消费,A-2 启用 |
| `scene_hint` | `str` | **必填**,枚举:`search2qa` / `toolace` / `toucan` / `envscaler` / `mobile_agent` |
| `params` | `dict` | 透传给底层场景 Pipeline |
| `attachments` | `list \| null` | 预留,A-1 不处理 |

**Response 200(JSON)**:`{agent_run_id, scene, scene_task_id, status, message}`

**错误码**:
- `400` — `scene_hint` 缺失或不在枚举内
- `501` — `scene_hint` 是 `envscaler` / `mobile_agent`(A-1 未接通)
- `502` — 底层 Pipeline HTTP 错误

### GET `/api/trajectory-agent/runs/{agent_run_id}`

返回底层场景 task 的当前状态(透传上游响应)。

**错误码**:
- `404` — `agent_run_id` 未知

## A-1 版本限制

- `scene_hint` **必填**——A-1 不做 LLM 意图识别,由调用方显式指定场景。
- `envscaler` 和 `mobile_agent` 返回 **501**——前者 multipart 上传、后者 GUI 流,壳层 A-1 不接通,留给后续阶段。
- `agent_run_id → (scene, scene_task_id)` 映射用**进程内存字典**维护,**非持久化**;后端重启后映射丢失。
- **不接 LLM**,无澄清、无重试、无聚合;纯 HTTP 自调用分发。

## 本地启动示例

```bash
# 1. 激活 uv 虚拟环境
source .opensandbox-env/bin/activate

# 2. 启动后端(默认 3100 端口,由 BACKEND_PORT 环境变量覆盖)
python3 backend.py

# 3. 在 FastAPI 的 /docs 页面确认新路由
open http://127.0.0.1:3100/docs

# 4. curl 发起 search2qa 请求
curl -X POST http://127.0.0.1:3100/api/trajectory-agent/submit \
  -H "Content-Type: application/json" \
  -d '{
    "scene_hint": "search2qa",
    "params": {"task_desc": "测试请求", "seed": "机器学习"}
  }'

# 5. 查询 agent_run_id 对应的底层任务状态
curl http://127.0.0.1:3100/api/trajectory-agent/runs/<agent_run_id>
```

## 运行测试

```bash
uv run pytest trajectory_agent/tests/ -v
```
