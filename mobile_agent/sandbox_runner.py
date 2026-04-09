"""
Step 1: Android 沙箱启动与管理（基于 OpenSandbox）

架构:
  OpenSandbox Server → 创建 Docker 容器 (Android 模拟器镜像)
                     → 通过 sandbox.commands.run() 执行 ADB 命令
                     → 统一纳入 CCE/K8s 集群管理

与 envscaler 共用同一个 OpenSandbox 控制面, 区别仅在于:
  - 镜像不同: envscaler 用 code-interpreter, 这里用 docker-android
  - 交互方式不同: envscaler 通过 MCP Server, 这里通过 ADB shell

ADB 命令映射:
  - tap(x, y)           → adb shell input tap X Y
  - swipe(...)          → adb shell input swipe X1 Y1 X2 Y2 DURATION
  - input_text(text)    → adb shell input text "TEXT"
  - key_event(code)     → adb shell input keyevent CODE
  - screenshot()        → adb shell screencap -p FILE && base64 FILE
  - ui_tree()           → adb shell uiautomator dump && cat XML
  - screen_resolution() → adb shell wm size
"""

import asyncio
import base64
import json
import os
import re
import time
from datetime import timedelta
from typing import Callable, Optional

import httpx
from opensandbox.sandbox import Sandbox
from opensandbox.config import ConnectionConfig

from .config import (
    MobileAgentPipelineConfig,
    MobileScenarioTask,
    OPENSANDBOX_SERVER,
    MOBILE_SANDBOX_IMAGE,
)


# ─── OpenSandbox 沙箱管理 ────────────────────────────────────────────────────

async def create_mobile_sandbox(config: MobileAgentPipelineConfig) -> str:
    """
    通过 OpenSandbox API 创建 Android 模拟器沙箱

    Returns:
        sandbox_id: str
    """
    image = config.mobile_image or MOBILE_SANDBOX_IMAGE

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes",
            json={
                "image": {"uri": image},
                # docker-android 需要 privileged 或 /dev/kvm 访问
                # OpenSandbox 在 CCE 上会根据集群配置处理设备透传
                "resourceLimits": {
                    "memory": "4Gi",
                    "cpu": "2",
                },
            },
            timeout=120,  # 镜像大, 首次拉取可能较慢
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def connect_sandbox(sandbox_id: str) -> Sandbox:
    """连接到已创建的沙箱"""
    server = OPENSANDBOX_SERVER.replace("http://", "").replace("https://", "")
    protocol = "https" if "https" in OPENSANDBOX_SERVER else "http"
    config = ConnectionConfig(domain=server, protocol=protocol)
    return await Sandbox.connect(sandbox_id, connection_config=config)


async def delete_sandbox(sandbox_id: str):
    """删除沙箱"""
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{OPENSANDBOX_SERVER}/v1/sandboxes/{sandbox_id}",
            timeout=30,
        )


# ─── 沙箱内命令执行辅助 ──────────────────────────────────────────────────────

async def _run_cmd(sandbox: Sandbox, cmd: str, timeout_sec: int = 30) -> str:
    """
    在沙箱内执行命令并返回 stdout

    这是本模块的底层原语 — 所有 ADB 操作最终都经过这里。
    """
    result = await sandbox.commands.run(
        cmd,
        timeout=timedelta(seconds=timeout_sec),
    )
    stdout = ""
    if result.logs.stdout:
        stdout = "\n".join([l.text for l in result.logs.stdout])
    return stdout


async def _run_adb(sandbox: Sandbox, adb_cmd: str, timeout_sec: int = 30) -> str:
    """
    在沙箱内执行 ADB shell 命令

    docker-android 镜像中 ADB 直接可用, 无需额外连接。
    """
    return await _run_cmd(sandbox, f"adb shell {adb_cmd}", timeout_sec)


# ─── MobileSandboxRunner (基于 OpenSandbox) ─────────────────────────────────

class MobileSandboxRunner:
    """
    基于 OpenSandbox 的 Android 模拟器操控器。

    生命周期:
      1. create_mobile_sandbox()  → 获得 sandbox_id
      2. connect_sandbox()        → 获得 Sandbox 对象
      3. _wait_emulator_boot()    → 等待 Android 模拟器启动完成
      4. 执行动作 / 截图 / UI dump
      5. delete_sandbox()         → 清理

    支持两种后端:
      - opensandbox: 真实 OpenSandbox + Android 模拟器
      - mock: 模拟模式, 用于离线开发和测试
    """

    def __init__(self, config: MobileAgentPipelineConfig, backend: str = "opensandbox"):
        self.config = config
        self.backend = backend
        self._sandbox_id: Optional[str] = None
        self._sandbox: Optional[Sandbox] = None
        self._screen_width = 1080
        self._screen_height = 2340
        self._started = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def start(self, emit: Callable = None):
        """创建沙箱 → 连接 → 等待模拟器启动"""
        _emit = emit or (lambda t, m: None)

        if self.backend == "mock":
            _emit("sandbox_start", "🧪 Mock 模式: 跳过真实沙箱启动")
            self._started = True
            return

        # 1. 创建沙箱
        image = self.config.mobile_image or MOBILE_SANDBOX_IMAGE
        _emit("sandbox_create", f"创建 Android 沙箱 (镜像: {image})...")
        self._sandbox_id = await create_mobile_sandbox(self.config)
        _emit("sandbox_create", f"沙箱已创建: {self._sandbox_id[:12]}...")

        # 2. 连接沙箱
        await asyncio.sleep(3)  # 等待容器初始化
        self._sandbox = await connect_sandbox(self._sandbox_id)

        # 3. 等待 Android 模拟器启动
        _emit("emulator_boot", "等待 Android 模拟器启动...")
        boot_ok = await self._wait_emulator_boot(
            timeout=self.config.emulator_boot_timeout,
            emit=_emit,
        )

        if not boot_ok:
            _emit("emulator_warn", "⚠ 模拟器可能未完全启动, 继续尝试...")

        # 4. 获取屏幕分辨率
        try:
            size_str = await _run_adb(self._sandbox, "wm size")
            match = re.search(r'(\d+)x(\d+)', size_str)
            if match:
                self._screen_width = int(match.group(1))
                self._screen_height = int(match.group(2))
            _emit("sandbox_ready",
                  f"✅ 设备就绪: {self._screen_width}x{self._screen_height}")
        except Exception as e:
            _emit("sandbox_warn", f"⚠ 获取分辨率失败, 使用默认值: {e}")

        self._started = True

    async def stop(self, emit: Callable = None):
        """停止并清理沙箱"""
        _emit = emit or (lambda t, m: None)

        if not self._started:
            return

        if self._sandbox_id:
            try:
                await delete_sandbox(self._sandbox_id)
                _emit("sandbox_stop", f"沙箱已删除: {self._sandbox_id[:12]}...")
            except Exception as e:
                _emit("sandbox_warn", f"⚠ 沙箱清理异常: {e}")
            finally:
                self._sandbox_id = None
                self._sandbox = None

        self._started = False
        _emit("sandbox_stop", "Android 沙箱已停止")

    async def _wait_emulator_boot(self, timeout: int = 120, emit: Callable = None) -> bool:
        """等待 ADB 和 Android 系统就绪"""
        _emit = emit or (lambda t, m: None)

        start = time.time()
        for attempt in range(timeout // 5):
            elapsed = int(time.time() - start)
            if elapsed >= timeout:
                break

            try:
                # 检查系统是否已完成启动
                boot = await _run_adb(self._sandbox, "getprop sys.boot_completed", timeout_sec=10)
                if boot.strip() == "1":
                    _emit("emulator_boot", f"✅ 模拟器启动完成 ({elapsed}s)")
                    return True

                _emit("emulator_boot", f"  等待中... ({elapsed}s/{timeout}s)")
            except Exception:
                pass

            await asyncio.sleep(5)

        _emit("emulator_warn", f"⚠ 模拟器启动超时 ({timeout}s)")
        return False

    @property
    def sandbox_id(self) -> Optional[str]:
        return self._sandbox_id

    @property
    def screen_width(self) -> int:
        return self._screen_width

    @property
    def screen_height(self) -> int:
        return self._screen_height

    @property
    def is_running(self) -> bool:
        return self._started

    # ── 核心操控接口 (通过 ADB) ───────────────────────────────────────────

    async def execute_action(self, action_type: str, params: dict) -> dict:
        """
        执行一个 GUI 动作 (通过 OpenSandbox → ADB shell)
        """
        start_t = time.time()

        try:
            result = await self._dispatch_action(action_type, params)

            duration_ms = int((time.time() - start_t) * 1000)

            # 动作后等待界面稳定
            if action_type not in ("wait", "finish"):
                await asyncio.sleep(self.config.wait_after_action)

            return {
                "success": True,
                "result": str(result) if result else "OK",
                "duration_ms": duration_ms,
            }

        except Exception as e:
            duration_ms = int((time.time() - start_t) * 1000)
            return {
                "success": False,
                "result": f"动作执行失败: {e}",
                "duration_ms": duration_ms,
            }

    async def _dispatch_action(self, action_type: str, params: dict) -> str:
        """将 action 分发到对应的 ADB 命令"""

        if self.backend == "mock":
            return f"mock:{action_type}({params})"

        sandbox = self._ensure_sandbox()

        if action_type == "tap":
            x, y = params["coords"]
            return await _run_adb(sandbox, f"input tap {x} {y}")

        elif action_type == "long_press":
            x, y = params["coords"]
            dur = params.get("duration_ms", 1000)
            return await _run_adb(sandbox, f"input swipe {x} {y} {x} {y} {dur}")

        elif action_type == "swipe":
            sx, sy = params["start"]
            ex, ey = params["end"]
            dur = params.get("duration_ms", 300)
            return await _run_adb(sandbox, f"input swipe {sx} {sy} {ex} {ey} {dur}")

        elif action_type == "input_text":
            text = params["text"]
            escaped = text.replace(" ", "%s").replace("'", "\\'")
            return await _run_adb(sandbox, f"input text '{escaped}'")

        elif action_type == "key_event":
            keycode = params["keycode"]
            return await _run_adb(sandbox, f"input keyevent {keycode}")

        elif action_type == "wait":
            secs = params.get("seconds", 2)
            await asyncio.sleep(secs)
            return f"等待 {secs} 秒完成"

        elif action_type == "finish":
            return params.get("summary", "任务完成")

        else:
            raise ValueError(f"未知动作类型: {action_type}")

    async def get_screenshot(self) -> dict:
        """获取当前屏幕截图 (通过 ADB screencap)"""
        if self.backend == "mock":
            return {
                "success": True,
                "image_base64": _MOCK_SCREENSHOT_B64,
                "format": "png",
            }

        try:
            sandbox = self._ensure_sandbox()

            # screencap → 写文件 → base64 编码 → 读取
            await _run_adb(sandbox, "screencap -p /tmp/screen.png", timeout_sec=15)
            img_b64 = await _run_cmd(sandbox, "base64 /tmp/screen.png", timeout_sec=15)
            img_b64 = img_b64.strip().replace("\n", "")

            return {
                "success": bool(img_b64),
                "image_base64": img_b64,
                "format": "png",
            }
        except Exception as e:
            return {"success": False, "image_base64": "", "format": "png", "error": str(e)}

    async def get_ui_tree(self) -> dict:
        """获取当前 UI hierarchy (通过 ADB + uiautomator dump)"""
        if not self.config.enable_ui_tree:
            return {"success": False, "xml": "", "elements": [], "reason": "ui_tree 已禁用"}

        if self.backend == "mock":
            return {"success": True, "xml": "<mock/>", "elements": []}

        try:
            sandbox = self._ensure_sandbox()
            xml_str = await _run_adb(
                sandbox,
                "uiautomator dump /tmp/ui.xml 2>/dev/null && cat /tmp/ui.xml",
                timeout_sec=15,
            )
            elements = _parse_ui_tree_simple(xml_str)

            return {
                "success": bool(xml_str),
                "xml": xml_str[:8000],
                "elements": elements[:50],
            }
        except Exception as e:
            return {"success": False, "xml": "", "elements": [], "error": str(e)}

    # ── 初始化任务环境 ────────────────────────────────────────────────────

    async def setup_task(self, task: MobileScenarioTask, emit: Callable = None):
        """为特定任务初始化环境: 安装 APK, 启动目标 Activity, 执行初始化动作"""
        _emit = emit or (lambda t, m: None)

        if self.backend == "mock":
            _emit("setup", "Mock: 跳过任务初始化")
            return

        sandbox = self._ensure_sandbox()

        for apk_path in task.pre_install_apks:
            _emit("setup", f"安装 APK: {apk_path}")
            await _run_adb(sandbox, f"pm install -r {apk_path}", timeout_sec=60)

        if task.app_package:
            if task.app_activity:
                cmd = f"am start -n {task.app_package}/{task.app_activity}"
            else:
                cmd = f"monkey -p {task.app_package} -c android.intent.category.LAUNCHER 1"
            _emit("setup", f"启动应用: {task.app_package}")
            await _run_adb(sandbox, cmd)
            await asyncio.sleep(2)

        for action in task.initial_actions:
            a_type = action.get("type", "")
            a_params = action.get("params", {})
            _emit("setup", f"初始化动作: {a_type}")
            await self.execute_action(a_type, a_params)

    async def check_task(self, task: MobileScenarioTask) -> dict:
        """执行任务验证"""
        if task.check_type == "shell" and task.check_command:
            if self.backend == "mock":
                return {"passed": None, "detail": "Mock 模式"}

            sandbox = self._ensure_sandbox()
            cmd = task.check_command
            if not cmd.startswith("adb"):
                result = await _run_adb(sandbox, cmd)
            else:
                result = await _run_cmd(sandbox, cmd)

            return {
                "passed": bool(result and result.strip()),
                "detail": result[:500] if result else "(无输出)",
            }

        return {"passed": None, "detail": "需要 Review Agent 通过截图判断"}

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _ensure_sandbox(self) -> Sandbox:
        """确保有可用的 Sandbox 连接"""
        if self._sandbox is None:
            raise RuntimeError("沙箱未启动或已断开连接")
        return self._sandbox


# ─── UI Tree 解析 ────────────────────────────────────────────────────────────

def _parse_ui_tree_simple(xml_str: str) -> list:
    """从 uiautomator dump 的 XML 中提取可交互元素"""
    elements = []
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
        if text or clickable == "true":
            cx = (int(x1) + int(x2)) // 2
            cy = (int(y1) + int(y2)) // 2
            elements.append({
                "text": text,
                "resource_id": res_id,
                "class": cls.split(".")[-1],
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
    Step 1: 通过 OpenSandbox 启动 Android 模拟器沙箱

    Returns:
        {"runner": MobileSandboxRunner, "sandbox_id": str, "screen_width": int, ...}
    """
    def emit(t, m):
        if event_callback:
            event_callback(t, m)
        print(f"  [{t}] {m}")

    emit("step1_start", "Step 1: 启动 Android 模拟器沙箱 (OpenSandbox)")

    # 判断后端: 检查 OpenSandbox Server 是否可达
    backend = "opensandbox"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OPENSANDBOX_SERVER}/healthz", timeout=5)
            if resp.status_code != 200:
                raise ConnectionError()
    except Exception:
        emit("step1_warn", "⚠ OpenSandbox Server 不可达, 切换到 Mock 模式")
        backend = "mock"

    runner = MobileSandboxRunner(config, backend=backend)
    await runner.start(emit=emit)

    emit("step1_done",
         f"✅ Android 沙箱就绪 ({backend}): "
         f"{runner.screen_width}x{runner.screen_height}")

    return {
        "runner": runner,
        "sandbox_id": runner.sandbox_id,
        "screen_width": runner.screen_width,
        "screen_height": runner.screen_height,
        "backend": backend,
    }


# ─── Mock 截图占位符 ─────────────────────────────────────────────────────────

_MOCK_SCREENSHOT_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
