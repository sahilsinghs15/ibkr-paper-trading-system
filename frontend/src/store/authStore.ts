import { create } from 'zustand'
import axios from 'axios'
import { APP_VERSION, getClientDeviceId } from '../utils/clientContext'

function clientContextHeaders(): Record<string, string> {
  const deviceId = getClientDeviceId()
  return {
    'X-Client-App-Version': APP_VERSION,
    ...(deviceId ? { 'X-Client-Device-Id': deviceId } : {}),
  }
}

export interface User {
  id: number
  email: string
  role: 'user' | 'admin'
  is_active: boolean
  ibkr_account_id: number | null
  ibkr_account: string | null
}

interface AuthState {
  token: string | null
  user: User | null
  isAuthenticated: boolean
  login: (token: string, user: User) => void
  /**
   * Clear local credentials. With ``revokeServerSession`` the server-side
   * session is ended too, so the token stops working everywhere (audited).
   */
  logout: (options?: { revokeServerSession?: boolean }) => void
  initAuth: () => void
}

const TOKEN_KEY = 'ibkr_trading_jwt_token'
const USER_KEY = 'ibkr_trading_user'

export const useAuthStore = create<AuthState>((set) => ({
  token: localStorage.getItem(TOKEN_KEY),
  user: (() => {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    try {
      return JSON.parse(raw) as User
    } catch {
      return null
    }
  })(),
  isAuthenticated: Boolean(localStorage.getItem(TOKEN_KEY)),

  login: (token: string, user: User) => {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
    axios.defaults.headers.common['Authorization'] = `Bearer ${token}`
    set({ token, user, isAuthenticated: true })
  },

  logout: (options) => {
    const token = localStorage.getItem(TOKEN_KEY)
    if (options?.revokeServerSession && token) {
      // fetch (not axios) so the 401 interceptor can never re-enter logout.
      void fetch('/api/v1/auth/logout', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          ...clientContextHeaders(),
        },
        keepalive: true,
      }).catch(() => undefined)
    }
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    delete axios.defaults.headers.common['Authorization']
    set({ token: null, user: null, isAuthenticated: false })
  },

  initAuth: () => {
    const token = localStorage.getItem(TOKEN_KEY)
    const userRaw = localStorage.getItem(USER_KEY)
    if (token && userRaw) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${token}`
    }
  },
}))

export async function fetchSseToken(): Promise<string | null> {
  const token = localStorage.getItem(TOKEN_KEY)
  if (!token) return null
  try {
    const res = await axios.post<{ sse_token: string }>('/api/v1/auth/sse-token')
    return res.data.sse_token
  } catch (err) {
    console.warn('Failed to fetch short-lived SSE token:', err)
    return null
  }
}

// Setup global Axios interceptor
const initialToken = localStorage.getItem(TOKEN_KEY)
if (initialToken) {
  axios.defaults.headers.common['Authorization'] = `Bearer ${initialToken}`
}

axios.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      if (window.location.pathname !== '/login') {
        useAuthStore.getState().logout()
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  }
)
