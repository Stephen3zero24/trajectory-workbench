import { useState, useEffect, useCallback, useRef } from "react";

const API_BASE = "http://localhost:3000/api";

// ─── Style Constants ────────────────────────────────────────────────────────
const C = {
  bg: "#0a0e17", surface: "#111827", surfaceHover: "#1a2332",
  border: "#1e293b", borderActive: "#334155",
  text: "#e2e8f0", textMuted: "#64748b", textDim: "#475569",
  accent: "#22d3ee", accentDim: "rgba(34,211,238,0.12)",
  green: "#34d399", greenDim: "rgba(52,211,153,0.12)",
  amber: "#fbbf24", amberDim: "rgba(251,191,36,0.12)",
  red: "#f87171", redDim: "rgba(248,113,113,0.12)",
  purple: "#a78bfa", purpleDim: "rgba(167,139,250,0.12)",
};

// ─── Utility Components ─────────────────────────────────────────────────────
function Badge({ color, children }) {
  const c = C[color] || color;
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 10px", borderRadius: 99, fontSize: 11, fontWeight: 600, background: C[color + "Dim"] || "rgba(255,255,255,0.06)", color: c, border: `1px solid ${c}22` }}>{children}</span>;
}

function ProgressBar({ value, color = C.accent, height = 6 }) {
  return (
    <div style={{ width: "100%", height, background: C.border, borderRadius: 99, overflow: "hidden" }}>
      <div style={{ width: `${Math.min(100, value * 100)}%`, height: "100%", background: color, borderRadius: 99, transition: "width 0.8s cubic-bezier(0.4,0,0.2,1)" }} />
    </div>
  );
}

function Btn({ onClick, children, variant = "primary", size = "md", disabled, style: sx }) {
  const base = { border: "none", borderRadius: 8, cursor: disabled ? "not-allowed" : "pointer", fontWeight: 600, fontFamily: "'JetBrains Mono', monospace", transition: "all 0.2s", opacity: disabled ? 0.4 : 1, display: "inline-flex", alignItems: "center", gap: 6 };
  const sizes = { sm: { padding: "6px 14px", fontSize: 12 }, md: { padding: "10px 20px", fontSize: 13 }, lg: { padding: "12px 28px", fontSize: 14 } };
  const variants = {
    primary: { background: C.accent, color: C.bg },
    secondary: { background: C.surfaceHover, color: C.text, border: `1px solid ${C.border}` },
    danger: { background: C.redDim, color: C.red, border: `1px solid ${C.red}33` },
    ghost: { background: "transparent", color: C.textMuted },
    success: { background: C.greenDim, color: C.green, border: `1px solid ${C.green}33` },
  };
  return <button onClick={onClick} disabled={disabled} style={{ ...base, ...sizes[size], ...variants[variant], ...sx }}>{children}</button>;
}

function Card({ children, style: sx }) {
  return <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12, padding: 20, ...sx }}>{children}</div>;
}

function Title({ children, sub }) {
  return <div style={{ marginBottom: 16 }}><h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>{children}</h3>{sub && <p style={{ margin: "4px 0 0", fontSize: 12, color: C.textMuted }}>{sub}</p>}</div>;
}

// ─── API 调用 ────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}

// ─── Phase: 任务定义 ────────────────────────────────────────────────────────
function PhaseInput({ onSubmit }) {
  const [taskDesc, setTaskDesc] = useState("");
  const [scene, setScene] = useState("code_exec");
  const [model, setModel] = useState("deepseek-chat");
  const [temp, setTemp] = useState(0.7);
  const [maxSteps, setMaxSteps] = useState(15);
  const [maxIter, setMaxIter] = useState(3);
  const [threshold, setThreshold] = useState(0.80);
  const [health, setHealth] = useState(null);

  // ── EnvScaler 专用状态 ──
  const [envSceneUploadId, setEnvSceneUploadId] = useState(null);
  const [envSceneInfo, setEnvSceneInfo] = useState(null);
  const [envUploading, setEnvUploading] = useState(false);
  const [envMaxTasks, setEnvMaxTasks] = useState(0);

  const scenes = [
    { id: "envscaler", name: "EnvScaler工具调用", icon: "🏗️" },
    { id: "mcp_tool", name: "MCP工具交互", icon: "⚙️" },
    { id: "gui", name: "GUI操作", icon: "🖥️" },
    { id: "deep_search", name: "Deep Search", icon: "🔍" },
    { id: "multi_agent", name: "多Agent协调", icon: "🤖" },
    { id: "code_exec", name: "代码执行", icon: "💻" },
  ];

  const isEnvScaler = scene === "envscaler";

  const samples = isEnvScaler ? [] : [
    "在Linux沙箱中创建一个Python计算器项目，包含四则运算功能，编写单元测试并运行通过",
    "使用Python爬取一个网页的标题和链接，保存为JSON文件",
    "编写一个Shell脚本，统计当前系统的CPU、内存、磁盘使用情况并生成报告",
    "创建一个简单的Flask Web应用，包含首页和API端点，并测试功能",
  ];

  // ── EnvScaler: 场景文件上传 ──
  const handleEnvFileUpload = async () => {
    const scenarioInput = document.getElementById("envscaler-scenario");
    const metadataInput = document.getElementById("envscaler-metadata");
    if (!scenarioInput?.files[0] || !metadataInput?.files[0]) return;
    setEnvUploading(true);
    try {
      const readJSON = (file) => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => { try { resolve(JSON.parse(reader.result)); } catch(e) { reject(e); } };
        reader.onerror = reject;
        reader.readAsText(file);
      });
      const scenario = await readJSON(scenarioInput.files[0]);
      const metadata = await readJSON(metadataInput.files[0]);
      const res = await api("/envscaler/upload-scene-json", { method: "POST", body: { scenario, metadata } });
      setEnvSceneUploadId(res.upload_id);
      setEnvSceneInfo(res);
    } catch (e) {
      alert("场景文件解析失败: " + e.message);
    } finally {
      setEnvUploading(false);
    }
  };

  // ── EnvScaler: 提交处理 ──
  const handleEnvScalerSubmit = async () => {
    if (!envSceneUploadId) { alert("请先上传场景文件"); return; }
    const res = await api("/envscaler/tasks", { method: "POST", body: {
      scene_source: "upload",
      scene_upload_id: envSceneUploadId,
      model, temperature: temp, max_steps: maxSteps, max_tasks: envMaxTasks,
      max_iterations: maxIter, quality_threshold: threshold,
    }});
    // 使用 onSubmit 的回调机制, 传入特殊标记
    onSubmit({ _envscaler: true, task_id: res.task_id });
  };

  useEffect(() => {
    api("/health").then(setHealth).catch(() => setHealth({ status: "error" }));
  }, []);

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      <div style={{ textAlign: "center", marginBottom: 32 }}>
        <div style={{ fontSize: 40, marginBottom: 8 }}>🎯</div>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: C.text }}>定义合成任务</h2>
        <p style={{ margin: "6px 0 0", color: C.textMuted, fontSize: 13 }}>描述任务需求，系统将在OpenSandbox中执行并采集轨迹</p>
        {health && (
          <div style={{ display: "flex", gap: 12, justifyContent: "center", marginTop: 12 }}>
            <Badge color={health.opensandbox === "connected" ? "green" : "red"}>
              OpenSandbox: {health.opensandbox || "checking"}
            </Badge>
            <Badge color={health.deepseek === "configured" ? "green" : "red"}>
              DeepSeek: {health.deepseek || "checking"}
            </Badge>
          </div>
        )}
      </div>

      {!isEnvScaler && (
      <Card style={{ marginBottom: 16 }}>
        <Title sub="描述Agent需要完成的任务场景">任务描述</Title>
        <textarea value={taskDesc} onChange={e => setTaskDesc(e.target.value)}
          placeholder="例如：创建一个Python项目，实现某个功能，编写测试并运行..."
          style={{ width: "100%", minHeight: 100, padding: 14, borderRadius: 8, background: C.bg, border: `1px solid ${C.border}`, color: C.text, fontSize: 13, fontFamily: "'JetBrains Mono', monospace", resize: "vertical", outline: "none", boxSizing: "border-box", lineHeight: 1.6 }} />
        <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
          {samples.map((t, i) => (
            <button key={i} onClick={() => setTaskDesc(t)} style={{ padding: "5px 12px", borderRadius: 6, fontSize: 11, cursor: "pointer", background: C.accentDim, color: C.accent, border: `1px solid ${C.accent}22`, fontFamily: "'JetBrains Mono', monospace" }}>示例{i + 1}</button>
          ))}
        </div>
      </Card>
      )}

      {isEnvScaler && (
      <Card style={{ marginBottom: 16, borderColor: envSceneUploadId ? C.green + "44" : C.amber + "44" }}>
        <Title sub="上传 EnvScaler 产出的场景文件（env_scenario.json + filtered_env_metadata.json）">🏗️ EnvScaler 场景文件</Title>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
          <div>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>env_scenario.json</label>
            <input type="file" id="envscaler-scenario" accept=".json"
              style={{ width: "100%", padding: 8, borderRadius: 6, background: C.bg, border: `1px solid ${C.border}`, color: C.text, fontSize: 11 }} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>filtered_env_metadata.json</label>
            <input type="file" id="envscaler-metadata" accept=".json"
              style={{ width: "100%", padding: 8, borderRadius: 6, background: C.bg, border: `1px solid ${C.border}`, color: C.text, fontSize: 11 }} />
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Btn size="sm" variant="secondary" onClick={handleEnvFileUpload} disabled={envUploading}>
            {envUploading ? "上传中..." : "📤 上传场景文件"}
          </Btn>
          {envSceneUploadId && (
            <Badge color="green">✅ 已上传 · 环境: {envSceneInfo?.env_name || "?"} · {envSceneInfo?.task_count || "?"} 个任务</Badge>
          )}
        </div>
        {envSceneUploadId && (
          <div style={{ marginTop: 12 }}>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>处理任务数量 (0=全部): {envMaxTasks}</label>
            <input type="range" min={0} max={50} step={1} value={envMaxTasks} onChange={e => setEnvMaxTasks(+e.target.value)} style={{ width: "100%", accentColor: C.accent }} />
          </div>
        )}
      </Card>
      )}

      <Card style={{ marginBottom: 16 }}>
        <Title sub="选择场景类型">场景匹配</Title>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
          {scenes.map(s => (
            <div key={s.id} onClick={() => setScene(s.id)} style={{ padding: 12, borderRadius: 8, cursor: "pointer", textAlign: "center", background: scene === s.id ? C.accentDim : C.bg, border: `1px solid ${scene === s.id ? C.accent + "55" : C.border}`, transition: "all 0.2s" }}>
              <div style={{ fontSize: 20 }}>{s.icon}</div>
              <div style={{ fontSize: 11, fontWeight: 600, color: C.text, marginTop: 4 }}>{s.name}</div>
            </div>
          ))}
        </div>
      </Card>

      <Card style={{ marginBottom: 24 }}>
        <Title sub="Agent模型、推理参数、迭代策略">Pipeline 配置</Title>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>Agent 模型</label>
            <select value={model} onChange={e => setModel(e.target.value)} style={{ width: "100%", padding: "8px 10px", borderRadius: 6, background: C.bg, border: `1px solid ${C.border}`, color: C.text, fontSize: 12, fontFamily: "'JetBrains Mono', monospace" }}>
              <option value="deepseek-chat">DeepSeek-Chat (V3.2)</option>
              <option value="deepseek-reasoner">DeepSeek-Reasoner (R1)</option>
            </select>
          </div>
          <div>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>Temperature: {temp}</label>
            <input type="range" min={0} max={1} step={0.05} value={temp} onChange={e => setTemp(+e.target.value)} style={{ width: "100%", accentColor: C.accent }} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>最大步数: {maxSteps}</label>
            <input type="range" min={5} max={30} step={1} value={maxSteps} onChange={e => setMaxSteps(+e.target.value)} style={{ width: "100%", accentColor: C.accent }} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>最大迭代轮次: {maxIter}</label>
            <input type="range" min={1} max={5} step={1} value={maxIter} onChange={e => setMaxIter(+e.target.value)} style={{ width: "100%", accentColor: C.accent }} />
          </div>
          <div style={{ gridColumn: "1 / -1" }}>
            <label style={{ fontSize: 11, color: C.textMuted, display: "block", marginBottom: 6 }}>质量达标阈值: {threshold}</label>
            <input type="range" min={0.5} max={1} step={0.05} value={threshold} onChange={e => setThreshold(+e.target.value)} style={{ width: "100%", accentColor: C.accent }} />
          </div>
        </div>
      </Card>

      <div style={{ textAlign: "center" }}>
        {isEnvScaler ? (
          <Btn size="lg" disabled={!envSceneUploadId} onClick={handleEnvScalerSubmit}>
            🏗️ 启动 EnvScaler Pipeline
          </Btn>
        ) : (
          <Btn size="lg" disabled={!taskDesc} onClick={() => onSubmit({ task_desc: taskDesc, scene_type: scene, model, temperature: temp, max_steps: maxSteps, max_iterations: maxIter, quality_threshold: threshold })}>
            ▶ 启动Pipeline
          </Btn>
        )}
      </div>
    </div>
  );
}

// ─── Phase: 执行监控 ─────────────────────────────────────────────────────────
function PhaseMonitor({ taskId, isEnvScaler, onReview, onComplete }) {
  const [task, setTask] = useState(null);
  const [events, setEvents] = useState([]);
  const evtIdx = useRef(0);
  const logRef = useRef(null);

  // EnvScaler uses dedicated API paths
  const taskApiPath = isEnvScaler ? `/envscaler/tasks/${taskId}` : `/tasks/${taskId}`;
  const eventsApiPath = isEnvScaler ? `/envscaler/tasks/${taskId}/events` : `/tasks/${taskId}/events`;

  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        const t = await api(taskApiPath);
        setTask(t);

        const e = await api(`${eventsApiPath}?since=${evtIdx.current}`);
        if (e.events && e.events.length > 0) {
          setEvents(prev => [...prev, ...e.events]);
          evtIdx.current = e.total;
        }

        // EnvScaler tasks have different status flow
        if (isEnvScaler) {
          if (t.status === "completed" || t.status === "failed") {
            onComplete({ ...t, iterations: t.summary ? [{ review: t.summary.review || {} }] : [] });
          }
        } else {
          if (t.status === "waiting_approval") {
            onReview(t);
          } else if (t.status === "completed" || t.status === "failed") {
            onComplete(t);
          }
        }
      } catch (err) { /* ignore */ }
    }, 2000);
    return () => clearInterval(poll);
  }, [taskId]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [events]);

  const statusLabel = { created: "初始化", executing: "沙箱执行中", running: "Pipeline执行中", reviewing: "评估中", waiting_approval: "等待审批", completed: "已完成", failed: "失败" };
  const statusColor = { executing: "accent", running: "accent", reviewing: "purple", waiting_approval: "amber", completed: "green", failed: "red" };

  return (
    <div style={{ maxWidth: 800, margin: "0 auto" }}>
      <div style={{ textAlign: "center", marginBottom: 24 }}>
        <div style={{ fontSize: 40, marginBottom: 8 }}>⚡</div>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: C.text }}>Pipeline 执行中</h2>
        <p style={{ margin: "6px 0 0", color: C.textMuted, fontSize: 13 }}>
          任务 {taskId} · {task ? statusLabel[task.status] || task.status : "加载中..."}
        </p>
        {task && <div style={{ marginTop: 8 }}><Badge color={statusColor[task.status] || "accent"}>{statusLabel[task.status] || task.status}</Badge></div>}
      </div>

      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <Title>实时日志</Title>
          {task && (isEnvScaler
            ? <Badge color="purple">Step: {task.current_step || "init"}</Badge>
            : <Badge color="purple">迭代 {(task.current_iteration || 0) + 1} / {task.max_iterations}</Badge>
          )}
        </div>
        <div ref={logRef} style={{ background: C.bg, borderRadius: 8, padding: 16, fontFamily: "'JetBrains Mono', monospace", fontSize: 12, maxHeight: 450, overflowY: "auto", border: `1px solid ${C.border}` }}>
          {events.map((e, i) => {
            const colors = { sandbox_create: C.accent, sandbox_ready: C.accent, agent_start: C.green, agent_step: C.textMuted, agent_action: C.green, agent_complete: C.green, review_start: C.purple, review_complete: C.purple, auto_fix: C.green, waiting_approval: C.amber, error: C.red, pipeline_complete: C.green, next_iteration: C.accent, human_confirm: C.amber, human_approve: C.red, human_reject: C.textMuted, export: C.green, manual_iterate: C.accent, /* EnvScaler events */ scene_setup: C.accent, upload: C.accent, install: C.textMuted, install_warn: C.amber, mcp_start: C.accent, mcp_ready: C.green, mcp_verify: C.green, mcp_warn: C.amber, mcp_error: C.red, step0_start: C.accent, step0_done: C.green, step1_start: C.accent, step1_done: C.green, step2_start: C.accent, step2_done: C.green, trajectory_gen: C.green, trajectory_progress: C.textMuted, trajectory_done: C.green, review_done: C.purple, export_start: C.purple, export_done: C.green, pipeline_start: C.accent, pipeline_done: C.green, pipeline_error: C.red, pipeline_warn: C.amber, pipeline_log: C.textMuted, created: C.accent };
            return (
              <div key={i} style={{ marginBottom: 4, display: "flex", gap: 10, lineHeight: 1.5 }}>
                <span style={{ color: C.textDim, flexShrink: 0, fontSize: 11 }}>{e.timestamp?.split("T")[1]?.substring(0, 8) || ""}</span>
                <span style={{ color: colors[e.type] || C.text }}>{e.message}</span>
              </div>
            );
          })}
          {task && !["completed", "failed", "waiting_approval"].includes(task.status) && (
            <span style={{ color: C.accent, animation: "blink 1s infinite" }}>▍</span>
          )}
        </div>
      </Card>
      <style>{`@keyframes blink { 0%,50% { opacity: 1 } 51%,100% { opacity: 0 } }`}</style>
    </div>
  );
}

// ─── Phase: 审批评估 ─────────────────────────────────────────────────────────
function PhaseReview({ task, taskId, isEnvScaler, onIterate, onExport, onTaskUpdate }) {
  const [selectedOptions, setSelectedOptions] = useState({});
  const [expandedStep, setExpandedStep] = useState(null);
  const [approving, setApproving] = useState(false);

  if (!task || !task.iterations || task.iterations.length === 0) {
    // Show error state for failed tasks
    if (task && task.status === "failed") {
      return (
        <div style={{ maxWidth: 720, margin: "0 auto", textAlign: "center" }}>
          <div style={{ fontSize: 56, marginBottom: 12 }}>❌</div>
          <h2 style={{ margin: 0, fontSize: 24, fontWeight: 800, color: C.red }}>Pipeline 执行失败</h2>
          <p style={{ color: C.textMuted, fontSize: 13, margin: "12px 0 24px" }}>{task.error || "未知错误"}</p>
          <Btn variant="secondary" onClick={onExport}>查看已收集的数据</Btn>
        </div>
      );
    }
    return null;
  }

  const latestIt = task.iterations[task.iterations.length - 1];
  const review = latestIt.review || {};
  const steps = latestIt.trajectory?.steps || [];
  const score = review.overall_score || 0;
  const dims = review.dimensions || {};
  const qColor = score >= 0.8 ? C.green : score >= 0.5 ? C.amber : C.red;
  const passThreshold = score >= (task.quality_threshold || task.config?.quality_threshold || 0.8);

  // EnvScaler summary stats
  const envSummary = isEnvScaler ? (task.summary || {}) : null;

  const pending = task.pending_suggestions || [];
  const confirmItems = pending.filter(s => s.level === "confirm");
  const approveItems = pending.filter(s => s.level === "approve");

  const handleApprove = async (idx, approved, selectedOption = null) => {
    setApproving(true);
    try {
      const res = await api(`/tasks/${taskId}/approve`, {
        method: "POST",
        body: { suggestion_index: idx, approved, selected_option: selectedOption },
      });

      if (res.remaining === 0) {
        // 所有审批完成，后端已自动触发下一轮，回到监控页
        onIterate();
      } else {
        // 还有待处理的建议，重新获取任务数据刷新当前页面
        const updated = await api(`/tasks/${taskId}`);
        onTaskUpdate(updated);
      }
    } catch (err) {
      console.error("审批失败:", err);
    } finally {
      setApproving(false);
    }
  };

  return (
    <div style={{ maxWidth: 880, margin: "0 auto" }}>
      <div style={{ textAlign: "center", marginBottom: 24 }}>
        <div style={{ fontSize: 40, marginBottom: 8 }}>🔍</div>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: C.text }}>Review Agent 评估报告</h2>
        <p style={{ margin: "6px 0 0", color: C.textMuted, fontSize: 13 }}>第 {task.iterations.length} 轮迭代</p>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <Card>
          <Title sub="综合评分">轨迹质量</Title>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 16 }}>
            <span style={{ fontSize: 48, fontWeight: 800, color: qColor, fontFamily: "'JetBrains Mono', monospace" }}>{(score * 100).toFixed(0)}</span>
            <span style={{ fontSize: 16, color: C.textMuted }}>/100</span>
            {passThreshold && <Badge color="green">✓ 达标</Badge>}
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            {Object.entries(dims).map(([k, v]) => {
              const labels = { tool_usage: "工具调用", reasoning: "推理质量", error_handling: "错误处理", completeness: "任务完成度", tool_selection: "工具选择", tool_execution: "工具执行", efficiency: "执行效率", question_quality: "问题质量", answer_accuracy: "回答准确", trace_quality: "轨迹质量", evolution_effectiveness: "演化效果", cross_source_reasoning: "跨源推理", multi_tool: "多工具协同" };
              const vc = v >= 0.8 ? C.green : v >= 0.5 ? C.amber : C.red;
              return (
                <div key={k}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                    <span style={{ fontSize: 12, color: C.textMuted }}>{labels[k] || k}</span>
                    <span style={{ fontSize: 12, color: C.text, fontWeight: 600 }}>{(v * 100).toFixed(0)}%</span>
                  </div>
                  <ProgressBar value={v} color={vc} height={4} />
                </div>
              );
            })}
          </div>
        </Card>

        <Card>
          {isEnvScaler && envSummary ? (
            <>
              <Title sub="EnvScaler 执行摘要">🏗️ Pipeline 结果</Title>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
                {[
                  { label: "环境", value: envSummary.env_name || "—" },
                  { label: "轨迹数", value: envSummary.trajectories_count || 0 },
                  { label: "任务完成", value: `${envSummary.tasks_completed || 0}/${envSummary.tasks_count || 0}` },
                  { label: "工具调用", value: envSummary.total_tool_calls || 0 },
                  { label: "成功调用", value: envSummary.successful_tool_calls || 0 },
                  { label: "Token消耗", value: (envSummary.total_tokens || 0).toLocaleString() },
                ].map((s, i) => (
                  <div key={i} style={{ padding: 10, borderRadius: 6, background: C.bg, border: `1px solid ${C.border}`, textAlign: "center" }}>
                    <div style={{ fontSize: 18, fontWeight: 800, color: C.text }}>{s.value}</div>
                    <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>{s.label}</div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <>
              <Title sub="Agent执行过程">轨迹预览 ({steps.length} 步)</Title>
              <div style={{ maxHeight: 240, overflowY: "auto" }}>
                {steps.map((step, i) => (
                  <div key={i} onClick={() => setExpandedStep(expandedStep === i ? null : i)} style={{ padding: 10, borderRadius: 6, marginBottom: 4, cursor: "pointer", background: expandedStep === i ? C.bg : "transparent", border: `1px solid ${expandedStep === i ? C.border : "transparent"}` }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <span style={{ width: 22, height: 22, borderRadius: 99, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 700, flexShrink: 0, background: C.accentDim, color: C.accent }}>{step.step_id}</span>
                      <span style={{ fontSize: 11, color: C.text, fontFamily: "'JetBrains Mono', monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{step.action}</span>
                    </div>
                    {expandedStep === i && (
                      <div style={{ marginTop: 8, paddingLeft: 30, fontSize: 11, lineHeight: 1.8 }}>
                        <div><span style={{ color: C.purple }}>思考:</span> <span style={{ color: C.textMuted }}>{step.thought}</span></div>
                        <div><span style={{ color: C.green }}>结果:</span> <span style={{ color: C.textMuted }}>{step.observation}</span></div>
                      </div>
                    )}
                  </div>
                ))}
                {steps.length === 0 && <div style={{ color: C.textDim, fontSize: 12, textAlign: "center", padding: 20 }}>无轨迹步骤数据</div>}
              </div>
            </>
          )}
        </Card>
      </div>

      {review.fail_modes && review.fail_modes.length > 0 && (
        <Card style={{ marginBottom: 16, borderColor: C.red + "33" }}>
          <Title sub="主要问题">失败模式</Title>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {review.fail_modes.map((f, i) => <Badge key={i} color="red">⚠ {f}</Badge>)}
          </div>
        </Card>
      )}

      {review.reasoning && (
        <Card style={{ marginBottom: 16 }}>
          <Title>评估推理</Title>
          <p style={{ fontSize: 12, color: C.textMuted, lineHeight: 1.7, margin: 0 }}>{review.reasoning}</p>
        </Card>
      )}

      {confirmItems.length > 0 && (
        <Card style={{ marginBottom: 16, borderColor: C.amber + "33" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <Title sub="请从方案中选择">🟡 人工确认区</Title>
            <Badge color="amber">需要您选择</Badge>
          </div>
          {confirmItems.map((s, ci) => {
            const globalIdx = pending.indexOf(s);
            return (
              <div key={ci} style={{ padding: 14, borderRadius: 8, background: C.bg, marginBottom: 8, border: `1px solid ${C.border}` }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{s.category}</div>
                <p style={{ margin: "4px 0 10px", fontSize: 12, color: C.textMuted }}>{s.desc}</p>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {(s.options || []).map((opt, j) => (
                    <label key={j} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 12px", borderRadius: 6, cursor: "pointer", background: selectedOptions[ci] === j ? C.amberDim : "transparent", border: `1px solid ${selectedOptions[ci] === j ? C.amber + "44" : C.border}` }}>
                      <input type="radio" name={`c_${ci}`} checked={selectedOptions[ci] === j} onChange={() => setSelectedOptions(p => ({ ...p, [ci]: j }))} style={{ accentColor: C.amber }} />
                      <span style={{ fontSize: 12, color: C.text }}>{opt}</span>
                    </label>
                  ))}
                </div>
                <div style={{ marginTop: 10 }}>
                  <Btn size="sm" disabled={selectedOptions[ci] === undefined || approving} onClick={() => handleApprove(globalIdx, true, selectedOptions[ci])}>
                    {approving ? "处理中..." : "确认选择"}
                  </Btn>
                </div>
              </div>
            );
          })}
        </Card>
      )}

      {approveItems.length > 0 && (
        <Card style={{ marginBottom: 16, borderColor: C.red + "33" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <Title sub="高风险变更">🔴 人工审批区</Title>
            <Badge color="red">需要审批</Badge>
          </div>
          {approveItems.map((s, ai) => {
            const globalIdx = pending.indexOf(s);
            return (
              <div key={ai} style={{ padding: 14, borderRadius: 8, background: C.bg, marginBottom: 8, border: `1px solid ${C.border}` }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{s.category}</div>
                <p style={{ margin: "4px 0 8px", fontSize: 12, color: C.textMuted }}>{s.desc}</p>
                {s.impact && (
                  <div style={{ padding: 10, borderRadius: 6, background: C.redDim, fontSize: 12, color: C.red, border: `1px solid ${C.red}22`, lineHeight: 1.6, marginBottom: 10 }}>
                    ⚠ 影响评估：{s.impact}
                  </div>
                )}
                <div style={{ display: "flex", gap: 8 }}>
                  <Btn variant="danger" size="sm" disabled={approving} onClick={() => handleApprove(globalIdx, true)}>✓ 批准</Btn>
                  <Btn variant="ghost" size="sm" disabled={approving} onClick={() => handleApprove(globalIdx, false)}>✗ 拒绝</Btn>
                </div>
              </div>
            );
          })}
        </Card>
      )}

      <div style={{ display: "flex", justifyContent: "center", gap: 12, marginTop: 24 }}>
        {isEnvScaler ? (
          <Btn size="lg" variant="success" onClick={onExport}>✓ 导出 EnvScaler 数据集</Btn>
        ) : passThreshold ? (
          <Btn size="lg" variant="success" onClick={onExport}>✓ 质量达标 · 导出数据集</Btn>
        ) : (
          <>
            {pending.length === 0 && (
              <Btn size="lg" onClick={onIterate}>▶ 触发下一轮迭代</Btn>
            )}
            <Btn size="md" variant="secondary" onClick={onExport}>跳过迭代 · 直接导出</Btn>
          </>
        )}
      </div>
    </div>
  );
}

// ─── Phase: 导出 ─────────────────────────────────────────────────────────────
function PhaseExport({ task, taskId, isEnvScaler }) {
  const [exportResult, setExportResult] = useState(null);

  useEffect(() => {
    const exportPath = isEnvScaler ? `/envscaler/tasks/${taskId}/export` : `/tasks/${taskId}/export`;
    api(exportPath, { method: "POST" }).then(setExportResult).catch(() => {});
  }, [taskId, isEnvScaler]);

  const iterations = task?.iterations || [];
  const scores = iterations.map(it => it.review?.overall_score || 0);
  const envSummary = isEnvScaler ? (task?.summary || {}) : null;

  // EnvScaler export info
  const envExportFiles = exportResult?.export || {};
  const envScore = envSummary?.avg_quality || (scores.length ? Math.max(...scores) : 0);

  return (
    <div style={{ maxWidth: 720, margin: "0 auto", textAlign: "center" }}>
      <div style={{ fontSize: 56, marginBottom: 12 }}>✅</div>
      <h2 style={{ margin: 0, fontSize: 24, fontWeight: 800, color: C.green }}>数据集导出完成</h2>
      <p style={{ color: C.textMuted, fontSize: 13, margin: "8px 0 28px" }}>
        {isEnvScaler
          ? `${envSummary?.trajectories_count || 0} 条轨迹 · 平均质量: ${envScore ? (envScore * 100).toFixed(0) : "--"}/100`
          : `经过 ${iterations.length} 轮迭代 · 最佳质量: ${exportResult?.best_score ? (exportResult.best_score * 100).toFixed(0) : "--"}/100`
        }
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 28 }}>
        {isEnvScaler ? (
          [
            { fmt: "SFT 格式", icon: "📋", file: envExportFiles.sft_path },
            { fmt: "DPO 格式", icon: "⚖️", file: envExportFiles.dpo_path },
            { fmt: "Raw 格式", icon: "📦", file: envExportFiles.raw_path },
          ].map((item, i) => (
            <Card key={i} style={{ textAlign: "center", padding: 20 }}>
              <div style={{ fontSize: 28, marginBottom: 8 }}>{item.icon}</div>
              <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{item.fmt}</div>
              <div style={{ fontSize: 11, color: item.file ? C.green : C.textDim, marginTop: 4 }}>
                {item.file ? item.file.split("/").pop() : "(未生成)"}
              </div>
            </Card>
          ))
        ) : (
          ["SFT 格式", "DPO 格式", "RLHF 格式"].map((fmt, i) => (
            <Card key={i} style={{ textAlign: "center", padding: 20 }}>
              <div style={{ fontSize: 28, marginBottom: 8 }}>{["📋", "⚖️", "🎯"][i]}</div>
              <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{fmt}</div>
              <div style={{ fontSize: 11, color: C.textMuted, marginTop: 4 }}>{exportResult?.file || exportResult?.output_dir || "..."}</div>
            </Card>
          ))
        )}
      </div>

      {isEnvScaler && envSummary ? (
        <Card style={{ textAlign: "left" }}>
          <Title sub="Pipeline 执行统计">EnvScaler 摘要</Title>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, fontSize: 12 }}>
            {[
              ["环境名称", envSummary.env_name || "—"],
              ["任务总数", envSummary.tasks_count || 0],
              ["轨迹条数", envSummary.trajectories_count || 0],
              ["任务完成", `${envSummary.tasks_completed || 0} / ${envSummary.tasks_count || 0}`],
              ["工具调用", `${envSummary.successful_tool_calls || 0} / ${envSummary.total_tool_calls || 0} 成功`],
              ["Token 消耗", (envSummary.total_tokens || 0).toLocaleString()],
              ["平均质量", `${((envSummary.avg_quality || 0) * 100).toFixed(1)}%`],
              ["平均 Reward", (envSummary.avg_reward || 0).toFixed(3)],
              ["耗时", `${envSummary.elapsed_seconds || 0}s`],
              ["输出目录", envSummary.output_dir || "—"],
            ].map(([label, value], i) => (
              <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "6px 10px", borderRadius: 4, background: i % 2 === 0 ? C.bg : "transparent" }}>
                <span style={{ color: C.textMuted }}>{label}</span>
                <span style={{ color: C.text, fontWeight: 600 }}>{value}</span>
              </div>
            ))}
          </div>
        </Card>
      ) : (
        <Card style={{ textAlign: "left" }}>
          <Title sub="各轮次质量变化">迭代历史</Title>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 120, padding: "0 8px" }}>
            {scores.map((s, i) => {
              const h = Math.max(s * 100, 5);
              const c = s >= 0.8 ? C.green : s >= 0.5 ? C.amber : C.red;
              return (
                <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
                  <span style={{ fontSize: 10, color: c, fontWeight: 700 }}>{(s * 100).toFixed(0)}</span>
                  <div style={{ width: "100%", height: `${h}%`, background: c, borderRadius: "4px 4px 0 0", minHeight: 8 }} />
                  <span style={{ fontSize: 10, color: C.textDim }}>R{i + 1}</span>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

// ─── 主应用 ──────────────────────────────────────────────────────────────────
export default function App() {
  const [stage, setStage] = useState("input");
  const [taskId, setTaskId] = useState(null);
  const [taskData, setTaskData] = useState(null);
  const [isEnvScaler, setIsEnvScaler] = useState(false); // track if current task is envscaler

  const handleSubmit = async (config) => {
    // EnvScaler 场景: 任务已由 PhaseInput 直接创建
    if (config._envscaler) {
      setTaskId(config.task_id);
      setIsEnvScaler(true);
      setStage("monitor");
      return;
    }
    // 通用场景
    const res = await api("/tasks", { method: "POST", body: config });
    setTaskId(res.task_id);
    setIsEnvScaler(false);
    setStage("monitor");
  };

  const handleReview = (task) => {
    setTaskData(task);
    if (task.status === "waiting_approval") setStage("review");
  };

  const handleComplete = (task) => {
    setTaskData(task);
    if (task.status === "completed" || task.status === "failed") setStage("review");
  };

  // 审批完成后不调 /iterate API（后端已自动触发），只切换到监控页
  const handleAfterApproval = () => {
    setStage("monitor");
  };

  // 手动触发下一轮迭代（用于"触发下一轮迭代"按钮）
  const handleManualIterate = async () => {
    await api(`/tasks/${taskId}/iterate`, { method: "POST" });
    setStage("monitor");
  };

  // 审批页面中更新任务数据（处理部分审批后刷新）
  const handleTaskUpdate = (updatedTask) => {
    setTaskData(updatedTask);
  };

  const handleExport = () => setStage("export");

  const handleReset = () => {
    setStage("input");
    setTaskId(null);
    setTaskData(null);
    setIsEnvScaler(false);
  };

  const navItems = [
    { key: "input", label: "任务定义", done: stage !== "input" },
    { key: "monitor", label: "沙箱执行", done: ["review", "export"].includes(stage) },
    { key: "review", label: "评估审批", done: stage === "export" },
    { key: "export", label: "导出数据", done: false },
  ];

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, fontFamily: "'JetBrains Mono', 'Fira Code', monospace" }}>
      <div style={{ padding: "16px 28px", borderBottom: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "space-between", background: "linear-gradient(180deg, #0f1520 0%, #0a0e17 100%)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 22 }}>📦</span>
          <div style={{ fontSize: 15, fontWeight: 800 }}>
            <span style={{ color: C.accent }}>Open</span><span style={{ color: C.text }}>Sandbox</span>
            <span style={{ color: C.textDim, fontWeight: 400, marginLeft: 8, fontSize: 12 }}>轨迹合成工作台</span>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {taskId && <Badge color="purple">Task: {taskId}</Badge>}
          <Btn variant="ghost" size="sm" onClick={handleReset}>↺ 重置</Btn>
        </div>
      </div>

      <div style={{ padding: "12px 28px", borderBottom: `1px solid ${C.border}`, display: "flex", background: C.surface }}>
        {navItems.map((n, i) => (
          <div key={n.key} style={{ display: "flex", alignItems: "center", flex: 1 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", borderRadius: 6, background: n.key === stage ? C.accentDim : "transparent" }}>
              <span style={{ width: 22, height: 22, borderRadius: 99, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 700, background: n.done ? C.green : n.key === stage ? C.accent : C.border, color: n.done || n.key === stage ? C.bg : C.textDim }}>{n.done ? "✓" : i + 1}</span>
              <span style={{ fontSize: 12, color: n.key === stage ? C.accent : C.textMuted, fontWeight: n.key === stage ? 700 : 400 }}>{n.label}</span>
            </div>
            {i < navItems.length - 1 && <div style={{ flex: 1, height: 1, background: C.border, margin: "0 8px" }} />}
          </div>
        ))}
      </div>

      <div style={{ padding: "32px 28px", maxWidth: 960, margin: "0 auto" }}>
        {stage === "input" && <PhaseInput onSubmit={handleSubmit} />}
        {stage === "monitor" && taskId && <PhaseMonitor taskId={taskId} isEnvScaler={isEnvScaler} onReview={handleReview} onComplete={handleComplete} />}
        {stage === "review" && taskData && (
          <PhaseReview
            task={taskData}
            taskId={taskId}
            isEnvScaler={isEnvScaler}
            onIterate={handleAfterApproval}
            onExport={handleExport}
            onTaskUpdate={handleTaskUpdate}
          />
        )}
        {stage === "export" && <PhaseExport task={taskData} taskId={taskId} isEnvScaler={isEnvScaler} />}
      </div>

      <style>{`
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: ${C.bg}; }
        ::-webkit-scrollbar-thumb { background: ${C.border}; border-radius: 99px; }
        select option { background: ${C.bg}; color: ${C.text}; }
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700;800&display=swap');
      `}</style>
    </div>
  );
}
