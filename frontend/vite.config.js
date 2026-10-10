import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发代理：把前端 /api 转发到后端 FastAPI (8000)
//
// ⚠️ 后端没起来时，代理会返回一个**空 body 的 HTTP 500**（http-proxy 对
// ECONNREFUSED 的默认行为），页面上就显示成"服务端错误（HTTP 500）" ——
// 看起来像后端崩了，其实只是"后端没在跑"。api.js 里靠"5xx + 空 body"把这种情况
// 识别出来并给出明确提示（2026-10-09 踩过：点停止时后端已被重启，看到的正是它）。
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
