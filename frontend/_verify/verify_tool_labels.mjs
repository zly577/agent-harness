// verify_tool_labels.mjs — 工具行文案的廉价回归（不需要跑模型）
//
// 规则：**状态只由右侧徽标说一次**。
//   左列（.act-verb）：正在跑 → "正在查看/正在写入/…"；跑完 → **工具本身**（工具名）
//   右列（.act-badge）：进行中 / 已完成
// 因此任何 .act-verb 都不应出现"已完成"或"已打开"。
//
// 做法：直接打开一个**历史会话**（带工具调用），检查渲染出来的 DOM ——
// 不触发任何模型调用，几秒钟跑完，改样式/文案后随手就能跑一遍。
//
// 用法: node _verify/verify_tool_labels.mjs <devtools端口> <页面地址> <输出目录> [backendBase]
import fs from 'node:fs'
import { getJson, loginToken } from './_auth.mjs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.', backend = 'http://127.0.0.1:8010'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
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

// 找一个"工具体多、回答完整"的历史会话
// P18：业务接口默认拒绝，先登录拿 token（凭证走 AH_EMAIL/AH_PASSWORD 环境变量）
const token = await loginToken(backend)
const sessions = await getJson(`${backend}/api/v1/sessions`, token)
let target = null
for (const s of sessions.slice(0, 30)) {
  const msgs = await getJson(`${backend}/api/v1/sessions/${s.id}/messages`, token)
  const a = msgs.find((m) => m.role === 'assistant' && (m.toolCalls || []).length >= 4)
  if (a) { target = { sid: s.id, tools: a.toolCalls.length }; break }
}
if (!target) throw new Error('找不到带足够工具调用的会话（先跑一次带工具的任务）')
console.log(`============ 工具行文案回归 ============\n  用历史会话 ${target.sid}（${target.tools} 次工具调用）`)

await send('Page.navigate', { url })
// P18：打开页面先看到的是登录页；不登录的话后面每个选择器都拿到 null，
// 而报错往往出现在很后面（点按钮时才炸），很容易被误判成界面坏了。
await ensureBrowserLoggedIn(evaluate, sleep)
await sleep(2500)
await evaluate(`localStorage.setItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous'), ${JSON.stringify(target.sid)})`)
await send('Page.reload', {})
await sleep(6000)

// 注意：一律用 textContent 而不是 innerText —— innerText 对"滚出视口/未渲染"的
// 元素会返回空串（历史条目多了必然踩到），会让校验误报成"文案是空的"。
const READ = `(() => {
  const txt = (el) => (el ? (el.textContent || '').trim() : '')
  const act = document.querySelector('.act')
  const bad = (t) => /已完成|已打开/.test(t || '')
  const histVerbs = Array.from(document.querySelectorAll('.act-history .act-verb')).map((e) => txt(e))
  // P20 之后左列有两种东西：模型写的意图（带 .act-intent，一句人话）与
  // 工具名/动作文案（不带 .act-intent）。这条老断言只管后者，
  // 不把意图排除掉的话每次都必然失败（意图本来就是中文短句）。
  // ⚠️ 这段在模板字符串里，注释里**不能**出现反引号。
  const histPlainVerbs = Array.from(document.querySelectorAll('.act-history .act-verb:not(.act-intent)')).map(txt)
  const actVerbPlain = txt(act?.querySelector('.act-verb:not(.act-intent)'))
  return {
    hasAct: !!act,
    actVerb: txt(act?.querySelector('.act-verb')),
    actVerbPlain,
    actDetail: txt(act?.querySelector('.act-detail')),
    actBadge: txt(act?.querySelector('.act-badge')),
    oldBlock: !!document.querySelector('.tools-sum'),
    histCount: histVerbs.length,
    histVerbs,
    histPlainVerbs,
    badActVerb: bad(txt(act?.querySelector('.act-verb'))),
    badHistVerb: histVerbs.find(bad) || '',
    badges: Array.from(document.querySelectorAll('.act-badge, .tool-badge'))
      .map((e) => txt(e)).filter(Boolean),
  }
})()`

// 先看折叠态（默认不展开）
const collapsed = await evaluate(READ)

console.log('\n[1] 折叠态（默认）')
ok(collapsed.hasAct || collapsed.histCount > 0, '工具区已渲染')
ok(!collapsed.oldBlock, '旧的外层折叠块 .tools-sum 不存在')

// 展开历史，检查列表里每一条
await evaluate(`(() => { const h = document.querySelector('.act-history'); if (h) h.setAttribute('open','') })()`)
await sleep(500)
const open = await evaluate(READ)

console.log('\n[2] 展开后逐条检查')
console.log(`  最新一条：左「${open.actVerb}」 右「${open.actBadge}」 细节「${open.actDetail}」`)
console.log(`  历史 ${open.histCount} 条：${JSON.stringify(open.histVerbs)}`)
ok(!open.badActVerb, `最新一条左列不出现"已完成/已打开"（实际「${open.actVerb}」）`)
ok(!open.badHistVerb, `历史列表左列不出现"已完成/已打开"（实际 ${open.badHistVerb || '无'}）`)
ok(open.actVerb.length > 0, '最新一条左列有内容')
ok(/已完成|进行中|成功|失败|已中断|已返回|生成参数/.test(open.actBadge), `右侧徽标负责说状态（「${open.actBadge}」）`)
// P22 起徽标还负责说"成没成/在不在跑"，词表扩了：
// 成功 / 失败 / 已中断 / 已返回（另有耗时后缀）—— 但"状态只由右列说"这条规矩不变。
ok(open.badges.length > 0 && open.badges.every((b) => /已完成|进行中|成功|失败|已中断|已返回|生成参数/.test(b)),
  `所有徽标都是状态词：${JSON.stringify([...new Set(open.badges)])}`)

// 左列**非意图**的那些（即工具名/动作文案）应当都是"工具名"或"正在…"
const looksLikeToolName = (t) => /^[A-Za-z0-9_\-、，\s]+$/.test(t) || /等 \d+ 个$/.test(t)
const allLeft = [open.actVerbPlain, ...open.histPlainVerbs].filter(Boolean)
const notToolName = allLeft.filter((t) => !looksLikeToolName(t) && !/^正在/.test(t))
ok(notToolName.length === 0,
  `左列要么是"正在…"、要么是工具名（不合规：${JSON.stringify(notToolName)}）`)

const shot = await send('Page.captureScreenshot', { format: 'png' })
fs.writeFileSync(`${outDir}/tool_labels_shot.png`, Buffer.from(shot.data, 'base64'))

const errors = events.filter((e) => e.method === 'Runtime.exceptionThrown')
console.log('\n[3] 控制台')
ok(errors.length === 0, `无 JS 异常（${errors.length} 条）`)

console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 项未通过：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ 工具行文案全部通过')
}
ws.close()
process.exit(problems.length ? 1 : 0)
