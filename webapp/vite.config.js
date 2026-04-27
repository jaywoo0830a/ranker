import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During dev, the SPA runs on :5173 and the FastAPI server on :8000.
// Vite proxies /api/* to the backend so fetch('/api/jobs') just works
// without CORS configuration. ``ws: true`` extends that to WebSocket
// upgrades — without it, the /logs/stream WS handshake would 502.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
