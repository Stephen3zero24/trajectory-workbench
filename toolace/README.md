# 🔧 ToolACE — 工具调用轨迹数据合成

基于 [ToolACE](https://arxiv.org/abs/2409.00920) (ICLR 2025) 和 [ToolACE-MT](https://arxiv.org/abs/2508.12685) 论文思路实现的工具调用数据合成框架，集成到 Trajectory Workbench。

## 架构

```
Step 1: 工具自进化合成 (TSS)
  源 tools → 完善 refinement → 扩展 expansion → 工具组 → 耦合优化 coupling
                ↓
Step 2: 任务生成 (SDG)
  工具组 → 格式转换 → 使用流程 → 任务生成
  改进: ├── 标准任务（单组内）
        ├── 跨组交叉任务（相似label两两组合）
        ├── 角色背景注入（8种预置角色）
        └── 缺参任务（缺少参数→追问→补充）
                ↓
Step 3: 轨迹生成 (ToolACE-MT)
  tools+task 封装 → 线程池并发 → 三阶段生成:
    3.1 粗粒度初始化（对话骨架）
    3.2 迭代精炼（mask-and-fill）
    3.3 离线验证（6维评分）
                ↓
Review Agent → 数据集导出 (SFT / DPO / Raw)
```

## 文件结构

```
toolace/
├── __init__.py                  # 模块入口
├── config.py                    # 配置管理 & 预置源工具
├── llm_utils.py                 # LLM 调用工具函数
├── prompts.py                   # 三阶段提示词模板
├── step1_tool_evolution.py      # Step 1: 工具自进化合成
├── step2_task_generation.py     # Step 2: 任务生成（含改进）
├── step3_trajectory_gen.py      # Step 3: 轨迹生成（ToolACE-MT）
├── toolace_pipeline.py          # Pipeline 编排 + Review + 导出
├── toolace_api.py               # FastAPI 路由
├── backend_patch.py             # backend.py 集成指南
└── README.md                    # 本文档
```

## 快速开始

```bash
# 确保设置了 API Key
export DEEPSEEK_API_KEY="your-key"

# 独立运行（CLI）
python -m toolace.toolace_pipeline --task-count 5 --expansion-count 2

# 或集成到 backend.py（见 backend_patch.py）
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/toolace/tasks` | 创建任务 |
| GET | `/api/toolace/tasks` | 列出任务 |
| GET | `/api/toolace/tasks/{id}` | 任务详情 |
| GET | `/api/toolace/tasks/{id}/events` | 事件流 |
| GET | `/api/toolace/presets` | 预置工具 & 角色列表 |

## 改进点（相比原论文）

### 跨组交叉任务生成
- 按 label 对工具分组
- 同 label 工具两两组合
- 排除名称过于相似的组（如 "Model Context Protocol Reference Servers" 与 "Model Context Protocol Servers"）
- 跨组任务要求同时使用两个组的工具，数据有合理流转

### 角色背景注入
- 8 种预置角色背景（运维、分析师、PM、客服、营销、研发、财务、运营）
- 在任务描述中融入角色视角和需求
- 使生成的任务更接近真实场景

### 缺参任务
- 按配置比例（默认 30%）生成参数不完整的任务
- 标注缺失参数和追问方式
- 轨迹生成时体现 assistant 追问 → user 补充 → 执行 的完整流程
- 产出更自然的多轮交互数据

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `expansion_count` | 3 | 每个源工具扩展生成的新工具数 |
| `coupling_rounds` | 2 | 耦合优化轮次 |
| `task_count` | 10 | 生成任务总数 |
| `enable_cross_group` | true | 启用跨组交叉 |
| `enable_role_background` | true | 启用角色背景 |
| `missing_param_ratio` | 0.3 | 缺参任务比例 |
| `max_turns` | 15 | 轨迹最大轮次 |
| `max_workers` | 3 | 并发线程数 |
| `quality_threshold` | 0.80 | 质量达标阈值 |

## 输出格式

### SFT (`_sft.jsonl`)
```json
{
  "id": "traj_xxx",
  "task_id": "task_001",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "用户任务"},
    {"role": "assistant", "thought": "推理", "tool_calls": [...]},
    {"role": "tool", "tool_results": [...]},
    {"role": "assistant", "content": "最终回复"}
  ],
  "metadata": {"task_type": "standard", "quality_score": 0.85}
}
```

### DPO (`_dpo.jsonl`)
```json
{
  "prompt": "任务描述",
  "chosen": "高质量回复",
  "rejected": "低质量回复",
  "chosen_score": 0.9,
  "rejected_score": 0.5
}
```

## 参考

- [ToolACE: Winning the Points of LLM Function Calling](https://arxiv.org/abs/2409.00920) (ICLR 2025)
- [ToolACE-MT: Non-Autoregressive Generation for Agentic Multi-Turn Interaction](https://arxiv.org/abs/2508.12685)
- [ToolACE HuggingFace](https://huggingface.co/datasets/Team-ACE/ToolACE)
- [ToolACE GitHub](https://github.com/Team-ACE/ToolACE)
