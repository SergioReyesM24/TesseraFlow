import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

/** Configure the React app and proxy both HTTP and WebSocket traffic in development. */
export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, process.cwd(), '')
  const backendUrl = environment.TESSERAFLOW_BACKEND_URL || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/v1': {
          target: backendUrl,
          changeOrigin: true,
          ws: true,
        },
        '/health': {
          target: backendUrl,
          changeOrigin: true,
        },
      },
    },
  }
})
