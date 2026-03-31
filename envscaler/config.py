"""
EnvScaler 场景配置模块

管理场景文件路径、MCP Server 配置、LLM 配置、沙箱配置等。
"""

import os
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─── 外部配置 ────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
OPENSANDBOX_SERVER = os.environ.get("OPENSANDBOX_SERVER", "http://127.0.0.1:8080")


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class SceneFile:
    """一组场景文件（env_scenario.json + filtered_env_metadata.json）"""
    scenario_path: str = ""             # env_scenario.json 的本地路径
    metadata_path: str = ""             # filtered_env_metadata.json 的本地路径
    scenario_content: dict = field(default_factory=dict)   # 解析后的场景数据
    metadata_content: dict = field(default_factory=dict)   # 解析后的元数据
    env_name: str = ""                  # 环境名称（从元数据提取）
    task_count: int = 0                 # 场景中包含的任务数量


@dataclass
class SceneTask:
    """场景中的单个任务"""
    task_id: str = ""
    task_desc: str = ""                 # 任务描述
    env_name: str = ""                  # 所属环境
    init_config: dict = field(default_factory=dict)   # 初始数据状态配置
    check_func: str = ""               # 任务检查函数（Python 代码字符串）
    available_tools: list = field(default_factory=list)  # 可用工具列表


@dataclass
class EnvScalerPipelineConfig:
    """EnvScaler 完整 pipeline 配置"""

    # ── 基础信息 ──
    task_id: str = ""
    scene_type: str = "envscaler"

    # ── Step 0: 场景文件配置 ──
    scene_source: str = "upload"        # upload | local | extract
    scene_dir: str = ""                 # 场景文件目录（local 模式）
    envscaler_data_dir: str = ""        # EnvScaler 数据目录（extract 模式）
    extract_count: int = 1              # 提取场景数量（extract 模式）

    # ── Step 1: MCP Server 配置 ──
    mcp_port: int = 8888                # MCP Server 端口
    mcp_transport: str = "streamable-http"  # streamable-http | sse
    fastmcp_version: str = "3.1.1"      # fastmcp 库版本
    qwen_agent_version: str = "0.0.31"  # qwen-agent 库版本

    # ── Step 2: Agent 轨迹生成配置 ──
    agent_model: str = "deepseek-chat"  # LLM 模型
    agent_temperature: float = 0.7      # 温度
    agent_framework: str = "openai"     # openai | qwen
    max_steps: int = 20                 # 每条轨迹最大步数
    max_tasks: int = 0                  # 从场景中选取的最大任务数（0=全部）
    timeout_per_task: int = 300         # 每个任务超时秒数

    # ── Review & 迭代 ──
    max_iterations: int = 3
    quality_threshold: float = 0.80

    # ── 输出 ──
    output_dir: str = "output/envscaler"

    # ── 内部 LLM 配置 ──
    deepseek_api_key: str = ""
    deepseek_base_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EnvScalerPipelineConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── MCP Server 模板 ─────────────────────────────────────────────────────────

# 这是上传到沙箱内运行的 MCP Server 脚本模板
# 它读取场景文件，暴露 scene_action 和 get_current_time 两个 MCP 工具
MCP_SERVER_TEMPLATE = r'''#!/usr/bin/env python3
"""
EnvScaler MCP Scene Server — 在沙箱内运行
读取场景文件，通过 fastmcp 暴露工具接口供 Agent 调用。

工具:
  - get_current_time: 获取当前时间
  - scene_action: 执行场景内的具体操作（调度到环境中的函数）

启动: python3 main_mcp.py
"""

import json
import os
import sys
import importlib.util
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any

from fastmcp import FastMCP

# ─── 加载场景文件 ────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("SCENE_DATA_DIR", "/workspace/envscaler/data")

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# 加载场景和元数据
scenario_path = os.path.join(DATA_DIR, "env_scenario.json")
metadata_path = os.path.join(DATA_DIR, "filtered_env_metadata.json")

scenario_data = load_json(scenario_path) if os.path.exists(scenario_path) else {}
metadata_data = load_json(metadata_path) if os.path.exists(metadata_path) else {}

# ─── 解析环境 ────────────────────────────────────────────────────────────────

class EnvRunner:
    """动态加载并运行环境代码"""

    def __init__(self, metadata, scenario):
        self.metadata = metadata
        self.scenario = scenario
        self.env_instances = {}
        self.env_state = {}
        self._init_environments()

    def _init_environments(self):
        """从元数据中初始化环境"""
        envs = self.metadata if isinstance(self.metadata, list) else [self.metadata]
        for env_meta in envs:
            env_name = env_meta.get("env_name", env_meta.get("name", "default"))
            env_code = env_meta.get("code", "")
            if env_code:
                try:
                    namespace = {}
                    exec(env_code, namespace)
                    # 查找环境类
                    for k, v in namespace.items():
                        if isinstance(v, type) and k != "type" and hasattr(v, "__init__"):
                            self.env_instances[env_name] = {
                                "class": v,
                                "namespace": namespace,
                                "meta": env_meta,
                            }
                            break
                except Exception as e:
                    print(f"[EnvRunner] 环境 {env_name} 初始化失败: {e}")

        # 从场景数据初始化状态
        scenarios = self.scenario if isinstance(self.scenario, list) else [self.scenario]
        for sc in scenarios:
            env_name = sc.get("env_name", "default")
            init_config = sc.get("init_config", sc.get("initial_state", {}))
            if env_name in self.env_instances:
                try:
                    cls = self.env_instances[env_name]["class"]
                    instance = cls(**init_config) if init_config else cls()
                    self.env_state[env_name] = instance
                except Exception as e:
                    print(f"[EnvRunner] 环境 {env_name} 状态初始化失败: {e}")

    def get_available_tools(self) -> list:
        """获取所有可用的工具列表"""
        tools = []
        for env_name, env_info in self.env_instances.items():
            meta = env_info["meta"]
            operations = meta.get("operations", meta.get("tools", []))
            for op in operations:
                if isinstance(op, dict):
                    tools.append({
                        "name": op.get("name", ""),
                        "description": op.get("description", ""),
                        "parameters": op.get("parameters", {}),
                        "env_name": env_name,
                    })
                elif isinstance(op, str):
                    tools.append({
                        "name": op,
                        "description": f"{env_name}.{op}",
                        "parameters": {},
                        "env_name": env_name,
                    })
        return tools

    def execute_action(self, name: str, arguments: dict) -> dict:
        """执行一个环境操作"""
        # 尝试在所有环境中查找对应方法
        for env_name, instance in self.env_state.items():
            if hasattr(instance, name):
                try:
                    method = getattr(instance, name)
                    result = method(**arguments)
                    return {
                        "success": True,
                        "data": result,
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"执行 {name} 失败: {str(e)}",
                        "traceback": traceback.format_exc(),
                    }

        # 尝试在 namespace 中查找函数
        for env_name, env_info in self.env_instances.items():
            ns = env_info["namespace"]
            if name in ns and callable(ns[name]):
                try:
                    result = ns[name](**arguments)
                    return {"success": True, "data": result}
                except Exception as e:
                    return {"success": False, "error": str(e)}

        return {"success": False, "error": f"未找到操作: {name}"}


# ─── 初始化 ──────────────────────────────────────────────────────────────────

env_runner = EnvRunner(metadata_data, scenario_data)
available_tools = env_runner.get_available_tools()

print(f"[MCP Server] 已加载 {len(env_runner.env_instances)} 个环境")
print(f"[MCP Server] 可用工具: {[t['name'] for t in available_tools]}")

# ─── FastMCP Server ──────────────────────────────────────────────────────────

import datetime as _dt_module  # 用于在 get_current_time 中访问 datetime.timezone.utc

mcp = FastMCP("scene-server")


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> dict:
    """获取指定时区的当前时间"""
    try:
        if timezone == "UTC":
            tz = _dt_module.timezone.utc
        else:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now(_dt_module.timezone.utc)
        timezone = "UTC"

    return {
        "success": True,
        "timezone": timezone,
        "datetime": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timestamp": int(now.timestamp()),
    }


@mcp.tool()
def scene_action(name: str, arguments: dict) -> dict:
    """执行场景指令

    Args:
        name: 要调用的环境操作名称
        arguments: 操作参数（JSON 对象）
    """
    observation = env_runner.execute_action(name, arguments)

    # 计算 reward（如果场景中定义了 check 函数）
    reward = 0.0
    terminated = False
    truncated = False

    result = {
        "success": True,
        "observation": {
            "type": "tool",
            "content": str(observation),
            "reward": reward,
            "terminated": terminated,
            "truncated": truncated,
            "info": {
                "action": {"name": name, "arguments": arguments},
            },
        },
    }
    return result


if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", "8888"))
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    print(f"[MCP Server] 启动: port={port}, transport={transport}")
    mcp.run(transport=transport, port=port)
'''


# ─── Agent System Prompt 模板 ────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """你是一个能够使用环境工具完成任务的 AI 助手。

## 你的能力

你可以通过 `scene_action` 工具与环境交互，执行各种操作。
你也可以通过 `get_current_time` 获取当前时间。

## scene_action 工具使用方式

调用 `scene_action` 时需要提供:
- `name`: 要执行的操作名称
- `arguments`: 操作的参数（JSON 对象）

## 可用的环境操作

{available_tools}

## 当前任务

{task_desc}

## 工作流程

1. 仔细分析任务需求
2. 确定需要调用哪些操作、按什么顺序
3. 逐步执行操作，根据返回结果决定下一步
4. 任务完成后给出清晰的总结

## 注意

- 仔细阅读每个操作的参数要求
- 根据操作返回的结果进行推理
- 如果某步操作失败，分析原因并尝试替代方案
- 完成所有必要步骤后再总结
"""
