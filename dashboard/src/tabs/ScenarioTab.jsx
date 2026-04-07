import { useEffect, useRef, useState } from "react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { API_BASE } from "../apiConfig.js"

async function parseJson(res) {
  const t = await res.text()
  if (!t) return {}
  try { return JSON.parse(t) } catch { return {} }
}

// ─── 상수 ────────────────────────────────────────────────

const DEFAULT_PARAMS = {
  fleet_sizes: "5,7,9,12",
  duration_s: 1800,
  job_interval_s: 4,
  nav_speed: 2.0,
  battery_drain_running: 0.18,
  battery_charge_rate: 5.0,
  sla_threshold_s: 300,
}

const FIELD_META = [
  { key: "fleet_sizes",           label: "비교 대수",          hint: "쉼표 구분 예) 5,7,9,12",  type: "text" },
  { key: "duration_s",            label: "시뮬 시간 (s)",       hint: "30 ~ 3600",               type: "number", min: 30,   max: 3600, step: 1    },
  { key: "job_interval_s",        label: "작업 생성 간격 (s)",  hint: "2 ~ 60",                  type: "number", min: 2,    max: 60,   step: 0.5  },
  { key: "nav_speed",             label: "주행 속도 (m/s)",     hint: "0.2 ~ 4.0",               type: "number", min: 0.2,  max: 4.0,  step: 0.1  },
  { key: "battery_drain_running", label: "배터리 소모 (%/s)",   hint: "0.01 ~ 5.0",              type: "number", min: 0.01, max: 5.0,  step: 0.01 },
  { key: "battery_charge_rate",   label: "충전 속도 (%/s)",     hint: "0.5 ~ 20",                type: "number", min: 0.5,  max: 20,   step: 0.5  },
  { key: "sla_threshold_s",       label: "SLA 기준 (s)",        hint: "10 ~ 3600",               type: "number", min: 10,   max: 3600, step: 10   },
]

const RUN_COLORS = ["#5b9cf5", "#4ade80", "#fbbf24", "#f87171", "#a78bfa", "#34d399"]
const TOOLTIP_STYLE = { background: "#12151c", border: "1px solid #2a3142", color: "#e6e9ef", fontSize: 12, borderRadius: 6 }
const GRID_STROKE = "#2a3142"
const AXIS_STROKE = "#8b93a7"

// ─── 진행률 표시 컴포넌트 ─────────────────────────────────

function ProgressPanel({ jobStatus, onCancel }) {
  if (!jobStatus) return null
  const { status, message, progress_pct, current_fleet_size, completed_sizes, total, fleet_sizes } = jobStatus
  const isRunning = status === "running"

  return (
    <div style={{
      background: "#1a1f2e", border: "1px solid #2a3142",
      borderRadius: 10, padding: 16, marginBottom: 16,
    }}>
      {/* 상태 헤더 */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {isRunning && (
            <span style={{
              width: 8, height: 8, borderRadius: "50%", background: "#5b9cf5",
              display: "inline-block", animation: "pulse 1.2s ease-in-out infinite",
            }} />
          )}
          <span style={{ fontSize: 13, color: isRunning ? "#5b9cf5" : status === "done" ? "#4ade80" : "#f87171", fontWeight: 600 }}>
            {status === "running" ? "시뮬레이션 실행 중" : status === "done" ? "완료" : status === "cancelled" ? "취소됨" : "오류"}
          </span>
        </div>
        {isRunning && (
          <button
            type="button"
            onClick={onCancel}
            style={{
              background: "transparent", color: "#f87171",
              border: "1px solid #f87171", borderRadius: 6,
              padding: "4px 12px", fontSize: 12, cursor: "pointer",
            }}
          >
            취소
          </button>
        )}
      </div>

      {/* 메시지 */}
      <div style={{ fontSize: 12, color: "#8b93a7", marginBottom: 10 }}>{message}</div>

      {/* 전체 진행 바 */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#8b93a7", marginBottom: 4 }}>
          <span>전체 진행률</span>
          <span>{progress_pct}%</span>
        </div>
        <div style={{ background: "#2a3142", borderRadius: 4, height: 8, overflow: "hidden" }}>
          <div style={{
            width: `${progress_pct}%`, height: "100%",
            background: status === "done" ? "#4ade80" : "#5b9cf5",
            borderRadius: 4, transition: "width 0.4s ease",
          }} />
        </div>
      </div>

      {/* 대수별 상태 */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {(fleet_sizes || []).map(size => {
          const isDone = (completed_sizes || []).includes(size)
          const isCurrent = isRunning && size === current_fleet_size && !isDone
          return (
            <div key={size} style={{
              display: "flex", alignItems: "center", gap: 6,
              background: isDone ? "#0f1e10" : isCurrent ? "#0d1829" : "#12151c",
              border: `1px solid ${isDone ? "#4ade80" : isCurrent ? "#5b9cf5" : "#2a3142"}`,
              borderRadius: 6, padding: "5px 10px", fontSize: 12,
            }}>
              {isDone
                ? <span style={{ color: "#4ade80" }}>✓</span>
                : isCurrent
                  ? <span style={{ color: "#5b9cf5", animation: "spin 1s linear infinite", display: "inline-block" }}>↻</span>
                  : <span style={{ color: "#4b5568" }}>○</span>
              }
              <span style={{ color: isDone ? "#4ade80" : isCurrent ? "#5b9cf5" : "#4b5568" }}>
                {size}대
              </span>
            </div>
          )
        })}
      </div>

      <style>{`
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
        @keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
      `}</style>
    </div>
  )
}

// ─── 파라미터 패널 ────────────────────────────────────────

function ParamPanel({ params, onChange, onRun, onReset, disabled }) {
  const fieldStyle = { display: "flex", flexDirection: "column", gap: 4 }
  const labelStyle = { fontSize: 11, color: "#8b93a7" }
  const inputStyle = {
    background: "#12151c", border: "1px solid #2a3142", borderRadius: 6,
    color: "#e6e9ef", padding: "7px 10px", fontSize: 13,
    width: "100%", boxSizing: "border-box",
  }

  return (
    <div style={{
      width: 220, flexShrink: 0,
      background: "#1a1f2e", border: "1px solid #2a3142",
      borderRadius: 10, padding: 16, display: "flex", flexDirection: "column", gap: 14,
      alignSelf: "flex-start", position: "sticky", top: 20,
    }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "#e6e9ef" }}>파라미터 설정</div>

      {FIELD_META.map(f => (
        <div key={f.key} style={fieldStyle}>
          <label style={labelStyle}>{f.label}</label>
          <input
            style={{ ...inputStyle, opacity: disabled ? 0.5 : 1 }}
            type={f.type}
            min={f.min} max={f.max} step={f.step}
            value={params[f.key]}
            disabled={disabled}
            onChange={e => onChange(f.key, e.target.value)}
          />
          <span style={{ fontSize: 10, color: "#4b5568", marginTop: 2 }}>{f.hint}</span>
        </div>
      ))}

      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 4 }}>
        <button
          type="button"
          onClick={onRun}
          disabled={disabled}
          style={{
            background: disabled ? "#2a3142" : "#5b9cf5",
            color: "#fff", border: "none", borderRadius: 7,
            padding: "9px 0", fontSize: 13, fontWeight: 600,
            cursor: disabled ? "not-allowed" : "pointer", width: "100%",
          }}
        >
          {disabled ? "실행 중…" : "시나리오 실행"}
        </button>
        <button
          type="button"
          onClick={onReset}
          disabled={disabled}
          style={{
            background: "transparent", color: "#8b93a7",
            border: "1px solid #2a3142", borderRadius: 7,
            padding: "7px 0", fontSize: 12, cursor: "pointer", width: "100%",
          }}
        >
          기본값으로 초기화
        </button>
      </div>
    </div>
  )
}

// ─── 결과 컴포넌트들 ──────────────────────────────────────

function RunBadge({ color, label, onRemove, onRename }) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(label)
  const commit = () => { onRename(val.trim() || label); setEditing(false) }
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      background: "#1a1f2e", border: `1px solid ${color}`,
      borderRadius: 6, padding: "3px 8px", fontSize: 12,
    }}>
      <span style={{ width: 8, height: 8, borderRadius: 2, background: color, flexShrink: 0 }} />
      {editing
        ? <input autoFocus value={val}
            onChange={e => setVal(e.target.value)}
            onBlur={commit} onKeyDown={e => e.key === "Enter" && commit()}
            style={{ background: "transparent", border: "none", outline: "none", color: "#e6e9ef", fontSize: 12, width: 80 }} />
        : <span style={{ color: "#e6e9ef", cursor: "pointer" }} onDoubleClick={() => setEditing(true)} title="더블클릭으로 라벨 편집">{label}</span>
      }
      <button type="button" onClick={onRemove}
        style={{ background: "none", border: "none", color: "#4b5568", cursor: "pointer", fontSize: 14, padding: 0, lineHeight: 1 }}>×</button>
    </span>
  )
}

function SummaryBanner({ run }) {
  const rec = run.recommendation
  const opt = rec.optimal_fleet_size
  const optResult = run.results.find(x => x.fleet_size === opt)
  return (
    <div style={{ background: "#0f1e10", border: "1px solid #4ade80", borderRadius: 8, padding: "12px 16px", marginBottom: 12 }}>
      <div style={{ fontSize: 12, color: "#4ade80", fontWeight: 700, marginBottom: 4 }}>
        {run.label} — 최적: {opt}대
      </div>
      <div style={{ fontSize: 12, color: "#8b93a7", lineHeight: 1.6 }}>{rec.reason}</div>
      {optResult && (
        <div style={{ display: "flex", gap: 20, marginTop: 8, flexWrap: "wrap" }}>
          {[["완료율", `${optResult.task_completion_rate.toFixed(1)}%`],
            ["처리량", `${optResult.throughput_per_hour}건/h`],
            ["리드타임", `${optResult.avg_lead_time_s.toFixed(1)}s`],
            ["SLA", `${optResult.sla_achievement_pct.toFixed(1)}%`],
            ["가동률", `${optResult.utilization_run_pct.toFixed(1)}%`],
          ].map(([k, v]) => (
            <div key={k} style={{ fontSize: 12 }}>
              <span style={{ color: "#4b5568" }}>{k} </span>
              <span style={{ color: "#e6e9ef", fontWeight: 600 }}>{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function KpiTable({ runs }) {
  const allResults = runs.flatMap(run =>
    run.results.map(r => ({ ...r, runLabel: run.label, runColor: run.color, runId: run.id, optimalSize: run.recommendation.optimal_fleet_size }))
  )
  const cc = v => v >= 90 ? "#4ade80" : v >= 75 ? "#fbbf24" : "#f87171"
  const sc = v => v >= 95 ? "#4ade80" : v >= 80 ? "#fbbf24" : "#f87171"

  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ borderBottom: "1px solid #2a3142" }}>
            {["실행", "대수", "완료", "완료율", "처리량/h", "리드타임s", "P95s", "가동률%", "SLA%", "효율점수"].map(h => (
              <th key={h} style={{ padding: "7px 10px", color: "#8b93a7", fontWeight: 500, textAlign: "right", whiteSpace: "nowrap" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {allResults.map((r, i) => {
            const isOpt = r.fleet_size === r.optimalSize
            return (
              <tr key={i} style={{ borderBottom: "1px solid #1a1f2e", background: isOpt ? "rgba(74,222,128,0.04)" : "transparent" }}>
                <td style={{ padding: "6px 10px" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: r.runColor }} />
                    <span style={{ color: "#8b93a7", fontSize: 11 }}>{r.runLabel}</span>
                  </span>
                </td>
                <td style={{ padding: "6px 10px", color: isOpt ? "#4ade80" : "#e6e9ef", fontWeight: isOpt ? 700 : 400, textAlign: "right" }}>{r.fleet_size}대{isOpt ? " ★" : ""}</td>
                <td style={{ padding: "6px 10px", color: "#e6e9ef", textAlign: "right" }}>{r.tasks_completed}</td>
                <td style={{ padding: "6px 10px", textAlign: "right", color: cc(r.task_completion_rate) }}>{r.task_completion_rate.toFixed(1)}%</td>
                <td style={{ padding: "6px 10px", color: "#e6e9ef", textAlign: "right" }}>{r.throughput_per_hour}</td>
                <td style={{ padding: "6px 10px", color: "#e6e9ef", textAlign: "right" }}>{r.avg_lead_time_s.toFixed(1)}</td>
                <td style={{ padding: "6px 10px", color: "#e6e9ef", textAlign: "right" }}>{r.p95_lead_time_s.toFixed(1)}</td>
                <td style={{ padding: "6px 10px", textAlign: "right", color: r.utilization_run_pct >= 70 ? "#5b9cf5" : "#8b93a7" }}>{r.utilization_run_pct.toFixed(1)}</td>
                <td style={{ padding: "6px 10px", textAlign: "right", color: sc(r.sla_achievement_pct) }}>{r.sla_achievement_pct.toFixed(1)}%</td>
                <td style={{ padding: "6px 10px", color: "#fbbf24", textAlign: "right", fontWeight: 600 }}>{r.efficiency_score.toFixed(2)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function CompareCharts({ runs }) {
  const allSizes = [...new Set(runs.flatMap(r => r.results.map(x => x.fleet_size)))].sort((a, b) => a - b)
  const makeData = key => allSizes.map(size => {
    const point = { name: `${size}대` }
    runs.forEach(run => {
      const r = run.results.find(x => x.fleet_size === size)
      if (r) point[run.label] = +Number(r[key]).toFixed(2)
    })
    return point
  })

  const chartBox = (title, children) => (
    <div style={{ background: "#1a1f2e", border: "1px solid #2a3142", borderRadius: 8, padding: 14 }}>
      <div style={{ fontSize: 12, color: "#8b93a7", marginBottom: 10 }}>{title}</div>
      {children}
    </div>
  )
  const legend = (
    <div style={{ display: "flex", gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
      {runs.map(run => (
        <span key={run.id} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "#8b93a7" }}>
          <span style={{ width: 10, height: 10, borderRadius: 2, background: run.color }} />{run.label}
        </span>
      ))}
    </div>
  )
  const lines = () => runs.map(run => (
    <Line key={run.id} type="monotone" dataKey={run.label} stroke={run.color} strokeWidth={2} dot={{ r: 4, fill: run.color }} activeDot={{ r: 5 }} />
  ))

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 14 }}>
      {[
        ["효율 점수", "efficiency_score"],
        ["시간당 처리량 (건/h)", "throughput_per_hour"],
        ["평균 리드타임 (s)", "avg_lead_time_s"],
        ["SLA 달성률 (%)", "sla_achievement_pct"],
        ["가동률 (%)", "utilization_run_pct"],
        ["완료율 (%)", "task_completion_rate"],
      ].map(([title, key]) => chartBox(title, <>
        {legend}
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={makeData(key)} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
            <XAxis dataKey="name" stroke={AXIS_STROKE} tick={{ fontSize: 11 }} />
            <YAxis stroke={AXIS_STROKE} tick={{ fontSize: 11 }} />
            <Tooltip contentStyle={TOOLTIP_STYLE} />
            {lines()}
          </LineChart>
        </ResponsiveContainer>
      </>))}
    </div>
  )
}

function OptimalRobotCharts({ runs }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {runs.map(run => {
        const opt = run.results.find(r => r.fleet_size === run.recommendation.optimal_fleet_size)
        if (!opt?.robot_utilization_pct) return null
        const data = Object.entries(opt.robot_utilization_pct).map(([id, v]) => ({
          name: id, 운행: +Number(v).toFixed(1), 충전: +Number(opt.robot_charge_pct?.[id] ?? 0).toFixed(1),
        }))
        return (
          <div key={run.id} style={{ background: "#1a1f2e", border: `1px solid ${run.color}`, borderRadius: 8, padding: 14 }}>
            <div style={{ fontSize: 12, color: "#8b93a7", marginBottom: 10 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: run.color, display: "inline-block", marginRight: 6 }} />
              {run.label} — {opt.fleet_size}대 (최적) 개별 로봇 가동률
            </div>
            <ResponsiveContainer width="100%" height={150}>
              <BarChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                <XAxis dataKey="name" stroke={AXIS_STROKE} tick={{ fontSize: 10 }} />
                <YAxis stroke={AXIS_STROKE} tick={{ fontSize: 11 }} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Bar dataKey="운행" stackId="a" fill={run.color} radius={[4, 4, 0, 0]} />
                <Bar dataKey="충전" stackId="a" fill="#fbbf24" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )
      })}
    </div>
  )
}

// ─── 메인 탭 ─────────────────────────────────────────────

export default function ScenarioTab() {
  const [params, setParams] = useState(DEFAULT_PARAMS)
  const [runs, setRuns] = useState([])
  const [activeJob, setActiveJob] = useState(null)   // { jobId, status, fleetSizes }
  const [error, setError] = useState(null)
  const runCountRef = useRef(0)
  const pollRef = useRef(null)

  // 폴링 시작
  const startPolling = (jobId, fleetSizes) => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/scenario/status/${jobId}`)
        const data = await parseJson(res)
        if (!res.ok) { stopPolling(); return }

        setActiveJob({ jobId, fleetSizes, ...data })

        if (data.status === "done") {
          stopPolling()
          // 결과 가져오기
          const rRes = await fetch(`${API_BASE}/scenario/result/${jobId}`)
          const result = await parseJson(rRes)
          if (rRes.ok) {
            runCountRef.current += 1
            const idx = runCountRef.current - 1
            setRuns(prev => [...prev, {
              id: Date.now(),
              label: `실행 #${runCountRef.current}`,
              color: RUN_COLORS[idx % RUN_COLORS.length],
              results: result.results,
              recommendation: result.recommendation,
            }])
          }
          setActiveJob(null)
        } else if (data.status === "error" || data.status === "cancelled") {
          stopPolling()
          if (data.status === "error") setError(data.message)
          setActiveJob(null)
        }
      } catch {
        stopPolling()
        setActiveJob(null)
      }
    }, 1000)
  }

  const stopPolling = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }

  useEffect(() => () => stopPolling(), [])

  const handleRun = async () => {
    setError(null)
    const fleet_sizes = String(params.fleet_sizes)
      .split(",").map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n) && n > 0)
    if (!fleet_sizes.length) { setError("대수 목록이 비어있습니다"); return }

    try {
      const body = {
        fleet_sizes,
        duration_s:            Number(params.duration_s),
        job_interval_s:        Number(params.job_interval_s),
        nav_speed:             Number(params.nav_speed),
        battery_drain_running: Number(params.battery_drain_running),
        battery_charge_rate:   Number(params.battery_charge_rate),
        sla_threshold_s:       Number(params.sla_threshold_s),
      }
      const res = await fetch(`${API_BASE}/scenario/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
      const data = await parseJson(res)
      if (!res.ok) throw new Error(data.detail || "API 오류")

      setActiveJob({ jobId: data.job_id, fleetSizes: fleet_sizes, status: "running", progress_pct: 0, message: "시뮬레이션 준비 중…", completed_sizes: [] })
      startPolling(data.job_id, fleet_sizes)
    } catch (e) {
      setError(e.message)
    }
  }

  const handleCancel = async () => {
    if (!activeJob?.jobId) return
    await fetch(`${API_BASE}/scenario/cancel/${activeJob.jobId}`, { method: "POST" })
    stopPolling()
    setActiveJob(null)
  }

  return (
    <div style={{ display: "flex", gap: 20, padding: "20px 24px", alignItems: "flex-start", minHeight: "80vh" }}>

      {/* 좌측: 파라미터 */}
      <ParamPanel
        params={params}
        onChange={(k, v) => setParams(p => ({ ...p, [k]: v }))}
        onRun={handleRun}
        onReset={() => setParams(DEFAULT_PARAMS)}
        disabled={!!activeJob}
      />

      {/* 우측: 결과 */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>

        {/* 진행 패널 */}
        {activeJob && (
          <ProgressPanel jobStatus={activeJob} onCancel={handleCancel} />
        )}

        {/* 에러 */}
        {error && (
          <div style={{ background: "#2d1515", border: "1px solid #f87171", borderRadius: 8, padding: "10px 14px", color: "#f87171", fontSize: 13 }}>
            {error}
          </div>
        )}

        {/* 실행 배지 */}
        {runs.length > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontSize: 12, color: "#8b93a7" }}>저장된 결과:</span>
            {runs.map(run => (
              <RunBadge key={run.id} color={run.color} label={run.label}
                onRemove={() => setRuns(prev => prev.filter(r => r.id !== run.id))}
                onRename={label => setRuns(prev => prev.map(r => r.id === run.id ? { ...r, label } : r))}
              />
            ))}
            <button type="button" onClick={() => { setRuns([]); runCountRef.current = 0 }}
              style={{ background: "transparent", color: "#4b5568", border: "1px solid #2a3142", borderRadius: 6, padding: "3px 10px", fontSize: 11, cursor: "pointer" }}>
              전체 삭제
            </button>
          </div>
        )}

        {/* 빈 상태 */}
        {!runs.length && !activeJob && !error && (
          <div style={{
            flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
            color: "#4b5568", fontSize: 14, textAlign: "center",
            border: "1px dashed #2a3142", borderRadius: 10, minHeight: 300,
          }}>
            <div>
              <div style={{ fontSize: 32, marginBottom: 12 }}>⚡</div>
              <div>파라미터를 설정하고 시나리오를 실행하세요.</div>
              <div style={{ fontSize: 12, marginTop: 6 }}>실행할 때마다 결과가 누적되어 비교할 수 있습니다.</div>
            </div>
          </div>
        )}

        {/* 결과 */}
        {runs.map(run => <SummaryBanner key={run.id} run={run} />)}

        {runs.length > 0 && (
          <div style={{ background: "#1a1f2e", border: "1px solid #2a3142", borderRadius: 8, padding: 14 }}>
            <div style={{ fontSize: 12, color: "#8b93a7", marginBottom: 12 }}>전체 KPI 비교</div>
            <KpiTable runs={runs} />
          </div>
        )}

        {runs.length > 0 && <CompareCharts runs={runs} />}
        {runs.length > 0 && <OptimalRobotCharts runs={runs} />}
      </div>
    </div>
  )
}