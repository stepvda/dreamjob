import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// CR-407: React single-page application. The dev server proxies /api to the
// Python backend so the SPA and the API share an origin in development, which
// keeps the session cookie working without CORS exceptions.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
