import { useCallback, useEffect, useRef, useState } from "react"

const MAP_PAD = 1.8
const GRID_STEP = 10
const LERP = 0.22

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v))
}

function calcFitZoom(boundsW, boundsH, canvasW, canvasH) {
  const zx = canvasW / boundsW
  const zy = canvasH / boundsH
  return Math.min(zx, zy) * 0.88
}

// ACS 상태별 색상 정의
function getRobotColor(mode, status, bat) {
  const s = (mode || status || "").toLowerCase()
  if (s.includes("error") || s.includes("unresponsive")) return { fill: "rgba(220,38,38,0.95)", stroke: "rgba(255,100,100,0.9)", label: "#f87171" }
  if (s.includes("charg"))  return { fill: "rgba(34,197,94,0.90)",  stroke: "rgba(74,222,128,0.9)", label: "#4ade80" }
  if (s.includes("mov") || s.includes("running") || s.includes("busy")) return { fill: "rgba(59,130,246,0.95)", stroke: "rgba(147,197,253,0.9)", label: "#93c5fd" }
  if (s.includes("pending")) return { fill: "rgba(234,179,8,0.90)", stroke: "rgba(253,224,71,0.9)",  label: "#fde047" }
  // idle
  return { fill: "rgba(100,116,139,0.85)", stroke: "rgba(148,163,184,0.8)", label: "#94a3b8" }
}

export default function RobotMap({
  stations = {},
  robots = [],
  stationOverlay = {},
  waveMap = null,
  className = "",
}) {
  const canvasRef = useRef(null)
  const wrapRef = useRef(null)
  const [hoverTip, setHoverTip] = useState(null)
  const floorImgRef = useRef(null)

  const cameraRef = useRef({ cx: 320, cy: 60, zoom: 1 })
  const initializedRef = useRef(false)
  const displayRef = useRef({})
  const rafRef = useRef(null)
  const targetsRef = useRef({})
  const minZoomRef = useRef(0.3)
  const boundsRef = useRef({ minX: 0, maxX: 640, minY: 0, maxY: 120 })

  const robotsRef = useRef(robots)
  useEffect(() => { robotsRef.current = robots }, [robots])

  useEffect(() => {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    Object.values(stations).forEach(([x, y]) => {
      minX = Math.min(minX, x); minY = Math.min(minY, y)
      maxX = Math.max(maxX, x); maxY = Math.max(maxY, y)
    })
    ;(waveMap?.nodes ?? []).forEach((n) => {
      if (typeof n.x === "number" && typeof n.y === "number") {
        minX = Math.min(minX, n.x); minY = Math.min(minY, n.y)
        maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y)
      }
    })
    if (!Number.isFinite(minX)) { minX = 0; minY = 0; maxX = 640; maxY = 120 }
    const padded = {
      minX: minX - MAP_PAD, minY: minY - MAP_PAD,
      maxX: maxX + MAP_PAD, maxY: maxY + MAP_PAD,
    }
    boundsRef.current = padded
    const boundsW = padded.maxX - padded.minX
    const boundsH = padded.maxY - padded.minY
    const wrap = wrapRef.current
    const canvasW = wrap ? wrap.clientWidth : 800
    const canvasH = Math.max(320, Math.min(560, Math.round(canvasW * 0.52)))
    const fitZoom = calcFitZoom(boundsW, boundsH, canvasW, canvasH)
    minZoomRef.current = Math.max(0.3, fitZoom * 0.5)
    if (!initializedRef.current) {
      cameraRef.current.zoom = fitZoom
      cameraRef.current.cx = (padded.minX + padded.maxX) / 2
      cameraRef.current.cy = (padded.minY + padded.maxY) / 2
      initializedRef.current = true
    }
  }, [stations, waveMap])

  useEffect(() => {
    const url = waveMap?.imageDataUrl
    if (!url || typeof url !== "string") { floorImgRef.current = null; return }
    const im = new Image()
    im.onload = () => { floorImgRef.current = im }
    im.src = url
    return () => { floorImgRef.current = null }
  }, [waveMap?.imageDataUrl])

  useEffect(() => {
    const next = {}
    for (const r of robots) {
      const name = r.name || r.id
      const lx = r.location?.x ?? r.x
      const ly = r.location?.y ?? r.y
      const yaw = r.location?.yaw ?? r.yaw ?? 0
      if (typeof lx !== "number" || typeof ly !== "number") continue
      next[name] = { x: lx, y: ly, yaw }
    }
    targetsRef.current = next
    const disp = displayRef.current
    for (const name of Object.keys(next)) {
      if (!disp[name]) disp[name] = { ...next[name] }
    }
  }, [robots])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const dpr = window.devicePixelRatio || 1
    const w = wrap.clientWidth
    const h = Math.max(320, Math.min(560, Math.round(w * 0.52)))
    canvas.width = Math.floor(w * dpr)
    canvas.height = Math.floor(h * dpr)
    canvas.style.width = w + "px"
    canvas.style.height = h + "px"
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    const { cx, cy, zoom } = cameraRef.current
    const b = boundsRef.current
    ctx.fillStyle = "#0a0c10"
    ctx.fillRect(0, 0, w, h)
    const worldToScreen = (wx, wy) => ({
      sx: (wx - cx) * zoom + w / 2,
      sy: h / 2 - (wy - cy) * zoom,
    })

    // 배경 이미지
    const floor = floorImgRef.current
    if (floor && floor.complete && floor.naturalWidth > 0) {
      const tl = worldToScreen(b.minX, b.maxY)
      const br = worldToScreen(b.maxX, b.minY)
      ctx.save(); ctx.globalAlpha = 0.35
      try { ctx.drawImage(floor, tl.sx, tl.sy, br.sx - tl.sx, br.sy - tl.sy) } catch {}
      ctx.restore()
    }

    // 그래프 엣지
    const nodeById = Object.fromEntries((waveMap?.nodes ?? []).map((n) => [n.id, n]))
    ctx.save()
    ctx.strokeStyle = "rgba(94,184,255,0.35)"
    ctx.lineWidth = Math.max(0.5, zoom * 0.5)
    ;(waveMap?.edges ?? []).forEach((e) => {
      const a = nodeById[e.from]; const c = nodeById[e.to]
      if (!a || !c) return
      const p0 = worldToScreen(a.x, a.y); const p1 = worldToScreen(c.x, c.y)
      ctx.beginPath(); ctx.moveTo(p0.sx, p0.sy); ctx.lineTo(p1.sx, p1.sy); ctx.stroke()
    })
    ctx.restore()

    // 그리드
    ctx.save()
    ctx.strokeStyle = "rgba(94,184,255,0.06)"
    ctx.lineWidth = 0.5
    const gx0 = Math.floor(b.minX / GRID_STEP) * GRID_STEP
    const gy0 = Math.floor(b.minY / GRID_STEP) * GRID_STEP
    for (let x = gx0; x <= b.maxX; x += GRID_STEP) {
      const p0 = worldToScreen(x, b.minY); const p1 = worldToScreen(x, b.maxY)
      ctx.beginPath(); ctx.moveTo(p0.sx, p0.sy); ctx.lineTo(p1.sx, p1.sy); ctx.stroke()
    }
    for (let y = gy0; y <= b.maxY; y += GRID_STEP) {
      const p0 = worldToScreen(b.minX, y); const p1 = worldToScreen(b.maxX, y)
      ctx.beginPath(); ctx.moveTo(p0.sx, p0.sy); ctx.lineTo(p1.sx, p1.sy); ctx.stroke()
    }
    ctx.strokeStyle = "rgba(91,156,245,0.2)"
    ctx.lineWidth = 1
    const tl2 = worldToScreen(b.minX, b.maxY)
    const br2 = worldToScreen(b.maxX, b.minY)
    ctx.strokeRect(tl2.sx, tl2.sy, br2.sx - tl2.sx, br2.sy - tl2.sy)
    ctx.restore()

    // 스테이션
    Object.entries(stations).forEach(([label, coord]) => {
      if (!Array.isArray(coord) || coord.length < 2) return
      const [sx, sy] = coord
      const ov = stationOverlay[label] || {}
      const occupied = Boolean(ov.occupied)
      const charging = Boolean(ov.charging)
      const p = worldToScreen(sx, sy)
      const size = Math.max(3, zoom * 6)
      if (charging) {
        ctx.fillStyle = occupied ? "rgba(34,197,94,0.18)" : "rgba(94,184,255,0.10)"
        ctx.strokeStyle = occupied ? "rgba(34,197,94,0.75)" : "rgba(94,184,255,0.5)"
      } else {
        ctx.fillStyle = occupied ? "rgba(240,113,120,0.18)" : "rgba(240,180,41,0.12)"
        ctx.strokeStyle = occupied ? "rgba(240,113,120,0.85)" : "rgba(240,180,41,0.7)"
      }
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.rect(p.sx - size / 2, p.sy - size / 2, size, size)
      ctx.fill(); ctx.stroke()
      if (zoom > 0.6) {
        ctx.fillStyle = "rgba(232,234,239,0.82)"
        ctx.font = clamp(zoom * 8, 8, 11) + "px ui-monospace,monospace"
        ctx.fillText(label, p.sx + size * 0.6, p.sy + 3)
      }
      if (zoom > 1.2 && occupied && ov.by) {
        ctx.font = "7px ui-monospace,monospace"
        ctx.fillStyle = "rgba(232,234,239,0.65)"
        ctx.fillText(String(ov.by), p.sx - size * 0.4, p.sy - size * 1.2)
      }
    })

    // 로봇 lerp 업데이트
    const disp = displayRef.current
    const tgt = targetsRef.current
    for (const name of Object.keys(tgt)) {
      if (!disp[name]) disp[name] = { ...tgt[name] }
      const d = disp[name]; const t = tgt[name]
      d.x += (t.x - d.x) * LERP; d.y += (t.y - d.y) * LERP
      let dy = t.yaw - d.yaw
      while (dy > Math.PI) dy -= 2 * Math.PI
      while (dy < -Math.PI) dy += 2 * Math.PI
      d.yaw += dy * LERP
    }

    // 로봇 렌더링 — ACS 상태별 색상
    robotsRef.current.forEach((r) => {
      const name = r.name || r.id
      const d = displayRef.current[name]
      if (!d) return
      const p = worldToScreen(d.x, d.y)
      const bat = Number(r.battery_percent ?? r.battery ?? 0)
      const mode = r.mode || r.acs_state || r.status || ""
      const { fill, stroke, label: labelColor } = getRobotColor(mode, r.status, bat)
      const rad = Math.max(4, zoom * 4)
      const lowBat = bat < 40  // 운영 하한 접근

      // 저배터리 경고 링 (배터리 상태만 별도 표시)
      if (lowBat) {
        ctx.beginPath()
        ctx.arc(p.sx, p.sy, rad + 3, 0, Math.PI * 2)
        ctx.strokeStyle = bat < 35 ? "rgba(220,38,38,0.9)" : "rgba(250,204,21,0.8)"
        ctx.lineWidth = 1.5
        ctx.setLineDash([3, 2])
        ctx.stroke()
        ctx.setLineDash([])
      }

      // 본체 원
      ctx.fillStyle = fill
      ctx.strokeStyle = stroke
      ctx.lineWidth = 1.5
      ctx.beginPath(); ctx.arc(p.sx, p.sy, rad, 0, Math.PI * 2)
      ctx.fill(); ctx.stroke()

      // 진행 방향 화살표
      ctx.save()
      ctx.translate(p.sx, p.sy); ctx.rotate(-d.yaw)
      ctx.fillStyle = "rgba(255,255,255,0.85)"
      ctx.beginPath()
      ctx.moveTo(rad * 1.1, 0)
      ctx.lineTo(-rad * 0.4, rad * 0.5)
      ctx.lineTo(-rad * 0.4, -rad * 0.5)
      ctx.closePath(); ctx.fill()
      ctx.restore()

      // 레이블
      if (zoom > 0.4) {
        ctx.fillStyle = "rgba(232,234,239,0.92)"
        ctx.font = "600 " + clamp(zoom * 6, 9, 11) + "px system-ui,sans-serif"
        ctx.fillText(String(name), p.sx + rad + 3, p.sy - 2)

        // ACS 상태 텍스트
        ctx.font = clamp(zoom * 5, 8, 10) + "px ui-monospace,monospace"
        ctx.fillStyle = labelColor
        const stateText = mode.replace("MODE_", "").toLowerCase()
        ctx.fillText(stateText + " · " + bat.toFixed(0) + "%", p.sx + rad + 3, p.sy + 10)
      }
    })

    // 범례
    if (zoom > 0.3) {
      const legend = [
        { label: "idle",     color: "rgba(100,116,139,0.85)" },
        { label: "moving",   color: "rgba(59,130,246,0.95)"  },
        { label: "charging", color: "rgba(34,197,94,0.90)"   },
        { label: "pending",  color: "rgba(234,179,8,0.90)"   },
        { label: "error",    color: "rgba(220,38,38,0.95)"   },
      ]
      let lx = 10; const ly = 20
      ctx.font = "10px ui-monospace,monospace"
      legend.forEach(({ label, color }) => {
        ctx.fillStyle = color
        ctx.beginPath(); ctx.arc(lx + 5, ly, 5, 0, Math.PI * 2); ctx.fill()
        ctx.fillStyle = "rgba(200,210,220,0.75)"
        ctx.fillText(label, lx + 13, ly + 4)
        lx += label.length * 6 + 22
      })
    }

    // HUD
    ctx.fillStyle = "rgba(139,147,167,0.5)"
    ctx.font = "10px ui-monospace,monospace"
    ctx.fillText(
      "WAVE · " + Object.keys(stations).length + " stations · zoom " + cameraRef.current.zoom.toFixed(2) + "x · 더블클릭: 전체보기",
      10, h - 10,
    )
  }, [stations, stationOverlay, waveMap])

  useEffect(() => {
    const loop = () => { draw(); rafRef.current = requestAnimationFrame(loop) }
    rafRef.current = requestAnimationFrame(loop)
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current) }
  }, [draw])

  useEffect(() => {
    const ro = new ResizeObserver(() => draw())
    if (wrapRef.current) ro.observe(wrapRef.current)
    return () => ro.disconnect()
  }, [draw])

  // non-passive wheel 이벤트 — 페이지 스크롤과 분리
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const handleWheel = (e) => {
      e.preventDefault()
      e.stopPropagation()
      const rect = canvas.getBoundingClientRect()
      const mx = e.clientX - rect.left; const my = e.clientY - rect.top
      const w = rect.width; const h = rect.height
      const cam = cameraRef.current
      const wx = (mx - w / 2) / cam.zoom + cam.cx
      const wy = (h / 2 - my) / cam.zoom + cam.cy
      const factor = e.deltaY > 0 ? 0.9 : 1.11
      const z = clamp(cam.zoom * factor, minZoomRef.current, 20)
      cam.zoom = z
      cam.cx = wx - (mx - w / 2) / z
      cam.cy = wy - (h / 2 - my) / z
    }
    canvas.addEventListener("wheel", handleWheel, { passive: false })
    return () => canvas.removeEventListener("wheel", handleWheel)
  }, [])

  const dragRef = useRef(null)

  const onPointerDown = (e) => {
    if (e.button !== 0) return
    dragRef.current = { x: e.clientX, y: e.clientY, cx: cameraRef.current.cx, cy: cameraRef.current.cy }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const onPointerMove = (e) => {
    const d = dragRef.current
    if (d) {
      const cam = cameraRef.current
      cam.cx = d.cx - (e.clientX - d.x) / cam.zoom
      cam.cy = d.cy + (e.clientY - d.y) / cam.zoom
      setHoverTip(null); return
    }
    const canvas = canvasRef.current; if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mx = e.clientX - rect.left; const my = e.clientY - rect.top
    const w = rect.width; const h = rect.height
    const { cx, cy, zoom } = cameraRef.current
    const wx = (mx - w / 2) / zoom + cx
    const wy = (h / 2 - my) / zoom + cy
    let best = null; let bestD = 5.0
    for (const r of robotsRef.current) {
      const lx = r.location?.x ?? r.x; const ly = r.location?.y ?? r.y
      if (typeof lx !== "number" || typeof ly !== "number") continue
      const d0 = Math.hypot(wx - lx, wy - ly)
      if (d0 < bestD) {
        bestD = d0
        const bat = Number(r.battery_percent ?? r.battery ?? 0)
        const mode = (r.mode || r.acs_state || r.status || "").replace("MODE_", "").toLowerCase()
        best = {
          x: e.clientX, y: e.clientY,
          text: (r.name || r.id || "?") + " | " + mode + " | 배터리 " + bat.toFixed(0) + "%",
        }
      }
    }
    setHoverTip(best)
  }

  const onPointerUp = (e) => {
    dragRef.current = null; setHoverTip(null)
    try { e.currentTarget.releasePointerCapture(e.pointerId) } catch {}
  }

  const onDoubleClick = () => {
    const b = boundsRef.current
    const wrap = wrapRef.current; if (!wrap) return
    const canvasW = wrap.clientWidth
    const canvasH = Math.max(320, Math.min(560, Math.round(canvasW * 0.52)))
    const fitZoom = calcFitZoom(b.maxX - b.minX, b.maxY - b.minY, canvasW, canvasH)
    cameraRef.current.zoom = fitZoom
    cameraRef.current.cx = (b.minX + b.maxX) / 2
    cameraRef.current.cy = (b.minY + b.maxY) / 2
  }

  return (
    <div ref={wrapRef} className={"robot-map-wrap " + className}>
      <div className="robot-map__chrome">
        <span className="robot-map__label">WAVE Fleet Map</span>
        <span className="robot-map__sub">A* 경로 · 실시간 플릿 · 더블클릭: 전체 보기</span>
      </div>
      <canvas
        ref={canvasRef}
        className="robot-map-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onDoubleClick={onDoubleClick}
        onPointerLeave={() => { dragRef.current = null; setHoverTip(null) }}
      />
      {hoverTip && (
        <div className="robot-map-tooltip" style={{ left: hoverTip.x + 12, top: hoverTip.y + 12 }}>
          {hoverTip.text}
        </div>
      )}
    </div>
  )
}