import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { API_BASE, fleetWebSocketUrl } from "./apiConfig.js"
import RobotMap from "./components/RobotMap.jsx"
import { ToastStack } from "./components/ToastStack.jsx"
import WaveTopNav from "./components/WaveTopNav.jsx"
import { useToast } from "./hooks/useToast.js"
import AnalysisReportTab from "./tabs/AnalysisReportTab.jsx"
import MapEditorTab from "./tabs/MapEditorTab.jsx"
import ScenarioTab from "./tabs/ScenarioTab.jsx"


function badgeClass(status) {
  const map = {
    idle: "badge badge--idle",
    running: "badge badge--running",
    charging: "badge badge--charging",
    error: "badge badge--error",
    pending: "badge badge--pending",
    done: "badge badge--done",
    failed: "badge badge--error",
  }
  return map[status] || "badge badge--default"
}

function acsBadgeClass(acs) {
  const map = {
    Idle: "acs-badge acs-badge--idle",
    Moving: "acs-badge acs-badge--moving",
    Busy: "acs-badge acs-badge--busy",
    Charging: "acs-badge acs-badge--charging",
    Error: "acs-badge acs-badge--error",
  }
  return map[acs] || "acs-badge acs-badge--default"
}

function batteryTone(pct) {
  if (pct <= 30) return "var(--danger)"
  if (pct <= 60) return "var(--warning)"
  return "var(--success)"
}

async function parseJson(res) {
  const text = await res.text()
  if (!text) return {}
  try {
    return JSON.parse(text)
  } catch {
    return { error: "응답 파싱 실패" }
  }
}

function OpsPanel({ onFleetReset }) {
  const [nav, setNav] = useState(1.35)
  const [dr, setDr] = useState(7.5)
  const [di, setDi] = useState(1.2)
  const [drYield, setDrYield] = useState(4.0)
  const [ch, setCh] = useState(14.0)
  const [fleet, setFleet] = useState(3)
  const [autoDispatch, setAutoDispatch] = useState(true)
  const [autoInterval, setAutoInterval] = useState(7)
  const [msg, setMsg] = useState("")

  const load = useCallback(async () => {
    const res = await fetch(`${API_BASE}/sim/params`)
    const d = await parseJson(res)
    if (res.ok && d.params) {
      setNav(Number(d.params.nav_speed) || 1.35)
      setDr(Number(d.params.battery_drain_running) || 7.5)
      setDi(Number(d.params.battery_drain_idle) || 1.2)
      setDrYield(Number(d.params.battery_drain_running_nopower) || 4)
      setCh(Number(d.params.battery_charge_rate) || 14)
      setFleet(Number(d.fleet_size) || 3)
      setAutoDispatch(Number(d.params.auto_dispatch_enabled) !== 0)
      setAutoInterval(Number(d.params.auto_job_interval_s) || 7)
    }
  }, [])

  useEffect(() => {
    const t = window.setTimeout(() => {
      void load()
    }, 0)
    return () => window.clearTimeout(t)
  }, [load])

  const apply = async (patch) => {
    setMsg("")
    const res = await fetch(`${API_BASE}/sim/params`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
    const d = await parseJson(res)
    if (!res.ok) {
      setMsg(String(d.detail || "적용 실패"))
      return
    }
    if (patch.fleet_size != null) onFleetReset?.()
    setMsg("적용됨")
    load()
  }

  return (
    <section className="panel ops-panel" aria-labelledby="ops-heading">
      <h2 id="ops-heading" className="panel__title">
        운영 파라미터
      </h2>
      <p className="empty-hint" style={{ marginBottom: "0.75rem" }}>
        실시간 튜닝 (REST). 플릿 대수 변경 시 진행 중 작업이 초기화됩니다.
      </p>
      <div className="form-row form-row--stack">
        <div className="form-field">
          <label>주행 속도 (m/s)</label>
          <input
            type="number"
            step="0.05"
            min={0.2}
            max={4}
            value={nav}
            onChange={(e) => setNav(parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="form-field">
          <label>운행 배터리 소모 (/s)</label>
          <input
            type="number"
            step="0.1"
            value={dr}
            onChange={(e) => setDr(parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="form-field">
          <label>대기 배터리 소모 (/s)</label>
          <input
            type="number"
            step="0.1"
            value={di}
            onChange={(e) => setDi(parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="form-field">
          <label>충전 속도 (/s)</label>
          <input
            type="number"
            step="0.5"
            value={ch}
            onChange={(e) => setCh(parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="form-field">
          <label>교행 대기 시 주행 소모 (/s)</label>
          <input
            type="number"
            step="0.1"
            value={drYield}
            onChange={(e) => setDrYield(parseFloat(e.target.value) || 0)}
          />
        </div>
        <div className="form-field">
          <label>플릿 대수</label>
          <input
            type="number"
            min={1}
            max={12}
            value={fleet}
            onChange={(e) => setFleet(parseInt(e.target.value, 10) || 1)}
          />
        </div>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() =>
            apply({
              nav_speed: nav,
              battery_drain_running: dr,
              battery_drain_idle: di,
              battery_drain_running_nopower: drYield,
              battery_charge_rate: ch,
            })
          }
        >
          속도·배터리 적용
        </button>
        <button
          type="button"
          className="btn btn--danger"
          onClick={() => apply({ fleet_size: fleet })}
        >
          대수 적용 (리셋)
        </button>
      </div>
      <div className="form-row form-row--stack" style={{ marginTop: "1rem" }}>
        <p className="empty-hint" style={{ marginBottom: "0.35rem" }}>
          ACS 자동 디스패처 (시뮬레이션)
        </p>
        <label className="toc-checkbox">
          <input
            type="checkbox"
            checked={autoDispatch}
            onChange={(e) => setAutoDispatch(e.target.checked)}
          />
          <span>주기적 가상 운반 작업 생성·할당</span>
        </label>
        <div className="form-field">
          <label>생성 간격 (초)</label>
          <input
            type="number"
            step="1"
            min={2}
            max={120}
            value={autoInterval}
            onChange={(e) =>
              setAutoInterval(parseInt(e.target.value, 10) || 7)
            }
          />
        </div>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() =>
            apply({
              auto_dispatch_enabled: autoDispatch ? 1 : 0,
              auto_job_interval_s: autoInterval,
            })
          }
        >
          ACS 디스패처 적용
        </button>
      </div>
      {msg && (
        <p className="empty-hint" style={{ marginTop: "0.5rem" }}>
          {msg}
        </p>
      )}
    </section>
  )
}

export default function App() {
  const [amrs, setAmrs] = useState([])
  const [tasks, setTasks] = useState([])
  const [activeJobs, setActiveJobs] = useState([])
  const [acsLogs, setAcsLogs] = useState([])
  const [fleetStates, setFleetStates] = useState(null)
  const [mapMeta, setMapMeta] = useState({
    stations: {},
    station_overlay: {},
    battery_cost_per_meter: 1.15,
    min_battery_after_task_pct: 5,
    low_battery_charge_pct: 15,
    charging_station_ids: [],
  })
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [restOk, setRestOk] = useState(false)
  const [wsLive, setWsLive] = useState(false)
  const [lastStreamAt, setLastStreamAt] = useState(null)
  const [banner, setBanner] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [tab, setTab] = useState("dashboard")
  const [waveMapState, setWaveMapState] = useState(null)
  const [analyticsSnap, setAnalyticsSnap] = useState(null)

  const { toasts, push, dismiss } = useToast(7200)
  const lowBatteryAnnounced = useRef(new Set())
  const rescueAnnounced = useRef(new Set())
  const tocLogContainerRef = useRef(null)
  const tocLogUserScrolled = useRef(false)

  const [form, setForm] = useState({
    amr_id: "",
    task_type: "move",
    destination: "",
    pickup_station: "",
    drop_station: "",
    priority: 1,
  })

  const fetchRestSnapshot = useCallback(async () => {
    const [amrRes, taskRes] = await Promise.all([
      fetch(`${API_BASE}/amrs`),
      fetch(`${API_BASE}/tasks`),
    ])
    const amrData = await parseJson(amrRes)
    const taskData = await parseJson(taskRes)
    if (!amrRes.ok) {
      const msg =
        amrData.error ||
        (Array.isArray(amrData.detail)
          ? amrData.detail.map((x) => x.msg || x).join(", ")
          : amrData.detail) ||
        `AMR API 오류 (${amrRes.status})`
      throw new Error(msg)
    }
    if (!taskRes.ok) {
      const msg =
        taskData.error ||
        (Array.isArray(taskData.detail)
          ? taskData.detail.map((x) => x.msg || x).join(", ")
          : taskData.detail) ||
        `작업 API 오류 (${taskRes.status})`
      throw new Error(msg)
    }
    if (amrData.error) throw new Error(amrData.error)
    if (taskData.error) throw new Error(taskData.error)
    setAmrs(amrData.amrs ?? [])
    setTasks(taskData.tasks ?? [])
    try {
      const fsRes = await fetch(`${API_BASE}/fleet_states`)
      const fsData = await parseJson(fsRes)
      if (fsRes.ok && !fsData.error) setFleetStates(fsData)
    } catch {
      /* fleet_states optional */
    }
    setRestOk(true)
  }, [])

  const fetchHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/`)
      const data = await parseJson(res)
      if (data.error) throw new Error(data.error)
      setHealth(data)
    } catch {
      setHealth(null)
    }
  }, [])

  const fetchStations = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/map/stations`)
      const data = await parseJson(res)
      if (res.ok && data.stations) {
        setMapMeta((m) => ({
          ...m,
          stations: data.stations,
          station_overlay: data.station_overlay ?? m.station_overlay,
          battery_cost_per_meter:
            data.battery_cost_per_meter ?? m.battery_cost_per_meter,
          min_battery_after_task_pct:
            data.min_battery_after_task_pct ?? m.min_battery_after_task_pct,
          low_battery_charge_pct:
            data.low_battery_charge_pct ?? m.low_battery_charge_pct,
          charging_station_ids:
            data.charging_station_ids ?? m.charging_station_ids,
        }))
        if (data.wave_map) setWaveMapState(data.wave_map)
      }
    } catch {
      /* keep defaults */
    }
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    setBanner(null)
    try {
      await Promise.all([fetchRestSnapshot(), fetchHealth(), fetchStations()])
    } catch (e) {
      setRestOk(false)
      setBanner({
        type: "error",
        text:
          e.message ||
          "서버에 연결할 수 없습니다. 백엔드(uvicorn)가 실행 중인지 확인하세요.",
      })
    } finally {
      setLoading(false)
    }
  }, [fetchRestSnapshot, fetchHealth, fetchStations])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    if (wsLive) return
    const id = window.setInterval(() => {
      fetchRestSnapshot().catch(() => setRestOk(false))
      fetchHealth()
    }, 5000)
    return () => clearInterval(id)
  }, [wsLive, fetchRestSnapshot, fetchHealth])

  useEffect(() => {
    const url = fleetWebSocketUrl()
    let ws
    let stopped = false
    const connect = () => {
      if (stopped) return
      ws = new WebSocket(url)
      ws.onopen = () => setWsLive(true)
      ws.onclose = () => {
        setWsLive(false)
        if (!stopped) window.setTimeout(connect, 1500)
      }
      ws.onerror = () => {
        ws.close()
      }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === "fleet_tick") {
            if (Array.isArray(msg.amrs)) setAmrs(msg.amrs)
            if (Array.isArray(msg.tasks)) setTasks(msg.tasks)
            if (Array.isArray(msg.active_jobs)) setActiveJobs(msg.active_jobs)
            if (Array.isArray(msg.acs_logs)) setAcsLogs(msg.acs_logs)
            if (msg.fleet_states) {
              setFleetStates(msg.fleet_states)
              setLastStreamAt(new Date())
            }
            if (msg.station_overlay) {
              setMapMeta((m) => ({
                ...m,
                station_overlay: msg.station_overlay,
              }))
            }
            if (msg.wave_map) setWaveMapState(msg.wave_map)
            if (msg.analytics) setAnalyticsSnap(msg.analytics)
            setRestOk(true)
          }
        } catch {
          /* ignore */
        }
      }
    }
    connect()
    return () => {
      stopped = true
      try {
        ws?.close()
      } catch {
        /* noop */
      }
    }
  }, [])

  useEffect(() => {
    const robots = fleetStates?.robots ?? []
    const seen = lowBatteryAnnounced.current
    robots.forEach((r) => {
      const name = r.name
      const bat = Number(r.battery_percent)
      if (Number.isFinite(bat) && bat < 20) {
        if (!seen.has(name)) {
          seen.add(name)
          push({
            level: "danger",
            title: `저배터리 · ${name}`,
            body: `잔량 ${bat}% (20% 미만). 충전 스테이션 배정을 권장합니다.`,
          })
        }
      } else if (Number.isFinite(bat) && bat >= 22) {
        seen.delete(name)
      }
    })
  }, [fleetStates, push])

  useEffect(() => {
    const robots = fleetStates?.robots ?? []
    const seen = rescueAnnounced.current
    robots.forEach((r) => {
      const name = r.name
      if (r.issues?.includes("RESCUE_REQUIRED")) {
        if (!seen.has(name)) {
          seen.add(name)
          push({
            level: "danger",
            title: `Rescue Required · ${name}`,
            body:
              "배터리 방전 등으로 Error 상태입니다. 관제에서 충전소 워프(수동 구조)를 실행하세요.",
          })
        }
      } else {
        seen.delete(name)
      }
    })
  }, [fleetStates, push])

  useEffect(() => {
    if (!tocLogUserScrolled.current) {
      const el = tocLogContainerRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [acsLogs])

  const fleetRobotByName = useMemo(() => {
    const m = {}
    fleetStates?.robots?.forEach((r) => {
      m[r.name] = r
    })
    return m
  }, [fleetStates])

  const acsSummary = useMemo(() => {
    const m = { Idle: 0, Moving: 0, Busy: 0, Charging: 0, Error: 0 }
    amrs.forEach((a) => {
      const s = a.acs_state
      if (s && m[s] !== undefined) m[s] += 1
    })
    return m
  }, [amrs])

  const idleAmrs = useMemo(
    () =>
      amrs.filter(
        (a) => a.status === "idle" && a.acs_state !== "Error",
      ),
    [amrs],
  )

  const selectedAmr = useMemo(
    () => amrs.find((a) => a.id === form.amr_id),
    [amrs, form.amr_id],
  )

  const batteryPreview = useMemo(() => {
    const dest = form.destination.trim()
    if (!selectedAmr || !dest) return null
    const st = mapMeta.stations[dest]
    if (!Array.isArray(st) || st.length < 2) return null
    const sx = Number(selectedAmr.x)
    const sy = Number(selectedAmr.y)
    if (!Number.isFinite(sx) || !Number.isFinite(sy)) return null
    const [tx, ty] = st
    const dist = Math.hypot(tx - sx, ty - sy)
    const cost = dist * mapMeta.battery_cost_per_meter
    const bat = Number(selectedAmr.battery)
    const after = bat - cost
    const min = mapMeta.min_battery_after_task_pct
    return {
      dist,
      cost,
      after,
      ok: after >= min,
      min,
    }
  }, [
    selectedAmr,
    form.destination,
    mapMeta.stations,
    mapMeta.battery_cost_per_meter,
    mapMeta.min_battery_after_task_pct,
  ])

  const lowBatteryChargeOnly =
    selectedAmr &&
    Number(selectedAmr.battery) <= mapMeta.low_battery_charge_pct

  const stats = useMemo(() => {
    const by = (s) => amrs.filter((a) => a.status === s).length
    const tBy = (s) => tasks.filter((t) => t.status === s).length
    return {
      amrTotal: amrs.length,
      idle: by("idle"),
      running: by("running"),
      charging: by("charging"),
      taskPending: tBy("pending"),
      taskRunning: tBy("running"),
      taskDone: tBy("done"),
    }
  }, [amrs, tasks])

  const mapRobots = useMemo(() => {
    if (fleetStates?.robots?.length) return fleetStates.robots
    return amrs.map((a) => ({
      name: a.id,
      id: a.id,
      battery_percent: a.battery,
      mode: a.status === "running" ? "MODE_MOVING" : a.status === "charging" ? "MODE_CHARGING" : "MODE_IDLE",
      location: {
        x: a.x,
        y: a.y,
        yaw: a.yaw ?? 0,
      },
    }))
  }, [fleetStates, amrs])

  useEffect(() => {
    if (idleAmrs.length === 0) {
      setForm((f) => (f.amr_id ? { ...f, amr_id: "" } : f))
      return
    }
    setForm((f) =>
      idleAmrs.some((a) => a.id === f.amr_id)
        ? f
        : { ...f, amr_id: idleAmrs[0].id },
    )
  }, [idleAmrs])

  const showApiResult = (data) => {
    if (data?.error) {
      setBanner({ type: "error", text: data.error })
      return false
    }
    if (data?.message) {
      setBanner({ type: "success", text: data.message })
    }
    return true
  }

  const apiAction = async (path, options = {}) => {
    setBusyId(path)
    setBanner(null)
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        headers: { "Content-Type": "application/json", ...options.headers },
        ...options,
      })
      const data = await parseJson(res)
      if (!res.ok && !data.error) {
        let msg
        if (typeof data.detail === "string") msg = data.detail
        else if (Array.isArray(data.detail))
          msg = data.detail.map((x) => x.msg || JSON.stringify(x)).join(", ")
        else msg = data.detail || `요청 실패 (${res.status})`
        setBanner({ type: "error", text: String(msg) })
        return
      }
      if (!showApiResult(data)) return
      if (!wsLive) await fetchRestSnapshot()
    } catch (e) {
      setBanner({ type: "error", text: e.message || "요청 실패" })
      setRestOk(false)
    } finally {
      setBusyId(null)
    }
  }

  const submitTask = (e) => {
    e.preventDefault()
    if (!form.destination.trim()) {
      setBanner({ type: "error", text: "목적지를 입력하세요." })
      return
    }
    const payload = {
      amr_id: form.amr_id,
      task_type: form.task_type,
      destination: form.destination.trim(),
      priority: Number(form.priority) || 1,
    }
    const pu = form.pickup_station.trim()
    const dr = form.drop_station.trim()
    if (pu) payload.pickup_station = pu
    if (dr) payload.drop_station = dr
    apiAction("/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    })
  }

  return (
    <div className="dashboard ics-dashboard wave-shell">
      <ToastStack toasts={toasts} onDismiss={dismiss} />

      <WaveTopNav tab={tab} onTab={setTab} />

      <header className="dashboard__header ics-header">
        <div className="dashboard__title-block">
          <div className="ics-brand">
            <span className="ics-brand__mark" aria-hidden />
            <div>
              <h1>WAVE · 관제 대시보드</h1>
              <p>
                WebSocket 플릿 스트림
                {lastStreamAt && wsLive && (
                  <>
                    {" "}
                    · 마지막 프레임{" "}
                    <span className="mono">
                      {lastStreamAt.toLocaleTimeString()}
                    </span>
                  </>
                )}
                {!wsLive && " · REST 폴백(5s)"}
              </p>
            </div>
          </div>
          {health?.time && (
            <div className="health-line">
              {health.message} · 서버 시각 {health.time}
            </div>
          )}
        </div>
        <div className="dashboard__header-actions">
          <span
            className={
              wsLive ? "live-pill live-pill--ok" : "live-pill live-pill--err"
            }
          >
            <span className="live-pill__dot" aria-hidden />
            {wsLive ? "스트림 LIVE" : "스트림 대기"}
          </span>
          <span
            className={
              restOk ? "live-pill live-pill--ok" : "live-pill live-pill--err"
            }
          >
            REST {restOk ? "OK" : "끊김"}
          </span>
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => refresh()}
            disabled={loading}
          >
            {loading ? "불러오는 중…" : "동기화"}
          </button>
        </div>
      </header>

      {banner && tab === "dashboard" && (
        <div
          className={
            banner.type === "error"
              ? "banner banner--error"
              : "banner banner--success"
          }
          role="status"
        >
          {banner.text}
        </div>
      )}

      {tab === "map-editor" && (
        <MapEditorTab
          onSaved={() => {
            refresh()
            fetchStations()
          }}
        />
      )}

      {tab === "analysis" && (
        <AnalysisReportTab analyticsSnap={analyticsSnap} />
      )}

      {tab === "scenario" && <ScenarioTab />}


      {tab === "dashboard" && (
      <div className="ics-main">
        <aside className="ics-col ics-col--left">
          <section className="stats stats--compact" aria-label="요약">
            <div className="stat-card stat-card--wide">
              <div className="stat-card__label">ACS 상태 (실시간)</div>
              <div className="acs-summary">
                {Object.entries(acsSummary).map(([k, v]) => (
                  <span key={k} className={acsBadgeClass(k)} title={k}>
                    {k} <strong>{v}</strong>
                  </span>
                ))}
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">등록 AMR</div>
              <div className="stat-card__value">{stats.amrTotal}</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">대기</div>
              <div className="stat-card__value">{stats.idle}</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">운행</div>
              <div className="stat-card__value">{stats.running}</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">충전</div>
              <div className="stat-card__value">{stats.charging}</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">작업 대기</div>
              <div className="stat-card__value">{stats.taskPending}</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">작업 실행</div>
              <div className="stat-card__value">{stats.taskRunning}</div>
            </div>
            <div className="stat-card">
              <div className="stat-card__label">작업 완료</div>
              <div className="stat-card__value">{stats.taskDone}</div>
            </div>
          </section>

          <section className="section" aria-labelledby="amr-heading">
            <h2 id="amr-heading" className="section__title">
              AMR 현황
            </h2>
            <div className="grid-amr grid-amr--compact">
              {amrs.map((amr) => (
                <article key={amr.id} className="amr-card">
                  <div className="amr-card__id">{amr.id}</div>
                  <div className="amr-card__badges">
                    <span className={badgeClass(amr.status)}>{amr.status}</span>
                    {amr.acs_state && (
                      <span className={acsBadgeClass(amr.acs_state)}>
                        ACS · {amr.acs_state}
                      </span>
                    )}
                  </div>
                  <div className="amr-card__meta">위치 · {amr.location}</div>
                  <div className="amr-card__meta mono">
                    좌표 ·{" "}
                    {typeof amr.x === "number"
                      ? `${amr.x.toFixed(2)}, ${amr.y?.toFixed?.(2) ?? "—"}`
                      : "—"}
                  </div>
                  <div className="amr-card__meta">
                    배터리 · {amr.battery}%
                  </div>
                  <div className="battery-bar" title={`${amr.battery}%`}>
                    <div
                      className="battery-bar__fill"
                      style={{
                        width: `${amr.battery}%`,
                        background: batteryTone(amr.battery),
                      }}
                    />
                  </div>
                  {fleetRobotByName[amr.id]?.issues?.includes("RESCUE_REQUIRED") && (
                    <div className="rescue-strip">
                      <span className="rescue-strip__label">구조 필요</span>
                      <button
                        type="button"
                        className="btn btn--sm btn--danger"
                        disabled={busyId !== null}
                        onClick={() =>
                          apiAction(`/amrs/${amr.id}/rescue-warp`, {
                            method: "POST",
                            body: JSON.stringify({}),
                          })
                        }
                      >
                        충전소 워프
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>
            {amrs.length === 0 && !loading && (
              <p className="empty-hint">등록된 AMR이 없습니다.</p>
            )}
          </section>
        </aside>

        <main className="ics-col ics-col--center">
          <RobotMap
            stations={mapMeta.stations}
            stationOverlay={mapMeta.station_overlay}
            robots={mapRobots}
            waveMap={waveMapState}
          />
          <section className="fleet-json panel panel--flat" aria-label="Fleet states">
            <h2 className="panel__title">/fleet_states (OpenRMF 모사)</h2>
            <pre className="fleet-json__pre mono fleet-json__pre--compact">
              {fleetStates
                ? JSON.stringify(fleetStates, null, 2)
                : "스트림 수신 대기 중…"}
            </pre>
          </section>
        </main>

        <aside className="ics-col ics-col--right">
          <OpsPanel onFleetReset={() => fetchRestSnapshot()} />

          <section className="panel toc-panel" aria-labelledby="active-jobs-heading">
            <h2 id="active-jobs-heading" className="panel__title">
              Active Jobs
            </h2>
            {activeJobs.length === 0 ? (
              <p className="empty-hint">진행·대기 중인 작업 없음</p>
            ) : (
              <div className="table-wrap table-wrap--scroll toc-table-wrap">
                <table className="data-table data-table--dense">
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th>로봇</th>
                      <th>출발→목적</th>
                      <th>%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {activeJobs.map((j) => (
                      <tr key={j.job_id}>
                        <td className="mono">
                          {j.job_id}
                          {j.auto ? (
                            <span className="toc-auto-tag" title="자동 생성">
                              A
                            </span>
                          ) : null}
                        </td>
                        <td className="mono">{j.robot}</td>
                        <td className="mono toc-route">
                          {j.origin} → {j.destination}
                        </td>
                        <td>
                          <div className="job-progress">
                            <div
                              className="job-progress__bar"
                              style={{
                                width: `${Math.min(100, Number(j.progress_pct) || 0)}%`,
                              }}
                            />
                            <span className="job-progress__pct">
                              {Number(j.progress_pct).toFixed(0)}%
                            </span>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel toc-panel toc-panel--log" aria-labelledby="toc-log-heading">
            <h2 id="toc-log-heading" className="panel__title">
              TOC 이벤트 로그
            </h2>
            <p className="empty-hint" style={{ marginBottom: "0.5rem" }}>
              WebSocket 실시간 스트림 (ACS 작업·충전·오류)
            </p>
            <div
              className="toc-log"
              role="log"
              aria-live="polite"
              ref={tocLogContainerRef}
              onScroll={() => {
                const el = tocLogContainerRef.current
                if (!el) return
                const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48
                tocLogUserScrolled.current = !atBottom
              }}
            >
              {acsLogs.length === 0 ? (
                <p className="empty-hint">이벤트 수신 대기…</p>
              ) : (
                acsLogs.map((e, i) => (
                  <div key={`${e.ts}-${i}`} className={`toc-log__line toc-log__line--${e.kind || "info"}`}>
                    <span className="toc-log__ts mono">
                      {(e.ts || "").slice(11, 19)}
                    </span>
                    <span className="toc-log__kind">{e.kind}</span>
                    <span className="toc-log__msg">{e.message}</span>
                  </div>
                ))
              )}
            </div>
          </section>

          <section className="panel" aria-labelledby="task-form-heading">
            <h2 id="task-form-heading" className="panel__title">
              작업 등록
            </h2>
            <form onSubmit={submitTask}>
              <div className="form-row form-row--stack">
                <div className="form-field">
                  <label htmlFor="amr_id">AMR</label>
                  <select
                    id="amr_id"
                    value={form.amr_id}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, amr_id: e.target.value }))
                    }
                  >
                    {idleAmrs.length === 0 ? (
                      <option value="">대기 중인 AMR 없음</option>
                    ) : (
                      idleAmrs.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.id} · {a.location}
                        </option>
                      ))
                    )}
                  </select>
                </div>
                <div className="form-field">
                  <label htmlFor="task_type">유형</label>
                  <select
                    id="task_type"
                    value={form.task_type}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, task_type: e.target.value }))
                    }
                  >
                    <option value="move">move</option>
                    <option value="pick">pick</option>
                    <option value="place">place</option>
                    <option value="charge">charge (충전 이동)</option>
                  </select>
                </div>
                <div className="form-field">
                  <label htmlFor="destination">목적지</label>
                  <input
                    id="destination"
                    type="text"
                    placeholder="Station-B"
                    value={form.destination}
                    list="station-suggestions"
                    onChange={(e) =>
                      setForm((f) => ({ ...f, destination: e.target.value }))
                    }
                  />
                  <datalist id="station-suggestions">
                    {Object.keys(mapMeta.stations)
                      .filter((name) => {
                        if (!lowBatteryChargeOnly) return true
                        return mapMeta.charging_station_ids.includes(name)
                      })
                      .map((name) => (
                        <option key={name} value={name} />
                      ))}
                  </datalist>
                  {lowBatteryChargeOnly && (
                    <p className="empty-hint" style={{ marginTop: "0.35rem" }}>
                      배터리 {mapMeta.low_battery_charge_pct}% 이하: 충전 스테이션만
                      선택할 수 있습니다.
                    </p>
                  )}
                </div>
                <div className="form-field">
                  <label htmlFor="pickup_station">상차지 (선택)</label>
                  <input
                    id="pickup_station"
                    type="text"
                    placeholder="비우면 AMR 현재 위치"
                    value={form.pickup_station}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, pickup_station: e.target.value }))
                    }
                  />
                </div>
                <div className="form-field">
                  <label htmlFor="drop_station">하차지 (선택)</label>
                  <input
                    id="drop_station"
                    type="text"
                    placeholder="비우면 목적지와 동일"
                    value={form.drop_station}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, drop_station: e.target.value }))
                    }
                  />
                </div>
                {batteryPreview && (
                  <div
                    className={`battery-preview${batteryPreview.ok ? "" : " battery-preview--bad"}`}
                  >
                    <strong>예상 이동 배터리 소모</strong> · 약{" "}
                    {batteryPreview.cost.toFixed(1)}% (거리{" "}
                    {batteryPreview.dist.toFixed(2)} m × 계수{" "}
                    {mapMeta.battery_cost_per_meter})
                    <br />
                    완료 후 예상 잔량: <strong>{batteryPreview.after.toFixed(1)}%</strong>{" "}
                    (정책 최소 {batteryPreview.min}%)
                    {!batteryPreview.ok &&
                      " — 서버에서 작업 등록이 거절될 수 있습니다."}
                  </div>
                )}
                <div className="form-field">
                  <label htmlFor="priority">우선순위</label>
                  <input
                    id="priority"
                    type="number"
                    min={1}
                    max={99}
                    value={form.priority}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, priority: e.target.value }))
                    }
                  />
                </div>
                <button
                  type="submit"
                  className="btn btn--primary"
                  disabled={busyId !== null || idleAmrs.length === 0}
                >
                  작업 등록
                </button>
              </div>
            </form>
          </section>

          <section className="section" aria-labelledby="tasks-heading">
            <h2 id="tasks-heading" className="section__title">
              작업 목록
            </h2>
            {tasks.length === 0 ? (
              <p className="empty-hint">등록된 작업이 없습니다.</p>
            ) : (
              <div className="table-wrap table-wrap--scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>작업</th>
                      <th>AMR</th>
                      <th>유형</th>
                      <th>목적지</th>
                      <th>상태</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {tasks.map((task) => (
                      <tr key={task.task_id}>
                        <td className="mono">{task.task_id}</td>
                        <td className="mono">{task.amr_id}</td>
                        <td>{task.task_type}</td>
                        <td>{task.destination}</td>
                        <td>
                          <span className={badgeClass(task.status)}>
                            {task.status}
                          </span>
                        </td>
                        <td>
                          <div className="data-table__actions">
                            {task.status === "pending" && (
                              <>
                                <button
                                  type="button"
                                  className="btn btn--sm btn--primary"
                                  disabled={busyId !== null}
                                  onClick={() =>
                                    apiAction(`/tasks/${task.task_id}/start`, {
                                      method: "PATCH",
                                    })
                                  }
                                >
                                  시작
                                </button>
                                <button
                                  type="button"
                                  className="btn btn--sm btn--danger"
                                  disabled={busyId !== null}
                                  onClick={() =>
                                    apiAction(`/tasks/${task.task_id}`, {
                                      method: "DELETE",
                                    })
                                  }
                                >
                                  취소
                                </button>
                              </>
                            )}
                            {task.status === "running" && (
                              <button
                                type="button"
                                className="btn btn--sm btn--primary"
                                disabled={busyId !== null}
                                onClick={() =>
                                  apiAction(`/tasks/${task.task_id}/done`, {
                                    method: "PATCH",
                                  })
                                }
                              >
                                완료
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </aside>
      </div>
      )}
    </div>
  )
}
