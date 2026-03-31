"""
ToolACE 提示词模板

Step 1: 工具自进化合成 (TSS)
Step 2: 任务生成 (SDG)
Step 3: 轨迹生成
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Step 1: 工具自进化合成 (Tool Self-Evolution Synthesis)
# ═══════════════════════════════════════════════════════════════════════════════

# Step 1.1 — 工具完善：补全描述、参数约束、返回值定义
TOOL_REFINEMENT_PROMPT = """你是一个 API 设计专家。请完善以下工具定义，使其更加规范和完整。

## 输入工具
{tool_json}

## 完善要求
1. 补充缺失的参数描述和类型约束
2. 为可选参数添加合理的默认值
3. 完善返回值的结构定义（包含具体字段）
4. 添加 error_responses 定义（至少 2 种错误场景）
5. 确保 description 清晰且包含使用场景
6. 保留 label 字段不变

## 输出格式（严格 JSON）
```json
{{
    "name": "tool_name",
    "label": "分类标签",
    "description": "完整的功能描述，包含典型使用场景",
    "parameters": {{
        "type": "object",
        "properties": {{ ... }},
        "required": [...]
    }},
    "returns": {{
        "type": "object",
        "properties": {{ ... }}
    }},
    "error_responses": [
        {{"code": "ERROR_CODE", "description": "错误描述"}}
    ]
}}
```
只输出 JSON。"""

# Step 1.2 — 工具扩展：从源工具生成同领域的新工具
TOOL_EXPANSION_PROMPT = """你是一个 API 生态系统架构师。根据以下源工具，生成 {count} 个同领域的新工具。

## 源工具
{source_tool_json}

## 生成要求
1. 新工具必须与源工具属于同一领域（使用相同的 label）
2. 新工具之间应该有功能互补性（如：源工具是"查询"，新工具可以是"创建/更新/删除/统计"等）
3. 参数之间可以存在依赖关系（如工具A的输出可作为工具B的输入）
4. 每个工具的 name 必须唯一且有描述性
5. 确保参数类型多样化（string, integer, boolean, array, object 等）
6. 部分工具应包含嵌套参数（nested parameters）

## 输出格式（严格 JSON 数组）
```json
[
    {{
        "name": "new_tool_name",
        "label": "与源工具相同的分类标签",
        "description": "功能描述",
        "parameters": {{ ... }},
        "returns": {{ ... }},
        "error_responses": [...]
    }}
]
```
只输出 JSON 数组。"""

# Step 1.3 — 工具组耦合优化：增强工具之间的关联性
TOOL_COUPLING_PROMPT = """你是一个工具生态系统优化专家。请分析以下工具组，优化工具之间的耦合关系。

## 工具组
{tools_group_json}

## 优化要求
1. 识别工具之间的数据流关系（工具A的输出 → 工具B的输入）
2. 添加 "depends_on" 字段标记依赖关系
3. 统一相关工具之间共享参数的命名和类型
4. 为有关联的工具添加 "related_tools" 字段
5. 确保存在至少 2 条完整的工具调用链
6. 调整参数描述使工具间的配合更直观

## 输出格式（严格 JSON）
```json
{{
    "group_label": "工具组标签",
    "tools": [
        {{
            "name": "...",
            "label": "...",
            "description": "...",
            "parameters": {{ ... }},
            "returns": {{ ... }},
            "error_responses": [...],
            "depends_on": ["other_tool_name"],
            "related_tools": ["related_tool_name"]
        }}
    ],
    "tool_chains": [
        {{
            "name": "链名称",
            "description": "链描述",
            "sequence": ["tool_a", "tool_b", "tool_c"]
        }}
    ]
}}
```
只输出 JSON。"""


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2: 任务生成 (Self-Guided Dialog Generation)
# ═══════════════════════════════════════════════════════════════════════════════

# Step 2.1 — 工具使用流程生成
USAGE_FLOW_PROMPT = """你是一个工具使用流程设计师。根据以下工具组，设计合理的工具使用流程。

## 工具组
{tools_json}

## 工具调用链参考
{tool_chains_json}

## 生成要求
1. 每个流程描述一个具体的使用场景
2. 流程应包含明确的步骤序列和每步使用的工具
3. 标明每步所需的输入参数和数据来源
4. 生成 {count} 个不同复杂度的流程（简单/中等/复杂）

## 输出格式（严格 JSON 数组）
```json
[
    {{
        "flow_id": "flow_001",
        "name": "流程名称",
        "complexity": "simple|medium|complex",
        "description": "场景描述",
        "steps": [
            {{
                "step_id": 1,
                "tool_name": "要使用的工具",
                "purpose": "此步骤目的",
                "input_source": "参数来源说明",
                "output_usage": "输出将用于..."
            }}
        ]
    }}
]
```
只输出 JSON 数组。"""

# Step 2.2 — 任务生成（标准模式）
TASK_GENERATION_PROMPT = """你是一个任务生成专家。根据以下工具和使用流程，生成具体的用户任务。

## 可用工具
{tools_json}

## 使用流程
{flow_json}

{role_background_section}

## 生成要求
1. 任务描述应自然、具体，像真实用户提出的需求
2. 任务应需要使用流程中指定的工具链来完成
3. 包含足够的上下文让 Agent 能正确选择工具和参数
4. 任务描述中自然地包含所需参数值

## 输出格式（严格 JSON）
```json
{{
    "task_id": "task_xxx",
    "description": "用户任务描述",
    "expected_tools": ["tool_1", "tool_2"],
    "expected_flow": "flow_id",
    "complexity": "simple|medium|complex",
    "role_background": "角色背景（如有）",
    "metadata": {{
        "domain": "领域",
        "requires_multi_step": true
    }}
}}
```
只输出 JSON。"""

# Step 2.2b — 跨组交叉任务生成
CROSS_GROUP_TASK_PROMPT = """你是一个跨领域任务设计专家。以下是来自不同工具组但具有关联性的工具，请生成需要跨组协作的任务。

## 工具组 A（{label_a}）
{tools_a_json}

## 工具组 B（{label_b}）
{tools_b_json}

{role_background_section}

## 生成要求
1. 任务必须同时使用两个组的工具
2. 工具组之间的数据应有合理的流转关系
3. 任务场景要自然，不要生硬拼凑
4. 生成 {count} 个跨组任务

## 输出格式（严格 JSON 数组）
```json
[
    {{
        "task_id": "cross_xxx",
        "description": "跨组任务描述",
        "expected_tools": ["group_a_tool", "group_b_tool"],
        "cross_groups": ["{label_a}", "{label_b}"],
        "complexity": "medium|complex",
        "role_background": "角色背景（如有）",
        "data_flow": "数据流转说明"
    }}
]
```
只输出 JSON 数组。"""

# Step 2.2c — 缺参任务生成
MISSING_PARAM_TASK_PROMPT = """你是一个用户行为模拟专家。生成一些参数不完整的用户任务——在真实场景中，用户常常不会一次提供所有必要信息。

## 可用工具
{tools_json}

{role_background_section}

## 生成要求
1. 任务描述中故意省略 1~2 个必要参数
2. 标注哪些参数被省略了
3. Agent 需要模拟 user 来追问补充这些参数
4. 生成 {count} 个缺参任务

## 输出格式（严格 JSON 数组）
```json
[
    {{
        "task_id": "mp_xxx",
        "description": "用户任务描述（参数不完整）",
        "expected_tools": ["tool_1"],
        "missing_params": [
            {{
                "tool_name": "对应工具",
                "param_name": "缺失参数名",
                "param_description": "参数描述",
                "clarification_question": "Agent 应该如何追问"
            }}
        ],
        "complete_description": "补充完整参数后的任务描述",
        "complexity": "simple|medium",
        "role_background": "角色背景（如有）"
    }}
]
```
只输出 JSON 数组。"""

# 角色背景模板
ROLE_BACKGROUNDS = [
    "你是一位中小企业的技术运维工程师，负责日常系统维护和监控。",
    "你是一位数据分析师，每天需要处理多个数据源并生成报表。",
    "你是一位产品经理，需要协调多个团队完成产品迭代。",
    "你是一位客户服务主管，负责处理客户投诉和工单管理。",
    "你是一位市场营销专员，需要管理多渠道营销活动和数据追踪。",
    "你是一位研发团队负责人，管理项目进度和代码仓库。",
    "你是一位财务人员，需要处理报销审批和财务报表。",
    "你是一位内容运营，负责多平台内容发布和效果分析。",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3: 轨迹生成 (Non-Autoregressive Trajectory Generation)
# ═══════════════════════════════════════════════════════════════════════════════

# Step 3.1 — 粗粒度初始化：生成对话骨架
TRAJECTORY_INIT_PROMPT = """你是一个 Agent 对话骨架生成器。根据任务和工具，生成一个完整的对话骨架。

## 任务
{task_json}

## 可用工具
{tools_json}

{missing_param_section}

## 生成要求
1. 生成完整的多轮对话骨架，包含所有角色的发言
2. 角色包括：user（用户）、assistant（AI助手）、tool（工具执行结果）
3. assistant 角色的发言应包含 thought（推理过程）和 action（工具调用或回复）
4. 如果是缺参任务，assistant 应先追问缺失参数，user 补充后再执行
5. 工具调用使用标准 function calling 格式
6. 确保对话逻辑完整：有开头、执行过程、结论

## 输出格式（严格 JSON）
```json
{{
    "trajectory_id": "traj_xxx",
    "task_id": "对应的task_id",
    "turns": [
        {{
            "turn_id": 1,
            "role": "user",
            "content": "用户消息",
            "thought": null,
            "tool_calls": null,
            "tool_results": null
        }},
        {{
            "turn_id": 2,
            "role": "assistant",
            "content": "助手回复或为null（当有tool_calls时）",
            "thought": "推理过程",
            "tool_calls": [
                {{
                    "call_id": "call_001",
                    "tool_name": "工具名",
                    "arguments": {{ "参数": "值" }}
                }}
            ],
            "tool_results": null
        }},
        {{
            "turn_id": 3,
            "role": "tool",
            "content": null,
            "thought": null,
            "tool_calls": null,
            "tool_results": [
                {{
                    "call_id": "call_001",
                    "tool_name": "工具名",
                    "output": "模拟的工具执行结果",
                    "success": true
                }}
            ]
        }},
        {{
            "turn_id": 4,
            "role": "assistant",
            "content": "根据工具结果给用户的回复",
            "thought": "分析工具结果的推理",
            "tool_calls": null,
            "tool_results": null
        }}
    ],
    "total_turns": 4,
    "tools_used": ["工具列表"],
    "is_multi_turn": true
}}
```
只输出 JSON。"""

# Step 3.2 — 迭代精炼：mask-and-fill 操作
TRAJECTORY_REFINE_PROMPT = """你是一个对话质量优化专家。请精炼以下对话轨迹，使其更加真实和复杂。

## 当前轨迹
{trajectory_json}

## 精炼要求
1. 增强 assistant 的推理过程（thought），使其更详细、更像真实的推理链
2. 丰富 user 的表述，使其更自然、更像真实用户
3. 完善 tool_results 的输出，使其更像真实的 API 响应
4. 如果工具调用有错误场景，增加错误处理和重试逻辑
5. 确保前后文逻辑一致性
6. 不要改变对话的基本结构和步骤顺序

## 输出格式
输出与输入相同结构的 JSON，但内容经过精炼。
只输出 JSON。"""

# Step 3.3 — 离线验证
TRAJECTORY_VERIFY_PROMPT = """你是一个对话数据质量检验员。请验证以下对话轨迹的质量。

## 任务
{task_json}

## 可用工具
{tools_json}

## 对话轨迹
{trajectory_json}

## 验证维度（0-1分）
1. tool_selection: 工具选择是否正确
2. param_accuracy: 参数填写是否准确
3. reasoning_quality: 推理过程是否合理
4. completeness: 任务是否被完整解决
5. naturalness: 对话是否自然流畅
6. error_handling: 错误处理是否合理

## 输出格式（严格 JSON）
```json
{{
    "passed": true,
    "overall_score": 0.85,
    "dimensions": {{
        "tool_selection": 0.9,
        "param_accuracy": 0.8,
        "reasoning_quality": 0.85,
        "completeness": 0.9,
        "naturalness": 0.8,
        "error_handling": 0.8
    }},
    "issues": ["发现的问题"],
    "fix_suggestions": ["修复建议"]
}}
```
只输出 JSON。"""
