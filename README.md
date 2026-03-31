# 📦 Trajectory Workbench

基于 [OpenSandbox](https://github.com/alibaba/OpenSandbox) 的 AI Agent 轨迹数据合成工作台。

通过在安全隔离的沙箱环境中运行 LLM 驱动的 Agent，采集真实的 **Observation → Thought → Action → Result** 交互轨迹，并通过 Review Agent 自动评估与自迭代优化，最终产出高质量的 SFT / DPO / RLHF 训练数据集。

---

## ✨ 核心特性

- **真实环境交互** — Agent 在 OpenSandbox 沙箱中执行真实命令，轨迹数据源于闭环交互而非凭空生成
- **8 大场景覆盖** — 内置 8 种主流 Agent 场景（含 4 个独立合成引擎），一套工作台产出多类型训练数据
- **LLM 驱动决策** — Agent 由 DeepSeek API 驱动（OpenAI 兼容格式），每一步自主推理和决策
- **自迭代闭环** — Review Agent 自动评估轨迹质量，按三级授权模型迭代优化
- **三级授权审批** — 🟢 低风险自动执行 → 🟡 中风险人工确认 → 🔴 高风险人工审批
- **可视化工作台** — Web UI 实时展示执行日志、质量评分、审批操作和迭代历史
- **多格式导出** — 支持 SFT / DPO / RLHF / Raw 等多种训练数据格式

---

## 🎯 支持场景

Trajectory Workbench 内置 8 种场景，覆盖当前主流 Agent 能力评估维度。其中 4 个场景由独立合成引擎驱动，具备完整的数据生成 Pipeline。

### 独立合成引擎场景

| 场景 | 引擎 | 参考论文 | 说明 |
|------|------|---------|------|
| 🏗️ EnvScaler 工具调用 | `envscaler` | — | 基于状态化环境骨架的工具调用轨迹合成，支持 reward 信号与 check function 验证 |
| 🔧 ToolACE 工具调用 | `toolace` | [ToolACE (ICLR 2025)](https://arxiv.org/abs/2409.00920)、[ToolACE-MT](https://arxiv.org/abs/2508.12685) | 工具自进化合成 + 多轮调用轨迹生成，含跨组交叉、角色背景注入、缺参追问 |
| 🔍 Search2QA | `search2qa` | [WebExplorer](https://arxiv.org/abs/2509.06501) | 搜索轨迹驱动的 QA 合成，支持 Query Evolution 与轨迹改写 |
| ⚙️ Toucan MCP 工具交互 | `toucan` | [Toucan](https://arxiv.org/abs/2510.01179) | 基于 Smithery MCP Server 注册表的工具调用轨迹合成，含 6 维质量检查 |

### 通用沙箱场景

| 场景 | ID | 说明 |
|------|----|------|
| 🖥️ GUI 操作 | `gui` | 浏览器 / 安卓系统的界面操控，生成 GUI 自动化与 RPA 训练数据 |
| 🌐 Deep Search | `deep_search` | 搜索引擎检索与信息整合，生成深度搜索和综合推理训练数据 |
| 🤖 多 Agent 协调 | `multi_agent` | 多智能体协作与交互，生成多轮多角色对话与协调决策训练数据 |
| 💻 代码执行 | `code_exec` | 代码编写、测试与调试，最基础也最通用的场景 |

---

## 🏗️ 系统架构

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│   Web UI    │────▶│    Backend API       │────▶│  OpenSandbox Server │
│  (React +   │     │    (FastAPI)          │     │  (沙箱控制面)        │
│   Vite)     │     │                      │     │                     │
│  port:5173  │     │  ┌────────────────┐  │     │  port:8080          │
└─────────────┘     │  │ Scene Engines  │  │     └──────────┬──────────┘
                    │  │                │  │                │
                    │  │ • EnvScaler    │  │                ▼
                    │  │ • ToolACE      │  │         ┌──────────────┐
                    │  │ • Search2QA    │  │         │ Docker 沙箱   │
                    │  │ • Toucan       │  │         │ (隔离执行环境) │
                    │  └────────────────┘  │         └──────────────┘
                    │                      │
                    │  port:3000            │
                    └──────────┬───────────┘
                               │
                        ┌──────▼──────┐
                        │ DeepSeek API│
                        │ (Agent 大脑) │
                        └─────────────┘
```

### 数据生成流程

```
用户提出任务 → 选择场景 → 生成 Pipeline → 沙箱执行 → 产出轨迹
                                                           ↓
                                                   Review Agent 评估
                                                           ↓
                       ┌── 🟢 自主执行区 → 自动修改，重跑
                       ├── 🟡 人工确认区 → Web UI 选择方案
                       └── 🔴 人工审批区 → Web UI 审批
                                                           ↓
                                                   质量达标 → 导出数据集
                                                       (SFT / DPO / RLHF)
```

---

## 📁 项目结构

```
trajectory-workbench/
├── README.md                 # 本文档
├── INSTALL_GUIDE.md          # 集成指南（新手版）
├── requirements.txt          # Python 依赖
├── backend.py                # 后端 API 服务 (FastAPI)，包含场景调度和完整执行逻辑
├── pipeline.py               # CLI Pipeline 编排脚本（可独立运行）
│
├── envscaler/                # 🏗️ EnvScaler 合成引擎
│   ├── config.py             #   配置 + MCP Server 模板 + Agent Prompt
│   ├── scene_manager.py      #   场景文件加载 / 解析 / 提取
│   ├── sandbox_runner.py     #   沙箱部署 MCP Server
│   ├── trajectory_gen.py     #   Agent 轨迹生成
│   ├── envscaler_pipeline.py #   Pipeline 编排 + Review + Export
│   └── envscaler_api.py      #   FastAPI 路由
│
├── toolace/                  # 🔧 ToolACE 合成引擎
│   ├── step1_tool_evolution.py   #   工具自进化合成 (TSS)
│   ├── step2_task_generation.py  #   任务生成 (SDG)
│   ├── step3_trajectory_gen.py   #   多轮轨迹生成 (ToolACE-MT)
│   ├── toolace_pipeline.py       #   Pipeline 编排 + Review + Export
│   └── toolace_api.py            #   FastAPI 路由
│
├── search2qa/                # 🔍 Search2QA 合成引擎
│   ├── main.py               #   三阶段流水线编排
│   ├── llm_engine.py         #   LLM 多轮交互引擎 (Function Calling)
│   ├── tools.py              #   工具实现 (DuckDuckGo + 网页爬取)
│   ├── prompts.py            #   三阶段提示词模板
│   ├── scene_handler.py      #   沙箱执行控制器
│   └── trace_manager.py      #   轨迹记录与管理
│
├── toucan/                   # ⚙️ Toucan 合成引擎
│   ├── step0_smithery_setup.py   #   Smithery MCP Server 注册
│   ├── step1_question_synthesis.py #  问题合成 + 嵌入去重
│   ├── step2_quality_check.py    #   6 维质量检查
│   ├── step3_trajectory_gen.py   #   Agent 轨迹生成 (MCP 调用)
│   ├── toucan_pipeline.py        #   Pipeline 编排
│   └── toucan_api.py             #   FastAPI 路由
│
├── web-ui/                   # 前端 Web UI
│   ├── src/
│   │   ├── App.jsx           #   主界面组件
│   │   └── App.css           #   样式
│   ├── package.json
│   └── vite.config.js
│
└── output/                   # 导出的轨迹数据（自动生成）
    ├── *_export.json         #   Web UI 导出格式
    ├── *_sft_*.jsonl         #   SFT 训练数据
    ├── *_dpo_*.jsonl         #   DPO 训练数据
    └── best_trajectory_*.json    # 最佳轨迹
```

---

## 📋 环境要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| macOS / Linux | — | 支持 Apple Silicon (M1/M2/M3/M4) |
| Docker Desktop | 4.0+ | 需分配至少 8GB 内存 |
| Python | 3.10+ | 推荐使用 `uv` 包管理器 |
| Node.js | 18+ | 用于前端 Web UI |
| DeepSeek API Key | — | 在 [platform.deepseek.com](https://platform.deepseek.com) 获取 |

---

## 🚀 快速开始

### 第一步：克隆项目

```bash
git clone https://github.com/Stephen3zero24/trajectory-workbench.git
cd trajectory-workbench
```

### 第二步：安装基础工具

**macOS 用户：**

```bash
# 安装 Homebrew（如未安装）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 uv（Python 包管理器）和 Node.js
brew install uv node
```

**安装 Docker Desktop：** 前往 [docker.com](https://www.docker.com/products/docker-desktop/) 下载安装。安装后进入 **Settings → Resources**，将 Memory 设为 **8GB**，CPUs 设为 **4**。

验证安装：

```bash
docker --version      # 应输出版本号
python3 --version     # 需要 3.10+
node --version        # 需要 18+
uv --version          # 应输出版本号
```

### 第三步：安装 Python 依赖

```bash
uv venv .venv
source .venv/bin/activate    # macOS / Linux
uv pip install -r requirements.txt
```

### 第四步：配置 OpenSandbox Server

```bash
opensandbox-server init-config ~/.sandbox.toml --example docker
```

### 第五步：配置 API Key

```bash
export DEEPSEEK_API_KEY="your-api-key-here"

# 建议写入 shell 配置文件使其永久生效
echo 'export DEEPSEEK_API_KEY="your-api-key-here"' >> ~/.zshrc
source ~/.zshrc
```

### 第六步：安装前端依赖

```bash
cd web-ui && npm install && cd ..
```

### 第七步：启动服务

需要打开 **3 个终端窗口**，分别启动 3 个服务：

**终端 1 — OpenSandbox Server（沙箱控制面）：**

```bash
source .venv/bin/activate
opensandbox-server
# ✅ INFO: Uvicorn running on http://127.0.0.1:8080
```

**终端 2 — Backend API（后端服务）：**

```bash
source .venv/bin/activate
export DEEPSEEK_API_KEY="your-api-key-here"
python3 backend.py
# ✅ 🚀 轨迹合成工作台后端启动
# ✅ INFO: Uvicorn running on http://0.0.0.0:3000
```

**终端 3 — Web UI（前端界面）：**

```bash
cd web-ui && npm run dev
# ✅ VITE ready — Local: http://localhost:5173/
```

### 第八步：打开浏览器

访问 **http://localhost:5173**，页面顶部应显示两个绿色状态标签：

- `OpenSandbox: connected` ✅
- `DeepSeek: configured` ✅

---

## 📖 使用教程

### 1. 定义任务

在首页任务描述框中输入希望 Agent 完成的任务（或点击示例按钮快速填入），选择场景类型，配置模型参数，点击 **"▶ 启动 Pipeline"**。

> 💡 **新手建议**：从「代码执行」场景开始，沙箱环境最通用、上手门槛最低。

### 2. 观察执行过程

页面自动进入"沙箱执行"阶段，可实时看到沙箱初始化日志、Agent 每一步的决策与执行结果、Review Agent 的评估过程。

### 3. 处理审批请求

当 Review Agent 提出修改建议时：

- **🟢 自主执行区**：低风险修改（如温度调整），系统已自动应用
- **🟡 人工确认区**：中风险修改（如任务描述优化），选择一个方案后确认
- **🔴 人工审批区**：高风险修改（如环境依赖变更），查看影响评估后审批

### 4. 导出数据集

轨迹质量达到阈值（默认 80 分）或手动点击"跳过迭代 · 直接导出"后，数据将导出至 `output/` 目录。

---

## 🔧 CLI 模式（无 Web UI）

```bash
source .venv/bin/activate
export DEEPSEEK_API_KEY="your-api-key-here"

# 通用 Pipeline（确保 OpenSandbox Server 已在另一终端运行）
python3 pipeline.py

# 独立运行各合成引擎
python -m toolace.toolace_pipeline --task-count 5 --expansion-count 2
python -m toucan.toucan_pipeline
python3 search2qa/main.py --seed "量子计算" --mode question --evolutions 2
```

可修改 `pipeline.py` 底部的 `TaskConfig` 切换场景：

```python
config = TaskConfig(
    task_id="task_001",
    task_desc="你的任务描述...",
    scene_type="mcp_tool",    # mcp_tool / gui / deep_search / multi_agent / code_exec
    model="deepseek-chat",
    temperature=0.7,
    max_steps=15,
)
```

---

## 🔌 场景引擎详解

### 🏗️ EnvScaler — 状态化环境工具调用

将外部生成的状态化环境（由 `skel_builder` + `scen_generator` 产出）部署为沙箱内 MCP Server，Agent 通过 `scene_action` 工具与环境交互，支持 reward 信号和 check function 自动验证。

与 Toucan / ToolACE 的核心区别在于：环境是**有状态**的领域模拟（如诊所预约系统、库存管理等），而非无状态 API 调用。

### 🔧 ToolACE — 工具自进化 + 多轮轨迹

三阶段 Pipeline：工具自进化合成 (TSS) → 任务生成 (SDG) → 多轮轨迹生成 (ToolACE-MT)。相比原论文新增了跨组交叉任务生成、8 种角色背景注入、缺参追问等改进，使生成的轨迹数据更接近真实场景。

### 🔍 Search2QA — 搜索轨迹驱动的 QA 合成

三阶段流水线：初始化 QA → 迭代复杂化 (Query Evolution) → 轨迹改写（造题轨迹 → 答题轨迹）。支持 Question 模式（种子→QA）和 Answer 模式（答案→问题），使用 DuckDuckGo 搜索（免费无需 API Key）。

### ⚙️ Toucan — MCP 工具调用

基于 Smithery MCP Server 注册表，Pipeline 为：MCP Server 注册 → 问题合成 + 嵌入去重 → 6 维质量检查（难度/质量/真实性/独特性/可验证性/稳定性） → Agent 轨迹生成。预置 8 个 MCP Server（Exa Search、Brave Search、GitHub、Filesystem 等），支持 4 种采样策略。

---

## ⚙️ 配置说明

### 服务端口

| 服务 | 默认端口 | 配置方式 |
|------|---------|---------| 
| OpenSandbox Server | 8080 | `~/.sandbox.toml` |
| Backend API | 3000 | `backend.py` 末行 |
| Web UI | 5173 | Vite 默认 |

### 环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API 密钥 |
| `OPENSANDBOX_SERVER` | ❌ | OpenSandbox 地址，默认 `http://127.0.0.1:8080` |
| `DEEPSEEK_BASE_URL` | ❌ | DeepSeek API 地址，默认 `https://api.deepseek.com` |
| `SMITHERY_API_KEY` | ❌ | Smithery API 密钥（Toucan 场景可选） |

### 支持的模型

| 模型 ID | 名称 | 说明 |
|--------|------|------|
| `deepseek-chat` | DeepSeek-Chat (V3.2) | 默认模型，性价比高 |
| `deepseek-reasoner` | DeepSeek-Reasoner (R1) | 推理能力更强，适合复杂任务 |

DeepSeek API 使用 OpenAI 兼容格式，可替换为任何兼容的模型提供商。

### Docker 资源配置（推荐）

| 资源 | 推荐值 | 说明 |
|------|-------|------|
| CPUs | 4 | 留一半给宿主机 |
| Memory | 8 GB | 每个沙箱约占 1–2 GB |
| Disk | 40 GB+ | 沙箱镜像需要存储空间 |

---

## ❓ 常见问题

**Q: Docker 镜像拉取很慢？**
在 Docker Desktop → Settings → Docker Engine 中添加镜像加速：
```json
{ "registry-mirrors": ["https://mirror.ccs.tencentyun.com"] }
```

**Q: SDK 创建沙箱报 `NoneType` 错误？**
这是 SDK 0.1.5 和 Server 0.1.8 之间的已知兼容性问题。项目已通过 `httpx 创建 + SDK connect 接管` 的方式绕过，无需额外处理。

**Q: 如何更换 LLM？**
修改 `DEEPSEEK_BASE_URL` 环境变量指向其他 OpenAI 兼容 API 地址即可，同时修改 `DEEPSEEK_API_KEY` 为对应密钥。

**Q: 不同场景对沙箱环境有什么要求？**
所有场景共用同一个沙箱镜像（`opensandbox/code-interpreter:v1.0.2`），差异体现在 Agent 的系统提示和任务描述上。后续计划为 GUI 操作场景提供带桌面环境的专用镜像。

**Q: 并发多少个沙箱合适？**
16 GB 内存的机器建议最多同时运行 3 个沙箱，可在 Web UI 中调整并发数。

**Q: 轨迹质量一直不达标？**
可尝试：简化任务描述 → 降低质量阈值（0.8→0.7） → 增加最大迭代轮次 → 调低 Temperature（如 0.3） → 切换至 `deepseek-reasoner` 模型。

---

## 🗺️ Roadmap

- [ ] GUI 操作场景专用桌面环境镜像
- [ ] Deep Search 场景接入真实搜索引擎 API
- [ ] 多 Agent 协调场景支持自定义角色编排
- [ ] 更多导出格式（ShareGPT、Alpaca）
- [ ] 接入更多 LLM 提供商（OpenAI、Anthropic、本地模型）
- [ ] 批量任务调度与数据集自动化生产

---

## 🤝 技术栈

| 层级 | 技术 |
|------|------|
| 沙箱平台 | [OpenSandbox](https://github.com/alibaba/OpenSandbox) (Alibaba) |
| LLM API | [DeepSeek](https://platform.deepseek.com) (OpenAI 兼容格式) |
| 后端 | Python 3.10+ · FastAPI · uvicorn · httpx |
| 前端 | React 19 · Vite 8 |
| 合成引擎 | ToolACE · Search2QA (WebExplorer) · Toucan · EnvScaler |
| 容器化 | Docker |
| MCP | fastmcp · qwen-agent · sentence-transformers |

---

## 📚 参考论文

- [ToolACE: Winning the Points of LLM Function Calling](https://arxiv.org/abs/2409.00920) — ICLR 2025
- [ToolACE-MT: Non-Autoregressive Generation for Agentic Multi-Turn Interaction](https://arxiv.org/abs/2508.12685)
- [WebExplorer: Towards Building an Open Web Agent](https://arxiv.org/abs/2509.06501)
- [Toucan: Generating Diverse MCP Tool-Use Scenarios](https://arxiv.org/abs/2510.01179)

---

## 📄 License

Apache 2.0
