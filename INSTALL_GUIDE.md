# 🛠️ Search2QA 集成指南（新手版）

## 你拿到了什么？

```
这个压缩包里有：
├── backend.py           ← 用这个替换你项目里的 backend.py（已集成 search2qa）
└── search2qa/           ← 整个文件夹复制到你项目根目录
    ├── __init__.py
    ├── main.py
    ├── llm_engine.py
    ├── tools.py
    ├── prompts.py
    ├── trace_manager.py
    ├── scene_handler.py
    ├── requirements.txt
    └── README.md
```

## 操作步骤

### Step 1: 解压并复制文件

解压这个压缩包后，你会看到 `backend.py` 和 `search2qa/` 文件夹。

把它们复制到你的 `trajectory-workbench/` 项目目录中：
- `backend.py` 会替换掉原来的 `backend.py`（建议先备份原文件）
- `search2qa/` 整个文件夹放到项目根目录

复制完成后，你的项目结构应该是：
```
trajectory-workbench/
├── backend.py              ← 新的（已集成 search2qa）
├── pipeline.py             ← 原有的（不需要改动）
├── requirements.txt        ← 原有的
├── search2qa/              ← 新增的文件夹
│   ├── __init__.py
│   ├── main.py
│   ├── llm_engine.py
│   ├── tools.py
│   ├── prompts.py
│   ├── trace_manager.py
│   ├── scene_handler.py
│   ├── requirements.txt
│   └── README.md
└── web-ui/                 ← 原有的（前端暂不需要改动）
```

### Step 2: 完成！启动测试

启动方式和之前完全一样（3 个终端窗口）。

在 Web UI 的场景选择中，你会看到新增的 "Search2QA" 选项（🔍图标）。

---

这就是全部操作了！只需要复制 2 样东西进你的项目。
