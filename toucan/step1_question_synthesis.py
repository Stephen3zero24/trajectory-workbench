"""
Step 1: 问题合成

子步骤:
  1.1 生成问题合成 Prompt（基于 MCP Server 工具定义 + 采样策略）
  1.2 调用 LLM 批量生成工具使用问题
  1.3 去重 + 清洗（句子嵌入去重 + 格式清理）
"""

import json
import random
import hashlib
from typing import Optional
from dataclasses import dataclass, field, asdict

from openai import OpenAI

from .config import (
    ToucanPipelineConfig,
    MCPServerInfo,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    SAMPLING_STRATEGIES,
)
from .step0_smithery import get_tools_summary


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class GeneratedQuestion:
    """合成的工具使用问题"""
    question_id: str
    question: str
    target_servers: list        # 涉及的 MCP Server ID
    target_tools: list          # 预期使用的工具名称
    difficulty: str = "medium"  # easy | medium | hard
    scenario: str = ""          # 使用场景描述
    is_multi_server: bool = False


# ─── Step 1.1: 生成 Prompt ──────────────────────────────────────────────────────

def sample_servers(
    servers: list,
    count: int,
    strategy: str = "random",
    multi_server: bool = False,
) -> list:
    """
    按采样策略选择 MCP Server 子集

    策略:
      - random: 随机采样
      - uniform: 均匀覆盖每个 Server
      - power_law: 幂律分布（热门 Server 更多）
      - curated: 手工精选（使用前 N 个）
    """
    if not servers:
        return []

    if strategy == "uniform":
        # 确保每个 server 至少被选一次
        result = []
        while len(result) < count:
            for s in servers:
                result.append(s)
                if len(result) >= count:
                    break
        return result[:count]

    elif strategy == "power_law":
        # 幂律：前面的 Server 权重更高
        weights = [1.0 / (i + 1) ** 0.8 for i in range(len(servers))]
        total = sum(weights)
        probs = [w / total for w in weights]
        return random.choices(servers, weights=probs, k=count)

    elif strategy == "curated":
        return servers[:count]

    else:  # random
        return random.choices(servers, k=count)


def build_question_prompt(
    servers: list,
    count: int = 5,
    multi_server: bool = False,
    difficulty_mix: bool = True,
) -> str:
    """
    构建问题生成 Prompt

    Args:
        servers: 选中的 MCP Server 列表
        count: 要生成的问题数量
        multi_server: 是否生成跨服务器问题
        difficulty_mix: 是否混合不同难度

    Returns:
        str: 完整的 System + User Prompt
    """
    tools_summary = get_tools_summary(servers)

    server_scope = "多个服务器协同" if multi_server else "单个服务器"
    difficulty_instruction = """
请混合生成不同难度的问题：
- easy (30%): 只需调用一个工具即可完成
- medium (50%): 需要调用2-3个工具，有一定的逻辑编排
- hard (20%): 需要多个工具协同，可能包含条件判断、错误处理等
""" if difficulty_mix else ""

    prompt = f"""你是一个工具使用问题生成专家。请根据以下可用的 MCP 工具服务，生成 {count} 个真实、多样、有意义的用户问题。

## 可用的 MCP 工具服务

{tools_summary}

## 生成要求

1. 每个问题应是用户自然会问的、需要使用工具才能回答的问题
2. 问题应涉及{server_scope}的工具调用
3. 问题应具有真实场景感，像真正的用户需求
4. 避免生成过于简单或过于模糊的问题
5. 每个问题应明确到可以通过工具调用来解决
{difficulty_instruction}

## 输出格式（严格 JSON 数组）

[
  {{
    "question": "用户的自然语言问题",
    "target_servers": ["server_id_1"],
    "target_tools": ["tool_name_1", "tool_name_2"],
    "difficulty": "easy|medium|hard",
    "scenario": "简短的场景描述"
  }},
  ...
]

请只输出 JSON 数组，不要有其他内容。生成 {count} 个问题。
"""
    return prompt


# ─── Step 1.2: LLM 生成问题 ────────────────────────────────────────────────────

def generate_questions_llm(
    prompt: str,
    config: ToucanPipelineConfig,
) -> list:
    """
    调用 LLM 生成工具使用问题

    Args:
        prompt: 问题生成 Prompt
        config: Pipeline 配置

    Returns:
        list[GeneratedQuestion]: 生成的问题列表
    """
    api_key = config.smithery_api_key or DEEPSEEK_API_KEY
    client = OpenAI(api_key=api_key or DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    try:
        response = client.chat.completions.create(
            model=config.question_model,
            messages=[
                {"role": "system", "content": "你是一个工具使用问题生成专家。请严格按要求输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=config.question_temperature,
            max_tokens=4096,
            stream=False,
        )

        raw = response.choices[0].message.content
        # 提取 JSON
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]

        questions_data = json.loads(raw.strip())

        questions = []
        for i, q in enumerate(questions_data):
            qid = hashlib.md5(q.get("question", str(i)).encode()).hexdigest()[:12]
            questions.append(GeneratedQuestion(
                question_id=f"q_{qid}",
                question=q.get("question", ""),
                target_servers=q.get("target_servers", []),
                target_tools=q.get("target_tools", []),
                difficulty=q.get("difficulty", "medium"),
                scenario=q.get("scenario", ""),
                is_multi_server=len(q.get("target_servers", [])) > 1,
            ))

        return questions

    except Exception as e:
        print(f"  ⚠ 问题生成 LLM 调用失败: {e}")
        return []


# ─── Step 1.3: 去重 + 清洗 ──────────────────────────────────────────────────────

def dedup_questions(
    questions: list,
    threshold: float = 0.85,
) -> list:
    """
    问题去重（基于字符串相似度，可选句子嵌入）

    使用简单的 Jaccard 相似度进行去重。
    如果安装了 sentence-transformers，则使用嵌入相似度。

    Args:
        questions: 原始问题列表
        threshold: 去重阈值（0-1, 越高越严格）

    Returns:
        list[GeneratedQuestion]: 去重后的问题列表
    """
    if not questions:
        return []

    # 尝试使用 sentence-transformers
    try:
        from sentence_transformers import SentenceTransformer, util
        model = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [q.question for q in questions]
        embeddings = model.encode(texts, convert_to_tensor=True)

        keep = []
        keep_embeddings = []
        for i, q in enumerate(questions):
            if not keep_embeddings:
                keep.append(q)
                keep_embeddings.append(embeddings[i])
                continue

            import torch
            sims = util.cos_sim(embeddings[i].unsqueeze(0),
                                torch.stack(keep_embeddings))
            max_sim = sims.max().item()
            if max_sim < threshold:
                keep.append(q)
                keep_embeddings.append(embeddings[i])

        print(f"  [去重] 嵌入去重: {len(questions)} → {len(keep)}")
        return keep

    except ImportError:
        pass

    # 回退: 简单的 Jaccard 去重
    def jaccard(a: str, b: str) -> float:
        sa = set(a.lower().split())
        sb = set(b.lower().split())
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    keep = [questions[0]]
    for q in questions[1:]:
        is_dup = False
        for kept in keep:
            if jaccard(q.question, kept.question) > threshold:
                is_dup = True
                break
        if not is_dup:
            keep.append(q)

    print(f"  [去重] Jaccard 去重: {len(questions)} → {len(keep)}")
    return keep


def sanitize_questions(questions: list) -> list:
    """清洗问题格式"""
    sanitized = []
    for q in questions:
        # 基本清洗
        q.question = q.question.strip()
        if not q.question:
            continue
        if len(q.question) < 10:
            continue
        # 去掉编号前缀
        if q.question[0].isdigit() and q.question[1] in ".、)":
            q.question = q.question[2:].strip()
        sanitized.append(q)
    return sanitized


# ─── 整合: 执行 Step 1 完整流程 ────────────────────────────────────────────────

def run_step1(
    servers: list,
    config: ToucanPipelineConfig,
    event_callback=None,
) -> list:
    """
    执行 Step 1 完整流程: 问题合成

    Args:
        servers: 可用的 MCP Server 列表
        config: Pipeline 配置
        event_callback: 事件回调（用于 Web UI 实时展示）

    Returns:
        list[GeneratedQuestion]: 清洗去重后的问题列表
    """
    def emit(msg):
        print(f"  {msg}")
        if event_callback:
            event_callback("question_gen", msg)

    emit("[Step 1.1] 采样 MCP Server 并构建问题生成 Prompt...")
    sampled = sample_servers(
        servers,
        count=min(config.question_count, len(servers)),
        strategy=config.sampling_strategy,
        multi_server=config.multi_server,
    )
    prompt = build_question_prompt(
        sampled,
        count=config.question_count,
        multi_server=config.multi_server,
    )

    emit(f"[Step 1.2] 调用 {config.question_model} 生成 {config.question_count} 个问题...")
    questions = generate_questions_llm(prompt, config)
    emit(f"  生成了 {len(questions)} 个原始问题")

    if not questions:
        emit("⚠ 未生成任何问题，使用备选问题")
        questions = _fallback_questions(servers)

    emit("[Step 1.3] 去重 + 清洗...")
    questions = sanitize_questions(questions)
    questions = dedup_questions(questions, threshold=config.dedup_threshold)
    emit(f"  最终保留 {len(questions)} 个问题")

    return questions


def _fallback_questions(servers: list) -> list:
    """当 LLM 生成失败时使用的备选问题"""
    fallback = []
    templates = [
        "帮我搜索关于'{topic}'的最新信息",
        "请查找'{topic}'相关的技术文档并总结要点",
        "获取当前的'{topic}'状态并给出分析",
    ]
    topics = ["人工智能发展趋势", "Python 最佳实践", "云原生架构"]

    for i, tmpl in enumerate(templates):
        topic = topics[i % len(topics)]
        server = servers[i % len(servers)] if servers else None
        fallback.append(GeneratedQuestion(
            question_id=f"q_fallback_{i}",
            question=tmpl.format(topic=topic),
            target_servers=[server.server_id] if server else [],
            target_tools=[],
            difficulty="medium",
            scenario="备选问题",
        ))
    return fallback
