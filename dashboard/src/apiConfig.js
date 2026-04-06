/** REST 베이스 */
export const API_BASE = import.meta.env.DEV
  ? "/api"
  : (import.meta.env.VITE_API_URL ?? "")

/** OpenRMF 플릿 스트림 WebSocket */
export function fleetWebSocketUrl() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws"
  const host = window.location.host
  return `${proto}://${host}/ws/fleet`
}