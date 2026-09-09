import { useNotificationStore } from '../store/notificationStore'

let seq = 0

function formatTime(d: Date): string {
  return d.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** Transient top-right toast. Does not write to the notification-center feed. */
export function showFeedbackToast(
  variant: 'success' | 'error',
  title: string,
  message = '',
): void {
  seq += 1
  const now = new Date()
  useNotificationStore.getState().addToast({
    id: `feedback-${now.getTime()}-${seq}`,
    eventId: -(now.getTime() * 100 + seq),
    kind: variant === 'success' ? 'SUCCESS' : 'ERROR',
    icon: variant === 'success' ? '✓' : '✕',
    title,
    message,
    timeStr: formatTime(now),
  })
}
