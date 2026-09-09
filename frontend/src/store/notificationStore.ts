import { create } from 'zustand'
import type {
  NotificationFeedResponse,
  NotificationItem,
  ToastNotification,
} from '../types/systemEvent'

const MAX_TOAST_STACK = 5
const TOAST_AUTO_DISMISS_MS = 7000

interface NotificationState {
  // Toasts (transient top-right)
  toasts: ToastNotification[]
  addToast: (toast: ToastNotification) => void
  removeToast: (id: string) => void
  clearAllToasts: () => void

  // Notification Center History (persistent panel)
  notifications: NotificationItem[]
  unreadCount: number
  totalCount: number
  isPanelOpen: boolean
  isLoading: boolean

  togglePanel: () => void
  closePanel: () => void
  setIsLoading: (loading: boolean) => void
  setFeed: (feed: NotificationFeedResponse) => void
  appendFeed: (feed: NotificationFeedResponse) => void
  markReadOptimistic: (eventId: number) => void
  markAllReadOptimistic: () => void
  addNewNotification: (item: NotificationItem) => void
}

export const useNotificationStore = create<NotificationState>((set) => ({
  toasts: [],
  notifications: [],
  unreadCount: 0,
  totalCount: 0,
  isPanelOpen: false,
  isLoading: false,

  // Toast actions
  addToast: (toast) => {
    set((state) => {
      if (state.toasts.some((t) => t.id === toast.id || t.eventId === toast.eventId)) {
        return state
      }
      const updated = [toast, ...state.toasts].slice(0, MAX_TOAST_STACK)
      return { toasts: updated }
    })

    setTimeout(() => {
      set((state) => ({
        toasts: state.toasts.filter((t) => t.id !== toast.id),
      }))
    }, TOAST_AUTO_DISMISS_MS)
  },

  removeToast: (id) => {
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    }))
  },

  clearAllToasts: () => set({ toasts: [] }),

  // Panel actions
  togglePanel: () => set((state) => ({ isPanelOpen: !state.isPanelOpen })),
  closePanel: () => set({ isPanelOpen: false }),
  setIsLoading: (loading) => set({ isLoading: loading }),

  setFeed: (feed) => {
    set({
      notifications: feed.items,
      unreadCount: feed.unread_count,
      totalCount: feed.total,
    })
  },

  appendFeed: (feed) => {
    set((state) => {
      const existingIds = new Set(state.notifications.map((n) => n.id))
      const fresh = feed.items.filter((n) => !existingIds.has(n.id))
      return {
        notifications: [...state.notifications, ...fresh],
        unreadCount: feed.unread_count,
        totalCount: feed.total,
      }
    })
  },

  markReadOptimistic: (eventId) => {
    set((state) => {
      let changed = false
      const updated = state.notifications.map((n) => {
        if (n.id === eventId && !n.is_read) {
          changed = true
          return { ...n, is_read: true }
        }
        return n
      })
      return {
        notifications: updated,
        unreadCount: changed ? Math.max(0, state.unreadCount - 1) : state.unreadCount,
      }
    })
  },

  markAllReadOptimistic: () => {
    set((state) => ({
      notifications: state.notifications.map((n) => ({ ...n, is_read: true })),
      unreadCount: 0,
    }))
  },

  addNewNotification: (item) => {
    set((state) => {
      if (state.notifications.some((n) => n.id === item.id)) {
        return state
      }
      return {
        notifications: [item, ...state.notifications],
        unreadCount: state.unreadCount + (item.is_read ? 0 : 1),
        totalCount: state.totalCount + 1,
      }
    })
  },
}))
