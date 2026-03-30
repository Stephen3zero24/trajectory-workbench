# 🦤 Toucan 工具调用场景

基于 [Toucan](https://github.com/TheAgentArk/Toucan) 的 MCP 工具调用轨迹数据合成，集成到 Trajectory Workbench。

## 架构

```
Step 0: Smithery 配置 → MCP Server 注册表 (registry.json)
                ↓
Step 1: 问题合成 → Prompt生成 → LLM生成 → 句子嵌入去重
                ↓
Step 2: 质量检查 → 6维评分(难度/质量/真实性/独特性/可验证性/稳定性)
                ↓
Step 3: 轨迹生成 → LLM Function Calling + Smithery MCP → 单轮/多轮
                ↓
Review Agent → 数据集导出 (SFT / DPO / RLHF)
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements_toucan.txt

# 2. 配置
export DEEPSEEK_API_KEY="your-key"
export SMITHERY_API_KEY="your-smithery-key"  # 可选

# 3. 初始化 MCP 注册表
python -m toucan.step0_smithery_setup --no-fetch

# 4. 集成到 backend.py (见 backend_patch.py)

# 5. 或独立运行
python -m toucan.toucan_pipeline
```

## 文件结构

```
toucan/
├── __init__.py                 # 模块入口
├── config.py                   # 配置管理
├── step0_smithery_setup.py     # Smithery 配置 & MCP 元数据
├── step1_question_synthesis.py # 问题合成 (Prompt→生成→去重)
├── step2_quality_check.py      # 6维质量检查
├── step3_trajectory_gen.py     # Agent轨迹生成 (MCP工具调用)
├── toucan_pipeline.py          # Pipeline编排器
├── toucan_api.py               # FastAPI路由
├── backend_patch.py            # 集成指南
└── mcp_servers/registry.json   # MCP Server注册表(自动生成)
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/toucan/tasks` | 创建任务 |
| GET | `/api/toucan/tasks` | 列出任务 |
| GET | `/api/toucan/tasks/{id}` | 任务详情 |
| GET | `/api/toucan/tasks/{id}/events` | 事件流 |
| GET | `/api/toucan/servers` | MCP Server列表 |
| POST | `/api/toucan/servers/refresh` | 刷新注册表 |

## 预置 MCP Server

| ID | 名称 | 工具数 | 类别 |
|----|------|--------|------|
| exa | Exa Search | 3 | 搜索 |
| @anthropics/brave-search | Brave Search | 1 | 搜索 |
| @anthropics/github | GitHub | 4 | 开发 |
| @anthropics/filesystem | Filesystem | 3 | 文件 |
| mcp-server-time | Time | 1 | 工具 |
| mcp-server-sqlite | SQLite | 4 | 数据库 |
| @anthropics/memory | Memory | 2 | 记忆 |
| @anthropics/fetch | Fetch | 1 | 网络 |

## 采样策略

- **uniform**: 均匀覆盖每个Server
- **random**: 随机选择
- **power_law**: 幂律分布（模拟热门工具）
- **curated**: 优先工具最多的Server

## 参考

- [Toucan 论文](https://arxiv.org/abs/2510.01179)
- [Toucan GitHub](https://github.com/TheAgentArk/Toucan)
- [Smithery Connect](https://smithery.ai/docs/use/connect)
- [Qwen-Agent MCP](https://qwenlm.github.io/Qwen-Agent/en/guide/core_moduls/mcp/)
