// 只做「能不能编译通过」的构建检查 —— 输出到 scratch 目录，且**不清空**输出目录。
//
// 为什么不直接 `vite build`：它启动时会 `emptyDir(outDir)` → `fs.rmSync(frontend/dist)`，
// 而 dist 里有 60+ 个文件，会撞上 WorkBuddy 的 safe-delete 批量删除守卫
// （`SAFE_DELETE_BULK_CONFIRM_REQUIRED`；把守卫环境变量 unset 又会变成
// `SAFE_DELETE_BULK_GUARD_ERROR: helper-unavailable`，更糟）。
// 本配置**完全不碰 frontend/dist**，产物落在 `_probe/tmp/dist_check/`（scratch，gitignore）。
//
// 用法（在 frontend/ 下）：
//   node node_modules/vite/bin/vite.js build --config _verify/vite.build.check.mjs
//
// ⚠️ 配置文件必须留在 frontend/ 内（本目录）：`import 'vite'` 是按**配置文件所在位置**
// 解析的，放到仓库根的 `_probe/` 下会因为找不到 node_modules 直接 ERR_MODULE_NOT_FOUND。
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export default defineConfig({
  root: frontend,
  plugins: [vue()],
  build: {
    outDir: path.resolve(frontend, '..', '_probe', 'tmp', 'dist_check'),
    emptyOutDir: false,
    reportCompressedSize: false,
  },
})
