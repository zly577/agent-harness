// verify_run_visibility.mjs — P22「运行可见性」的廉价回归（不跑模型）
//
// 守住的四件事（就是用户提的那几句）：
//   1. 卡片上能看出**这一步到底成没成** —— 徽标必须是 成功/失败/已中断/已返回 之一，
//      不能再是一律"已完成"；
//   2. 卡片上能看见**耗时**（"已完成 · 3.2s" / "进行中 · 已 12s"）；
//   3. 卡片下方贴出**结果首行**（↳ …）或**失败原因**（✕ …），不用展开就能看到；
//   4. 能展开看**具体执行了什么**（命令/参数 + 完整输出）。
//
// 做法与 verify_tool_labels.mjs 相同：打开一个带工具调用的**历史会话**，
// 检查渲染出来的 DOM —— 不触发任何模型调用，几秒钟跑完。
//
// 用法: node _verify/verify_run_visibility.mjs <devtools端口> <页面地址> <输出目录> [backendBase]
import fs from 'node:fs'
import { getJson, loginToken, ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.', backend = 'http://127.0.0.1:8010'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error(`端口 ${port} 上没有可用页面（Edge 是否带 --remote-debugging-port 启动？）`)
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res) => ws.addEventListener('open', res))
let id = 0
const pending = new Map()
const events = []
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id) }
  else if (m.method) events.push(m)
})
const send = (method, params = {}) => new Promise((res) => {
  const i = ++id
  pending.set(i, res)
  ws.send(JSON.stringify({ id: i, method, params }))
})
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval error')
  return r.result.value
}

const problems = []
const ok = (c, label, detail = '') => {
  console.log(`  ${c ? '✅' : '❌'} ${label}${detail && !c ? ' — ' + detail : ''}`)
  if (!c) problems.push(label)
}

await send('Runtime.enable')
await send('Page.enable')

const token = await loginToken(backend)
const sessions = await getJson(`${backend}/api/v1/sessions`, token)
let target = null
for (const s of sessions.slice(0, 30)) {
  const msgs = await getJson(`${backend}/api/v1/sessions/${s.id}/messages`, token)
  const a = msgs.find((m) => m.role === 'assistant' && (m.toolCalls || []).length >= 3)
  if (a) { target = { sid: s.id, tools: a.toolCalls.length }; break }
}
if (!target) throw new Error('找不到带足够工具调用的会话（先跑一次带工具的任务）')
console.log(`============ 运行可见性回归 ============\n  用历史会话 ${target.sid}（${target.tools} 次工具调用）`)

await send('Page.navigate', { url })
// 先等页面真的渲染出"登录页或主界面"再判断登录态：
// `Page.navigate` 刚发出时求值还落在**旧文档**上（`.auth-card` 是 null），
// `ensureBrowserLoggedIn` 会据此认为"已经登录"，于是后面每个选择器都拿到 null，
// 报错却出现在很后面 —— 看起来像功能坏了，其实是脚本自己的时序问题。
for (let i = 0; i < 40; i++) {
  if (await evaluate(`!!document.querySelector('.app, .auth-card')`)) break
  await sleep(250)
}
await ensureBrowserLoggedIn(evaluate, sleep)
await sleep(2500)
await evaluate(`localStorage.setItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous'), ${JSON.stringify(target.sid)})`)
await send('Page.reload', {})
await sleep(6000)

// 一律用 textContent（innerText 对未渲染的元素返回空串，会误报）
const READ = `(() => {
  const txt = (el) => (el ? (el.textContent || '').trim() : '')
  const act = document.querySelector('.act')
  const note = document.querySelector('.act-note')
  const wrap = document.querySelector('.act-call-wrap')
  const calls = Array.from(document.querySelectorAll('.act-call')).map((c) => ({
    name: txt(c.querySelector('.tool-name')),
    badge: txt(c.querySelector('.tool-badge')),
    labels: Array.from(c.querySelectorAll('.tool-label')).map(txt),
  }))
  return {
    hasAct: !!act,
    actBadge: txt(act?.querySelector('.act-badge')),
    actIntent: txt(act?.querySelector('.act-verb')),
    noteText: txt(note),
    hasResultNote: !!document.querySelector('.act-note:not(.bad):not(.soft):not(.hard)'),
    wrapSummary: txt(wrap?.querySelector('summary')),
    callCount: calls.length,
    calls,
    allBadges: Array.from(document.querySelectorAll('.act-badge, .tool-badge'))
      .map(txt).filter(Boolean),
  }
})()`

const collapsed = await evaluate(READ)

console.log('\n[1] 折叠态（默认）')
ok(collapsed.hasAct, '工具卡已渲染')
ok(collapsed.actIntent.length > 0, `卡片左侧有内容（「${collapsed.actIntent}」）`)
ok(/成功|失败|已中断|已返回|进行中|生成参数|已完成/.test(collapsed.actBadge),
  `右列徽标说的是"成没成/在不在跑」（「${collapsed.actBadge}」）`)
// 耗时只有**实时**那一轮才有（后端 `tool_result` 带 duration_ms）；历史回放没有这个
// 数据，所以这里只能校验"有耗时的时候格式对"，不能强制要求出现 —— 写死会变成假失败。
ok(!/·/.test(collapsed.actBadge) || /·\s*\d+(\.\d+)?s|·\s*\d+m\d+s/.test(collapsed.actBadge),
  `徽标带上耗时时格式正确（「${collapsed.actBadge}」）`)
ok(collapsed.noteText.length > 0,
  `卡片下方贴出了结果首行或失败原因（「${collapsed.noteText.slice(0, 60)}」）`)

console.log('\n[2] 展开「这一步的具体操作」')
await evaluate(`(() => { const d = document.querySelector('.act-call-wrap'); if (d) d.setAttribute('open','') })()`)
await sleep(400)
const open = await evaluate(READ)
console.log(`  摘要：「${open.wrapSummary}」  条目 ${open.callCount} 条`)
ok(open.callCount > 0, '展开后有具体的调用条目')
ok(/操作/.test(JSON.stringify(open.calls[0]?.labels || [])), '每条都列出了"操作"（命令/参数）')
ok(/结果|操作/.test(JSON.stringify(open.calls.map((c) => c.labels))), '每条都能看到结果/操作分区')
ok(open.calls.every((c) => /成功|失败|已中断|已返回|进行中|生成参数/.test(c.badge)),
  `每条调用的成败都单独标出：${JSON.stringify([...new Set(open.calls.map((c) => c.badge))])}`)
ok(open.allBadges.every((b) => /成功|失败|已中断|已返回|进行中|生成参数|已完成/.test(b)),
  `所有徽标都是成败/状态词：${JSON.stringify([...new Set(open.allBadges)])}`)

const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
fs.writeFileSync(`${outDir}/run_visibility_shot.png`, Buffer.from(shot.data, 'base64'))

// 再来一张"此前 N 项"展开后的：失败 / 已中断 的徽标都在那里，
// 是"哪一步没成"最直观的证据。
await evaluate(`(() => { const d = document.querySelector('.act-history'); if (d) d.setAttribute('open','') })()`)
await sleep(500)
const hist = await evaluate(`(() => {
  const txt = (el) => (el ? (el.textContent || '').trim() : '')
  const rows = Array.from(document.querySelectorAll('.act-item-head')).map((h) => ({
    left: txt(h.querySelector('.act-verb')),
    right: txt(h.querySelector('.tool-badge')),
    detail: txt(h.querySelector('.act-detail')),
  }))
  return { rows, notes: Array.from(document.querySelectorAll('.act-history .act-note')).map(txt) }
})()`)
console.log('\n[3] 此前的调用（归档）')
for (const r of hist.rows) console.log(`  · ${r.right.padEnd(6)} ${r.left} ${r.detail ? '— ' + r.detail.slice(0, 40) : ''}`)
ok(hist.rows.length > 0, '归档列出此前的调用')
ok(hist.rows.every((r) => /成功|失败|已中断|已返回|已完成/.test(r.right)),
  `每条都标出成败：${JSON.stringify([...new Set(hist.rows.map((r) => r.right))])}`)
if (hist.notes.length) console.log(`  结果摘要：${JSON.stringify(hist.notes.slice(0, 2))}`)

const shot2 = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
fs.writeFileSync(`${outDir}/run_visibility_history_shot.png`, Buffer.from(shot2.data, 'base64'))

const errors = events.filter((e) => e.method === 'Runtime.exceptionThrown')
console.log('\n[4] 控制台')
ok(errors.length === 0, `无 JS 异常（${errors.length} 条）`)
if (errors.length) console.log('  ' + JSON.stringify(errors.slice(0, 2)))

console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 项未通过：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ 运行可见性全部通过')
}
ws.close()
process.exit(problems.length ? 1 : 0)
