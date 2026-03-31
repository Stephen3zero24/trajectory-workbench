"""
EnvScaler — 工具调用场景模块

基于 EnvScaler 环境骨架系统的工具调用轨迹数据合成。

整体流程:
  skel_builder（外部）→ scen_generator（外部）→ 场景文件 →
  本模块: MCP Server 启动 → Agent 交互 → 轨迹采集 → Review → 导出

Pipeline（本模块内）:
  Step 0: 场景文件管理 — 加载/提取 env_scenario.json + filtered_env_metadata.json
  Step 1: MCP Server 部署 — 在 OpenSandbox 中启动 fastmcp 场景服务器
  Step 2: Agent 轨迹生成 — qwen-agent / DeepSeek 通过 MCP 工具交互采集轨迹
  Review + 导出
"""

__version__ = "0.1.0"
