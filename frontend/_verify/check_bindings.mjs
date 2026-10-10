// check_bindings.mjs — 用 Vue 官方编译器解析模板 AST，逐个表达式做标识符解析
// 判定：模板里每个根作用域标识符，是否真的由 setup() 提供（或来自 v-for 局部作用域）。
// 同时反向找出 returned-but-unused 的死导出，并对关键回归项做断言。
import fs from 'node:fs'
import { parse as parseSFC, compileTemplate } from 'vue/compiler-sfc'
import { NodeTypes } from '@vue/compiler-core'
import { parse as parseJS } from '@babel/parser'

const SRC = 'D:/Deepseek_Harness/agent-harness/frontend/src/App.vue'
const raw = fs.readFileSync(SRC, 'utf8')

const problems = []
const notes = []

// ---------- 1. 用官方编译器解析 SFC ----------
const { descriptor, errors: sfcErrors } = parseSFC(raw, { filename: 'App.vue' })
for (const e of sfcErrors) problems.push(`SFC 解析错误：${e.message}`)

if (!descriptor.template) throw new Error('没有 <template>')
if (!descriptor.script) throw new Error('没有 <script>')

// 编译模板：这会真正跑一遍 Vue 模板编译，语法错误会在这里暴露
const compiled = compileTemplate({
  source: descriptor.template.content,
  filename: 'App.vue',
  id: 'app',
})
for (const e of compiled.errors || []) {
  problems.push(`模板编译错误：${typeof e === 'string' ? e : e.message}`)
}
notes.push(`模板编译：${compiled.errors?.length ? '有错误' : '通过'}，生成代码 ${compiled.code.length} 字节`)

// ---------- 2. setup() 返回项 ----------
const setupIdx = descriptor.script.content.indexOf('setup()')
const retIdx = descriptor.script.content.lastIndexOf('return {')
const retBody = descriptor.script.content.slice(retIdx + 'return {'.length, descriptor.script.content.indexOf('\n    }', retIdx))
const returned = new Set(
  retBody.split(/[,\n]/).map((s) => s.replace(/\/\/.*$/, '').trim()).filter((s) => /^[A-Za-z_$][\w$]*$/.test(s)),
)
if (setupIdx < 0) problems.push('没找到 setup()')
notes.push(`setup() 导出 ${returned.size} 项`)

// ---------- 3. 遍历模板 AST ----------
// 注意：AST 必须取自 SFC 解析结果（descriptor.template.ast），
// 它才是带 INTERPOLATION / DIRECTIVE 节点的模板 AST。
const root = descriptor.template.ast
if (!root) throw new Error('SFC 未产出模板 AST')
const referenced = new Set()
const refDetails = new Map()
const localScopes = []
// v-for 局部变量按「宿主元素」登记，避免多个 v-for 互相覆盖
const forLocalsByEl = new Map()
let nodeCount = 0
const typeTally = new Map()

const collectIdentifiers = (node) => {
  const names = new Set()
  if (!node) return names
  const src = typeof node === 'string' ? node : node.content
  if (DEBUG) console.error('DBG collectIdentifiers src =', JSON.stringify(src))
  try {
    const ast = parseJS(`(${src})`, { sourceType: 'module', plugins: [] })
    const walk = (n, depth = 0) => {
      if (!n || typeof n !== 'object') return
      if (Array.isArray(n)) return n.forEach((c) => walk(c, depth))
      if (n.type === 'Identifier') { names.add(n.name); return }
      if (n.type === 'MemberExpression') { walk(n.object, depth + 1); return } // 只取根对象
      if (n.type === 'ObjectProperty' && !n.computed) { walk(n.value, depth + 1); return } // 键名不算引用
      if (n.type === 'ObjectMethod') { walk(n.body, depth + 1); return }
      for (const k of Object.keys(n)) {
        if (['loc', 'start', 'end', 'leadingComments', 'trailingComments', 'extra'].includes(k)) continue
        walk(n[k], depth + 1)
      }
    }
    walk(ast)
  } catch (e) {
    problems.push(`表达式无法解析（${node.content.slice(0, 50)}）：${e.message}`)
  }
  return names
}

const recordRef = (name, where) => {
  referenced.add(name)
  if (!refDetails.has(name)) refDetails.set(name, where)
}

const DEBUG = process.env.DEBUG_BIND === '1'
let interpSeen = 0

const walkNode = (n) => {
  if (!n || typeof n !== 'object') return
  if (Array.isArray(n)) return n.forEach(walkNode)
  nodeCount++
  typeTally.set(n.type, (typeTally.get(n.type) || 0) + 1)

  const isLocal = (name) => localScopes.some((s) => s.has(name))

  // Vue 模板 AST 的节点类型是数字枚举（NodeTypes），必须用常量比较。
  // 插值节点单独处理；指令与属性统一在下面的元素分支里解析（指令本身也是元素的 prop）。
  if (n.type === NodeTypes.INTERPOLATION) {
    interpSeen++
    for (const name of collectIdentifiers(n.content)) if (!isLocal(name)) recordRef(name, '{{ }}')
    return
  }

  if (n.type === NodeTypes.ELEMENT || n.type === NodeTypes.COMPONENT) {
    const ownProps = n.props || []
    let pushedScopes = 0
    // 先登记本元素上的 v-for 局部变量：属性（含指令）统一在此解析。
    // 注意 forParseResult.value/index/key 可能是字符串，也可能是带 .content 的节点对象。
    const nameOf = (v) => {
      if (v == null) return null
      const s = typeof v === 'string' ? v : v.content
      return typeof s === 'string' && /^[A-Za-z_$][\w$]*$/.test(s.trim()) ? s.trim() : null
    }
    for (const p of ownProps) {
      if (p.type === NodeTypes.DIRECTIVE && p.name === 'for' && p.forParseResult) {
        const locals = new Set()
        for (const part of [p.forParseResult.value, p.forParseResult.index, p.forParseResult.key]) {
          const nm = nameOf(part)
          if (nm) locals.add(nm)
        }
        localScopes.push(locals)
        pushedScopes++
        if (locals.size === 0) problems.push(`v-for 解析不出局部变量名：${p.forParseResult.exp || p.exp?.content}`)
      }
    }

    for (const p of ownProps) {
      if (p.type === NodeTypes.ATTRIBUTE) {
        // 结构性属性：值本身就是一个标识符
        if (['ref', 'v-html'].includes(p.name) && /^[A-Za-z_$][\w$]*$/.test(p.value?.content ?? '')) {
          recordRef(p.value.content, p.name)
        }
        continue
      }
      if (p.type !== NodeTypes.DIRECTIVE || !p.exp) continue
      if (p.name === 'for' && p.forParseResult) {
        // 源表达式的解析必须在本元素的局部变量入栈【之前】语义下进行：
        // 源里的标识符不是循环变量。临时弹出本元素的循环作用域再解析。
        const saved = localScopes.splice(localScopes.length - pushedScopes, pushedScopes)
        for (const name of collectIdentifiers(p.forParseResult.source)) {
          if (!isLocal(name)) recordRef(name, 'v-for 源')
        }
        localScopes.push(...saved)
        continue
      }
      const where = `v-${p.name}`
      for (const name of collectIdentifiers(p.exp)) if (!isLocal(name)) recordRef(name, where)
    }

    walkNode(n.children)
    for (let k = 0; k < pushedScopes; k++) localScopes.pop()
    return
  }

  if (n.children) walkNode(n.children)
  if (n.branches) n.branches.forEach((b) => walkNode(b.children))
}

walkNode(root)

// ---------- 4. 判定 ----------
// Vue 模板里可以合法使用的全局对象（编译后按全局量查找，不走组件实例）
const vGLOBALS = new Set(['Object', 'Array', 'Boolean', 'Number', 'String', 'JSON', 'Math', 'Date', 'Set', 'Map', 'RegExp', 'parseInt', 'parseFloat', 'isNaN', 'encodeURIComponent', 'decodeURIComponent', 'console'])

const missing = [...referenced].filter((n) => !returned.has(n) && !vGLOBALS.has(n))
for (const n of missing) {
  problems.push(`模板引用了 "${n}"（${refDetails.get(n)}），但 setup() 没有提供 → 运行时会 undefined`)
}

const unused = [...returned].filter((k) => !referenced.has(k))
notes.push(`模板引用的根标识符 ${referenced.size} 个，全部有定义：${missing.length === 0 ? '是' : '否'}`)
notes.push(`setup() 导出但模板未引用（死导出，${unused.length}）：${unused.join(', ') || '（无）'}`)

// ---------- 5. 关键回归断言 ----------
// 上一版界面里存在、重做后必须仍然被接线的能力
const mustBeWired = ['messages', 'sessions', 'sessionId', 'input', 'send', 'decide', 'approval',
  'hitlOn', 'renderMarkdown', 'formatJson', 'truncate', 'argPairs', 'activityLatest',
  'openSession', 'newChat', 'deleteSession', 'openFilePanel', 'breadcrumbs', 'formatSize',
  'isSessionStreaming', 'showTools', 'streaming', 'toast', 'suggestions', 'currentTitle',
  'hasActivity', 'activityHistory', 'toggleHitlBtn', 'useSuggestion', 'autoGrow']
for (const k of mustBeWired) {
  if (!returned.has(k)) problems.push(`关键能力未从 setup 导出：${k}`)
}

// 模板结构断言：Codex 风的关键结构必须存在
const tpl = descriptor.template.content
const structural = [
  ['无气泡助手正文 .prose', /class="prose md"/],
  ['用户气泡 .bubble.user', /class="bubble user"/],
  ['居中输入框 .composer-inner', /class="composer-inner"/],
  ['发送按钮 .btn-send', /class="btn-send"/],
  ['工具·最新一条 .act', /class="act"/],
  ['工具·历史折叠 .act-history', /class="act-history"/],
  ['子智能体运行卡片 .subagent', /class="subagent"/],
  ['子智能体归档区 .subagent-archive', /class="subagent-archive"/],
  ['计时器 .meter', /class="meter"/],
  ['空状态建议词 .chip', /class="chip"/],
  ['审批弹窗 .modal', /class="modal approval-modal"/],
  ['文件面板 .file-modal', /class="modal file-modal"/],
  ['顶栏开关按钮 .tbtn', /class="tbtn switchable"/],
]
for (const [label, re] of structural) {
  if (!re.test(tpl)) problems.push(`模板缺少结构：${label}`)
}

// ---------- 6. 每个模板 class 都必须有对应样式 ----------
// 从 SFC 的 style 块里抽类名，与模板实际用到的类名对账。
// 只做「模板有、CSS 无」的单向检查（CSS 里多出的状态类由 :class 动态挂载，属正常）。
const styleSrc = descriptor.styles.map((s) => s.content).join('\n')
const cssClasses = new Set([...styleSrc.matchAll(/\.([A-Za-z][\w-]*)/g)].map((m) => m[1]))
const tplClasses = new Set()
for (const m of tpl.matchAll(/\sclass="([^"]*)"/g)) {
  for (const c of m[1].split(/\s+/)) if (/^[A-Za-z][\w-]*$/.test(c)) tplClasses.add(c)
}
// :class 里引号包裹的字符串也是类名
for (const m of tpl.matchAll(/:class="([^"]*)"/g)) {
  for (const c of m[1].matchAll(/'([A-Za-z][\w-]*)'/g)) tplClasses.add(c[1])
}
const styleStates = ['active', 'off', 'done', 'running', 'error', 'ok', 'dir', 'user', 'assistant', 'interrupted']
for (const c of tplClasses) {
  if (cssClasses.has(c) || styleStates.includes(c)) continue
  problems.push(`模板 class="${c}" 没有对应样式规则 → 该元素不会有样式`)
}
notes.push(`模板 class ${tplClasses.size} 个，全部有样式规则：${[...tplClasses].every((c) => cssClasses.has(c) || styleStates.includes(c)) ? '是' : '否'}`)

// 旧界面元素不应残留
const shouldBeGone = [
  ['旧头像 .avatar', /class="avatar/],
  ['旧气泡提示 .typing-dots', /typing-dots/],
  ['旧开关 <input type="checkbox"', /type="checkbox"/],
  ['旧 emoji 图标 📁', /📁|🛠|🛡|⚡|✨|📋|📂|📄|⛔/],
  ['旧输入区 .inputbar', /inputbar/],
]
for (const [label, re] of shouldBeGone) {
  if (re.test(tpl)) problems.push(`模板仍残留：${label}`)
}
notes.push('已确认移除：头像、打字气泡、checkbox 开关、emoji 图标、旧输入区')

// ---------- 输出 ----------
console.log('================ 模板绑定 AST 校验 ================')
console.log(`模板编译：${(compiled.errors || []).length ? '❌ 有错' : '✅ 通过'}`)
console.log(`模板 AST 节点：${nodeCount} 个，引用根标识符：${referenced.size} 个`)
console.log(`节点类型分布：${[...typeTally].map(([t, c]) => `type${t}×${c}`).join(' ')}`)
console.log(`setup() 导出：${returned.size} 项`)
console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 个问题：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ 模板引用的每个标识符都有定义；关键能力全部接线；新结构齐全，旧结构已清除')
}
console.log('\nℹ️ 说明：')
for (const n of notes) console.log('  - ' + n)
process.exitCode = problems.length ? 1 : 0
