import { useCallback, useState } from "react"

let _id = 0

export function useToast(durationMs = 6500) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const push = useCallback(
    (toast) => {
      const id = ++_id
      const item = { id, ...toast }
      setToasts((t) => [...t, item])
      window.setTimeout(() => {
        setToasts((t) => t.filter((x) => x.id !== id))
      }, durationMs)
      return id
    },
    [durationMs],
  )

  return { toasts, push, dismiss }
}
