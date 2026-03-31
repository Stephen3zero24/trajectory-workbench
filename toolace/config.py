"""
ToolACE 场景配置模块
"""

import os
from dataclasses import dataclass, field
from typing import Optional


# ─── LLM 配置 ───────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


@dataclass
class LLMConfig:
    """LLM 调用配置"""
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass
class ToolACEPipelineConfig:
    """ToolACE 完整 pipeline 配置"""

    # ── 基础信息 ──
    task_id: str = ""
    scene_type: str = "toolace"

    # ── LLM 配置 ──
    llm: LLMConfig = field(default_factory=LLMConfig)

    # ── Step 1: 工具自进化合成 ──
    source_tools: list = field(default_factory=list)    # 输入的源工具定义
    expansion_count: int = 3                             # 每个工具扩展生成的数量
    coupling_rounds: int = 2                             # 耦合优化轮次

    # ── Step 2: 任务生成 ──
    task_count: int = 10                                 # 生成任务数量
    enable_cross_group: bool = True                      # 是否启用跨组交叉生成
    enable_role_background: bool = True                  # 是否加入角色背景
    missing_param_ratio: float = 0.3                     # 缺参任务比例 (0~1)

    # ── Step 3: 轨迹生成 ──
    max_turns: int = 15                                  # 每条轨迹最大轮次
    max_workers: int = 3                                 # 并发线程数
    trajectory_style: str = "non_autoregressive"         # non_autoregressive | autoregressive

    # ── 质量控制 ──
    quality_threshold: float = 0.80
    max_iterations: int = 3

    # ── 输出 ──
    output_dir: str = "output/toolace"


# ─── 预置源工具集合（示例）─────────────────────────────────────────────────

PRESET_SOURCE_TOOLS = [
    {
        "name": "get_weather",
        "label": "天气服务",
        "description": "获取指定城市的当前天气信息",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "温度单位"},
            },
            "required": ["city"],
        },
        "returns": {"type": "object", "description": "包含温度、湿度、天气描述的对象"},
    },
    {
        "name": "search_web",
        "label": "搜索引擎",
        "description": "在互联网上搜索信息",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "description": "最大返回结果数"},
                "language": {"type": "string", "description": "结果语言偏好"},
            },
            "required": ["query"],
        },
        "returns": {"type": "array", "description": "搜索结果列表"},
    },
    {
        "name": "send_email",
        "label": "邮件服务",
        "description": "发送电子邮件",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人邮箱"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文"},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "抄送列表"},
            },
            "required": ["to", "subject", "body"],
        },
        "returns": {"type": "object", "description": "发送结果"},
    },
    {
        "name": "query_database",
        "label": "数据库",
        "description": "执行SQL查询并返回结果",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL查询语句"},
                "database": {"type": "string", "description": "数据库名称"},
                "timeout": {"type": "integer", "description": "查询超时(秒)"},
            },
            "required": ["sql", "database"],
        },
        "returns": {"type": "object", "description": "查询结果集"},
    },
    {
        "name": "create_file",
        "label": "文件系统",
        "description": "创建文件并写入内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
                "encoding": {"type": "string", "description": "文件编码"},
            },
            "required": ["path", "content"],
        },
        "returns": {"type": "object", "description": "创建结果"},
    },
    {
        "name": "call_api",
        "label": "API网关",
        "description": "调用外部REST API",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "API端点URL"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "description": "HTTP方法"},
                "headers": {"type": "object", "description": "请求头"},
                "body": {"type": "object", "description": "请求体"},
            },
            "required": ["url", "method"],
        },
        "returns": {"type": "object", "description": "API响应"},
    },
]
