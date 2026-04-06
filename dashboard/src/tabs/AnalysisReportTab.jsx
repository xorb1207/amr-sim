import { useEffect, useState } from "react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { API_BASE } from "../apiConfig.js"

async function parseJson(res) {
  const t = await res.text()
  if (!t) return {}
  try {
    return JSON.parse(t)
  } catch {
    return {}
  }
}

export default function AnalysisReportTab({ analyticsSnap }) {
  const [fetched, setFetched] = useState(null)

  useEffect(() => {
    const t = window.setTimeout(() => {
      ;(async () => {
        const res = await fetch(`${API_BASE}/analytics/summary`)
        const d = await parseJson(res)
        if (res.ok) setFetched(d)
      })()
    }, 0)
    return () => window.clearTimeout(t)
  }, [])

  const s = analyticsSnap || fetched || {}
  const chartData = [
    { name: "운행", value: Number(s.utilization_run_pct) || 0 },
    { name: "대기", value: Number(s.utilization_idle_pct) || 0 },
    {
      name: "기타",
      value: Math.max(
        0,
        100 -
          (Number(s.utilization_run_pct) || 0) -
          (Number(s.utilization_idle_pct) || 0),
      ),
    },
  ]

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

  const printSummary = () => {
    const w = window.open("", "_blank")
    if (!w) return
    w.document.write(`<!DOCTYPE html><html><head><title>WAVE Report</title>
      <style>
        body{font-family:system-ui;background:#111;color:#eee;padding:24px;}
        table{border-collapse:collapse;width:100%;max-width:640px}
        td,th{border:1px solid #444;padding:8px;text-align:left}
        h1{color:#5b9cf5}
      </style></head><body>
      <h1>WAVE Fleet Planner — Analysis</h1>
      <p>Generated ${new Date().toLocaleString()}</p>
      <table>
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td>Simulation time (s)</td><td>${s.sim_time_s ?? "—"}</td></tr>
        <tr><td>Fleet size</td><td>${s.fleet_size ?? "—"}</td></tr>
        <tr><td>Tasks completed</td><td>${s.tasks_completed ?? "—"}</td></tr>
        <tr><td>Tasks started</td><td>${s.tasks_started ?? "—"}</td></tr>
        <tr><td>Total transport (m)</td><td>${s.total_transport_m ?? "—"}</td></tr>
        <tr><td>Run utilization %</td><td>${s.utilization_run_pct ?? "—"}</td></tr>
        <tr><td>Idle %</td><td>${s.utilization_idle_pct ?? "—"}</td></tr>
        <tr><td>Avg pending wait (s/robot)</td><td>${s.avg_pending_wait_s ?? "—"}</td></tr>
      </table>
      <p style="margin-top:24px;color:#888">Print this page to PDF (Ctrl/Cmd+P).</p>
      </body></html>`)
    w.document.close()
    w.focus()
    w.print()
  }

  return (
    <div className="wave-report">
      <section className="panel">
        <h2 className="panel__title">Analysis Report</h2>
        <p className="empty-hint">
          시뮬레이션 누적 지표입니다. PDF는 인쇄 대화상자에서 &quot;PDF로
          저장&quot;을 선택하세요.
        </p>
        <div className="wave-report__actions">
          <button type="button" className="btn btn--primary" onClick={downloadTxt}>
            요약 TXT 다운로드
          </button>
          <button type="button" className="btn" onClick={printSummary}>
            인쇄 / PDF 저장…
          </button>
        </div>
      </section>

      <div className="wave-report__grid">
        <section className="panel">
          <h3 className="panel__title">플릿 가동률 분해 (%)</h3>
          <div style={{ width: "100%", height: 280 }}>
            <ResponsiveContainer>
              <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3142" />
                <XAxis dataKey="name" stroke="#8b93a7" />
                <YAxis stroke="#8b93a7" domain={[0, 100]} />
                <Tooltip
                  contentStyle={{
                    background: "#12151c",
                    border: "1px solid #2a3142",
                    color: "#e6e9ef",
                  }}
                />
                <Bar dataKey="value" fill="#5b9cf5" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section className="panel">
          <h3 className="panel__title">운송·작업 KPI</h3>
          <table className="data-table">
            <tbody>
              <tr>
                <td>누적 운송 거리 (m)</td>
                <td className="mono">{s.total_transport_m ?? "—"}</td>
              </tr>
              <tr>
                <td>완료 작업 수</td>
                <td className="mono">{s.tasks_completed ?? "—"}</td>
              </tr>
              <tr>
                <td>시작된 작업</td>
                <td className="mono">{s.tasks_started ?? "—"}</td>
              </tr>
              <tr>
                <td>시뮬 시간 (s)</td>
                <td className="mono">{s.sim_time_s ?? "—"}</td>
              </tr>
              <tr>
                <td>대기열 평균 (s/대)</td>
                <td className="mono">{s.avg_pending_wait_s ?? "—"}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
