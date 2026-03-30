"""
Step 0: Smithery MCP Server 发现与元数据抓取

功能:
  - 通过 Smithery REST API 获取可用 MCP Server 列表
  - 抓取每个 Server 的工具定义 (tools schema)
  - 过滤出无需认证的公开服务器
  - 保存为本地 JSON 缓存供后续步骤使用
"""

import json
import os
import time
from dataclasses import asdict

import httpx

from .config import (
    SMITHERY_API_KEY,
    SMITHERY_SERVER_BASE,
    MCPServerInfo,
    DEFAULT_MCP_SERVERS,
    save_mcp_servers_to_file,
)


async def discover_smithery_servers(
    api_key: str = "",
    max_servers: int = 50,
    cache_path: str = "toucan/mcp_servers/servers_cache.json",
) -> list:
    """
    通过 Smithery Connect API 发现可用的 MCP Server
    
    流程:
      1. 调用 Smithery registry 获取 Server 列表
      2. 逐个获取 Server 的 tools schema
      3. 过滤掉需要额外认证的 Server
      4. 保存到本地缓存

    Args:
        api_key: Smithery API Key
        max_servers: 最大抓取数量
        cache_path: 缓存文件路径

    Returns:
        list[MCPServerInfo]: 可用 MCP Server 列表
    """
    key = api_key or SMITHERY_API_KEY
    if not key:
        print("  ⚠ Smithery API Key 未配置，使用内置默认 Server 列表")
        return DEFAULT_MCP_SERVERS

    servers = []
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # ── 1. 获取 registry 列表 ──
        print("  [Step 0.1] 从 Smithery Registry 获取 MCP Server 列表...")
        try:
            resp = await client.get(
                "https://registry.smithery.ai/servers",
                headers=headers,
                params={"limit": max_servers},
            )
            resp.raise_for_status()
            registry_data = resp.json()
            server_entries = registry_data.get("servers", registry_data) \
                if isinstance(registry_data, dict) else registry_data
        except Exception as e:
            print(f"  ⚠ Registry API 调用失败: {e}，使用默认列表")
            return DEFAULT_MCP_SERVERS

        # ── 2. 逐个获取 tools schema ──
        print(f"  [Step 0.2] 获取 {len(server_entries)} 个 Server 的工具定义...")
        for entry in server_entries[:max_servers]:
            try:
                server_id = entry.get("qualifiedName", entry.get("name", ""))
                display_name = entry.get("displayName", entry.get("name", server_id))
                description = entry.get("description", "")
                
                server_url = f"{SMITHERY_SERVER_BASE}/{server_id}"

                # 获取 tools — 通过 MCP 的 tools/list 请求
                tools = []
                try:
                    tools_resp = await client.post(
                        f"{server_url}",
                        headers={
                            **headers,
                            "Accept": "application/json",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "method": "tools/list",
                            "id": 1,
                        },
                        timeout=15,
                    )
                    if tools_resp.status_code == 200:
                        tools_data = tools_resp.json()
                        tools = tools_data.get("result", {}).get("tools", [])
                except Exception:
                    pass  # 部分 server 可能不支持直接的 JSON-RPC

                # 判断是否需要认证
                requires_auth = entry.get("security", {}).get("oauth", False) or \
                    bool(entry.get("configSchema", {}).get("required", []))

                server_info = MCPServerInfo(
                    server_id=server_id,
                    name=display_name,
                    url=server_url,
                    description=description[:200],
                    tools=tools,
                    category=entry.get("category", ""),
                    requires_auth=requires_auth,
                )
                servers.append(server_info)

            except Exception as e:
                print(f"    ⚠ 跳过 Server {entry.get('name', '?')}: {e}")
                continue

        # ── 3. 过滤 ──
        public_servers = [s for s in servers if not s.requires_auth]
        print(f"  [Step 0.3] 过滤完成: {len(public_servers)}/{len(servers)} 个公开 Server")

        # ── 4. 保存缓存 ──
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        save_mcp_servers_to_file(public_servers, cache_path)
        print(f"  [Step 0.4] 缓存已保存: {cache_path}")

    return public_servers if public_servers else DEFAULT_MCP_SERVERS


def load_cached_servers(cache_path: str = "toucan/mcp_servers/servers_cache.json") -> list:
    """加载本地缓存的 MCP Server 列表"""
    if not os.path.exists(cache_path):
        print("  ⚠ 无本地缓存，使用默认 Server 列表")
        return DEFAULT_MCP_SERVERS

    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    servers = []
    for item in data:
        servers.append(MCPServerInfo(**item))
    print(f"  从缓存加载了 {len(servers)} 个 MCP Server")
    return servers


def get_tools_summary(servers: list) -> str:
    """生成 MCP Server + Tools 的文本摘要, 用于 Prompt 构建"""
    lines = []
    for s in servers:
        tool_names = []
        for t in s.tools:
            if isinstance(t, dict):
                tool_names.append(t.get("name", "unknown"))
            else:
                tool_names.append(str(t))
        tools_str = ", ".join(tool_names) if tool_names else "(tools pending discovery)"
        lines.append(f"- [{s.server_id}] {s.name}: {s.description}\n  Tools: {tools_str}")
    return "\n".join(lines)
