export function ToastStack({ toasts, onDismiss }) {
  if (toasts.length === 0) return null
  return (
    <div className="toast-stack" role="region" aria-label="알림">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast toast--${t.level || "warning"}`}
          role="alert"
        >
          <div className="toast__title">{t.title}</div>
          {t.body && <div className="toast__body">{t.body}</div>}
          <button
            type="button"
            className="toast__close"
            onClick={() => onDismiss(t.id)}
            aria-label="닫기"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
