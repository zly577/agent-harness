// test_sidebar.mjs — 侧边栏拖拽逻辑的单元验证
// 直接从 App.vue 源码里抽出真实实现（不复制代码），在受控上下文里跑边界用例。
import fs from 'node:fs'
import vm from 'node:vm'

const SRC = 'D:/Deepseek_Harness/agent-harness/frontend/src/App.vue'
const src = fs.readFileSync(SRC, 'utf8')

const problems = []
const ok = (cond, label, detail = '') => {
  if (cond) console.log(`  ✅ ${label}`)
  else { console.log(`  ❌ ${label}${detail ? ' — ' + detail : ''}`); problems.push(label) }
}

// ---------- 抽取实现 ----------
const must = (re, label) => {
  const m = src.match(re)
  if (!m) throw new Error(`抽取失败：${label}`)
  return m[0]
}

const consts = ['SIDEBAR_MIN', 'SIDEBAR_DEFAULT', 'SIDEBAR_MAX', 'SIDEBAR_KEEP_MAIN'].map((name) =>
  must(new RegExp(`const ${name} = \\d+`), name),
)
const keyConst = must(/const SIDEBAR_KEY = '[^']*'/, 'SIDEBAR_KEY')
const maxFn = must(/function maxSidebarWidth\(\) \{[\s\S]*?\n    \}/, 'maxSidebarWidth')
const clampFn = must(/function clampSidebar\(w\) \{[\s\S]*?\n    \}/, 'clampSidebar')
// readStoredWidth 依赖 window.localStorage，用桩替换掉
const readFn = must(/function readStoredWidth\(\) \{[\s\S]*?\n    \}/, 'readStoredWidth')
const nudgeRe = /function nudgeSidebar\(delta\) \{[\s\S]*?\n    \}/
const nudgeFn = must(nudgeRe, 'nudgeSidebar')

const localStore = new Map()
const sandbox = {
  window: {
    innerWidth: 1440,
    localStorage: {
      getItem: (k) => (localStore.has(k) ? localStore.get(k) : null),
      setItem: (k, v) => localStore.set(k, v),
    },
  },
  stubbedRead: readFn, // 让 readStoredWidth 体内的递归名有定义（不会被调用）
  stored: null,
  sidebarWidth: { value: 260 },
  persistSidebarWidth: () => {},
  Number,
  Math,
}
vm.createContext(sandbox)
// readStoredWidth 内部调用 clampSidebar / localStorage，保持原样最真实
vm.runInContext(
  ['"use strict"', ...consts, keyConst, maxFn, clampFn, readFn, nudgeFn, 'this.API = { clampSidebar, maxSidebarWidth, readStoredWidth, nudgeSidebar, SIDEBAR_MIN, SIDEBAR_DEFAULT, SIDEBAR_MAX, SIDEBAR_KEEP_MAIN, SIDEBAR_KEY }'].join('\n'),
  sandbox,
)
const API = sandbox.API

console.log('================ 侧边栏拖拽逻辑 ================')
console.log(`常量：MIN=${API.SIDEBAR_MIN} DEFAULT=${API.SIDEBAR_DEFAULT} MAX=${API.SIDEBAR_MAX} KEEP_MAIN=${API.SIDEBAR_KEEP_MAIN}`)
console.log('')

// ---------- 1. 夹取边界 ----------
console.log('[1] 宽度夹取 clampSidebar')
sandbox.window.innerWidth = 1440
sandbox.sidebarWidth.value = 300
ok(API.clampSidebar(300) === 300, '常规值原样通过')
ok(API.clampSidebar(10) === API.SIDEBAR_MIN, `过小值收到下限 ${API.SIDEBAR_MIN}`)
ok(API.clampSidebar(9999) === API.SIDEBAR_MAX, `过大值收到上限 ${API.SIDEBAR_MAX}`)
ok(API.clampSidebar(300.6) === 301, '四舍五入为整数像素')
ok(API.clampSidebar('320') === 320, '字符串数字可解析')
ok(API.clampSidebar('abc') === 300, '非法输入保持当前宽度不变（不跳回默认值）')
ok(API.clampSidebar(undefined) === 300, 'undefined 保持当前宽度不变')
ok(API.clampSidebar(NaN) === 300, 'NaN 保持当前宽度不变')
ok(API.clampSidebar(Infinity) === 300, 'Infinity 保持当前宽度不变（拖到屏幕外不跳变）')
ok(API.clampSidebar(-Infinity) === 300, '-Infinity 保持当前宽度不变')
ok(API.clampSidebar(200) === 200 && API.clampSidebar(460) === 460, '上下限本身可取到')

// ---------- 2. 窄窗口保护 ----------
console.log('\n[2] 窄窗口下必须给主区留空间')
const checkInvariant = (w) => {
  sandbox.window.innerWidth = w
  const clamped = API.clampSidebar(9999) // 用户拖到最宽
  const reserved = w - clamped
  return { w, clamped, reserved, inv: clamped <= API.SIDEBAR_MAX && clamped >= API.SIDEBAR_MIN && reserved >= Math.min(API.SIDEBAR_KEEP_MAIN, w - API.SIDEBAR_MIN) - 1 }
}
for (const w of [1440, 1000, 900, 800, 700, 620, 400]) {
  const r = checkInvariant(w)
  ok(r.inv, `窗口 ${w}px → 最大宽度 ${r.clamped}px，主区至少 ${r.reserved}px`)
}
sandbox.window.innerWidth = 1000
ok(API.maxSidebarWidth() === API.SIDEBAR_MAX, `宽窗口上限即 SIDEBAR_MAX=${API.SIDEBAR_MAX}`)
sandbox.window.innerWidth = 700
ok(API.maxSidebarWidth() === 700 - API.SIDEBAR_KEEP_MAIN, `窗口 700px 时上限压到 700-${API.SIDEBAR_KEEP_MAIN}=${700 - API.SIDEBAR_KEEP_MAIN}px`)
sandbox.window.innerWidth = 300
ok(API.maxSidebarWidth() === API.SIDEBAR_MIN, `极窄窗口 300px 时上限压到下限 ${API.SIDEBAR_MIN}（不会出现负值）`)
sandbox.window.innerWidth = undefined
ok(API.maxSidebarWidth() === API.SIDEBAR_MAX, '窗口宽度取不到时回落到安全兜底，上限仍受 SIDEBAR_MAX 约束')
ok(API.clampSidebar(9999) <= API.SIDEBAR_MAX, '兜底路径下依然不会超出上限')
sandbox.window.innerWidth = 1440
sandbox.window.innerWidth = 1440

// ---------- 3. localStorage 持久化 ----------
console.log('\n[3] 宽度持久化 readStoredWidth')
sandbox.window.innerWidth = 1440
localStore.clear()
ok(API.readStoredWidth() === API.SIDEBAR_DEFAULT, '无存储时用默认宽度')
localStore.set(API.SIDEBAR_KEY, '380')
ok(API.readStoredWidth() === 380, '读回存过的宽度')
localStore.set(API.SIDEBAR_KEY, '99999')
ok(API.readStoredWidth() === API.SIDEBAR_MAX, '存储里的越界值也会被夹取')
localStore.set(API.SIDEBAR_KEY, 'oops')
ok(API.readStoredWidth() === API.SIDEBAR_DEFAULT, '存储损坏时回落到默认值')

// ---------- 4. 键盘微调 ----------
console.log('\n[4] 键盘 ←/→ 微调 nudgeSidebar')
sandbox.window.innerWidth = 1440
sandbox.sidebarWidth.value = 300
API.nudgeSidebar(16)
ok(sandbox.sidebarWidth.value === 316, '向右 +16')
API.nudgeSidebar(-16)
ok(sandbox.sidebarWidth.value === 300, '向左 -16 回到原位')
API.nudgeSidebar(-10000)
ok(sandbox.sidebarWidth.value === API.SIDEBAR_MIN, '一直向左不会突破下限')
API.nudgeSidebar(10000)
ok(sandbox.sidebarWidth.value === API.SIDEBAR_MAX, '一直向右不会突破上限')

// ---------- 5. 模板与样式的接线 ----------
console.log('\n[5] 模板 / 样式接线')
const tpl = src.slice(src.indexOf('<template>'), src.indexOf('</template>'))
ok(/ref="sidebarEl"/.test(tpl), '侧栏元素挂了 sidebarEl ref')
ok(/:style="\{ width: sidebarWidth \+ 'px' \}"/.test(tpl), '侧栏宽度由 sidebarWidth 驱动')
ok(/class="sb-resizer"/.test(tpl), '存在拖拽手柄 .sb-resizer')
ok(/@pointerdown="startResize"/.test(tpl), '手柄接 pointerdown → startResize')
ok(/role="separator"/.test(tpl) && /aria-orientation="vertical"/.test(tpl), '手柄有无障碍语义 role=separator')
ok(/tabindex="0"/.test(tpl), '手柄可键盘聚焦')
ok(/@keydown\.left\.prevent="nudgeSidebar\(-16\)"/.test(tpl), '← 键缩小')
ok(/@keydown\.right\.prevent="nudgeSidebar\(16\)"/.test(tpl), '→ 键放大')
ok(/@dblclick="resetSidebarWidth"/.test(tpl), '双击复位')
ok(/:class="\{ resizing: resizing \}"/.test(tpl), '根元素带 resizing 状态类')

const style = src.slice(src.indexOf('<style>'), src.lastIndexOf('</style>'))
ok(/\.sb-resizer \{[\s\S]*?cursor: col-resize/.test(style), '手柄有 col-resize 光标')
ok(/touch-action: none/.test(style), '手柄禁用了触屏默认手势')
ok(/\.app\.resizing \* \{ cursor: col-resize !important/.test(style), '拖拽中全局统一光标')
ok(/\.app\.resizing \{ cursor: col-resize; user-select: none/.test(style), '拖拽中禁止选中文本')
ok(/@media \(max-width: 720px\)[\s\S]*?\.sb-resizer \{ display: none/.test(style), '窄屏隐藏手柄')

// ---------- 6. 监听器清理 ----------
console.log('\n[6] 事件监听清理（防泄漏）')
ok(/window\.removeEventListener\('pointermove', onResizeMove\)/.test(src), '拖拽结束移除 pointermove')
ok(/window\.removeEventListener\('pointerup', onResizeEnd\)/.test(src), '拖拽结束移除 pointerup')
ok(/window\.removeEventListener\('pointercancel', onResizeEnd\)/.test(src), '拖拽结束移除 pointercancel')
ok(/onBeforeUnmount\(\(\) => \{[\s\S]*?window\.removeEventListener\('resize', onWindowResize\)[\s\S]*?\n    \}\)/.test(src), '卸载时移除 window resize 监听')
ok(/onBeforeUnmount\(\(\) => \{[\s\S]*?onResizeEnd\(\)/.test(src), '卸载时若仍在拖拽会收尾')
ok(/e\.button !== undefined && e\.button !== 0/.test(src), '只响应左键拖拽')

console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 项未通过：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ 侧边栏拖拽：夹取边界、窄窗口保护、持久化、键盘微调、接线与清理 全部通过')
}
process.exitCode = problems.length ? 1 : 0
