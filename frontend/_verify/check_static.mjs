// check_static.mjs — 模板 / 脚本 / 样式 三方一致性校验（无需浏览器）
import fs from 'node:fs'

const SRC = 'D:/Deepseek_Harness/agent-harness/frontend/src/App.vue'
const raw = fs.readFileSync(SRC, 'utf8')

// 注意：绝不能用 indexOf('<template>')——它会命中 '</template>' 里的子串。
// 用行首锚定来定位真正的起始标签。
const tplStart = raw.search(/^<template>/m)
const tplEnd = raw.search(/^<\/template>/m)
const scriptStart = raw.search(/^<script>/m)
const scriptEnd = raw.search(/^<\/script>/m)
const styleStart = raw.search(/^<style>/m)
const styleEnd = raw.search(/^<\/style>/m)

for (const [name, v] of Object.entries({ tplStart, tplEnd, scriptStart, scriptEnd, styleStart, styleEnd })) {
  if (v < 0) throw new Error(`定位失败：${name}`)
}
if (!(tplStart < tplEnd && tplEnd < scriptStart && scriptStart < scriptEnd && scriptEnd < styleStart && styleStart < styleEnd)) {
  throw new Error('区块顺序异常，解析不可信')
}

const template = raw.slice(tplStart + '<template>'.length, tplEnd)
const script = raw.slice(scriptStart + '<script>'.length, scriptEnd)
const style = raw.slice(styleStart + '<style>'.length, styleEnd)

const problems = []
const notes = []

// ---------- 1. setup() 返回的键 ----------
const retIdx = script.lastIndexOf('return {')
const retBody = script.slice(retIdx + 'return {'.length, script.indexOf('\n    }', retIdx))
const returned = new Set(
  retBody.split(/[,\n]/).map((s) => s.replace(/\/\/.*$/, '').trim()).filter((s) => /^[A-Za-z_$][\w$]*$/.test(s)),
)

const declared = new Set()
for (const m of script.matchAll(/(?:const|let|function)\s+([A-Za-z_$][\w$]*)/g)) declared.add(m[1])

// ---------- 2. 模板标识符 ----------
const vueBuiltins = new Set(['true', 'false', 'null', 'undefined', 'Math', 'Object', 'Array', 'String', 'Number', 'JSON', 'Date', 'Boolean', 'Set', 'Map', 'console', 'window', 'document', '$event', 'in', 'of'])

const expressions = []
for (const m of template.matchAll(/\{\{([\s\S]*?)\}\}/g)) expressions.push(m[1])
for (const m of template.matchAll(/(?::|v-bind:|@|v-on:|v-if|v-else-if|v-for|v-model|v-show)[\w.-]*\s*=\s*"([^"]*)"/g)) expressions.push(m[1])

// v-for 局部变量（必须在剥离表达式之前先收集）
const locals = new Set()
for (const m of template.matchAll(/v-for\s*=\s*"\(?\s*([^)"]*?)\s*\)?\s+in\s+[^"]*"/g)) {
  for (const v of m[1].split(',')) {
    const n = v.trim()
    if (/^[A-Za-z_$][\w$]*$/.test(n)) locals.add(n)
  }
}

const used = new Map()
for (const expr of expressions) {
  const cleaned = expr
    .replace(/'[^']*'/g, "''")
    .replace(/"[^"]*"/g, '""')
    .replace(/`[^`]*`/g, '``')
  // 标识符：前面不能是 . 或 $ ；后面不能紧跟 : （排除对象字面量 key）
  for (const m of cleaned.matchAll(/(?<![\w$.'])([A-Za-z_$][\w$]*)(?![\w$]*\s*:)/g)) {
    const name = m[1]
    if (vueBuiltins.has(name) || locals.has(name)) continue
    if (!used.has(name)) used.set(name, expr.trim().slice(0, 70))
  }
}

for (const [name, where] of used) {
  if (!returned.has(name)) {
    if (declared.has(name)) problems.push(`模板用到 "${name}"，setup 里声明了但没 return → 运行时 undefined（${where}）`)
    else problems.push(`模板用到 "${name}"，setup 返回值里没有（${where}）`)
  }
}

const unused = [...returned].filter((k) => !used.has(k))
notes.push(`returned 但模板未直接引用（${unused.length}）：${unused.join(', ')}`)

// ---------- 3. class 一致性 ----------
const tplClasses = new Set()
for (const m of template.matchAll(/\sclass="([^"]*)"/g)) {
  for (const c of m[1].split(/\s+/)) if (/^[A-Za-z][\w-]*$/.test(c)) tplClasses.add(c)
}
// :class 里只有引号包裹的字符串才是类名
for (const m of template.matchAll(/:class="([^"]*)"/g)) {
  for (const c of m[1].matchAll(/'([A-Za-z][\w-]*)'/g)) tplClasses.add(c[1])
}

// CSS：同时收集单个类名与复合选择器
const cssSelectors = new Set()
for (const m of style.matchAll(/\.([A-Za-z][\w-]*)/g)) cssSelectors.add(m[1])
const cssCompound = new Set()
for (const m of style.matchAll(/\.([A-Za-z][\w-]*(?:\.[A-Za-z][\w-]*)+)/g)) cssCompound.add(m[1])

const cssOnly = [...cssSelectors].filter((c) => !tplClasses.has(c))
notes.push(`CSS 定义但模板没有的类（${cssOnly.length}）：${cssOnly.join(', ') || '（无）'}`)

for (const c of tplClasses) {
  if (cssSelectors.has(c)) continue
  if (['active', 'off', 'done', 'running', 'error', 'ok', 'dir', 'user', 'assistant'].includes(c)) {
    notes.push(`模板状态类（由脚本/CSS 组合出样式，无独立规则）：${c}`)
    continue
  }
  problems.push(`模板 class="${c}" 在 CSS 里没有规则 → 该元素无样式`)
}

// 关键结构
const mustCss = ['app', 'sidebar', 'sb-new', 'sb-nav', 'sb-foot', 'topbar', 'messages', 'composer-inner',
  'btn-send', 'btn-stop', 'chip', 'tools', 'tools-sum', 'delegation', 'overlay', 'modal', 'btn-primary',
  'btn-ghost', 'toast', 'md', 'prose', 'thinking', 'empty', 'row', 'row-inner', 'tbtn']
for (const c of mustCss) if (!cssSelectors.has(c)) problems.push(`CSS 缺关键规则：.${c}`)
const mustTpl = ['app', 'sidebar', 'topbar', 'messages', 'composer-inner', 'btn-send', 'row-inner', 'tools', 'delegation']
for (const c of mustTpl) if (!tplClasses.has(c)) problems.push(`模板缺关键 class：.${c}`)

// 简单选择器也要匹配到模板类
for (const c of ['bubble', 'user']) {
  if (!tplClasses.has(c) && !cssSelectors.has(c)) problems.push(`样式与模板都找不到 .${c}`)
}

// ---------- 4. 旧风格残留 ----------
for (const c of ['msg-row', 'msg-body', 'avatar', 'msg-meta', 'inputbar-inner', 'inputbar',
  'approval-overlay', 'approval-box', 'file-box', 'file-overlay', 'new-chat', 'session-item',
  'session-list', 'tool-toggle', 'slider', 'header-actions', 'typing-dots', 'cursor']) {
  if (tplClasses.has(c) || cssSelectors.has(c)) problems.push(`仍有旧风格遗留类：.${c}`)
}

// ---------- 5. CSS 变量 ----------
const varsDefined = new Set([...style.matchAll(/--([\w-]+)\s*:/g)].map((m) => m[1]))
const varsUsed = new Set([...style.matchAll(/var\(--([\w-]+)/g)].map((m) => m[1]))
for (const v of varsUsed) if (!varsDefined.has(v)) problems.push(`CSS 用了未定义变量：--${v}`)

const darkIdx = style.indexOf('prefers-color-scheme: dark')
const lightBlock = style.slice(0, darkIdx)
const darkBlock = style.slice(darkIdx)
const core = ['bg', 'sidebar', 'text', 'text-2', 'text-3', 'border', 'border-2', 'hover', 'active', 'fill', 'btn', 'pre-bg', 'overlay', 'danger', 'warn', 'ok']
for (const v of core) {
  if (!new RegExp(`--${v}\\s*:`).test(lightBlock)) problems.push(`浅色主题缺变量 --${v}`)
  if (!new RegExp(`--${v}\\s*:`).test(darkBlock)) problems.push(`深色主题缺变量 --${v}`)
}

// 变量块之外不应有硬编码的颜色（:root 与深色块内部除外）
const paletteZone = style.slice(0, darkBlock.indexOf('}', darkBlock.indexOf('{')))
const outsidePalette = style.replace(paletteZone, '')
const hard = [...new Set([...outsidePalette.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => m[0]))]
notes.push(`调色板外硬编码颜色（${hard.length}）：${hard.join(' ') || '（无）'}`)

// ---------- 6. 括号配对 ----------
const ob = (style.match(/\{/g) || []).length
const cb = (style.match(/\}/g) || []).length
if (ob !== cb) problems.push(`CSS 花括号不配对：{ ×${ob} vs } ×${cb}`)

// ---------- 输出 ----------
console.log('================ 静态一致性校验 ================')
console.log(`模板标识符引用 ${used.size} 个 / setup 返回 ${returned.size} 项`)
console.log(`模板 class ${tplClasses.size} 个 / CSS class ${cssSelectors.size} 个（复合选择器 ${cssCompound.size} 个）`)
console.log(`CSS 变量 定义 ${varsDefined.size} 个 / 引用 ${varsUsed.size} 个`)
console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 个问题：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ 模板 / 脚本 / 样式三方一致，无问题')
}
console.log('\nℹ️ 说明：')
for (const n of notes) console.log('  - ' + n)
process.exitCode = problems.length ? 1 : 0
