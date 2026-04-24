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
class LLMConfig:
    """通用 LLM 连接配置"""
    model: str = "deepseek-chat"
    temperature: float = 0.7
    api_key: str = field(default_factory=lambda: DEEPSEEK_API_KEY)
    base_url: str = field(default_factory=lambda: DEEPSEEK_BASE_URL)


@dataclass
class SmitheryConfig:
    """Smithery 认证与连接配置"""
    api_key: str = field(default_factory=lambda: SMITHERY_API_KEY)
    server_base: str = SMITHERY_SERVER_BASE

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class MCPServerRegistry:
    """MCP Server 注册表(servers + 元数据)"""
    servers: list = field(default_factory=list)  # list[MCPServerInfo]

    def list_servers(self) -> list:
        return list(self.servers)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = [asdict(s) for s in self.servers]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "MCPServerRegistry":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = [MCPServerInfo(**item) for item in data]
        return cls(servers=servers)


@dataclass
class ToucanPipelineConfig:
    """Toucan pipeline 配置(nested schema)"""

    # ── 基础信息 ──
    task_id: str = ""
    scene_type: str = "toucan_tool_call"

    # ── 输出 ──
    output_dir: str = ""
    mcp_registry_path: str = "toucan/mcp_servers/registry.json"

    # ── 子配置:外部依赖 ──
    smithery: SmitheryConfig = field(default_factory=SmitheryConfig)

    # ── 子配置:三组 LLM(作用于不同 step)──
    # question_llm: step1 问题合成
    # quality_llm: step2 QC 评估
    # agent_llm:   step3 Agent 轨迹生成
    question_llm: LLMConfig = field(
        default_factory=lambda: LLMConfig(temperature=0.8)
    )
    quality_llm: LLMConfig = field(
        default_factory=lambda: LLMConfig(temperature=0.3)
    )
    agent_llm: LLMConfig = field(
        default_factory=lambda: LLMConfig(temperature=0.7)
    )

    # ── MCP Server 选择 ──
    mcp_server_ids: list = field(default_factory=list)
    server_mode: str = "single"           # single | multi
    max_tools_per_question: int = 3

    # ── Step 1: 问题合成 ──
    question_count: int = 10
    sampling_strategy: str = "uniform"
    multi_server: bool = False            # 是否跨 server 合成
    dedup_threshold: float = 0.85         # 句向量去重阈值

    # ── Step 2: 质量检查 ──
    qc_criteria: list = field(default_factory=lambda: [
        "difficulty", "quality", "realism", "uniqueness"
    ])
    qc_min_score: float = 0.6             # 单题 QC 过滤线(step2 用)

    # ── Step 3: 轨迹生成 ──
    max_steps: int = 15
    enable_multi_turn: bool = False
    multi_turn_max_rounds: int = 3

    # ── Review / 迭代 ──
    max_iterations: int = 1
    quality_threshold: float = 0.7        # 最终轨迹接受阈值(Review 用)
    # 注意:qc_min_score 和 quality_threshold 是两个不同阶段的阈值,
    # 不要合并——前者是问题过滤,后者是轨迹接受

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ToucanPipelineConfig":
        # 嵌套子对象要从 dict 重建,不能直接 cls(**d)
        d = dict(d)
        if "smithery" in d and isinstance(d["smithery"], dict):
            d["smithery"] = SmitheryConfig(**d["smithery"])
        for key in ("question_llm", "quality_llm", "agent_llm"):
            if key in d and isinstance(d[key], dict):
                d[key] = LLMConfig(**d[key])
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
