import { useNotificationStore } from '../store/notificationStore'
import type { ToastNotification } from '../types/systemEvent'

export function NotificationContainer() {
  const toasts = useNotificationStore((s) => s.toasts)
  const removeToast = useNotificationStore((s) => s.removeToast)

  if (toasts.length === 0) {
    return null
  }

  return (
    <div className="notification-container" aria-live="polite" aria-atomic="true">
      {toasts.map((toast) => (
        <NotificationToastCard key={toast.id} toast={toast} onClose={() => removeToast(toast.id)} />
      ))}
    </div>
  )
}

function NotificationToastCard({
  toast,
  onClose,
}: {
  toast: ToastNotification
  onClose: () => void
}) {
  const typeClass =
    toast.kind === 'SERVICE_STARTED' || toast.kind === 'SUCCESS'
      ? 'toast-start'
      : toast.kind === 'SERVICE_STOPPED' || toast.kind === 'ERROR'
        ? 'toast-stop'
        : 'toast-holiday'

  return (
    <div className={`toast-card ${typeClass}`} role="alert">
      <div className="toast-icon" aria-hidden="true">
        {toast.icon}
      </div>
      <div className="toast-content">
        <div className="toast-header">
          <span className="toast-title">{toast.title}</span>
          {toast.timeStr && <span className="toast-time">{toast.timeStr}</span>}
        </div>
        <div className="toast-message">{toast.message}</div>
      </div>
      <button
        type="button"
        className="toast-close"
        onClick={onClose}
        aria-label="Dismiss notification"
      >
        ✕
      </button>
    </div>
  )
}
