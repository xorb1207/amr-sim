import { useCallback, useEffect, useRef, useState } from "react"
import { API_BASE } from "../apiConfig.js"

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v))
}

async function parseJson(res) {
  const t = await res.text()
  if (!t) return {}
  try {
    return JSON.parse(t)
  } catch {
    return {}
  }
}

export default function MapEditorTab({ onSaved }) {
  const [map, setMap] = useState({
    version: 1,
    name: "wave_map",
    imageDataUrl: "",
    imageWidth: 0,
    imageHeight: 0,
    nodes: [],
    edges: [],
  })
  const [tool, setTool] = useState("select")
  const [edgeFrom, setEdgeFrom] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [status, setStatus] = useState("")
  const canvasRef = useRef(null)
  const wrapRef = useRef(null)
  const camRef = useRef({ cx: 11, cy: 7, zoom: 28 })

  useEffect(() => {
    ;(async () => {
      const res = await fetch(`${API_BASE}/map/editor`)
      const d = await parseJson(res)
      if (res.ok && d.nodes) setMap((m) => ({ ...m, ...d }))
    })()
  }, [])

  const worldFromEvent = (e) => {
    const c = canvasRef.current
    if (!c) return null
    const r = c.getBoundingClientRect()
    const mx = e.clientX - r.left
    const my = e.clientY - r.top
    const w = r.width
    const h = r.height
    const { cx, cy, zoom } = camRef.current
    const wx = (mx - w / 2) / zoom + cx
    const wy = (h / 2 - my) / zoom + cy
    return { wx, wy }
  }

  const redraw = useCallback(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const dpr = window.devicePixelRatio || 1
    const w = wrap.clientWidth
    const h = Math.min(560, Math.max(400, Math.round(w * 0.55)))
    canvas.width = Math.floor(w * dpr)
    canvas.height = Math.floor(h * dpr)
    canvas.style.width = `${w}px`
    canvas.style.height = `${h}px`
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.fillStyle = "#0a0c10"
    ctx.fillRect(0, 0, w, h)
    const { cx, cy, zoom } = camRef.current
    const worldToScreen = (wx, wy) => ({
      sx: (wx - cx) * zoom + w / 2,
      sy: h / 2 - (wy - cy) * zoom,
    })
    if (map.imageDataUrl) {
      const im = new Image()
      im.src = map.imageDataUrl
      if (im.complete && im.naturalWidth) {
        const margin = 2
        const tl = worldToScreen(margin, 16)
        const br = worldToScreen(22 - margin, margin)
        ctx.save()
        ctx.globalAlpha = 0.4
        ctx.drawImage(im, tl.sx, tl.sy, br.sx - tl.sx, br.sy - tl.sy)
        ctx.restore()
      }
    }
    const byId = Object.fromEntries(map.nodes.map((n) => [n.id, n]))
    ctx.strokeStyle = "rgba(94, 184, 255, 0.6)"
    ctx.lineWidth = 2
    map.edges.forEach((e) => {
      const a = byId[e.from]
      const b = byId[e.to]
      if (!a || !b) return
      const p0 = worldToScreen(a.x, a.y)
      const p1 = worldToScreen(b.x, b.y)
      ctx.beginPath()
      ctx.moveTo(p0.sx, p0.sy)
      ctx.lineTo(p1.sx, p1.sy)
      ctx.stroke()
    })
    map.nodes.forEach((n) => {
      const p = worldToScreen(n.x, n.y)
      const sel = n.id === selectedId
      ctx.fillStyle = sel ? "rgba(91, 156, 245, 0.9)" : "rgba(240, 180, 41, 0.85)"
      ctx.beginPath()
      ctx.arc(p.sx, p.sy, sel ? 8 : 6, 0, Math.PI * 2)
      ctx.fill()
      ctx.fillStyle = "#e6e9ef"
      ctx.font = "10px ui-monospace, monospace"
      const lab = n.label || n.id
      ctx.fillText(lab, p.sx + 8, p.sy + 3)
    })
    ctx.fillStyle = "rgba(139, 147, 167, 0.8)"
    ctx.font = "10px monospace"
    ctx.fillText(
      `${tool} · wheel zoom · 노드 클릭 선택 / 엣지는 두 노드 순서 클릭`,
      8,
      h - 8,
    )
  }, [map, tool, selectedId])

  useEffect(() => {
    const id = requestAnimationFrame(function loop() {
      redraw()
      requestAnimationFrame(loop)
    })
    return () => cancelAnimationFrame(id)
  }, [redraw])

  useEffect(() => {
    const ro = new ResizeObserver(() => redraw())
    if (wrapRef.current) ro.observe(wrapRef.current)
    return () => ro.disconnect()
  }, [redraw])

  const onCanvasClick = (e) => {
    const p = worldFromEvent(e)
    if (!p) return
    const hit = map.nodes.find(
      (n) => Math.hypot(n.x - p.wx, n.y - p.wy) < 0.55,
    )
    if (tool === "node") {
      const id = `n_${Date.now()}`
      setMap((m) => ({
        ...m,
        nodes: [
          ...m.nodes,
          {
            id,
            x: round2(p.wx),
            y: round2(p.wy),
            label: "",
            role: "waypoint",
          },
        ],
      }))
      setSelectedId(id)
      setStatus(`노드 ${id} 추가`)
      return
    }
    if (tool === "edge") {
      if (!hit) {
        setStatus("엣지: 노드를 클릭하세요")
        return
      }
      if (!edgeFrom) {
        setEdgeFrom(hit.id)
        setStatus(`엣지 시작 ${hit.id} → 다음 노드 클릭`)
        return
      }
      if (edgeFrom === hit.id) {
        setEdgeFrom(null)
        setStatus("같은 노드 — 취소")
        return
      }
      const exists = map.edges.some(
        (x) =>
          (x.from === edgeFrom && x.to === hit.id) ||
          (x.from === hit.id && x.to === edgeFrom),
      )
      if (!exists) {
        setMap((m) => ({
          ...m,
          edges: [...m.edges, { from: edgeFrom, to: hit.id }],
        }))
        setStatus(`엣지 ${edgeFrom} — ${hit.id}`)
      }
      setEdgeFrom(null)
      return
    }
    if (hit) setSelectedId(hit.id)
  }

  const round2 = (x) => Math.round(x * 100) / 100

  const saveMap = async () => {
    setStatus("저장 중…")
    const body = {
      version: map.version || 1,
      name: map.name || "wave_map",
      imageDataUrl: map.imageDataUrl || "",
      imageWidth: map.imageWidth || 0,
      imageHeight: map.imageHeight || 0,
      nodes: map.nodes,
      edges: map.edges,
    }
    const res = await fetch(`${API_BASE}/map/editor/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
    const d = await parseJson(res)
    if (!res.ok) {
      setStatus(d.detail || "저장 실패")
      return
    }
    setStatus(d.message || "저장 완료 (venv/map.json)")
    onSaved?.()
  }

  const onImage = (ev) => {
    const f = ev.target.files?.[0]
    if (!f) return
    const r = new FileReader()
    r.onload = () => {
      const dataUrl = r.result
      const imgEl = new Image()
      imgEl.onload = () => {
        setMap((m) => ({
          ...m,
          imageDataUrl: dataUrl,
          imageWidth: imgEl.naturalWidth,
          imageHeight: imgEl.naturalHeight,
        }))
        setStatus(`도면 로드 ${imgEl.naturalWidth}×${imgEl.naturalHeight}`)
      }
      imgEl.src = dataUrl
    }
    r.readAsDataURL(f)
  }

  const updateNode = (id, patch) => {
    setMap((m) => ({
      ...m,
      nodes: m.nodes.map((n) => (n.id === id ? { ...n, ...patch } : n)),
    }))
  }

  const deleteNode = (id) => {
    setMap((m) => ({
      ...m,
      nodes: m.nodes.filter((n) => n.id !== id),
      edges: m.edges.filter((e) => e.from !== id && e.to !== id),
    }))
    if (selectedId === id) setSelectedId(null)
  }

  return (
    <div className="wave-editor">
      <div className="wave-editor__toolbar panel">
        <h2 className="panel__title">Map Editor</h2>
        <p className="empty-hint">
          FAB 도면 업로드 후 노드·엣지로 주행 그래프를 정의합니다. 저장 시{" "}
          <span className="mono">venv/map.json</span>에 기록되고 시뮬레이터에
          즉시 반영됩니다.
        </p>
        <div className="wave-editor__tools">
          <label className="btn btn--primary">
            도면 업로드
            <input type="file" accept="image/*" hidden onChange={onImage} />
          </label>
          <button
            type="button"
            className={`btn${tool === "select" ? " btn--primary" : ""}`}
            onClick={() => {
              setTool("select")
              setEdgeFrom(null)
            }}
          >
            선택
          </button>
          <button
            type="button"
            className={`btn${tool === "node" ? " btn--primary" : ""}`}
            onClick={() => setTool("node")}
          >
            노드 추가
          </button>
          <button
            type="button"
            className={`btn${tool === "edge" ? " btn--primary" : ""}`}
            onClick={() => {
              setTool("edge")
              setEdgeFrom(null)
            }}
          >
            엣지 (2클릭)
          </button>
          <button type="button" className="btn btn--primary" onClick={saveMap}>
            Save Map
          </button>
        </div>
        {status && <div className="banner banner--success">{status}</div>}
      </div>
      <div className="wave-editor__body">
        <div ref={wrapRef} className="wave-editor__canvas-wrap">
          <canvas
            ref={canvasRef}
            className="robot-map-canvas"
            onClick={onCanvasClick}
            onWheel={(e) => {
              e.preventDefault()
              const c = canvasRef.current
              if (!c) return
              const r = c.getBoundingClientRect()
              const mx = e.clientX - r.left
              const my = e.clientY - r.top
              const w = r.width
              const h = r.height
              const cam = camRef.current
              const wx = (mx - w / 2) / cam.zoom + cam.cx
              const wy = (h / 2 - my) / cam.zoom + cam.cy
              const z = clamp(cam.zoom * (e.deltaY > 0 ? 0.92 : 1.09), 12, 90)
              cam.zoom = z
              cam.cx = wx - (mx - w / 2) / z
              cam.cy = wy - (h / 2 - my) / z
            }}
          />
        </div>
        <aside className="wave-editor__side panel">
          <h3 className="panel__title">노드 속성</h3>
          {selectedId ? (
            (() => {
              const n = map.nodes.find((x) => x.id === selectedId)
              if (!n) return null
              return (
                <div className="form-row form-row--stack">
                  <div className="form-field">
                    <label>ID</label>
                    <input className="mono" readOnly value={n.id} />
                  </div>
                  <div className="form-field">
                    <label>라벨 (스테이션명)</label>
                    <input
                      value={n.label || ""}
                      onChange={(e) =>
                        updateNode(n.id, { label: e.target.value })
                      }
                    />
                  </div>
                  <div className="form-field">
                    <label>역할</label>
                    <select
                      value={n.role || "waypoint"}
                      onChange={(e) =>
                        updateNode(n.id, { role: e.target.value })
                      }
                    >
                      <option value="waypoint">waypoint</option>
                      <option value="station">station</option>
                      <option value="charger">charger</option>
                    </select>
                  </div>
                  <div className="form-field">
                    <label>x (m)</label>
                    <input
                      type="number"
                      step="0.01"
                      value={n.x}
                      onChange={(e) =>
                        updateNode(n.id, { x: parseFloat(e.target.value) || 0 })
                      }
                    />
                  </div>
                  <div className="form-field">
                    <label>y (m)</label>
                    <input
                      type="number"
                      step="0.01"
                      value={n.y}
                      onChange={(e) =>
                        updateNode(n.id, { y: parseFloat(e.target.value) || 0 })
                      }
                    />
                  </div>
                  <button
                    type="button"
                    className="btn btn--danger"
                    onClick={() => deleteNode(n.id)}
                  >
                    노드 삭제
                  </button>
                </div>
              )
            })()
          ) : (
            <p className="empty-hint">캔버스에서 노드를 선택하세요.</p>
          )}
          <h3 className="panel__title" style={{ marginTop: "1.25rem" }}>
            엣지 ({map.edges.length})
          </h3>
          <ul className="wave-editor__edge-list mono">
            {map.edges.map((e, i) => (
              <li key={`${e.from}-${e.to}-${i}`}>
                {e.from} ↔ {e.to}
                <button
                  type="button"
                  className="btn btn--sm btn--danger"
                  style={{ marginLeft: 8 }}
                  onClick={() =>
                    setMap((m) => ({
                      ...m,
                      edges: m.edges.filter((_, j) => j !== i),
                    }))
                  }
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </aside>
      </div>
    </div>
  )
}
