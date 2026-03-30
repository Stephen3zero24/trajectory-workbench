"""
Search2QA Tools — 搜索和爬虫工具实现

提供两个核心工具：
- search: 基于 DuckDuckGo 的互联网搜索
- crawl: 基于 crawl4ai + fallback 的网页内容爬取
"""

import asyncio
import json
import traceback
from typing import Optional


# ─── 搜索工具 ─────────────────────────────────────────────────────────────────

async def search_duckduckgo(query: str, max_results: int = 8) -> str:
    """使用 DuckDuckGo 搜索，返回格式化的搜索结果"""
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", r.get("link", "")),
                    "snippet": r.get("body", r.get("snippet", "")),
                })
        if not results:
            return f"搜索 '{query}' 未找到结果。"

        formatted = f"搜索 '{query}' 的结果：\n\n"
        for i, r in enumerate(results, 1):
            formatted += f"[{i}] {r['title']}\n    URL: {r['url']}\n    {r['snippet']}\n\n"
        return formatted

    except Exception as e:
        return f"搜索失败: {e}"


# ─── 爬虫工具 ─────────────────────────────────────────────────────────────────

async def crawl_with_crawl4ai(url: str) -> Optional[str]:
    """使用 crawl4ai 爬取网页（首选方案）"""
    try:
        from crawl4ai import AsyncWebCrawler
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(url=url)
            if result and result.markdown:
                text = result.markdown[:8000]  # 截断过长内容
                return text
        return None
    except Exception:
        return None


async def crawl_with_requests(url: str) -> Optional[str]:
    """使用 requests + BeautifulSoup 爬取网页（回退方案）"""
    try:
        import requests
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        # 移除无关标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        # 清理多余空行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        text = "\n".join(lines)

        return text[:8000] if text else None

    except Exception:
        return None


async def crawl_with_trafilatura(url: str) -> Optional[str]:
    """使用 trafilatura 提取正文（第二回退方案）"""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded)
            if text:
                return text[:8000]
        return None
    except Exception:
        return None


async def crawl_url(url: str) -> str:
    """爬取 URL 内容，按优先级尝试多种方法"""
    # PDF 文件特殊处理
    if url.lower().endswith(".pdf"):
        return await crawl_pdf(url)

    # 按优先级尝试
    for crawler_fn in [crawl_with_crawl4ai, crawl_with_trafilatura, crawl_with_requests]:
        result = await crawler_fn(url)
        if result and len(result.strip()) > 100:
            return f"网页内容（{url}）：\n\n{result}"

    return f"无法获取网页内容: {url}"


async def crawl_pdf(url: str) -> str:
    """下载并解析 PDF 文件"""
    try:
        import requests
        import tempfile
        import os

        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name

        try:
            import pymupdf4llm
            text = pymupdf4llm.to_markdown(tmp_path)
        except Exception:
            import fitz  # PyMuPDF
            doc = fitz.open(tmp_path)
            text = "\n".join(page.get_text() for page in doc)
            doc.close()

        os.unlink(tmp_path)
        return f"PDF 内容（{url}）：\n\n{text[:8000]}"

    except Exception as e:
        return f"PDF 解析失败 ({url}): {e}"


# ─── 统一工具调度 ──────────────────────────────────────────────────────────────

async def execute_tool_call(tool_name: str, tool_args: dict) -> str:
    """统一的工具调用入口"""
    try:
        if tool_name == "search":
            query = tool_args.get("query", "")
            if not query:
                return "搜索失败: 未提供查询关键词"
            return await search_duckduckgo(query)

        elif tool_name == "crawl":
            url = tool_args.get("url", "")
            if not url:
                return "爬取失败: 未提供 URL"
            return await crawl_url(url)

        else:
            return f"未知工具: {tool_name}"

    except Exception as e:
        return f"工具 '{tool_name}' 执行失败: {e}\n{traceback.format_exc()}"
