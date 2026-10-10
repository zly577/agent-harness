// 仅用于验证的 vite 配置：把 /api 代理到一个**独立端口**的后端（8010），
// 这样可以在不打扰用户正在运行的实例（8000 + 5173）的前提下做真实渲染验证。
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Windows 上不能用 URL.pathname（会变成 /D:/...），必须走 fileURLToPath
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export default defineConfig({
  root,
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8010', changeOrigin: true },
    },
  },
})
