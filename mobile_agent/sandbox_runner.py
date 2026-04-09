"""
Step 1: MobileSandbox 启动与管理

负责:
  - 启动 AgentScope Runtime MobileSandbox (拉取 agentscope/runtime-sandbox-mobile 镜像)
  - 获取屏幕分辨率等设备信息
  - 可选: 安装 APK、执行初始化动作
  - 提供统一的 action 执行接口
  - 沙箱生命周期管理 (创建 → 使用 → 销毁)

AgentScope MobileSandbox 提供的原生接口:
  - mobile_tap(coords)
  - mobile_input_text(text)
  - mobile_key_event(keycode)
  - mobile_get_screenshot()
  - mobile_get_screen_resolution()
  - run_shell_command(command)       ← 用于 adb shell / uiautomator
"""

import asyncio
import base64
import json
import os
import time
from typing import Callable, Optional

from .config import (
    MobileAgentPipelineConfig,
    MobileScenarioTask,
    AGENTSCOPE_MOBILE_IMAGE,
)


# ─── MobileSandbox 包装器 ────────────────────────────────────────────────────

class MobileSandboxRunner:
    """
    封装 AgentScope MobileSandbox, 提供统一的操控接口。

    支持两种后端:
      - agentscope: 使用 agentscope_runtime.sandbox.MobileSandbox
      - mock: 模拟模式, 用于离线开发和测试
    """

    def __init__(self, config: MobileAgentPipelineConfig, backend: str = "agentscope"):
        self.config = config
        self.backend = backend
        self._box = None
        self._screen_width = 1080
        self._screen_height = 2340
        self._started = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def start(self, emit: Callable = None):
        """启动 MobileSandbox"""
        _emit = emit or (lambda t, m: None)

        if self.backend == "mock":
            _emit("sandbox_start", "🧪 Mock 模式: 跳过真实沙箱启动")
            self._started = True
            return

        _emit("sandbox_start", "拉取 MobileSandbox 镜像并启动...")

        try:
            # 尝试同步版本
            from agentscope_runtime.sandbox import MobileSandbox
            self._box = MobileSandbox()
            self._box.__enter__()
            _emit("sandbox_start", "MobileSandbox 已启动 (sync)")

        except ImportError:
            # 回退: 尝试异步版本
            try:
                from agentscope_runtime.sandbox import MobileSandboxAsync
                self._box = MobileSandboxAsync()
                await self._box.__aenter__()
                _emit("sandbox_start", "MobileSandbox 已启动 (async)")
            except ImportError:
                raise RuntimeError(
                    "agentscope-runtime 未安装。请运行: "
                    "pip install agentscope-runtime\n"
                    "并拉取镜像: docker pull agentscope/runtime-sandbox-mobile:latest"
                )

        # 获取屏幕信息
        try:
            res = await self._call_box("mobile_get_screen_resolution")
            if isinstance(res, dict):
                self._screen_width = res.get("width", 1080)
                self._screen_height = res.get("height", 2340)
            elif isinstance(res, (list, tuple)) and len(res) >= 2:
                self._screen_width, self._screen_height = res[0], res[1]
            _emit("sandbox_ready",
                  f"✅ 设备就绪: {self._screen_width}x{self._screen_height}")
        except Exception as e:
            _emit("sandbox_warn", f"⚠ 获取分辨率失败, 使用默认值: {e}")

        self._started = True

    async def stop(self, emit: Callable = None):
        """停止并清理 MobileSandbox"""
        _emit = emit or (lambda t, m: None)

        if not self._started:
            return

        if self._box is not None:
            try:
                if hasattr(self._box, '__aexit__'):
                    await self._box.__aexit__(None, None, None)
                elif hasattr(self._box, '__exit__'):
                    self._box.__exit__(None, None, None)
            except Exception as e:
                _emit("sandbox_warn", f"⚠ 沙箱关闭异常: {e}")
            finally:
                self._box = None

        self._started = False
        _emit("sandbox_stop", "MobileSandbox 已停止")

    @property
    def screen_width(self) -> int:
        return self._screen_width

    @property
    def screen_height(self) -> int:
        return self._screen_height

    @property
    def is_running(self) -> bool:
        return self._started

    # ── 核心操控接口 ──────────────────────────────────────────────────────

    async def execute_action(self, action_type: str, params: dict) -> dict:
        """
        执行一个 GUI 动作

        Args:
            action_type: tap | long_press | swipe | input_text | key_event | wait | finish
            params: 动作参数

        Returns:
            {"success": bool, "result": str, "duration_ms": int}
        """
        start = time.time()

        try:
            if action_type == "tap":
                result = await self._call_box("mobile_tap", params["coords"])
            elif action_type == "long_press":
                coords = params["coords"]
                duration = params.get("duration_ms", 1000)
                # MobileSandbox 可能不直接支持 long_press, 通过 shell 实现
                result = await self._run_shell(
                    f"input swipe {coords[0]} {coords[1]} {coords[0]} {coords[1]} {duration}"
                )
            elif action_type == "swipe":
                start_pos = params["start"]
                end_pos = params["end"]
                duration = params.get("duration_ms", 300)
                result = await self._run_shell(
                    f"input swipe {start_pos[0]} {start_pos[1]} "
                    f"{end_pos[0]} {end_pos[1]} {duration}"
                )
            elif action_type == "input_text":
                result = await self._call_box("mobile_input_text", params["text"])
            elif action_type == "key_event":
                result = await self._call_box("mobile_key_event", params["keycode"])
            elif action_type == "wait":
                secs = params.get("seconds", 2)
                await asyncio.sleep(secs)
                result = f"等待 {secs} 秒完成"
            elif action_type == "finish":
                result = params.get("summary", "任务完成")
            else:
                return {
                    "success": False,
                    "result": f"未知动作类型: {action_type}",
                    "duration_ms": 0,
                }

            duration_ms = int((time.time() - start) * 1000)

            # 动作后等待界面稳定
            if action_type not in ("wait", "finish"):
                await asyncio.sleep(self.config.wait_after_action)

            return {
                "success": True,
                "result": str(result) if result else "OK",
                "duration_ms": duration_ms,
            }

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return {
                "success": False,
                "result": f"动作执行失败: {e}",
                "duration_ms": duration_ms,
            }

    async def get_screenshot(self) -> dict:
        """
        获取当前屏幕截图

        Returns:
            {"success": bool, "image_base64": str, "format": str}
        """
        if self.backend == "mock":
            return {
                "success": True,
                "image_base64": _MOCK_SCREENSHOT_B64,
                "format": "png",
            }

        try:
            result = await self._call_box("mobile_get_screenshot")

            # AgentScope 返回格式可能是 dict 或直接 base64
            if isinstance(result, dict):
                img_b64 = result.get("image", result.get("base64", result.get("data", "")))
            elif isinstance(result, str):
                img_b64 = result
            elif isinstance(result, bytes):
                img_b64 = base64.b64encode(result).decode("utf-8")
            else:
                img_b64 = str(result)

            # 去掉可能的 data:image/png;base64, 前缀
            if img_b64.startswith("data:"):
                img_b64 = img_b64.split(",", 1)[1]

            return {
                "success": True,
                "image_base64": img_b64,
                "format": self.config.screenshot_format,
            }
        except Exception as e:
            return {"success": False, "image_base64": "", "format": "png", "error": str(e)}

    async def get_ui_tree(self) -> dict:
        """
        获取当前 UI hierarchy (通过 uiautomator dump)

        Returns:
            {"success": bool, "xml": str, "elements": list}
        """
        if not self.config.enable_ui_tree:
            return {"success": False, "xml": "", "elements": [], "reason": "ui_tree 已禁用"}

        try:
            # 通过 adb shell uiautomator dump 获取
            result = await self._run_shell(
                "uiautomator dump /tmp/ui.xml 2>/dev/null && cat /tmp/ui.xml"
            )
            xml_str = str(result) if result else ""

            # 简单解析出可交互元素
            elements = _parse_ui_tree_simple(xml_str)

            return {
                "success": bool(xml_str),
                "xml": xml_str[:8000],  # 截断过长的 XML
                "elements": elements[:50],  # 最多 50 个元素
            }
        except Exception as e:
            return {"success": False, "xml": "", "elements": [], "error": str(e)}

    # ── 初始化任务环境 ────────────────────────────────────────────────────

    async def setup_task(self, task: MobileScenarioTask, emit: Callable = None):
        """
        为特定任务初始化环境: 安装 APK, 启动目标 Activity, 执行初始化动作
        """
        _emit = emit or (lambda t, m: None)

        # 安装 APK
        for apk_path in task.pre_install_apks:
            _emit("setup", f"安装 APK: {apk_path}")
            await self._run_shell(f"pm install -r {apk_path}")

        # 启动目标应用
        if task.app_package:
            if task.app_activity:
                cmd = f"am start -n {task.app_package}/{task.app_activity}"
            else:
                cmd = f"monkey -p {task.app_package} -c android.intent.category.LAUNCHER 1"
            _emit("setup", f"启动应用: {task.app_package}")
            await self._run_shell(cmd)
            await asyncio.sleep(2)  # 等待应用启动

        # 执行初始化动作序列
        for action in task.initial_actions:
            a_type = action.get("type", "")
            a_params = action.get("params", {})
            _emit("setup", f"初始化动作: {a_type}")
            await self.execute_action(a_type, a_params)

    async def check_task(self, task: MobileScenarioTask) -> dict:
        """
        执行任务验证

        Returns:
            {"passed": bool, "detail": str}
        """
        if task.check_type == "shell" and task.check_command:
            result = await self._run_shell(task.check_command)
            return {
                "passed": bool(result and str(result).strip()),
                "detail": str(result)[:500] if result else "(无输出)",
            }

        # visual / 默认: 由 Review Agent 根据截图判断
        return {"passed": None, "detail": "需要 Review Agent 通过截图判断"}

    # ── 内部方法 ──────────────────────────────────────────────────────────

    async def _call_box(self, method: str, *args, **kwargs):
        """调用 MobileSandbox 的方法 (兼容 sync/async)"""
        if self.backend == "mock":
            return f"mock:{method}({args})"

        func = getattr(self._box, method)
        result = func(*args, **kwargs)

        # 如果返回的是 coroutine, await 它
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            result = await result

        return result

    async def _run_shell(self, command: str) -> str:
        """在沙箱中执行 shell 命令"""
        if self.backend == "mock":
            return f"mock_shell: {command}"

        try:
            result = await self._call_box("run_shell_command", command=command)
            if isinstance(result, dict):
                return result.get("output", result.get("stdout", str(result)))
            return str(result)
        except Exception as e:
            return f"shell error: {e}"


# ─── UI Tree 解析 ────────────────────────────────────────────────────────────

def _parse_ui_tree_simple(xml_str: str) -> list:
    """
    从 uiautomator dump 的 XML 中提取可交互元素的简要信息

    Returns:
        [{"text": str, "class": str, "bounds": str, "clickable": bool, "resource_id": str}, ...]
    """
    import re

    elements = []
    # 匹配 <node ... /> 标签
    pattern = re.compile(
        r'<node\s+[^>]*?'
        r'text="([^"]*)"[^>]*?'
        r'resource-id="([^"]*)"[^>]*?'
        r'class="([^"]*)"[^>]*?'
        r'clickable="([^"]*)"[^>]*?'
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
    )

    for m in pattern.finditer(xml_str):
        text, res_id, cls, clickable, x1, y1, x2, y2 = m.groups()
        # 只保留有意义的元素（有文字或可点击）
        if text or clickable == "true":
            cx = (int(x1) + int(x2)) // 2
            cy = (int(y1) + int(y2)) // 2
            elements.append({
                "text": text,
                "resource_id": res_id,
                "class": cls.split(".")[-1],  # 只保留类名
                "clickable": clickable == "true",
                "center": [cx, cy],
                "bounds": f"[{x1},{y1}][{x2},{y2}]",
            })

    return elements


def format_ui_elements_for_prompt(elements: list) -> str:
    """将 UI 元素列表格式化为 Agent 可读的文本"""
    if not elements:
        return "（无可用 UI 元素信息）"

    lines = ["当前屏幕可交互元素:"]
    for i, el in enumerate(elements):
        parts = [f"  [{i}]"]
        if el.get("text"):
            parts.append(f'"{el["text"]}"')
        parts.append(f'({el.get("class", "?")})')
        if el.get("clickable"):
            parts.append("可点击")
        parts.append(f'中心坐标={el.get("center", "?")}')
        if el.get("resource_id"):
            parts.append(f'id={el["resource_id"]}')
        lines.append(" ".join(parts))

    return "\n".join(lines)


# ─── Step 1 整合入口 ─────────────────────────────────────────────────────────

async def run_step1(
    config: MobileAgentPipelineConfig,
    event_callback: Callable = None,
) -> dict:
    """
    Step 1: 启动 MobileSandbox

    Returns:
        {"runner": MobileSandboxRunner, "screen_width": int, "screen_height": int}
    """
    def emit(t, m):
        if event_callback:
            event_callback(t, m)
        print(f"  [{t}] {m}")

    emit("step1_start", "Step 1: 启动 MobileSandbox")

    # 判断后端
    backend = "agentscope"
    try:
        import agentscope_runtime  # noqa: F401
    except ImportError:
        emit("step1_warn", "⚠ agentscope-runtime 未安装, 切换到 Mock 模式")
        backend = "mock"

    runner = MobileSandboxRunner(config, backend=backend)
    await runner.start(emit=emit)

    emit("step1_done",
         f"✅ MobileSandbox 就绪 ({backend}): "
         f"{runner.screen_width}x{runner.screen_height}")

    return {
        "runner": runner,
        "screen_width": runner.screen_width,
        "screen_height": runner.screen_height,
        "backend": backend,
    }


# ─── Mock 截图占位符 ─────────────────────────────────────────────────────────

# 1x1 透明 PNG (最小合法 base64)
_MOCK_SCREENSHOT_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
