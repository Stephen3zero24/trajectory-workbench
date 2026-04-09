"""
Mobile Agent 场景配置模块

管理 MobileSandbox 配置、Agent 提示词、动作空间定义等。
"""

import os
from dataclasses import dataclass, field, asdict
from typing import Optional, List


# ─── 外部配置 ────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# AgentScope Runtime 配置
AGENTSCOPE_MOBILE_IMAGE = os.environ.get(
    "AGENTSCOPE_MOBILE_IMAGE",
    "agentscope/runtime-sandbox-mobile:latest",
)


# ─── 动作空间 ────────────────────────────────────────────────────────────────

# Android GUI Agent 可执行的动作类型
ACTION_TYPES = {
    "tap": {
        "description": "点击屏幕上的指定坐标",
        "parameters": {
            "coords": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "点击坐标 [x, y]",
            }
        },
        "required": ["coords"],
    },
    "long_press": {
        "description": "长按屏幕上的指定坐标",
        "parameters": {
            "coords": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "长按坐标 [x, y]",
            },
            "duration_ms": {
                "type": "integer",
                "description": "长按持续时间（毫秒），默认 1000",
                "default": 1000,
            },
        },
        "required": ["coords"],
    },
    "swipe": {
        "description": "在屏幕上从起点滑动到终点",
        "parameters": {
            "start": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "滑动起点 [x, y]",
            },
            "end": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "滑动终点 [x, y]",
            },
            "duration_ms": {
                "type": "integer",
                "description": "滑动持续时间（毫秒），默认 300",
                "default": 300,
            },
        },
        "required": ["start", "end"],
    },
    "input_text": {
        "description": "在当前聚焦的输入框中输入文本",
        "parameters": {
            "text": {
                "type": "string",
                "description": "要输入的文本内容",
            }
        },
        "required": ["text"],
    },
    "key_event": {
        "description": "发送 Android 按键事件",
        "parameters": {
            "keycode": {
                "type": "integer",
                "description": "Android KeyEvent 代码。常用: 3=HOME, 4=BACK, 24=VOLUME_UP, 25=VOLUME_DOWN, 26=POWER, 82=MENU, 187=APP_SWITCH",
            }
        },
        "required": ["keycode"],
    },
    "wait": {
        "description": "等待指定时间，让界面动画或加载完成",
        "parameters": {
            "seconds": {
                "type": "number",
                "description": "等待秒数，默认 2",
                "default": 2,
            }
        },
        "required": [],
    },
    "finish": {
        "description": "宣布任务完成，给出最终总结",
        "parameters": {
            "summary": {
                "type": "string",
                "description": "任务完成的总结说明",
            }
        },
        "required": ["summary"],
    },
}

# Android 常用按键名 → keycode 映射（供 Agent 参考）
KEY_NAME_MAP = {
    "HOME": 3,
    "BACK": 4,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "POWER": 26,
    "MENU": 82,
    "APP_SWITCH": 187,
    "ENTER": 66,
    "DELETE": 67,
    "TAB": 61,
}


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class MobileScenarioTask:
    """移动端场景中的单个任务"""
    task_id: str = ""
    task_desc: str = ""                     # 自然语言任务描述
    app_package: str = ""                   # 目标应用包名（可选）
    app_activity: str = ""                  # 目标 Activity（可选）
    pre_install_apks: list = field(default_factory=list)   # 需要预装的 APK
    initial_actions: list = field(default_factory=list)     # 初始化动作序列
    check_description: str = ""             # 校验条件的自然语言描述
    check_type: str = "visual"              # visual | shell | ui_tree
    check_command: str = ""                 # shell 命令检查（check_type=shell 时）
    max_steps: int = 0                      # 该任务的步数上限（0=使用全局配置）
    tags: list = field(default_factory=list) # 任务标签


@dataclass
class MobileAgentPipelineConfig:
    """Mobile Agent 完整 pipeline 配置"""

    # ── 基础信息 ──
    task_id: str = ""
    scene_type: str = "mobile_agent"

    # ── Step 0: 场景配置 ──
    scenario_source: str = "builtin"        # builtin | upload | local
    scenario_path: str = ""                 # 场景文件路径
    scenario_filter_tags: list = field(default_factory=list)  # 按标签筛选任务

    # ── Step 1: MobileSandbox 配置 ──
    mobile_image: str = ""                  # Docker 镜像（空=用默认）
    sandbox_timeout: int = 600              # 沙箱总超时秒数
    wait_after_action: float = 1.5          # 每次动作后等待秒数（等界面稳定）
    screenshot_format: str = "png"          # png | jpeg
    enable_ui_tree: bool = True             # 是否同时获取 UI hierarchy

    # ── Step 2: Agent 轨迹生成配置 ──
    agent_model: str = "deepseek-chat"      # LLM 模型
    agent_temperature: float = 0.7
    agent_framework: str = "openai"         # openai | qwen
    max_steps: int = 25                     # 每条轨迹最大步数
    max_tasks: int = 0                      # 从场景中选取的最大任务数（0=全部）
    timeout_per_task: int = 300             # 每个任务超时秒数
    enable_vision: bool = True              # 是否将截图发给 VLM（需要模型支持 vision）

    # ── Review & 迭代 ──
    max_iterations: int = 3
    quality_threshold: float = 0.75

    # ── 输出 ──
    output_dir: str = "output/mobile_agent"

    # ── 内部 LLM 配置 ──
    deepseek_api_key: str = ""
    deepseek_base_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MobileAgentPipelineConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── Agent System Prompt ─────────────────────────────────────────────────────

MOBILE_AGENT_SYSTEM_PROMPT = """你是一个 Android 手机操控 AI 助手。你通过观察屏幕截图来理解当前界面状态，然后决定执行哪个操作来完成用户任务。

## 你的能力

你可以执行以下操作来控制 Android 手机:

- `tap(coords)`: 点击屏幕指定坐标 [x, y]
- `long_press(coords, duration_ms)`: 长按指定坐标
- `swipe(start, end, duration_ms)`: 从 start 滑动到 end
- `input_text(text)`: 在当前输入框中输入文本（先点击输入框再输入）
- `key_event(keycode)`: 发送按键事件（3=HOME, 4=BACK, 66=ENTER, 187=APP_SWITCH）
- `wait(seconds)`: 等待界面加载
- `finish(summary)`: 任务完成，给出总结

## 屏幕信息

当前屏幕分辨率: {screen_width} x {screen_height}

{ui_tree_info}

## 当前任务

{task_desc}

## 工作流程

1. 仔细观察当前屏幕截图，分析界面元素的位置和状态
2. 确定当前需要执行的操作（点击哪个按钮、输入什么文本等）
3. 调用相应的操作函数
4. 观察操作结果（新的截图），判断是否需要继续
5. 任务完成后调用 `finish(summary)` 给出总结

## 重要注意事项

- 坐标 [x, y] 基于屏幕分辨率，x 是水平方向（左→右），y 是垂直方向（上→下）
- 点击按钮时，尽量点击按钮的中心位置
- 输入文本前，先确保已经点击了目标输入框
- 如果界面正在加载，使用 wait 等待
- 上滑浏览更多内容: swipe(start=[540, 1500], end=[540, 500])
- 下滑回到顶部: swipe(start=[540, 500], end=[540, 1500])
- 如果操作失败或界面未变化，尝试其他方式
- 始终使用中文进行推理和总结
"""


# ─── 工具定义（OpenAI function calling 格式）──────────────────────────────────

MOBILE_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "mobile_action",
            "description": (
                "在 Android 设备上执行一个 GUI 操作。"
                "支持的 action_type: tap, long_press, swipe, input_text, key_event, wait, finish"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": list(ACTION_TYPES.keys()),
                        "description": "操作类型",
                    },
                    "params": {
                        "type": "object",
                        "description": "操作参数（根据 action_type 不同而不同）",
                        "additionalProperties": True,
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "执行该操作的理由（为什么点击这里/输入这个文本等）",
                    },
                },
                "required": ["action_type", "params", "reasoning"],
            },
        },
    },
]
