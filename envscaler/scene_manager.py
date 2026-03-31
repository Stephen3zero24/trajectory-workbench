"""
Step 0: 场景文件管理

负责:
  - 从上传文件加载场景 (upload 模式)
  - 从本地目录加载场景 (local 模式)
  - 从 EnvScaler 数据目录提取场景 (extract 模式)
  - 解析场景内容, 提取任务列表和可用工具
"""

import json
import os
import shutil
import uuid
from typing import Callable, Optional

from .config import (
    EnvScalerPipelineConfig,
    SceneFile,
    SceneTask,
)


# ─── 场景文件加载 ─────────────────────────────────────────────────────────────

def load_scene_from_paths(
    scenario_path: str,
    metadata_path: str,
) -> SceneFile:
    """
    从指定路径加载一组场景文件

    Args:
        scenario_path: env_scenario.json 的路径
        metadata_path: filtered_env_metadata.json 的路径

    Returns:
        SceneFile: 解析后的场景文件对象
    """
    scene = SceneFile(
        scenario_path=scenario_path,
        metadata_path=metadata_path,
    )

    # 加载场景数据
    if os.path.exists(scenario_path):
        with open(scenario_path, "r", encoding="utf-8") as f:
            scene.scenario_content = json.load(f)
    else:
        raise FileNotFoundError(f"场景文件不存在: {scenario_path}")

    # 加载元数据
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as f:
            scene.metadata_content = json.load(f)
    else:
        raise FileNotFoundError(f"元数据文件不存在: {metadata_path}")

    # 提取环境名称
    if isinstance(scene.metadata_content, list) and scene.metadata_content:
        scene.env_name = scene.metadata_content[0].get(
            "env_name", scene.metadata_content[0].get("name", "unknown")
        )
    elif isinstance(scene.metadata_content, dict):
        scene.env_name = scene.metadata_content.get(
            "env_name", scene.metadata_content.get("name", "unknown")
        )

    # 统计任务数量
    if isinstance(scene.scenario_content, list):
        scene.task_count = len(scene.scenario_content)
    elif isinstance(scene.scenario_content, dict):
        tasks = scene.scenario_content.get("tasks", scene.scenario_content.get("scenarios", []))
        scene.task_count = len(tasks) if isinstance(tasks, list) else 1

    return scene


def load_scene_from_directory(scene_dir: str) -> SceneFile:
    """
    从目录中自动查找并加载场景文件

    Args:
        scene_dir: 包含场景文件的目录

    Returns:
        SceneFile
    """
    scenario_path = os.path.join(scene_dir, "env_scenario.json")
    metadata_path = os.path.join(scene_dir, "filtered_env_metadata.json")

    # 如果标准文件名不存在, 尝试查找 JSON 文件
    if not os.path.exists(scenario_path):
        json_files = [f for f in os.listdir(scene_dir) if f.endswith(".json")]
        for jf in json_files:
            if "scenario" in jf.lower():
                scenario_path = os.path.join(scene_dir, jf)
            elif "metadata" in jf.lower() or "env" in jf.lower():
                metadata_path = os.path.join(scene_dir, jf)

    return load_scene_from_paths(scenario_path, metadata_path)


def load_scene_from_upload(upload_dir: str) -> SceneFile:
    """
    从上传目录加载场景文件（Web UI 上传模式）

    Args:
        upload_dir: 上传文件所在目录

    Returns:
        SceneFile
    """
    return load_scene_from_directory(upload_dir)


# ─── 场景提取（extract 模式） ────────────────────────────────────────────────

def extract_scenes_from_envscaler(
    envscaler_data_dir: str,
    extract_count: int = 1,
    output_dir: str = "",
) -> list:
    """
    从 EnvScaler 数据目录提取指定数量的场景文件

    模拟 get_scences.py 的功能:
    - 检查可用场景数量
    - 提取指定数量的场景
    - 标记已使用的场景

    Args:
        envscaler_data_dir: EnvScaler 数据根目录
        extract_count: 要提取的场景数量
        output_dir: 提取后的输出目录

    Returns:
        list[SceneFile]: 提取的场景文件列表
    """
    # 查找所有可用的场景目录
    index_path = os.path.join(envscaler_data_dir, "scene_index.json")

    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    else:
        # 如果没有索引文件，扫描目录
        index = _build_scene_index(envscaler_data_dir)

    # 找出未使用的场景
    all_scenes = index.get("scenes", [])
    used_ids = set(index.get("used", []))
    available = [s for s in all_scenes if s.get("id") not in used_ids]

    if not available:
        print(f"  ⚠ 当前已无可用场景文件 (total={len(all_scenes)}, used={len(used_ids)})")
        return []

    actual_count = min(extract_count, len(available))
    selected = available[:actual_count]

    # 复制场景文件到输出目录
    if not output_dir:
        output_dir = os.path.join(envscaler_data_dir, "extract")
    os.makedirs(output_dir, exist_ok=True)

    scene_files = []
    for item in selected:
        src_dir = item.get("path", "")
        if src_dir and os.path.isdir(src_dir):
            # 复制文件
            for fname in ["env_scenario.json", "filtered_env_metadata.json"]:
                src = os.path.join(src_dir, fname)
                dst = os.path.join(output_dir, fname)
                if os.path.exists(src):
                    shutil.copy2(src, dst)

            try:
                sf = load_scene_from_directory(output_dir)
                scene_files.append(sf)
            except Exception as e:
                print(f"  ⚠ 加载场景失败 ({item.get('id', '?')}): {e}")
                continue

        # 标记为已使用
        used_ids.add(item.get("id"))

    # 更新索引
    index["used"] = list(used_ids)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"  提取了 {len(scene_files)} 个场景 (可用: {len(available) - len(selected)})")
    return scene_files


def _build_scene_index(data_dir: str) -> dict:
    """扫描目录构建场景索引"""
    scenes = []
    for item in os.listdir(data_dir):
        item_path = os.path.join(data_dir, item)
        if os.path.isdir(item_path):
            scenario = os.path.join(item_path, "env_scenario.json")
            metadata = os.path.join(item_path, "filtered_env_metadata.json")
            if os.path.exists(scenario) and os.path.exists(metadata):
                scenes.append({
                    "id": item,
                    "path": item_path,
                })
    return {"scenes": scenes, "used": []}


# ─── 任务解析 ─────────────────────────────────────────────────────────────────

def parse_tasks_from_scene(scene: SceneFile) -> list:
    """
    从场景文件中解析出任务列表

    Args:
        scene: 场景文件对象

    Returns:
        list[SceneTask]: 任务列表
    """
    tasks = []
    scenario = scene.scenario_content
    metadata = scene.metadata_content

    # 提取可用工具列表
    available_tools = _extract_tools_from_metadata(metadata)

    # 解析场景中的任务
    if isinstance(scenario, list):
        # 场景数据是一个任务列表
        for i, sc in enumerate(scenario):
            task = _parse_single_task(sc, i, scene.env_name, available_tools)
            if task:
                tasks.append(task)
    elif isinstance(scenario, dict):
        # 场景数据是单个对象，可能包含 tasks 列表
        task_list = scenario.get("tasks", scenario.get("scenarios", [scenario]))
        if isinstance(task_list, list):
            for i, sc in enumerate(task_list):
                task = _parse_single_task(sc, i, scene.env_name, available_tools)
                if task:
                    tasks.append(task)
        else:
            task = _parse_single_task(scenario, 0, scene.env_name, available_tools)
            if task:
                tasks.append(task)

    return tasks


def _parse_single_task(
    task_data: dict,
    index: int,
    env_name: str,
    available_tools: list,
) -> Optional[SceneTask]:
    """解析单个任务"""
    if not isinstance(task_data, dict):
        return None

    task_id = task_data.get("task_id", task_data.get("id", f"task_{index:03d}"))
    task_desc = task_data.get(
        "task_desc",
        task_data.get("task", task_data.get("description", task_data.get("query", "")))
    )

    if not task_desc:
        return None

    return SceneTask(
        task_id=str(task_id),
        task_desc=task_desc,
        env_name=env_name,
        init_config=task_data.get("init_config", task_data.get("initial_state", {})),
        check_func=task_data.get("check_func", task_data.get("check", "")),
        available_tools=available_tools,
    )


def _extract_tools_from_metadata(metadata) -> list:
    """从元数据中提取可用工具列表"""
    tools = []
    entries = metadata if isinstance(metadata, list) else [metadata]

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ops = entry.get("operations", entry.get("tools", entry.get("functions", [])))
        for op in ops:
            if isinstance(op, dict):
                tools.append({
                    "name": op.get("name", ""),
                    "description": op.get("description", ""),
                    "parameters": op.get("parameters", op.get("params", {})),
                })
            elif isinstance(op, str):
                tools.append({"name": op, "description": "", "parameters": {}})

    return tools


# ─── 格式化工具描述（用于 Prompt 构建） ───────────────────────────────────────

def format_tools_for_prompt(tools: list) -> str:
    """将工具列表格式化为可读的文本，嵌入到 Agent System Prompt 中"""
    if not tools:
        return "(无可用工具信息)"

    lines = []
    for i, tool in enumerate(tools, 1):
        name = tool.get("name", "unknown")
        desc = tool.get("description", "无描述")
        params = tool.get("parameters", {})

        lines.append(f"{i}. **{name}**: {desc}")

        # 格式化参数
        if isinstance(params, dict):
            props = params.get("properties", {})
            required = params.get("required", [])
            if props:
                for pname, pdef in props.items():
                    ptype = pdef.get("type", "any")
                    pdesc = pdef.get("description", "")
                    req_mark = " (必填)" if pname in required else " (可选)"
                    lines.append(f"   - `{pname}` ({ptype}){req_mark}: {pdesc}")

    return "\n".join(lines)


# ─── 整合: Step 0 完整流程 ───────────────────────────────────────────────────

def run_step0(
    config: EnvScalerPipelineConfig,
    scene_files_content: dict = None,
    event_callback: Callable = None,
) -> tuple:
    """
    执行 Step 0: 场景文件管理

    Args:
        config: Pipeline 配置
        scene_files_content: 直接传入的场景文件内容（Web UI 上传模式）
            {"scenario": dict, "metadata": dict}
        event_callback: 事件回调

    Returns:
        (SceneFile, list[SceneTask]): 场景文件对象和解析出的任务列表
    """
    def emit(msg):
        print(f"  {msg}")
        if event_callback:
            event_callback("scene_setup", msg)

    emit("[Step 0] 加载场景文件...")

    # ─── 从不同来源加载 ───
    if scene_files_content:
        # Web UI 上传模式: 直接从内容构建
        emit("  从上传内容加载场景...")
        scene = SceneFile(
            scenario_content=scene_files_content.get("scenario", {}),
            metadata_content=scene_files_content.get("metadata", {}),
        )
        # 提取环境名
        meta = scene.metadata_content
        if isinstance(meta, list) and meta:
            scene.env_name = meta[0].get("env_name", meta[0].get("name", "uploaded"))
        elif isinstance(meta, dict):
            scene.env_name = meta.get("env_name", meta.get("name", "uploaded"))

    elif config.scene_source == "local" and config.scene_dir:
        emit(f"  从本地目录加载: {config.scene_dir}")
        scene = load_scene_from_directory(config.scene_dir)

    elif config.scene_source == "extract" and config.envscaler_data_dir:
        emit(f"  从 EnvScaler 数据目录提取: {config.envscaler_data_dir}")
        scenes = extract_scenes_from_envscaler(
            config.envscaler_data_dir,
            extract_count=config.extract_count,
        )
        if not scenes:
            raise RuntimeError("无法提取场景文件 — 可能所有场景已被使用")
        scene = scenes[0]  # 当前只处理第一个

    else:
        raise ValueError(
            f"未知的场景来源: {config.scene_source}, "
            "请提供 scene_files_content 或配置 scene_dir/envscaler_data_dir"
        )

    # ─── 解析任务 ───
    emit(f"  环境: {scene.env_name}")
    tasks = parse_tasks_from_scene(scene)
    emit(f"  解析到 {len(tasks)} 个任务")

    # 限制任务数量
    if config.max_tasks > 0 and len(tasks) > config.max_tasks:
        tasks = tasks[:config.max_tasks]
        emit(f"  截取前 {config.max_tasks} 个任务")

    # 打印前 3 个任务预览
    for i, t in enumerate(tasks[:3]):
        emit(f"    [{t.task_id}] {t.task_desc[:60]}...")

    if not tasks:
        emit("  ⚠ 未解析到任何任务")

    return scene, tasks
