"""
Search2QA Trace Manager — 轨迹记录与管理

负责：
- 记录每一步交互（LLM输出、工具调用、工具结果）
- 分离存储简化轨迹和完整工具结果
- 导出最终轨迹数据
"""

import json
import os
import time
from datetime import datetime
from typing import Optional


class TraceManager:
    """轨迹管理器：记录完整的 LLM-Tool 交互过程"""

    def __init__(self, run_folder: str):
        self.run_folder = run_folder
        self.trace: list = []           # 简化轨迹
        self.tool_results: dict = {}    # 完整工具结果（tool_call_id -> full_result）
        self.metadata: dict = {
            "start_time": datetime.now().isoformat(),
            "end_time": None,
        }
        os.makedirs(run_folder, exist_ok=True)

    def add_llm_output(self, content: str, role: str = "assistant"):
        """记录 LLM 输出"""
        self.trace.append({
            "type": "llm_output",
            "role": role,
            "content": content,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def add_tool_call(self, tool_name: str, tool_args: dict, tool_call_id: str):
        """记录工具调用"""
        self.trace.append({
            "type": "tool_call",
            "tool_name": tool_name,
            "tool_args": json.dumps(tool_args, ensure_ascii=False),
            "tool_call_id": tool_call_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def add_tool_result(self, content: str, tool_call_id: str):
        """记录工具执行结果（简化版存轨迹，完整版存 tool_results）"""
        # 保存完整结果
        self.tool_results[tool_call_id] = content

        # 轨迹中只保存截断版本
        truncated = content[:300] + "..." if len(content) > 300 else content
        self.trace.append({
            "type": "tool_result",
            "content": truncated,
            "tool_call_id": tool_call_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def add_stage_marker(self, stage: str, info: str = ""):
        """添加阶段标记"""
        self.trace.append({
            "type": "stage_marker",
            "stage": stage,
            "info": info,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def save(self, question: str = "", answer: str = "",
             iterations: int = 0, stage: str = ""):
        """保存轨迹到文件"""
        self.metadata["end_time"] = datetime.now().isoformat()

        # trace.json — 简化轨迹
        trace_data = {
            "question": question,
            "answer": answer,
            "iterations": iterations,
            "stage": stage,
            "metadata": self.metadata,
            "trace": self.trace,
        }

        trace_path = os.path.join(self.run_folder, f"trace_{stage}.json")
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(trace_data, f, ensure_ascii=False, indent=2)

        # tool_results.json — 完整工具结果
        results_path = os.path.join(self.run_folder, f"tool_results_{stage}.json")
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(self.tool_results, f, ensure_ascii=False, indent=2)

        return trace_path, results_path

    def get_trace_summary(self) -> dict:
        """获取轨迹摘要统计"""
        tool_calls = [e for e in self.trace if e["type"] == "tool_call"]
        search_count = sum(1 for t in tool_calls if t["tool_name"] == "search")
        crawl_count = sum(1 for t in tool_calls if t["tool_name"] == "crawl")
        llm_outputs = [e for e in self.trace if e["type"] == "llm_output"]

        return {
            "total_steps": len(self.trace),
            "llm_turns": len(llm_outputs),
            "tool_calls": len(tool_calls),
            "search_calls": search_count,
            "crawl_calls": crawl_count,
        }

    def reset(self):
        """重置轨迹（用于新的阶段）"""
        self.trace = []
        self.tool_results = {}
        self.metadata = {
            "start_time": datetime.now().isoformat(),
            "end_time": None,
        }


def create_run_folder(seed: str, base_dir: str = "output/trace") -> str:
    """创建运行输出文件夹"""
    # 清理 seed 中的特殊字符
    safe_seed = "".join(c if c.isalnum() or c in "_- " else "_" for c in seed)
    safe_seed = safe_seed.strip()[:30]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"{safe_seed}_{timestamp}"
    folder_path = os.path.join(base_dir, folder_name)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path
