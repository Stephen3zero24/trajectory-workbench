# trajectory-workbench API Contract

5/9 demo 部署形态:VibeDataBot(平台,独立仓库) + opensandbox-server +
trajectory-workbench,后两者复用同一镜像,通过 docker-compose 编排。本文聚焦
**search2qa demo 主链**,其他场景 endpoint 仅作参考列出,schema 不在范围。

## 1. 部署形态与基础约定

- **Base URL**:容器内 `http://0.0.0.0:3100`(uvicorn host=0.0.0.0);docker-compose
  network 内 service 名 `trajectory-workbench`,即 `http://trajectory-workbench:3100`
- **Content-Type**:`application/json` (UTF-8)
- **Timezone**:event 与 task 的 `timestamp` 字段用 `datetime.now().isoformat()`,
  ⚠️ **无时区后缀**(本地时区,容器内即 UTC,host bind mount 时取决于 host TZ)
- **CORS**:`allow_origins=["*"]`、`allow_credentials=True`、`allow_methods=["*"]`、
  `allow_headers=["*"]`(`backend.py:828-834`)。⚠️ 这是 CORS 规范不允许的组合,
  浏览器会忽略 credentials,demo 阶段(无 cookie/auth)不影响功能
- **认证**:无。任何容器都可调用,平台侧需自行挡入站请求
- **Rate limit**:无
- **Export 文件路径**:写盘相对路径 `output/{task_id}_export.json`(容器内
  `/app/output/...`)。跨容器访问需在 docker-compose 挂载 `./output:/app/output`
- **Request ID**:无 middleware 注入。跨容器排查问题靠 `task_id` 串联

---

## 2. 健康检查

### GET /api/health

`backend.py:852-864`

**响应**(HTTP 200):

```json
{
  "status": "ok",
  "opensandbox": "connected" | "disconnected",
  "deepseek":    "configured" | "not_configured"
}
```

| 字段 | 语义 |
| - | - |
| `status` | ⚠️ **永远返回 "ok"**,即使 sandbox 断开 / DeepSeek 未配置。docker-compose `healthcheck` 仅靠 HTTP 200 判活 |
| `opensandbox` | 主动 GET `${OPENSANDBOX_SERVER}/health` 5 秒超时,200 即 `connected`,任何异常 `disconnected` |
| `deepseek` | 仅检查 `DEEPSEEK_API_KEY` env 是否非空,**不验证 key 是否有效** |

---

## 3. search2qa Demo 主链

### 3.1 POST /api/tasks — 创建任务

`backend.py:870-911`。Request body 为 `TaskCreateRequest` (Pydantic, `:92-107`)。

| 字段 | 类型 | required | default | 说明 |
| - | - | - | - | - |
| `task_desc` | str | ✅ | — | 任务描述,空 `seed` 时回退作 search2qa 种子 |
| `scene_type` | str | — | `"code_exec"` | search2qa 必须传 `"search2qa"`。⚠️ **无枚举校验**,任何字符串都接受;非 `"search2qa"` 走通用 Agent 分支 |
| `model` | str | — | `"deepseek-chat"` | LLM 模型 ID |
| `temperature` | float | — | `0.7` | LLM 温度 |
| `max_steps` | int | — | `15` | 通用 Agent 单轮 step 上限,**search2qa 不读** |
| `max_iterations` | int | — | `3` | 全局迭代上限 |
| `quality_threshold` | float | — | `0.80` | review 通过线 |
| `concurrent` | int | — | `1` | ⚠️ **代码内未消费**,纯 schema 占位 |
| `seed` | str | — | `""` | search2qa 种子词,空时回退到 `task_desc` |
| `qa_mode` | str | — | `"question"` | `"question"` 或 `"answer"`,无枚举校验 |
| `max_evolutions` | int | — | `2` | search2qa 复杂化迭代次数 |
| `max_turns` | int | — | `20` | search2qa 每阶段最大轮次 |
| `enable_evolution` | bool | — | `true` | search2qa 启用复杂化 |
| `enable_rewrite` | bool | — | `true` | search2qa 启用轨迹改写 |

**响应**(HTTP 200):

```json
{ "task_id": "task_abc12345", "status": "created" }
```

**行为**:

- handler **立即返回**,实际任务通过 FastAPI `BackgroundTasks` 异步执行
- 客户端通过 events polling 知道执行进度
- `task_id` 格式:`task_<8 位 hex>`

错误码见 §5。

### 3.2 GET /api/tasks/{task_id} — 任务详情

`backend.py:936-954`。

**响应**(HTTP 200):

```json
{
  "task_id": "task_abc12345",
  "config": { ... },
  "status": "created" | "executing" | "reviewing" | "waiting_approval" | "completed" | "failed",
  "current_iteration": 0,
  "max_iterations": 3,
  "quality_threshold": 0.80,
  "iterations": [ ... ],
  "pending_suggestions": [ ... ],
  "created_at": "2026-05-03T22:34:12.345678",
  "updated_at": "2026-05-03T22:34:12.345678"
}
```

**status 状态机**:

```
created → executing → reviewing → completed
                              ↘  waiting_approval (人工审批分支)
                              ↘  failed
```

**iterations[i] 嵌套结构**:

```json
{
  "iteration": 0,
  "trajectory": {
    "steps": [ /* WorkbenchTrajectoryStep[] */ ],
    "total_tokens": 12345,
    "search2qa_data": { /* 仅 search2qa */ }
  },
  "review": {
    "overall_score": 0.85,
    "suggestions": [
      { "level": "auto" | "confirm" | "approve", "category": "...", "options": [...] }
    ]
  },
  "config_snapshot": { ... },
  "search2qa_result": {
    "final_question": "...",
    "final_answer": "...",
    "status": "success" | "failed"
  },
  "timestamp": "2026-05-03T22:34:12.345678"
}
```

> M4(commit `d9dd200`)起,`trajectory.steps` 在 search2qa 场景下从沙箱产的
> `output/trace/<task_id>/<seed>_<ts>/trace_rewrite.json`(优先)或 `trace_init.json`
> 解析为 `WorkbenchTrajectoryStep[]`,**不再硬编码空数组**。

**已知约束**:

- ⚠️ detail 响应**不返回** `best_score` / `best_iteration` / `quality_progression`
  派生字段(那些在 `/export` 响应内)
- 客户端要拿"最佳轨迹"需自行 reduce `iterations[*].review.overall_score`,或调
  `POST /export` 让服务端代算
- `pending_suggestions` 仅在 `status == "waiting_approval"` 时非空

**错误码**:`404 Task not found`。

### 3.3 GET /api/tasks/{task_id}/events — 事件轮询

`backend.py:957-961`。

**传输模式声明**:**普通 GET 返 JSON**(`application/json`)。
⚠️ **不是 SSE** — 仓库内无 `StreamingResponse` / `EventSourceResponse`,
docstring 注明"前端轮询用"。

**`since` 参数语义**:整数 **offset/index**,⚠️ **不是 event id 也不是 timestamp**。
代码就是 `events[since:]`。客户端轮询模式:首次 `since=0`,服务端返
`{"events": [...], "total": N}`,下次轮询 `since=N`。

**响应**(HTTP 200):

```json
{
  "events": [ /* Event[] */ ],
  "total": 42
}
```

**单 Event schema**:

```json
{
  "type": "...",
  "message": "人类可读消息",
  "data": { },
  "timestamp": "2026-05-03T22:34:12.345678"
}
```

`data` 多数为 `{}`,少数事件附结构化数据(如 `agent_action` 塞 `{"step": step_data}`)。

**search2qa 涉及的 event types**(按 pipeline 阶段分组):

| 阶段 | event type |
| - | - |
| 启动 | `task_created`, `search2qa_start` |
| 沙箱阶段 | `sandbox_create`, `sandbox_ready`, `sandbox_cleanup` |
| 上传 / 安装 | `upload_start`, `upload`, `install_start`, `install`, `install_warn` |
| Pipeline 执行 | `pipeline_start`, `pipeline_log`, `pipeline_warn`, `collect_start` |
| 结束 | `pipeline_complete`, `trace_persisted`, `trace_persisted_warn` |
| 通用 review / 迭代 | `review_start`, `review_complete`, `auto_fix`, `waiting_approval`, `next_iteration` |
| 错误 | `warning`, `error` |

**关键约束**:

- ⚠️ **`pipeline_complete` 在 search2qa 会出现两次**:
  1. **第 1 次**(`search2qa/scene_handler.py:462`):sandbox 内 pipeline 结束
  2. **第 2 次**(`backend.py:673` / `:698`):review 后整 task 完成
- ⚠️ **客户端不能单凭 `pipeline_complete` event 判定任务结束**,必须配合
  `GET /api/tasks/{task_id}` 的 `task.status ∈ {completed, failed, waiting_approval}`
  **双信号**判停
- 无 event id,断线重连只能从 0 重拉或保存上次 `total`

### 3.4 POST /api/tasks/{task_id}/export — 导出

`backend.py:1052-1116`。请求**无 body**。

**响应**(HTTP 200):

```json
{
  "status": "exported",
  "file": "output/task_abc12345_export.json",
  "best_score": 0.87,
  "total_iterations": 3,
  "formats": ["SFT", "DPO", "RLHF"]
}
```

**写盘文件 schema**:

```json
{
  "task_id": "...",
  "task_desc": "...",
  "scene_type": "...",
  "total_iterations": 3,
  "best_iteration": 1,
  "best_score": 0.87,
  "quality_progression": [0.72, 0.87, 0.85],
  "best_trajectory": [ /* steps[] */ ],
  "all_trajectories": [
    { "iteration": 0, "score": 0.72, "steps": [...], "tokens": 12345 }
  ],
  "search2qa": {
    "final_question": "...",
    "final_answer": "...",
    "search2qa_data": { ... }
  }
}
```

**已知约束**:

- ⚠️ 接口**不接受** `format` 参数,响应里 `formats` 字段是**字面量字符串数组,无任何派发逻辑**
- 实际写盘的就是上面那个固定结构 JSON
- 客户端要 SFT / DPO / RLHF 格式需自行从 `best_trajectory` 转换
- Docker 部署时文件在容器内 `/app/output/{task_id}_export.json`,跨容器访问
  需挂载 `./output:/app/output`

**错误码**:`404 Task not found`,`400 No trajectories to export`(任务未跑完任何一轮)。

---

## 4. 其他场景 endpoint(参考)

| scene | scene_type 值(主链) | endpoint | 备注 |
| - | - | - | - |
| **search2qa** | `"search2qa"` | `POST /api/tasks` | **唯一**走主链 scene_type 派发 |
| toucan | — | `POST /api/toucan/tasks` | 独立 API,字段不通用 |
| toolace | — | `POST /api/toolace/tasks` | task_id prefix `toolace_` |
| envscaler | — | `POST /api/envscaler/tasks` | 需先 `POST /api/envscaler/upload-scene` |
| mobile_agent | — | `POST /api/mobile/tasks` | builtin / upload / local 三种 source |
| trajectory-agent | n/a | `POST /api/trajectory-agent/submit` | 编排接口,内部 dispatch 到具体场景 |

- **仅 search2qa** 走 `POST /api/tasks` 主链 `scene_type` 派发
- 其他场景各自独立 endpoint,字段 schema 与 search2qa 不通用
- 详细 schema 不在本文范围,demo 主链仅 search2qa

---

## 5. 错误响应格式

### 标准 FastAPI HTTPException

```json
{ "detail": "<message>" }
```

实际状态码集合:**400 / 404 / 501 / 502**。`422` 由 Pydantic 校验失败时 FastAPI
自动抛(代码不显式触发);`409` / `500` 主链不出现。

### POST /api/trajectory-agent/submit 特例

非 HTTPException,用自定义 `SubmitResponse` 序列化(HTTP **500**):

```json
{
  "status": "error",
  "error_code": "DISPATCH_FAILED" | "LLM_PROXY_UNAVAILABLE" | "LLM_BAD_REQUEST"
              | "LLM_RESPONSE_INVALID" | "SCENE_ROUTING_INVALID"
              | "CLARIFIER_INVALID" | "INTERNAL_ERROR",
  "message": "<exception str>",
  "scene": "<optional>"
}
```

### search2qa 主链错误码汇总

| HTTP | endpoint | detail |
| - | - | - |
| 404 | `GET /api/tasks/{task_id}` | `Task not found` |
| 404 | `GET /api/tasks/{task_id}/trajectory/{i}` | `Task not found` |
| 404 | 同上 | `Iteration not found`(`i ≥ len(iterations)`) |
| 404 | `POST /api/tasks/{task_id}/approve` | `Task not found` |
| 400 | 同上 | `Task is not waiting for approval` |
| 400 | 同上 | `Invalid suggestion index` |
| 404 | `POST /api/tasks/{task_id}/iterate` | `Task not found` |
| 400 | 同上 | `Already at max iterations` |
| 404 | `POST /api/tasks/{task_id}/export` | `Task not found` |
| 400 | 同上 | `No trajectories to export` |

---

## 6. 集成测试 Quick Start

### 提交一个 search2qa 任务

```bash
curl -X POST http://localhost:3100/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "task_desc": "合成医疗问诊领域的搜索 QA 数据",
    "scene_type": "search2qa",
    "seed": "糖尿病早期筛查",
    "qa_mode": "question",
    "max_iterations": 1,
    "max_evolutions": 1,
    "enable_evolution": true,
    "enable_rewrite": true
  }'
# 响应: {"task_id": "task_abc12345", "status": "created"}
```

### 轮询事件流

```bash
TASK_ID=task_abc12345
SINCE=0
curl "http://localhost:3100/api/tasks/${TASK_ID}/events?since=${SINCE}"
# 响应 {"events": [...], "total": N}
# 下次轮询 SINCE=N
```

### 拉任务详情

```bash
curl "http://localhost:3100/api/tasks/${TASK_ID}"
```

### 导出最终轨迹(任务完成后)

```bash
curl -X POST "http://localhost:3100/api/tasks/${TASK_ID}/export"
```

### 推荐客户端轮询模式

- **间隔**:2~3 秒
- **双停止信号**(任一触发即停):
  - events 里出现 `type == "error"`
  - `GET /api/tasks/{id}` 的 `task.status ∈ {completed, failed, waiting_approval}`
- **上限保护**:轮询次数 ≤ 1800(~60 min,对齐 batch preset 典型耗时)

---

## 7. 已知约束与现状

汇总全文标注的约束,Rui 集成测试一目了然:

- ⚠️ **`max_samples` 字段在 `POST /api/tasks` 被静默 ignore**(FastAPI `Config.extra="ignore"`),
  仅 `/api/trajectory-agent/submit` 路径走 clarifier `_scale_preset` `maps_to` 时才注入
- ⚠️ **`/export` 不接受 `format` 参数**,响应 `formats` 字段是字面量字符串数组,
  无任何派发逻辑
- ⚠️ **`pipeline_complete` 在 search2qa 双发**,不能单信号停轮询,需配合 `task.status` 判停
- ⚠️ **timestamp 无时区后缀**,跨容器排查需带 `task_id` 串联
- ⚠️ **detail 不返回 `best_*` 派生字段**,需 reduce `iterations` 或走 `/export`
- ⚠️ **跨场景 event type 不统一**:toucan 用 `pipeline_done` 收尾(不是 `pipeline_complete`);
  toolace 同时用 `pipeline_complete` + `completed`
- ⚠️ **export 文件路径硬编码相对路径** `output/{task_id}_export.json`,容器部署需挂卷
  `./output:/app/output`
- ⚠️ **`TaskCreateRequest.concurrent` 字段在 schema 里有但代码不消费**,纯占位
- ⚠️ **`GET /api/health` 的 `status` 永远返回 `"ok"`**,readiness 探测需检查
  `opensandbox` / `deepseek` 子字段
- ⚠️ **`scene_type` 无枚举校验**,传非 `"search2qa"` 会走通用 Agent 分支并尝试调 sandbox
- ⚠️ **CORS `allow_origins=["*"] + allow_credentials=True`** 是浏览器忽略 credentials
  的组合(spec 不允许),demo 阶段无 cookie/auth 不影响功能
