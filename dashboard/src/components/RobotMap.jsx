import { useCallback, useEffect, useRef, useState } from "react"

const MAP_PAD = 1.8
const GRID_STEP = 1
const LERP = 0.22

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v))
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

  const cameraRef = useRef({
    cx: 11,
    cy: 7,
    zoom: 42,
  })

  const displayRef = useRef({})
  const rafRef = useRef(null)
  const targetsRef = useRef({})

  const boundsRef = useRef({ minX: 0, maxX: 22, minY: 0, maxY: 16 })

  // 맵 토폴로지(stations, waveMap)가 바뀔 때만 bounds + 카메라 리셋
  useEffect(() => {
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    Object.values(stations).forEach(([x, y]) => {
      minX = Math.min(minX, x)
      minY = Math.min(minY, y)
      maxX = Math.max(maxX, x)
      maxY = Math.max(maxY, y)
    })
    ;(waveMap?.nodes ?? []).forEach((n) => {
      if (typeof n.x === "number" && typeof n.y === "number") {
        minX = Math.min(minX, n.x)
        minY = Math.min(minY, n.y)
        maxX = Math.max(maxX, n.x)
        maxY = Math.max(maxY, n.y)
      }
    })
    if (!Number.isFinite(minX)) {
      minX = 0
      minY = 0
      maxX = 22
      maxY = 16
    }
    boundsRef.current = {
      minX: minX - MAP_PAD,
      minY: minY - MAP_PAD,
      maxX: maxX + MAP_PAD,
      maxY: maxY + MAP_PAD,
    }
    const b = boundsRef.current
    cameraRef.current.cx = (b.minX + b.maxX) / 2
    cameraRef.current.cy = (b.minY + b.maxY) / 2
  }, [stations, waveMap])

  useEffect(() => {
    const url = waveMap?.imageDataUrl
    if (!url || typeof url !== "string") {
      floorImgRef.current = null
      return
    }
    const im = new Image()
    im.onload = () => {
      floorImgRef.current = im
    }
    im.src = url
    return () => {
      floorImgRef.current = null
    }
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
    canvas.style.width = `${w}px`
    canvas.style.height = `${h}px`
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    const { cx, cy, zoom } = cameraRef.current
    const b = boundsRef.current

    ctx.fillStyle = "#0a0c10"
    ctx.fillRect(0, 0, w, h)

    const worldToScreen = (wx, wy) => ({
      sx: (wx - cx) * zoom + w / 2,
      sy: h / 2 - (wy - cy) * zoom,
    })

    const floor = floorImgRef.current
    if (floor && floor.complete && floor.naturalWidth > 0) {
      const tl = worldToScreen(b.minX, b.maxY)
      const br = worldToScreen(b.maxX, b.minY)
      ctx.save()
      ctx.globalAlpha = 0.35
      try {
        ctx.drawImage(floor, tl.sx, tl.sy, br.sx - tl.sx, br.sy - tl.sy)
      } catch {
        /* ignore */
      }
      ctx.restore()
    }

    const nodeById = Object.fromEntries(
      (waveMap?.nodes ?? []).map((n) => [n.id, n]),
    )
    ctx.save()
    ctx.strokeStyle = "rgba(94, 184, 255, 0.55)"
    ctx.lineWidth = Math.max(1.5, zoom * 0.04)
    ;(waveMap?.edges ?? []).forEach((e) => {
      const a = nodeById[e.from]
      const c = nodeById[e.to]
      if (!a || !c) return
      const p0 = worldToScreen(a.x, a.y)
      const p1 = worldToScreen(c.x, c.y)
      ctx.beginPath()
      ctx.moveTo(p0.sx, p0.sy)
      ctx.lineTo(p1.sx, p1.sy)
      ctx.stroke()
    })
    ctx.restore()

    ctx.save()
    ctx.strokeStyle = "rgba(94, 184, 255, 0.12)"
    ctx.lineWidth = 1
    const gx0 = Math.floor(b.minX / GRID_STEP) * GRID_STEP
    const gy0 = Math.floor(b.minY / GRID_STEP) * GRID_STEP
    for (let x = gx0; x <= b.maxX; x += GRID_STEP) {
      const p0 = worldToScreen(x, b.minY)
      const p1 = worldToScreen(x, b.maxY)
      ctx.beginPath()
      ctx.moveTo(p0.sx, p0.sy)
      ctx.lineTo(p1.sx, p1.sy)
      ctx.stroke()
    }
    for (let y = gy0; y <= b.maxY; y += GRID_STEP) {
      const p0 = worldToScreen(b.minX, y)
      const p1 = worldToScreen(b.maxX, y)
      ctx.beginPath()
      ctx.moveTo(p0.sx, p0.sy)
      ctx.lineTo(p1.sx, p1.sy)
      ctx.stroke()
    }

    ctx.strokeStyle = "rgba(91, 156, 245, 0.35)"
    ctx.lineWidth = 1.5
    const tl = worldToScreen(b.minX, b.maxY)
    const br = worldToScreen(b.maxX, b.minY)
    ctx.strokeRect(tl.sx, tl.sy, br.sx - tl.sx, br.sy - tl.sy)

    Object.entries(stations).forEach(([label, coord]) => {
      if (!Array.isArray(coord) || coord.length < 2) return
      const [sx, sy] = coord
      const ov = stationOverlay[label] || {}
      const occupied = Boolean(ov.occupied)
      const charging = Boolean(ov.charging)
      const p = worldToScreen(sx, sy)
      const size = Math.max(7, zoom * 0.22)
      if (charging) {
        ctx.fillStyle = occupied
          ? "rgba(61, 214, 140, 0.18)"
          : "rgba(94, 184, 255, 0.12)"
        ctx.strokeStyle = occupied
          ? "rgba(61, 214, 140, 0.75)"
          : "rgba(94, 184, 255, 0.55)"
      } else {
        ctx.fillStyle = occupied
          ? "rgba(240, 113, 120, 0.2)"
          : "rgba(240, 180, 41, 0.2)"
        ctx.strokeStyle = occupied
          ? "rgba(240, 113, 120, 0.9)"
          : "rgba(240, 180, 41, 0.85)"
      }
      ctx.lineWidth = 1.25
      ctx.beginPath()
      ctx.rect(p.sx - size / 2, p.sy - size / 2, size, size)
      ctx.fill()
      ctx.stroke()
      ctx.fillStyle = "rgba(232, 234, 239, 0.88)"
      ctx.font = "11px ui-monospace, monospace"
      ctx.fillText(label, p.sx + size * 0.55, p.sy + 4)
      if (occupied) {
        ctx.font = "600 9px system-ui, sans-serif"
        ctx.fillStyle = "rgba(240, 113, 120, 0.95)"
        ctx.fillText("Occupied", p.sx - size * 0.45, p.sy - size * 0.85)
        if (ov.by) {
          ctx.font = "8px ui-monospace, monospace"
          ctx.fillStyle = "rgba(232, 234, 239, 0.75)"
          ctx.fillText(String(ov.by), p.sx - size * 0.45, p.sy - size * 1.35)
        }
      } else if (charging) {
        ctx.font = "600 8px system-ui, sans-serif"
        ctx.fillStyle = "rgba(94, 184, 255, 0.85)"
        ctx.fillText("Charge", p.sx - size * 0.45, p.sy - size * 0.85)
      }
    })

    const disp = displayRef.current
    const tgt = targetsRef.current
    for (const name of Object.keys(tgt)) {
      if (!disp[name]) disp[name] = { ...tgt[name] }
      const d = disp[name]
      const t = tgt[name]
      d.x += (t.x - d.x) * LERP
      d.y += (t.y - d.y) * LERP
      let dy = t.yaw - d.yaw
      while (dy > Math.PI) dy -= 2 * Math.PI
      while (dy < -Math.PI) dy += 2 * Math.PI
      d.yaw += dy * LERP
    }

    robots.forEach((r) => {
      const name = r.name || r.id
      const d = displayRef.current[name]
      if (!d) return
      const p = worldToScreen(d.x, d.y)
      const bat = Number(r.battery_percent ?? r.battery ?? 0)
      const critical = bat < 10
      const low = bat < 20
      ctx.fillStyle = critical
        ? "rgba(220, 38, 38, 0.98)"
        : low
          ? "rgba(240, 113, 120, 0.95)"
          : "rgba(91, 156, 245, 0.95)"
      ctx.strokeStyle = "rgba(232, 234, 239, 0.9)"
      ctx.lineWidth = 1.5
      const rad = Math.max(6, zoom * 0.18)
      ctx.beginPath()
      ctx.arc(p.sx, p.sy, rad, 0, Math.PI * 2)
      ctx.fill()
      ctx.stroke()

      ctx.save()
      ctx.translate(p.sx, p.sy)
      ctx.rotate(-d.yaw)
      ctx.fillStyle = "rgba(232, 234, 239, 0.95)"
      ctx.beginPath()
      ctx.moveTo(rad * 1.1, 0)
      ctx.lineTo(-rad * 0.45, rad * 0.55)
      ctx.lineTo(-rad * 0.45, -rad * 0.55)
      ctx.closePath()
      ctx.fill()
      ctx.restore()

      ctx.fillStyle = "rgba(232, 234, 239, 0.9)"
      ctx.font = "600 11px system-ui, sans-serif"
      ctx.fillText(String(name), p.sx + rad + 4, p.sy - rad - 2)
      ctx.font = "10px ui-monospace, monospace"
      ctx.fillStyle = critical
        ? "rgba(248, 113, 113, 0.98)"
        : low
          ? "rgba(240, 113, 120, 0.95)"
          : "rgba(139, 147, 167, 0.95)"
      const battLabel = critical ? "Low Batt" : `${bat.toFixed(0)}%`
      ctx.fillText(`${battLabel} · ${r.mode || ""}`, p.sx + rad + 4, p.sy + 8)
    })

    ctx.restore()

    ctx.fillStyle = "rgba(139, 147, 167, 0.75)"
    ctx.font = "10px ui-monospace, monospace"
    ctx.fillText(
        `WAVE · graph lanes · zoom ${cameraRef.current.zoom.toFixed(1)} px/m`,
      10,
      h - 10,
    )
  }, [stations, robots, stationOverlay, waveMap])

  useEffect(() => {
    const loop = () => {
      draw()
      rafRef.current = requestAnimationFrame(loop)
    }
    rafRef.current = requestAnimationFrame(loop)
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [draw])

  useEffect(() => {
    const ro = new ResizeObserver(() => draw())
    if (wrapRef.current) ro.observe(wrapRef.current)
    return () => ro.disconnect()
  }, [draw])

  const onWheel = (e) => {
    e.preventDefault()
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    const w = rect.width
    const h = rect.height
    const cam = cameraRef.current
    const wx = (mx - w / 2) / cam.zoom + cam.cx
    const wy = (h / 2 - my) / cam.zoom + cam.cy
    const factor = e.deltaY > 0 ? 0.9 : 1.11
    const z = clamp(cam.zoom * factor, 18, 140)
    cam.zoom = z
    cam.cx = wx - (mx - w / 2) / z
    cam.cy = wy - (h / 2 - my) / z
  }

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
      const dx = e.clientX - d.x
      const dy = e.clientY - d.y
      cam.cx = d.cx - dx / cam.zoom
      cam.cy = d.cy + dy / cam.zoom
      setHoverTip(null)
      return
    }

    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    const w = rect.width
    const h = rect.height
    const { cx, cy, zoom } = cameraRef.current
    const wx = (mx - w / 2) / zoom + cx
    const wy = (h / 2 - my) / zoom + cy
    const hitR = 0.55
    let best = null
    let bestD = hitR
    for (const r of robots) {
      const lx = r.location?.x ?? r.x
      const ly = r.location?.y ?? r.y
      if (typeof lx !== "number" || typeof ly !== "number") continue
      const d0 = Math.hypot(wx - lx, wy - ly)
      if (d0 < bestD) {
        bestD = d0
        const bat = Number(r.battery_percent ?? r.battery ?? 0)
        best = {
          x: e.clientX,
          y: e.clientY,
          text:
            bat < 10
              ? `Low Batt · ${(r.name || r.id) ?? "?"} (${bat.toFixed(1)}%)`
              : `${(r.name || r.id) ?? "?"} · ${bat.toFixed(0)}%`,
        }
      }
    }
    setHoverTip(best)
  }

  const onPointerUp = (e) => {
    dragRef.current = null
    setHoverTip(null)
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch {
      /* noop */
    }
  }

  return (
    <div ref={wrapRef} className={`robot-map-wrap ${className}`.trim()}>
      <div className="robot-map__chrome">
        <span className="robot-map__label">WAVE Fleet Map</span>
        <span className="robot-map__sub">A* 경로 · 에디터 그래프 · 실시간 플릿</span>
      </div>
      <canvas
        ref={canvasRef}
        className="robot-map-canvas"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => {
          dragRef.current = null
          setHoverTip(null)
        }}
      />
      {hoverTip && (
        <div
          className="robot-map-tooltip"
          style={{ left: hoverTip.x + 12, top: hoverTip.y + 12 }}
        >
          {hoverTip.text}
        </div>
      )}
    </div>
  )
}
