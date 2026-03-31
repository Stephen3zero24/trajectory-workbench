# 📦 Trajectory Workbench

基于 [OpenSandbox](https://github.com/alibaba/OpenSandbox) 的 AI Agent 轨迹数据合成工作台。

通过在安全隔离的沙箱环境中运行 LLM 驱动的 Agent，采集真实的 **Observation → Thought → Action → Result** 交互轨迹，并通过 Review Agent 自动评估和自迭代优化，最终产出高质量的 SFT / DPO / RLHF 训练数据集。

---

## ✨ 核心特性

- **真实环境交互**：Agent 在 OpenSandbox 沙箱中执行真实命令，轨迹数据来源于闭环交互而非凭空生成
- **多场景覆盖**：内置 5 种主流 Agent 场景，一套工作台即可产出多类型训练数据
- **LLM 驱动决策**：Agent 由 DeepSeek-chat API 驱动，每一步自主推理和决策
- **自迭代闭环**：Review Agent 自动评估轨迹质量，按三级授权模型迭代优化
- **三级授权审批**：低风险修改自动执行 → 中风险修改人工确认 → 高风险修改人工审批
- **可视化工作台**：Web UI 实时展示执行日志、质量评分、审批操作和迭代历史

---

## 🎯 支持场景

Trajectory Workbench 内置了 5 种 Agent 场景，覆盖当前主流的 Agent 能力评估维度。在 Web UI 或 CLI 中选择场景类型后，系统会在对应的沙箱环境中采集轨迹数据。

### ⚙️ MCP 工具交互（`mcp_tool`）

针对 Agent Harness 中的 MCP（Model Context Protocol）工具调用场景。Agent 需要根据任务描述，从可用工具集中选择合适的工具，正确构造参数并解析返回结果。适用于生成工具选择与调用链的训练数据。

**典型任务示例**：调用文件管理工具创建目录结构、使用数据库工具执行查询、组合多个工具完成复合任务。

### 🖥️ GUI 操作（`gui`）

针对浏览器或安卓系统的 GUI 操控场景。Agent 需要理解界面元素、规划操作序列、执行点击/输入/滚动等交互动作。适用于生成 GUI 自动化、RPA 相关的训练数据。

**典型任务示例**：在浏览器中填写表单并提交、在系统设置中修改配置项、在应用内完成多步操作流程。

### 🔍 Deep Search（`deep_search`）

针对搜索引擎检索与信息整合场景。Agent 需要将复杂问题拆解为多个子查询，逐步检索、筛选、交叉验证信息，最终整合形成结构化答案。适用于生成深度搜索和信息综合推理的训练数据。

**典型任务示例**：对比多家公司的技术方案优劣、收集某领域近期研究进展并归纳趋势、验证某一说法的真实性。

### 🤖 多 Agent 协调（`multi_agent`）

针对多智能体协作与交互场景。多个 Agent 角色各司其职，通过消息传递、任务委派、结果汇总完成协作任务。适用于生成多轮多角色对话与协调决策的训练数据。

**典型任务示例**：产品经理 Agent 提出需求 → 开发 Agent 编写代码 → 测试 Agent 验证结果；多个研究 Agent 分工调研后协作撰写报告。

### 💻 代码执行（`code_exec`）

针对代码编写、测试与执行场景。Agent 需要在沙箱中编写代码、安装依赖、运行测试，遇到错误时自主调试修复。这是最基础也最常用的场景，适用于生成代码生成与调试相关的训练数据。

**典型任务示例**：创建 Python 项目并实现指定功能、编写单元测试并确保通过、调试并修复已有代码中的 bug。

---

## 🏗️ 系统架构

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────┐
│   Web UI    │────▶│  Backend API │────▶│  OpenSandbox Server │
│  (Vite)     │     │  (FastAPI)   │     │  (沙箱控制面)        │
│  port:5173  │     │  port:3000   │     │  port:8080          │
└─────────────┘     └──────┬───────┘     └──────────┬──────────┘
                           │                        │
                           ▼                        ▼
                    ┌──────────────┐         ┌──────────────┐
                    │ DeepSeek API │         │ Docker 沙箱   │
                    │ (Agent大脑)  │         │ (执行环境)    │
                    └──────────────┘         └──────────────┘
```

**工作流程：**

```
用户提出任务 → 选择场景 → 生成Pipeline → OpenSandbox执行 → 产出轨迹
                                                               ↓
                                                       Review Agent 评估
                                                               ↓
                           ┌── 🟢 自主执行区 → 自动修改，重跑
                           ├── 🟡 人工确认区 → Web UI选择方案
                           └── 🔴 人工审批区 → Web UI审批
                                                               ↓
                                                       质量达标 → 导出数据集
```

---

## 📋 环境要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| macOS / Linux | - | 支持 Apple Silicon (M1/M2/M3/M4) |
| Docker Desktop | 4.0+ | 需分配至少 8GB 内存 |
| Python | 3.10+ | 推荐使用 `uv` 包管理器 |
| Node.js | 18+ | 用于前端 Web UI |
| DeepSeek API Key | - | 在 [platform.deepseek.com](https://platform.deepseek.com) 获取 |

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

**安装 Docker Desktop：**

前往 [docker.com](https://www.docker.com/products/docker-desktop/) 下载并安装。安装后打开 Docker Desktop，进入 **Settings → Resources**，将 Memory 设置为 **8GB**，CPUs 设置为 **4**。

验证安装：

```bash
docker --version      # 应输出版本号
python3 --version     # 需要 3.10+
node --version        # 需要 18+
uv --version          # 应输出版本号
```

### 第三步：安装 Python 依赖

```bash
# 创建虚拟环境
uv venv .venv
source .venv/bin/activate    # macOS/Linux

# 安装依赖
uv pip install -r requirements.txt
```

### 第四步：配置 OpenSandbox Server

```bash
# 初始化配置文件
opensandbox-server init-config ~/.sandbox.toml --example docker
```

### 第五步：配置 DeepSeek API Key

在 [platform.deepseek.com](https://platform.deepseek.com) 注册并获取 API Key，然后设置环境变量：

```bash
export DEEPSEEK_API_KEY="your-api-key-here"
```

> 💡 **建议**：将这行添加到 `~/.zshrc` 或 `~/.bashrc` 中，这样每次打开终端会自动生效：
>
> ```bash
> echo 'export DEEPSEEK_API_KEY="your-api-key-here"' >> ~/.zshrc
> source ~/.zshrc
> ```

### 第六步：安装前端依赖

```bash
cd web-ui
npm install
cd ..
```

### 第七步：启动所有服务

需要打开 **3 个终端窗口**，分别启动 3 个服务：

**终端 1 — OpenSandbox Server（沙箱控制面）：**

```bash
cd trajectory-workbench
source .venv/bin/activate
opensandbox-server
```

看到以下输出说明启动成功：

```
INFO: Docker service initialized from environment
INFO: Uvicorn running on http://127.0.0.1:8080
```

**终端 2 — Backend API（后端服务）：**

```bash
cd trajectory-workbench
source .venv/bin/activate
export DEEPSEEK_API_KEY="your-api-key-here"
python3 backend.py
```

看到以下输出说明启动成功：

```
🚀 轨迹合成工作台后端启动
   OpenSandbox Server: http://127.0.0.1:8080
   DeepSeek API Key: 已配置
INFO: Uvicorn running on http://0.0.0.0:3000
```

**终端 3 — Web UI（前端界面）：**

```bash
cd trajectory-workbench/web-ui
npm run dev
```

看到以下输出说明启动成功：

```
VITE ready in XXX ms
➜  Local: http://localhost:5173/
```

### 第八步：打开浏览器使用

在浏览器中访问 **http://localhost:5173**

页面顶部应显示两个绿色状态标签：

- `OpenSandbox: connected` ✅
- `DeepSeek: configured` ✅

如果显示红色，请检查对应的服务是否正常启动。

---

## 📖 使用教程

### 1. 定义任务

在首页的任务描述框中输入你希望 Agent 完成的任务，或者点击示例按钮快速填入预设任务。

然后选择场景类型（MCP工具交互 / GUI操作 / Deep Search / 多Agent协调 / 代码执行），配置模型参数，点击 **"▶ 启动Pipeline"**。

> 💡 **场景选择建议**：不确定时从「代码执行」开始，它的沙箱环境最通用、上手门槛最低。熟悉流程后再尝试其他场景。

### 2. 观察执行过程

页面自动跳转到"沙箱执行"阶段，你可以实时看到：

- 沙箱创建和初始化日志
- Agent 每一步的决策和执行结果
- Review Agent 的评估过程

### 3. 处理审批请求

如果 Review Agent 提出修改建议，页面会跳转到"评估审批"阶段：

- **🟢 自主执行区**：低风险修改（如温度调整），系统已自动应用，无需操作
- **🟡 人工确认区**：中风险修改（如任务描述优化），选择一个方案后点击"确认选择"
- **🔴 人工审批区**：高风险修改（如环境依赖变更），查看影响评估后点击"批准"或"拒绝"

处理完所有审批后，系统自动启动下一轮迭代。

### 4. 导出数据集

当轨迹质量达到阈值（默认 80 分），或者你手动点击"跳过迭代 · 直接导出"，系统会将轨迹数据导出为 SFT / DPO / RLHF 格式，保存在项目目录的 `output/` 文件夹中。

---

## 🔧 仅使用命令行（无 Web UI）

如果你不需要 Web UI，可以直接运行 Pipeline 脚本：

```bash
source .venv/bin/activate
export DEEPSEEK_API_KEY="your-api-key-here"

# 确保 OpenSandbox Server 在另一个终端运行着
python3 pipeline.py
```

脚本会在终端中输出完整的执行过程和质量评估结果。CLI 模式同样支持三级授权交互（通过终端输入选择/审批）。

你也可以修改 `pipeline.py` 底部的 `TaskConfig` 来切换不同场景：

```python
config = TaskConfig(
    task_id="task_001",
    task_desc="你的任务描述...",
    scene_type="mcp_tool",   # 修改为目标场景: mcp_tool / gui / deep_search / multi_agent / code_exec
    model="deepseek-chat",
    temperature=0.7,
    max_steps=15,
)
```

---

## 📁 项目结构

```
trajectory-workbench/
├── README.md                # 本文档
├── requirements.txt         # Python 依赖
├── .gitignore              # Git 忽略规则
├── backend.py              # 后端 API 服务（FastAPI），包含场景定义和完整执行逻辑
├── pipeline.py             # Pipeline 编排脚本（可独立运行的 CLI 版本）
├── web-ui/                 # 前端 Web UI
│   ├── src/
│   │   └── App.jsx         # 主界面组件（场景选择、执行监控、审批交互）
│   ├── package.json
│   └── ...
└── output/                 # 导出的轨迹数据（自动生成，不提交到 Git）
    ├── *_export.json       # Web UI 导出格式
    ├── *_sft_*.jsonl       # SFT 训练数据
    └── best_trajectory_*.json  # 最佳轨迹
```

---

## ⚙️ 配置说明

### 服务端口

| 服务 | 默认端口 | 配置方式 |
|------|---------|---------|
| OpenSandbox Server | 8080 | 在 `~/.sandbox.toml` 中配置 |
| Backend API | 3000 | 修改 `backend.py` 最后一行 |
| Web UI | 5173 | Vite 默认端口 |

### 环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API 密钥 |
| `OPENSANDBOX_SERVER` | ❌ | OpenSandbox 地址，默认 `http://127.0.0.1:8080` |
| `DEEPSEEK_BASE_URL` | ❌ | DeepSeek API 地址，默认 `https://api.deepseek.com` |

### 支持的模型

| 模型 ID | 名称 | 说明 |
|--------|------|------|
| `deepseek-chat` | DeepSeek-Chat (V3.2) | 默认模型，性价比高 |
| `deepseek-reasoner` | DeepSeek-Reasoner (R1) | 推理能力更强，适合复杂任务 |

DeepSeek API 使用 OpenAI 兼容格式，你也可以替换为其他兼容模型（见 FAQ）。

### Docker 资源配置（推荐）

在 Docker Desktop → Settings → Resources 中设置：

| 资源 | 推荐值 | 说明 |
|------|-------|------|
| CPUs | 4 | 留一半给宿主机 |
| Memory | 8 GB | 每个沙箱约占 1-2GB |
| Disk | 40 GB+ | 沙箱镜像需要存储空间 |

---

## ❓ 常见问题

### Q: Docker 镜像拉取很慢怎么办？

在 Docker Desktop → Settings → Docker Engine 中添加镜像加速：

```json
{
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com"]
}
```

### Q: SDK 创建沙箱报 `NoneType` 错误？

这是 SDK 0.1.5 和 Server 0.1.8 之间的已知兼容性问题。本项目已通过 `httpx 创建 + SDK connect 接管` 的方式绕过，无需额外处理。

### Q: 如何更换为其他 LLM？

DeepSeek API 使用 OpenAI 兼容格式，你可以在 `backend.py` 和 `pipeline.py` 中修改以下配置来切换模型：

```python
DEEPSEEK_BASE_URL = "https://api.openai.com/v1"  # 改为其他 API 地址
```

然后修改环境变量：

```bash
export DEEPSEEK_API_KEY="your-other-api-key"
```

### Q: 不同场景对沙箱环境有什么要求？

目前所有场景共用同一个沙箱镜像（`opensandbox/code-interpreter:v1.0.2`），场景差异主要体现在 Agent 的系统提示和任务描述上。后续计划为 GUI 操作场景提供带有桌面环境的专用镜像。

### Q: 并发多少个沙箱合适？

16GB 内存的机器建议最多同时运行 3 个沙箱。可在 Web UI 的 Pipeline 配置中调整并发数。

### Q: 轨迹质量一直不达标怎么办？

尝试以下方法：

1. 简化任务描述，降低任务难度
2. 降低质量达标阈值（如从 0.8 降到 0.7）
3. 增加最大迭代轮次
4. 调低 Temperature（如 0.3），提高 Agent 输出的确定性
5. 切换到 `deepseek-reasoner` 模型获得更强的推理能力

---

## 🗺️ Roadmap

- [ ] 为 GUI 操作场景提供带有桌面环境的沙箱镜像
- [ ] Deep Search 场景接入真实搜索引擎 API
- [ ] 多 Agent 协调场景支持自定义角色编排
- [ ] 支持更多导出格式（ShareGPT、Alpaca）
- [ ] 接入更多 LLM 提供商（OpenAI、Anthropic、本地模型）
- [ ] 批量任务调度与数据集自动化生产

---

## 🤝 技术栈

- **沙箱平台**：[OpenSandbox](https://github.com/alibaba/OpenSandbox) (Alibaba)
- **LLM API**：[DeepSeek](https://platform.deepseek.com) (OpenAI 兼容格式)
- **后端**：Python + FastAPI + uvicorn
- **前端**：React + Vite
- **容器化**：Docker

## 📄 License

Apache 2.0
