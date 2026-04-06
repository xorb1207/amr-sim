/** REST 베이스: 개발은 Vite 프록시 /api → 백엔드. 프로덕션은 동일 출처(nginx) 또는 VITE_API_URL. */
export const API_BASE = import.meta.env.DEV
  ? "/api"
  : (import.meta.env.VITE_API_URL ?? "")

/** OpenRMF 플릿 스트림 WebSocket (nginx/Vite에서 /api 제거 후 백엔드 /ws/fleet 으로 프록시) */
export function fleetWebSocketUrl() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws"
  const host = window.location.host
  return `${proto}://${host}/api/ws/fleet`
}
