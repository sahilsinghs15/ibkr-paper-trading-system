import { execSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

function appVersion(): string {
  const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf-8')) as {
    version?: string
  }
  let sha = 'nogit'
  try {
    sha = execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString()
      .trim()
  } catch {
    // Building outside a git checkout is fine; the version is informational.
  }
  return `${pkg.version ?? '0.0.0'}+${sha}`
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  define: {
    // Reported to the backend as X-Client-App-Version (client-reported, unverified).
    __APP_VERSION__: JSON.stringify(appVersion()),
  },
  server: {
    proxy: {
      '/demo': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
        xfwd: true,
      },
      // xfwd appends the real peer address to X-Forwarded-For so the backend's
      // trusted-proxy resolution (right-most untrusted hop) sees the true client
      // instead of a browser-supplied header.
      '/api/v1': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        xfwd: true,
      },
    },
  },
})
