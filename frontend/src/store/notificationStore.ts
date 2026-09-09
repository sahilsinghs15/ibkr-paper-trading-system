import { create } from 'zustand'
import type { ToastNotification } from '../types/systemEvent'

const MAX_STACK = 5
const AUTO_DISMISS_MS = 7000

interface NotificationState {
  toasts: ToastNotification[]
  addToast: (toast: ToastNotification) => void
  removeToast: (id: string) => void
  clearAll: () => void
}

export const useNotificationStore = create<NotificationState>((set) => ({
  toasts: [],
  addToast: (toast) => {
    set((state) => {
      // Prevent duplicate toast if same eventId or id already present
      if (state.toasts.some((t) => t.id === toast.id || t.eventId === toast.eventId)) {
        return state
      }
      const updated = [toast, ...state.toasts].slice(0, MAX_STACK)
      return { toasts: updated }
    })

    // Schedule auto-dismiss
    setTimeout(() => {
      set((state) => ({
        toasts: state.toasts.filter((t) => t.id !== toast.id),
      }))
    }, AUTO_DISMISS_MS)
  },
  removeToast: (id) => {
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    }))
  },
  clearAll: () => set({ toasts: [] }),
}))
