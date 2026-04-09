"""
Mobile Agent — Android GUI 操控轨迹合成模块

基于 AgentScope Runtime MobileSandbox 的 Android GUI Agent 轨迹数据合成。
Agent 通过截图 + UI 树理解界面，决策 tap/swipe/input/key 动作，
采集完整的 screenshot → thought → action → result 交互轨迹。

Pipeline:
  Step 0: 场景加载 — 解析 mobile_scenarios.json
  Step 1: MobileSandbox 启动 — 拉取 agentscope/runtime-sandbox-mobile 镜像
  Step 2: Agent 轨迹生成 — VLM 驱动的 GUI 操控循环
  Review + 导出
"""

__version__ = "0.1.0"
