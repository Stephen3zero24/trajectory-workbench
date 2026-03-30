# 🔍 Search2QA — 搜索轨迹驱动的 QA 合成模块

基于 [WebExplorer](https://arxiv.org/abs/2509.06501) 论文思路实现的 QA 合成框架，作为 Trajectory Workbench 的预置场景。

## 核心思路

```
                    ┌─────────────────────────┐
                    │    Stage 1: 初始化 QA    │
                    │  seed → search → QA生成  │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Stage 2: 迭代复杂化     │
                    │  QA → 模糊化 → 更难的QA  │  ← Query Evolution
                    │    (循环 N 轮)           │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Stage 3: 轨迹改写       │
                    │  造题轨迹 → 答题轨迹      │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │      最终输出数据        │
                    │  question + answer +     │
                    │  search trajectory       │
                    └─────────────────────────┘
```

## 两种 QA 生成模式

### Question 模式（种子 → QA）

```
输入: seed = "巴西国家队"

→ 搜索 "巴西国家队" 相关信息
→ 浏览多个网页，发现跨源事实
→ 生成问题: "在巴西队2002年世界杯夺冠阵容中，哪位球员后来执教了与其效力过的
             意甲俱乐部同城的球队，并带队获得了欧洲冠军？"
→ 答案: "卡洛·安切洛蒂"（此处为示例）
```

### Answer 模式（答案 → 问题）

```
输入: seed = "拉蒙·基罗加"

→ 搜索 "拉蒙·基罗加" 的相关信息
→ 发现此人的多维度信息
→ 反向构造问题: "哪位阿根廷作家因其短篇小说集而被誉为
                 '南美的莫泊桑'，并最终在一次划船事故中去世？"
→ 答案: "拉蒙·基罗加"
```

## 文件结构

```
search2qa/
├── __init__.py              # 模块初始化
├── main.py                  # 入口：三阶段流水线编排
├── llm_engine.py            # LLM 多轮交互引擎（含 function calling）
├── tools.py                 # 工具实现（DuckDuckGo 搜索 + 网页爬取）
├── prompts.py               # 三阶段提示词模板
├── trace_manager.py         # 轨迹记录与管理
├── scene_handler.py         # 沙箱执行控制器（与 backend.py 集成）
├── backend_integration.py   # backend.py 集成说明与代码片段
├── requirements.txt         # 沙箱内 Python 依赖
└── README.md                # 本文档
```

## 独立使用（命令行）

```bash
# 确保设置了 API Key
export DEEPSEEK_API_KEY="your-key"

# Question 模式
python3 search2qa/main.py --seed "量子计算" --mode question --evolutions 2

# Answer 模式
python3 search2qa/main.py --seed "拉蒙·基罗加" --mode answer --evolutions 1

# 跳过复杂化和改写（仅生成初始 QA）
python3 search2qa/main.py --seed "世界杯" --mode question --no-evolution --no-rewrite

# 自定义参数
python3 search2qa/main.py \
    --seed "深度学习" \
    --mode question \
    --evolutions 3 \
    --model deepseek-chat \
    --temperature 0.7 \
    --max-turns 25 \
    --output-dir output/my_traces
```

## 集成到 backend.py

请参照 `backend_integration.py` 中的说明，需要在 `backend.py` 中进行以下修改：

1. **添加 import**：引入 `search2qa.scene_handler`
2. **扩展请求模型**：`TaskCreateRequest` 添加 search2qa 专用字段
3. **场景分发**：在 `run_single_iteration` 开头判断 `scene_type == "search2qa"`
4. **专用执行函数**：`run_search2qa_iteration` 调用沙箱执行
5. **专用评估函数**：`review_search2qa_quality` 评估 QA 质量
6. **更新场景列表**：`/api/scenes` 端点添加 search2qa

## 输出文件说明

每次运行在 `output/trace/{seed}_{timestamp}/` 下生成：

| 文件 | 说明 |
|------|------|
| `trace_init.json` | Stage 1 简化轨迹（工具结果截断至300字符） |
| `tool_results_init.json` | Stage 1 完整工具结果映射 |
| `trace_evolve_1.json` | Stage 2 第1轮演化轨迹 |
| `trace_evolve_2.json` | Stage 2 第2轮演化轨迹 |
| `trace_rewrite.json` | Stage 3 改写后的答题轨迹 |
| `final_output.json` | 最终汇总（含所有阶段结果） |

### trace 文件格式

```json
{
  "question": "最终问题",
  "answer": "答案",
  "iterations": 8,
  "stage": "init",
  "trace": [
    {"type": "llm_output", "role": "assistant", "content": "...", "timestamp": "..."},
    {"type": "tool_call", "tool_name": "search", "tool_args": "{...}", "tool_call_id": "call_xxx"},
    {"type": "tool_result", "content": "Result: ...(截断)", "tool_call_id": "call_xxx"},
    ...
  ]
}
```

## 工具说明

### search（搜索）

- 后端：DuckDuckGo（免费、无需 API Key）
- 返回：标题、URL、摘要
- 每次返回最多 8 条结果

### crawl（爬取）

按优先级尝试三种方式：
1. **crawl4ai**：基于 Playwright 的智能爬取（首选）
2. **trafilatura**：正文提取库
3. **requests + BeautifulSoup**：基础 HTML 解析（回退）

对 PDF 链接自动使用 PyMuPDF 解析。

## 与 WebExplorer 的关系

| 特性 | WebExplorer 论文 | 本实现 |
|------|-----------------|--------|
| 探索方式 | model-based exploration | ✅ 相同 |
| Query Evolution | long-to-short (5轮) | ✅ 支持 (可配置轮次) |
| 搜索引擎 | Google Search API | DuckDuckGo (免费) |
| 浏览工具 | 自定义 browse | crawl4ai + fallback |
| LLM | Claude/GPT-4 | DeepSeek (可替换) |
| 数据规模 | 40K QA pairs | 按需生成 |
| 开源框架 | ❌ 未开源 | ✅ 完整开源 |
| 轨迹改写 | 未提及 | ✅ 造题→答题轨迹 |
