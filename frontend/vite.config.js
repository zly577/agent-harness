import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发代理：把前端 /api 转发到后端 FastAPI (8000)
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
