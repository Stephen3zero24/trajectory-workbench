"""
Toucan 场景配置模块
管理 Smithery API、MCP Server 列表、LLM 配置等
"""

import os
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─── Smithery 配置 ──────────────────────────────────────────────────────────────

SMITHERY_API_KEY = os.environ.get("SMITHERY_API_KEY", "")
SMITHERY_SERVER_BASE = "https://server.smithery.ai"

# ─── LLM 配置 ───────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

# ─── OpenSandbox 配置 ───────────────────────────────────────────────────────────

OPENSANDBOX_SERVER = os.environ.get("OPENSANDBOX_SERVER", "http://127.0.0.1:8080")

# ─── MCP Server 采样策略 ────────────────────────────────────────────────────────

SAMPLING_STRATEGIES = ["random", "uniform", "power_law", "curated"]


@dataclass
class MCPServerInfo:
    """单个 MCP Server 的元数据"""
    server_id: str              # 唯一标识, e.g. "exa", "@anthropic/brave-search"
    name: str                   # 展示名称
    url: str                    # Smithery server URL
    description: str = ""       # 功能描述
    tools: list = field(default_factory=list)      # 工具定义列表
    category: str = ""          # 分类标签
    requires_auth: bool = False # 是否需要额外认证


@dataclass
class ToucanPipelineConfig:
    """Toucan 完整 pipeline 配置"""

    # ── 基础信息 ──
    task_id: str = ""
    scene_type: str = "toucan_tool_call"

    # ── Step 0: Smithery 配置 ──
    smithery_api_key: str = ""
    mcp_server_ids: list = field(default_factory=list)  # 要使用的 MCP Server ID 列表

    # ── Step 1: 问题合成配置 ──
    question_count: int = 10            # 生成问题数量
    sampling_strategy: str = "random"   # random | uniform | power_law | curated
    multi_server: bool = False          # 是否生成跨服务器问题
    question_model: str = "deepseek-chat"
    question_temperature: float = 0.8

    # ── Step 2: 质量检查配置 ──
    qc_model: str = "deepseek-chat"
    qc_temperature: float = 0.3
    qc_criteria: list = field(default_factory=lambda: [
        "difficulty", "quality", "realism", "uniqueness"
    ])
    qc_min_score: float = 0.6       # 质量过滤最低分

    # ── Step 3: 轨迹生成配置 ──
    agent_model: str = "deepseek-chat"  # 也可用 qwen 系列
    agent_temperature: float = 0.7
    agent_framework: str = "qwen"       # qwen | openai_compat
    max_steps: int = 20
    timeout_minutes: int = 15
    enable_multi_turn: bool = False     # 是否生成多轮对话轨迹

    # ── Review & 迭代 ──
    max_iterations: int = 3
    quality_threshold: float = 0.80

    # ── 去重 ──
    dedup_threshold: float = 0.85       # 句子嵌入去重相似度阈值

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ToucanPipelineConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── 预置 MCP Server 列表（无需 API Key 的公开服务器）──────────────────────────

DEFAULT_MCP_SERVERS = [
    MCPServerInfo(
        server_id="exa",
        name="Exa Search",
        url=f"{SMITHERY_SERVER_BASE}/exa",
        description="Neural search engine — find pages, get contents, answer questions from the web",
        tools=[],
        category="web_search",
    ),
    MCPServerInfo(
        server_id="brave-search",
        name="Brave Search",
        url=f"{SMITHERY_SERVER_BASE}/@anthropic/brave-search",
        description="Brave web search and local search",
        tools=[],
        category="web_search",
    ),
    MCPServerInfo(
        server_id="fetch",
        name="Fetch",
        url=f"{SMITHERY_SERVER_BASE}/@anthropic/fetch",
        description="Fetch URLs and extract content as markdown",
        tools=[],
        category="web_fetch",
    ),
    MCPServerInfo(
        server_id="sequential-thinking",
        name="Sequential Thinking",
        url=f"{SMITHERY_SERVER_BASE}/@anthropic/sequential-thinking",
        description="Dynamic problem-solving through thought sequences",
        tools=[],
        category="reasoning",
    ),
    MCPServerInfo(
        server_id="mcp-server-time",
        name="Time",
        url=f"{SMITHERY_SERVER_BASE}/@anthropic/mcp-server-time",
        description="Get current time and timezone conversions",
        tools=[],
        category="utility",
    ),
]


def load_mcp_servers_from_file(path: str) -> list:
    """从 JSON 文件加载 MCP Server 列表"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    servers = []
    for item in data:
        servers.append(MCPServerInfo(**item))
    return servers


def save_mcp_servers_to_file(servers: list, path: str):
    """保存 MCP Server 列表到 JSON 文件"""
    data = [asdict(s) for s in servers]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
