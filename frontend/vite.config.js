import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时把 /api 代理到本地后端；生产可把 dist 交给后端一并托管（backend/app/main.py 会自动挂载）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1200,
  },
})
