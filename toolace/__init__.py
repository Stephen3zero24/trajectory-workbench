"""
ToolACE — 工具调用轨迹数据合成场景

基于 ToolACE (ICLR 2025) + ToolACE-MT 论文思路实现的工具调用数据合成框架。

Pipeline:
  Step 1: 工具自进化合成 (Tool Self-Evolution Synthesis)
          输入源 tools → 完善 → 扩展生成 tools 组 → 耦合优化
  Step 2: 任务生成 (Self-Guided Dialog Generation)
          tools 格式转换 → 工具使用流程 → 任务生成 (支持跨组交叉、角色背景、缺参任务)
  Step 3: 轨迹生成 (Non-Autoregressive Trajectory Generation)
          tools+task 封装 → 多线程循环按轮次生成 role → 完成所有子 task
"""

__version__ = "0.1.0"
