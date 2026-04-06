import { useEffect, useState } from "react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  RadialBar,
  RadialBarChart,
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

function KpiCard({ label, value, unit = "", tone = "normal" }) {
  const colorMap = { normal: "#5b9cf5", good: "#4ade80", warn: "#fbbf24", bad: "#f87171" }
  return (
    <div style={{
      background: "#1a1f2e", border: "1px solid #2a3142", borderRadius: 8,
      padding: "14px 16px", display: "flex", flexDirection: "column", gap: 4,
    }}>
      <span style={{ fontSize: 11, color: "#8b93a7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{label}</span>
      <span style={{ fontSize: 22, fontWeight: 700, color: colorMap[tone] ?? colorMap.normal, fontVariantNumeric: "tabular-nums" }}>
        {value ?? "—"}{unit && <span style={{ fontSize: 13, fontWeight: 400, marginLeft: 4, color: "#8b93a7" }}>{unit}</span>}
      </span>
    </div>
  )
}

function SlaGauge({ pct }) {
  const tone = pct >= 90 ? "#4ade80" : pct >= 70 ? "#fbbf24" : "#f87171"
  const data = [
    { name: "SLA", value: pct, fill: tone },
    { name: "gap", value: 100 - pct, fill: "#2a3142" },
  ]
  return (
    <div style={{ position: "relative", width: 140, height: 140, margin: "0 auto" }}>
      <ResponsiveContainer width="100%" height="100%">
        <RadialBarChart innerRadius={48} outerRadius={68} startAngle={180} endAngle={-180} data={data} barSize={14}>
          <RadialBar dataKey="value" cornerRadius={6}>
            {data.map((d, i) => <Cell key={i} fill={d.fill} />)}
          </RadialBar>
        </RadialBarChart>
      </ResponsiveContainer>
      <div style={{
        position: "absolute", top: "50%", left: "50%",
        transform: "translate(-50%,-50%)", textAlign: "center",
      }}>
        <div style={{ fontSize: 20, fontWeight: 700, color: tone }}>{pct}%</div>
        <div style={{ fontSize: 10, color: "#8b93a7" }}>SLA</div>
      </div>
    </div>
  )
}

export default function AnalysisReportTab({ analyticsSnap }) {
  const [fetched, setFetched] = useState(null)

  useEffect(() => {
    const t = window.setTimeout(async () => {
      const res = await fetch(`${API_BASE}/analytics/summary`)
      const d = await parseJson(res)
      if (res.ok) setFetched(d)
    }, 0)
    return () => window.clearTimeout(t)
  }, [])

  const s = analyticsSnap || fetched || {}

  const utilizationData = [
    { name: "운행", value: Number(s.utilization_run_pct) || 0, fill: "#5b9cf5" },
    { name: "충전", value: Number(s.utilization_charge_pct) || 0, fill: "#fbbf24" },
    { name: "유휴", value: Number(s.utilization_idle_pct) || 0, fill: "#4b5568" },
    {
      name: "기타",
      value: Math.max(0, 100
        - (Number(s.utilization_run_pct) || 0)
        - (Number(s.utilization_charge_pct) || 0)
        - (Number(s.utilization_idle_pct) || 0)),
      fill: "#374151",
    },
  ]

  const robotUtil = s.robot_utilization_pct || {}
  const robotCharge = s.robot_charge_pct || {}
  const robotChartData = Object.keys(robotUtil).map(id => ({
    name: id,
    운행: robotUtil[id] ?? 0,
    충전: robotCharge[id] ?? 0,
  }))

  const completionTone = (r) => r >= 95 ? "good" : r >= 80 ? "warn" : "bad"
  const slaTone = (r) => r >= 90 ? "good" : r >= 70 ? "warn" : "bad"
  const emergTone = (r) => r <= 20 ? "good" : r <= 40 ? "warn" : "bad"
  const slaNum = Number(s.sla_achievement_pct) || 0

  const downloadTxt = async () => {
    const res = await fetch(`${API_BASE}/analytics/report.txt`)
    const txt = await res.text()
    const blob = new Blob([txt], { type: "text/plain;charset=utf-8" })
    const a = document.createElement("a")
    a.href = URL.createObjectURL(blob)
    a.download = `wave-report-${new Date().toISOString().slice(0, 19)}.txt`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <div className="wave-report">
      <section className="panel">
        <h2 className="panel__title">Analysis Report</h2>
        <p className="empty-hint">시뮬레이션 누적 KPI 지표입니다.</p>
        <div className="wave-report__actions">
          <button type="button" className="btn btn--primary" onClick={downloadTxt}>
            요약 TXT 다운로드
          </button>
        </div>
      </section>

      {/* 핵심 KPI 카드 */}
      <section className="panel">
        <h3 className="panel__title">핵심 KPI</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(155px, 1fr))", gap: 12 }}>
          <KpiCard label="완료 작업" value={s.tasks_completed} />
          <KpiCard label="작업 완료율" value={s.task_completion_rate} unit="%" tone={completionTone(Number(s.task_completion_rate) || 0)} />
          <KpiCard label="SLA 달성률" value={s.sla_achievement_pct} unit="%" tone={slaTone(slaNum)} />
          <KpiCard label="평균 리드타임" value={s.avg_lead_time_s} unit="s" />
          <KpiCard label="P95 리드타임" value={s.p95_lead_time_s} unit="s" />
          <KpiCard label="평균 큐 대기" value={s.avg_queue_wait_s} unit="s" />
          <KpiCard label="누적 운송" value={s.total_transport_m} unit="m" />
          <KpiCard
            label="중단 / 실패"
            value={`${s.tasks_interrupted ?? 0} / ${s.tasks_failed ?? 0}`}
            tone={((s.tasks_interrupted ?? 0) + (s.tasks_failed ?? 0)) > 0 ? "warn" : "good"}
          />
        </div>
      </section>

      <div className="wave-report__grid">
        {/* 가동률 분해 */}
        <section className="panel">
          <h3 className="panel__title">플릿 가동률 분해 (%)</h3>
          <div style={{ width: "100%", height: 240 }}>
            <ResponsiveContainer>
              <BarChart data={utilizationData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3142" />
                <XAxis dataKey="name" stroke="#8b93a7" />
                <YAxis stroke="#8b93a7" domain={[0, 100]} />
                <Tooltip contentStyle={{ background: "#12151c", border: "1px solid #2a3142", color: "#e6e9ef" }} />
                <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                  {utilizationData.map((d, i) => <Cell key={i} fill={d.fill} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>

        {/* SLA 게이지 + 충전 지표 */}
        <section className="panel">
          <h3 className="panel__title">SLA · 충전 전략</h3>
          <SlaGauge pct={slaNum} />
          <div style={{ marginTop: 16, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <KpiCard label="SLA 기준" value={s.sla_threshold_s} unit="s" />
            <KpiCard label="긴급 충전 비율" value={s.emergency_charge_ratio} unit="%" tone={emergTone(Number(s.emergency_charge_ratio) || 0)} />
            <KpiCard label="총 충전 횟수" value={s.charge_count} />
            <KpiCard label="긴급 충전 횟수" value={s.emergency_charge_count} tone={(s.emergency_charge_count ?? 0) > 0 ? "warn" : "good"} />
          </div>
        </section>
      </div>

      {/* 개별 로봇 가동률 */}
      {robotChartData.length > 0 && (
        <section className="panel">
          <h3 className="panel__title">개별 로봇 가동률 — 운행 · 충전 (%)</h3>
          <div style={{ width: "100%", height: 220 }}>
            <ResponsiveContainer>
              <BarChart data={robotChartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3142" />
                <XAxis dataKey="name" stroke="#8b93a7" />
                <YAxis stroke="#8b93a7" domain={[0, 100]} />
                <Tooltip contentStyle={{ background: "#12151c", border: "1px solid #2a3142", color: "#e6e9ef" }} />
                <Bar dataKey="운행" fill="#5b9cf5" radius={[4, 4, 0, 0]} stackId="a" />
                <Bar dataKey="충전" fill="#fbbf24" radius={[0, 0, 0, 0]} stackId="a" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      )}
    </div>
  )
}
