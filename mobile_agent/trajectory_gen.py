"""
Step 2: Mobile Agent 轨迹生成

核心模块: VLM 驱动的 Android GUI 操控循环。

子步骤:
  2.1 初始化任务环境 (启动目标 App, 执行 initial_actions)
  2.2 Agent 循环:
      截图 + UI 树 → VLM 推理 → 选择动作 → 执行 → 截图 → ...
  2.3 任务验证 (check_func)
  2.4 采集并结构化轨迹
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from openai import OpenAI

from .config import (
    MobileAgentPipelineConfig,
    MobileScenarioTask,
    MOBILE_AGENT_SYSTEM_PROMPT,
    MOBILE_TOOLS_SCHEMA,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from .sandbox_runner import MobileSandboxRunner, format_ui_elements_for_prompt


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class MobileTrajectoryStep:
    """轨迹中的单个步骤"""
    step_id: int
    observation: dict = field(default_factory=dict)   # screenshot_b64, ui_elements
    thought: str = ""                                  # Agent 推理
    action_type: str = ""                              # tap/swipe/input_text/...
    action_params: dict = field(default_factory=dict)
    action_reasoning: str = ""                         # 为什么执行这个动作
    action_result: dict = field(default_factory=dict)  # success, result, duration_ms
    timestamp: float = field(default_factory=time.time)


@dataclass
class MobileTrajectory:
    """一条完整的 Mobile Agent 轨迹"""
    trajectory_id: str = ""
    task_id: str = ""
    task_desc: str = ""
    app_package: str = ""
    screen_resolution: str = ""
    steps: list = field(default_factory=list)          # list[MobileTrajectoryStep]
    messages: list = field(default_factory=list)        # 原始 chat messages
    tools_schema: list = field(default_factory=list)
    total_actions: int = 0
    successful_actions: int = 0
    total_tokens: int = 0
    quality_score: float = 0.0
    task_completed: bool = False
    task_check_result: dict = field(default_factory=dict)
    finish_summary: str = ""
    tags: list = field(default_factory=list)


# ─── VLM 消息构建 ────────────────────────────────────────────────────────────

def _build_observation_content(
    screenshot_b64: str,
    ui_elements: list,
    screen_w: int,
    screen_h: int,
    enable_vision: bool = True,
) -> list:
    """
    构建包含截图和 UI 元素的 observation 消息内容。

    返回 OpenAI vision 格式的 content 列表。
    """
    content_parts = []

    # 截图 (vision)
    if enable_vision and screenshot_b64:
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": "high",
            },
        })

    # UI 元素文本描述
    ui_text = format_ui_elements_for_prompt(ui_elements)
    content_parts.append({
        "type": "text",
        "text": f"[屏幕分辨率: {screen_w}x{screen_h}]\n{ui_text}",
    })

    return content_parts


# ─── 单任务轨迹生成 ──────────────────────────────────────────────────────────

async def generate_trajectory_for_task(
    task: MobileScenarioTask,
    config: MobileAgentPipelineConfig,
    runner: MobileSandboxRunner,
    event_callback: Callable = None,
) -> MobileTrajectory:
    """
    为单个任务生成 Agent 操控轨迹。

    流程:
      1. 初始化任务环境
      2. 截图 + UI 树 → 构建 observation
      3. VLM 推理 → 选择动作
      4. 执行动作 → 新截图
      5. 重复 2-4 直到 finish 或达到 max_steps
      6. 执行 check_func 验证
    """
    def emit(msg):
        if event_callback:
            event_callback("trajectory_gen", msg)

    traj_id = f"mobile_{task.task_id}_{int(time.time())}"
    max_steps = task.max_steps if task.max_steps > 0 else config.max_steps

    trajectory = MobileTrajectory(
        trajectory_id=traj_id,
        task_id=task.task_id,
        task_desc=task.task_desc,
        app_package=task.app_package,
        screen_resolution=f"{runner.screen_width}x{runner.screen_height}",
        tools_schema=MOBILE_TOOLS_SCHEMA,
        tags=task.tags,
    )

    # ── 1. 初始化任务环境 ──
    emit(f"  [{task.task_id}] 初始化任务环境...")
    await runner.setup_task(task, emit=lambda t, m: emit(f"    {m}"))

    # ── 2. 构建 System Prompt ──
    # UI 树信息的占位符 (每步动态填充)
    system_prompt = MOBILE_AGENT_SYSTEM_PROMPT.format(
        screen_width=runner.screen_width,
        screen_height=runner.screen_height,
        ui_tree_info="（UI 元素信息将在每次观察时提供）",
        task_desc=task.task_desc,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"请完成以下任务: {task.task_desc}"},
    ]

    # ── 3. Agent 循环 ──
    api_key = config.deepseek_api_key or DEEPSEEK_API_KEY
    base_url = config.deepseek_base_url or DEEPSEEK_BASE_URL
    client = OpenAI(api_key=api_key, base_url=base_url)

    for step_id in range(1, max_steps + 1):
        emit(f"  [{task.task_id}] Step {step_id}/{max_steps}: 观察屏幕...")

        step = MobileTrajectoryStep(step_id=step_id)

        # ── 3a. 获取 Observation ──
        screenshot_data = await runner.get_screenshot()
        ui_tree_data = await runner.get_ui_tree()

        screenshot_b64 = screenshot_data.get("image_base64", "")
        ui_elements = ui_tree_data.get("elements", [])

        step.observation = {
            "has_screenshot": bool(screenshot_b64),
            "ui_elements_count": len(ui_elements),
            "ui_elements": ui_elements[:20],  # 存储前 20 个供回放
        }

        # ── 3b. 构建 observation 消息并追加 ──
        obs_content = _build_observation_content(
            screenshot_b64=screenshot_b64,
            ui_elements=ui_elements,
            screen_w=runner.screen_width,
            screen_h=runner.screen_height,
            enable_vision=config.enable_vision,
        )

        # 第一步用 user 消息附带截图; 后续用 user 消息 (模拟 observation 反馈)
        if step_id == 1:
            # 替换初始 user 消息为带截图的版本
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"请完成以下任务: {task.task_desc}\n\n这是当前屏幕状态:"},
                    *obs_content,
                ],
            }
        else:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "动作已执行, 这是当前屏幕状态:"},
                    *obs_content,
                ],
            })

        # ── 3c. VLM 推理 ──
        emit(f"  [{task.task_id}] Step {step_id}: Agent 推理中...")

        try:
            response = client.chat.completions.create(
                model=config.agent_model,
                messages=messages,
                tools=MOBILE_TOOLS_SCHEMA,
                tool_choice="auto",
                temperature=config.agent_temperature,
                max_tokens=1024,
                stream=False,
            )

            trajectory.total_tokens += (
                response.usage.total_tokens if response.usage else 0
            )

            choice = response.choices[0]
            assistant_msg = choice.message

            # 记录 assistant 消息
            msg_dict = {"role": "assistant", "content": assistant_msg.content or ""}
            if assistant_msg.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_msg.tool_calls
                ]
            messages.append(msg_dict)

            step.thought = assistant_msg.content or ""

            # ── 3d. 处理工具调用 (动作执行) ──
            if assistant_msg.tool_calls:
                tc = assistant_msg.tool_calls[0]  # 每步只处理第一个动作
                try:
                    func_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    func_args = {}

                action_type = func_args.get("action_type", "")
                action_params = func_args.get("params", {})
                action_reasoning = func_args.get("reasoning", "")

                step.action_type = action_type
                step.action_params = action_params
                step.action_reasoning = action_reasoning

                emit(f"    🎯 {action_type}({json.dumps(action_params, ensure_ascii=False)[:60]})")

                # 检查是否是 finish 动作
                if action_type == "finish":
                    trajectory.finish_summary = action_params.get("summary", "")
                    trajectory.task_completed = True

                    # 反馈给消息
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"任务完成: {trajectory.finish_summary}",
                    })

                    step.action_result = {"success": True, "result": "finish"}
                    trajectory.steps.append(asdict(step))
                    trajectory.total_actions += 1
                    trajectory.successful_actions += 1

                    emit(f"  [{task.task_id}] ✓ Agent 宣布完成 (共 {step_id} 步)")
                    break

                # 执行动作
                action_result = await runner.execute_action(action_type, action_params)
                step.action_result = action_result

                trajectory.total_actions += 1
                if action_result["success"]:
                    trajectory.successful_actions += 1

                # 反馈给 Agent
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (
                        f"动作 {action_type} 执行{'成功' if action_result['success'] else '失败'}"
                        f": {action_result['result'][:200]}"
                    ),
                })

                emit(f"      {'✓' if action_result['success'] else '✗'} "
                     f"({action_result['duration_ms']}ms)")

            else:
                # 没有工具调用 — Agent 给出纯文本回复
                if choice.finish_reason == "stop":
                    emit(f"  [{task.task_id}] Agent 停止 (无 finish 调用, 共 {step_id} 步)")
                    break

            trajectory.steps.append(asdict(step))

        except Exception as e:
            emit(f"  [{task.task_id}] ⚠ Step {step_id} 异常: {e}")
            step.action_result = {"success": False, "result": f"异常: {str(e)[:300]}"}
            trajectory.steps.append(asdict(step))
            break

    # ── 4. 任务验证 ──
    try:
        check_result = await runner.check_task(task)
        trajectory.task_check_result = check_result
        if check_result.get("passed") is True:
            trajectory.task_completed = True
            emit(f"  [{task.task_id}] ✅ 任务验证通过")
        elif check_result.get("passed") is False:
            emit(f"  [{task.task_id}] ❌ 任务验证未通过: {check_result.get('detail', '')[:100]}")
    except Exception as e:
        emit(f"  [{task.task_id}] ⚠ 任务验证异常: {e}")

    # 保存完整 messages (去掉 base64 图片避免过大)
    trajectory.messages = _strip_images_from_messages(messages)

    return trajectory


def _strip_images_from_messages(messages: list) -> list:
    """从 messages 中去掉 base64 图片数据, 保留结构"""
    stripped = []
    for msg in messages:
        m = dict(msg)
        if isinstance(m.get("content"), list):
            new_content = []
            for part in m["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    new_content.append({"type": "image_url", "image_url": {"url": "[screenshot]"}})
                else:
                    new_content.append(part)
            m["content"] = new_content
        stripped.append(m)
    return stripped


# ─── 批量轨迹生成 ────────────────────────────────────────────────────────────

async def run_step2(
    tasks: list,
    config: MobileAgentPipelineConfig,
    runner: MobileSandboxRunner,
    event_callback: Callable = None,
) -> list:
    """
    Step 2: 为所有任务生成 Mobile Agent 轨迹

    Args:
        tasks: MobileScenarioTask 列表
        config: Pipeline 配置
        runner: MobileSandboxRunner 实例
        event_callback: 事件回调

    Returns:
        list[MobileTrajectory]
    """
    def emit(t, m):
        if event_callback:
            event_callback(t, m)
        print(f"  [{t}] {m}")

    emit("step2_start", f"Step 2: 为 {len(tasks)} 个任务生成 Mobile Agent 轨迹")

    trajectories = []

    for i, task in enumerate(tasks):
        emit("trajectory_progress",
             f"任务 {i + 1}/{len(tasks)}: {task.task_desc[:50]}...")

        traj = await generate_trajectory_for_task(
            task=task,
            config=config,
            runner=runner,
            event_callback=event_callback,
        )

        trajectories.append(traj)

        emit("trajectory_done",
             f"  ✓ {traj.total_actions} 次动作, "
             f"{traj.successful_actions} 次成功, "
             f"完成={'是' if traj.task_completed else '否'}")

        # 每个任务之间回到主屏幕
        try:
            await runner.execute_action("key_event", {"keycode": 3})  # HOME
            await asyncio.sleep(1)
        except Exception:
            pass

    return trajectories
