"""
OpenSandbox 轨迹合成 Pipeline
- Agent 由 DeepSeek-chat API 驱动
- 沙箱通过 httpx 创建 + SDK connect 接管（绕过 SDK create 的 metadata bug）
- 支持自迭代闭环（Review Agent 评估 + 三级授权修改）

使用前请设置环境变量：
  export DEEPSEEK_API_KEY="your-deepseek-api-key"
"""

import asyncio
import json
import os
import time
from datetime import timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx
from openai import OpenAI

from opensandbox.sandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models import WriteEntry


# ─── 配置 ─────────────────────────────────────────────────────────────────────

OPENSANDBOX_SERVER = "http://127.0.0.1:8080"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# DeepSeek 客户端（OpenAI 兼容格式）
llm_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


# ─── 数据模型 ──────────────────────────────────────────────────────────────────

@dataclass
class TaskConfig:
    """任务配置"""
    task_id: str
    task_desc: str
    scene_type: str  # "mcp_tool", "gui", "deep_search", "multi_agent"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_steps: int = 15
    timeout_minutes: int = 10
    system_prompt_extra: str = ""  # Review Agent 可追加的系统提示


@dataclass
class TrajectoryStep:
    """轨迹中的单个步骤"""
    step_id: int
    observation: str
    thought: str
    action: str
    result: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Trajectory:
    """完整轨迹"""
    task_id: str
    config_snapshot: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    quality_score: float = 0.0
    iteration: int = 0
    total_tokens: int = 0


@dataclass
class ReviewResult:
    """Review Agent 的评估结果"""
    overall_score: float
    dimensions: dict
    fail_modes: list
    suggestions: list
    reasoning: str = ""


# ─── 沙箱管理（绕过 SDK create bug）──────────────────────────────────────────

async def create_sandbox_via_api() -> str:
    """通过 HTTP API 创建沙箱，返回 sandbox_id"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes",
            json={
                "image": {"uri": "opensandbox/code-interpreter:v1.0.2"},
                "entrypoint": ["/opt/opensandbox/code-interpreter.sh"],
                "resourceLimits": {},
            },
            timeout=60,
        )
        resp.raise_for_status()
        sandbox_id = resp.json()["id"]
        print(f"  沙箱已创建: {sandbox_id}")
        return sandbox_id


async def connect_sandbox(sandbox_id: str) -> Sandbox:
    """通过 SDK connect 接管已创建的沙箱"""
    config = ConnectionConfig(domain="127.0.0.1:8080", protocol="http")
    sandbox = await Sandbox.connect(sandbox_id, connection_config=config)
    return sandbox


async def delete_sandbox_via_api(sandbox_id: str):
    """通过 HTTP API 删除沙箱"""
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes/{sandbox_id}",
            timeout=30,
        )
    print(f"  沙箱已清理: {sandbox_id}")


# ─── LLM 调用 ─────────────────────────────────────────────────────────────────

def call_deepseek(messages: list, config: TaskConfig) -> dict:
    """调用 DeepSeek-chat API，返回 assistant 的回复"""
    try:
        response = llm_client.chat.completions.create(
            model=config.model,
            messages=messages,
            temperature=config.temperature,
            max_tokens=2048,
            stream=False,
        )
        content = response.choices[0].message.content
        tokens = response.usage.total_tokens if response.usage else 0
        return {"content": content, "tokens": tokens}
    except Exception as e:
        return {"content": f"[LLM调用失败: {e}]", "tokens": 0}


def build_agent_system_prompt(config: TaskConfig) -> str:
    """构建 Agent 的系统提示"""
    base_prompt = f"""你是一个在沙箱环境中执行任务的AI Agent。

## 你的任务
{config.task_desc}

## 你的能力
你可以在Linux沙箱中执行以下操作：
- 执行shell命令（如 ls, cat, echo, python3, pip install 等）
- 创建和编辑文件
- 运行Python脚本
- 安装所需的包

## 输出格式
每一步请严格按以下JSON格式输出（不要输出其他内容）：
{{
    "thought": "你对当前状态的分析和下一步的推理",
    "action": "要执行的具体shell命令",
    "is_final": false
}}

当任务完成时，将 is_final 设为 true，并在 thought 中总结完成情况。

## 注意事项
- 每步只执行一个命令
- 遇到错误时分析原因并尝试修复
- 如果某个方法不可行，尝试替代方案
"""
    if config.system_prompt_extra:
        base_prompt += f"\n## 额外指令\n{config.system_prompt_extra}\n"

    return base_prompt


# ─── 核心执行逻辑 ──────────────────────────────────────────────────────────────

async def execute_agent_in_sandbox(sandbox: Sandbox, config: TaskConfig) -> Trajectory:
    """在沙箱内运行 Agent，采集完整轨迹"""
    trajectory = Trajectory(
        task_id=config.task_id,
        config_snapshot=asdict(config),
    )

    system_prompt = build_agent_system_prompt(config)
    messages = [{"role": "system", "content": system_prompt}]

    # 获取初始环境状态
    init_result = await sandbox.commands.run("uname -a && pwd && ls /")
    initial_obs = init_result.logs.stdout[0].text if init_result.logs.stdout else "环境就绪"

    # 第一轮 user message：告诉 Agent 当前环境状态
    messages.append({
        "role": "user",
        "content": f"沙箱环境已就绪。当前环境信息：\n{initial_obs}\n\n请开始执行任务。"
    })

    for step_id in range(1, config.max_steps + 1):
        print(f"    Step {step_id}: ", end="", flush=True)

        # 1. Agent 思考 + 决策
        llm_result = call_deepseek(messages, config)
        trajectory.total_tokens += llm_result["tokens"]
        raw_response = llm_result["content"]

        # 2. 解析 Agent 输出
        try:
            # 尝试从回复中提取 JSON
            json_str = raw_response
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
            agent_output = json.loads(json_str.strip())
            thought = agent_output.get("thought", "")
            action = agent_output.get("action", "echo 'no action'")
            is_final = agent_output.get("is_final", False)
        except (json.JSONDecodeError, IndexError):
            thought = raw_response[:200]
            action = "echo 'Agent输出解析失败'"
            is_final = False

        print(f"Action: {action[:60]}...")

        # 3. 在沙箱中执行 Action
        try:
            exec_result = await sandbox.commands.run(
                action,
                timeout=timedelta(seconds=30),
            )
            stdout = "\n".join([l.text for l in exec_result.logs.stdout]) if exec_result.logs.stdout else ""
            stderr = "\n".join([l.text for l in exec_result.logs.stderr]) if exec_result.logs.stderr else ""
            observation = stdout if stdout else (stderr if stderr else "(无输出)")
            result = f"exit_code=0\n{observation}" if not stderr else f"exit_code=1\nstdout: {stdout}\nstderr: {stderr}"
        except Exception as e:
            observation = f"命令执行异常: {e}"
            result = observation

        # 4. 记录轨迹
        step = TrajectoryStep(
            step_id=step_id,
            observation=observation[:1000],  # 截断过长的输出
            thought=thought[:500],
            action=action,
            result=result[:1000],
        )
        trajectory.steps.append(asdict(step))

        # 5. 将结果反馈给 Agent
        messages.append({"role": "assistant", "content": raw_response})
        messages.append({
            "role": "user",
            "content": f"命令执行结果：\n{observation[:800]}\n\n请继续。"
        })

        # 6. 检查是否完成
        if is_final:
            print(f"    ✓ Agent 声明任务完成（共 {step_id} 步）")
            break
    else:
        print(f"    ⚠ 达到最大步数限制 ({config.max_steps})")

    return trajectory


# ─── Review Agent ──────────────────────────────────────────────────────────────

def review_trajectory(trajectory: Trajectory, config: TaskConfig) -> ReviewResult:
    """用 DeepSeek 作为 Review Agent 评估轨迹质量"""

    trajectory_summary = json.dumps(trajectory.steps, ensure_ascii=False, indent=2)
    # 截断避免超出上下文限制
    if len(trajectory_summary) > 6000:
        trajectory_summary = trajectory_summary[:6000] + "\n...(截断)"

    review_prompt = f"""你是一个轨迹质量评估专家。请评估以下Agent执行轨迹的质量。

## 任务描述
{config.task_desc}

## Agent 执行轨迹
{trajectory_summary}

## 评估维度
请从以下维度评分（0-1），并给出综合评分：
1. tool_usage: 工具/命令使用是否正确高效
2. reasoning: 推理链是否清晰完整
3. error_handling: 是否正确处理了错误和异常
4. completeness: 任务是否完整完成

## 输出格式（严格JSON）
{{
    "overall_score": 0.75,
    "dimensions": {{
        "tool_usage": 0.8,
        "reasoning": 0.7,
        "error_handling": 0.6,
        "completeness": 0.9
    }},
    "fail_modes": ["失败模式1", "失败模式2"],
    "suggestions": [
        {{
            "level": "auto",
            "category": "修改类别",
            "desc": "具体描述",
            "field": "要修改的字段",
            "from": "原值",
            "to": "新值"
        }},
        {{
            "level": "confirm",
            "category": "修改类别",
            "desc": "具体描述",
            "options": ["方案A", "方案B", "方案C"]
        }}
    ],
    "reasoning": "整体评估的推理过程"
}}

level 说明：
- "auto": 可自动执行的低风险修改（如温度调整、系统提示优化）
- "confirm": 需要人工确认的中风险修改（如任务描述修改、工具列表调整）
- "approve": 需要人工审批的高风险修改（如环境依赖变更、镜像更换）

请只输出JSON，不要有其他内容。
"""

    result = call_deepseek(
        [{"role": "user", "content": review_prompt}],
        TaskConfig(task_id="review", task_desc="", scene_type="", temperature=0.3),
    )

    try:
        raw = result["content"]
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
        review_data = json.loads(raw.strip())

        return ReviewResult(
            overall_score=review_data.get("overall_score", 0.5),
            dimensions=review_data.get("dimensions", {}),
            fail_modes=review_data.get("fail_modes", []),
            suggestions=review_data.get("suggestions", []),
            reasoning=review_data.get("reasoning", ""),
        )
    except Exception as e:
        print(f"  ⚠ Review Agent 输出解析失败: {e}")
        return ReviewResult(
            overall_score=0.5,
            dimensions={"tool_usage": 0.5, "reasoning": 0.5, "error_handling": 0.5, "completeness": 0.5},
            fail_modes=["Review Agent 输出格式异常"],
            suggestions=[],
            reasoning=result["content"][:300],
        )


# ─── 三级授权处理 ──────────────────────────────────────────────────────────────

def apply_auto_fixes(config: TaskConfig, suggestions: list) -> TaskConfig:
    """自动应用自主执行区的修改"""
    for s in suggestions:
        if s.get("level") != "auto":
            continue

        field_name = s.get("field", "")
        new_value = s.get("to", "")

        if field_name == "temperature" and new_value:
            try:
                config.temperature = float(new_value)
                print(f"    🟢 自动修改 temperature: {s.get('from')} → {new_value}")
            except ValueError:
                pass
        elif field_name == "model" and new_value:
            config.model = new_value
            print(f"    🟢 自动修改 model: {s.get('from')} → {new_value}")
        elif field_name == "system_prompt_extra" or "系统提示" in s.get("category", ""):
            append_text = s.get("to", s.get("desc", ""))
            config.system_prompt_extra += f"\n{append_text}"
            print(f"    🟢 自动追加系统提示: {append_text[:50]}...")
        elif field_name == "max_steps" and new_value:
            try:
                config.max_steps = int(new_value)
                print(f"    🟢 自动修改 max_steps: {s.get('from')} → {new_value}")
            except ValueError:
                pass
        else:
            print(f"    🟢 自动应用: {s.get('category', '未知')} - {s.get('desc', '')[:50]}")

    return config


def handle_confirm_suggestions(suggestions: list):
    """处理人工确认区的建议（CLI 模式，实际部署通过 Web UI）"""
    for s in suggestions:
        if s.get("level") != "confirm":
            continue

        print(f"\n    🟡 需要人工确认: {s.get('category', '')}")
        print(f"       描述: {s.get('desc', '')}")
        options = s.get("options", [])
        for i, opt in enumerate(options):
            print(f"       [{i + 1}] {opt}")

        # CLI 模式下请求用户输入
        try:
            choice = input(f"       请选择 (1-{len(options)}, 回车跳过): ").strip()
            if choice and choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    print(f"       ✓ 已选择: {options[idx]}")
        except EOFError:
            print("       → 跳过（非交互模式）")


def handle_approve_suggestions(suggestions: list):
    """处理人工审批区的建议（CLI 模式）"""
    for s in suggestions:
        if s.get("level") != "approve":
            continue

        print(f"\n    🔴 需要人工审批: {s.get('category', '')}")
        print(f"       描述: {s.get('desc', '')}")
        print(f"       影响: {s.get('impact', 'N/A')}")

        try:
            choice = input("       批准执行? (y/n, 回车跳过): ").strip().lower()
            if choice == "y":
                print("       ✓ 已批准")
            else:
                print("       ✗ 已拒绝")
        except EOFError:
            print("       → 跳过（非交互模式）")


# ─── 主流程 ────────────────────────────────────────────────────────────────────

async def run_pipeline(
    config: TaskConfig,
    max_iterations: int = 3,
    quality_threshold: float = 0.80,
):
    """执行完整的自迭代轨迹合成流程"""

    if not DEEPSEEK_API_KEY:
        print("❌ 请设置环境变量 DEEPSEEK_API_KEY")
        print("   export DEEPSEEK_API_KEY='your-api-key'")
        return

    all_trajectories = []

    for iteration in range(max_iterations):
        print(f"\n{'=' * 60}")
        print(f"第 {iteration + 1} / {max_iterations} 轮迭代")
        print(f"配置: model={config.model}, temp={config.temperature}")
        print(f"{'=' * 60}")

        # ── 1. 创建沙箱 ──
        print("\n[1/4] 创建沙箱实例...")
        sandbox_id = await create_sandbox_via_api()
        await asyncio.sleep(3)  # 等待沙箱完全就绪

        # ── 2. Agent 执行 ──
        print("\n[2/4] Agent 在沙箱内执行任务...")
        sandbox = await connect_sandbox(sandbox_id)
        try:
            async with sandbox:
                trajectory = await execute_agent_in_sandbox(sandbox, config)
                trajectory.iteration = iteration
        finally:
            await delete_sandbox_via_api(sandbox_id)

        # ── 3. Review Agent 评估 ──
        print("\n[3/4] Review Agent 评估轨迹质量...")
        review = review_trajectory(trajectory, config)
        trajectory.quality_score = review.overall_score
        all_trajectories.append(trajectory)

        print(f"  综合评分: {review.overall_score:.2f}")
        if review.dimensions:
            for dim, score in review.dimensions.items():
                bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
                print(f"    {dim:20s} {bar} {score:.2f}")
        if review.fail_modes:
            print(f"  失败模式: {', '.join(review.fail_modes)}")
        if review.reasoning:
            print(f"  评估理由: {review.reasoning[:150]}...")

        # ── 4. 判断是否达标 ──
        if review.overall_score >= quality_threshold:
            print(f"\n✅ 质量达标 ({review.overall_score:.2f} >= {quality_threshold})，停止迭代")
            break

        # ── 5. 三级授权处理 ──
        print("\n[4/4] 处理修改建议...")

        auto_suggestions = [s for s in review.suggestions if s.get("level") == "auto"]
        confirm_suggestions = [s for s in review.suggestions if s.get("level") == "confirm"]
        approve_suggestions = [s for s in review.suggestions if s.get("level") == "approve"]

        if auto_suggestions:
            config = apply_auto_fixes(config, auto_suggestions)

        if confirm_suggestions:
            handle_confirm_suggestions(confirm_suggestions)

        if approve_suggestions:
            handle_approve_suggestions(approve_suggestions)

    # ── 6. 导出数据集 ──
    print(f"\n{'=' * 60}")
    print("导出数据集")
    print(f"{'=' * 60}")

    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)

    # SFT 格式
    sft_path = f"{output_dir}/trajectories_sft_{config.task_id}.jsonl"
    with open(sft_path, "w", encoding="utf-8") as f:
        for t in all_trajectories:
            f.write(json.dumps(asdict(t), ensure_ascii=False) + "\n")
    print(f"  SFT 格式: {sft_path}")

    # 最佳轨迹单独导出
    best = max(all_trajectories, key=lambda t: t.quality_score)
    best_path = f"{output_dir}/best_trajectory_{config.task_id}.json"
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(asdict(best), f, ensure_ascii=False, indent=2)
    print(f"  最佳轨迹: {best_path} (score={best.quality_score:.2f})")

    # 汇总统计
    print(f"\n📊 汇总:")
    print(f"  总迭代轮次: {len(all_trajectories)}")
    print(f"  质量变化: {' → '.join([f'{t.quality_score:.2f}' for t in all_trajectories])}")
    print(f"  总Token消耗: {sum(t.total_tokens for t in all_trajectories):,}")
    print(f"  最佳轨迹步数: {len(best.steps)}")

    return all_trajectories


# ─── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = TaskConfig(
        task_id="task_001",
        task_desc=(
            "在Linux沙箱环境中完成以下任务：\n"
            "1. 创建一个Python项目目录 /tmp/demo_project\n"
            "2. 在其中创建一个 calculator.py 文件，实现加减乘除四则运算\n"
            "3. 创建对应的单元测试文件 test_calculator.py\n"
            "4. 运行单元测试并确保全部通过\n"
            "5. 输出测试报告"
        ),
        scene_type="code_exec",
        model="deepseek-chat",
        temperature=0.7,
        max_steps=15,
    )

    asyncio.run(run_pipeline(config, max_iterations=3, quality_threshold=0.80))
