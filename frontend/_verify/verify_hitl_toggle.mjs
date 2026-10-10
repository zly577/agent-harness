// 验证"审批弹窗与人工介入开关"的完整行为（真实浏览器 + 真实后端）。
//
// 覆盖用户报的两个现象：
//   A. 自动执行之后仍弹审批窗         → 关闭开关后弹窗必须消失，重新打开也不许"复活"
//   B. 点同意之后说"已审批过"          → 关闭开关时要**真的收尾**（不是靠看不见），
//                                      且一轮结束时必须有可读的回复
//   C. 幽灵弹窗（前端记忆）            → 切走再切回时必须以**后端**为准
//
// 用法: node verify_hitl_toggle.mjs <debugPort> <url> <outDir> <sid1> [sid2] [sid3] [ghostTitleMatch]
//   sid1 = 用来测"点同意"的挂起会话
//   sid2 = 用来测"开关"的挂起会话
//   sid3 = 用来测"幽灵弹窗"的挂起会话
//   ghostTitleMatch = 侧边栏里点回 sid3 时用的标题片段（默认 ghost）
import fs from 'node:fs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const port = Number(process.argv[2] || 9222)
const url = process.argv[3] || 'http://127.0.0.1:5174/'
const outDir = process.argv[4] || '_verify'
const S1 = process.argv[5]
const S2 = process.argv[6]
const S3 = process.argv[7]
const GHOST_MATCH = process.argv[8] || 'ghost'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const results = []
function ok(cond, label) {
  results.push([!!cond, label])
  console.log(`${cond ? '✅' : '❌'} ${label}`)
}

async function connect() {
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
  if (!page) throw new Error('找不到可调试页面')
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((r) => ws.addEventListener('open', r))
  let id = 0
  const pend = new Map()
  const errors = []
  const netErrors = []
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data)
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); return }
    if (m.method === 'Runtime.exceptionThrown') {
      errors.push(m.params.exceptionDetails?.exception?.description || 'exception')
    } else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      const t = m.params.entry.text || ''
      if (/favicon/.test(t)) return
      // "Failed to load resource: ... 40x" 是浏览器网络层日志，不是 JS 异常：
      // 本脚本会故意打几次已被处理的审批接口，那条 409 属于预期噪音。
      if (/Failed to load resource/.test(t)) { netErrors.push(t.slice(0, 120)); return }
      errors.push(t.slice(0, 200))
    }
  })
  const send = (method, params = {}) => new Promise((r) => {
    const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params }))
  })
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval failed')
    return r.result.value
  }
  return { ws, send, evaluate, errors, netErrors }
}

// 弹窗 + 气泡状态快照
const SNAP = `(() => {
  const modal = document.querySelector('.approval-modal')
  const bubbles = Array.from(document.querySelectorAll('.prose.md'))
  const last = bubbles[bubbles.length - 1]
  const toggle = Array.from(document.querySelectorAll('button.tbtn.switchable'))
    .find((b) => /需要审批|自动执行|切换中/.test(b.textContent))
  return {
    popup: !!modal,
    popupTools: modal ? Array.from(modal.querySelectorAll('.approve-tool')).map((e) => e.textContent.trim()) : [],
    popupVals: modal ? Array.from(modal.querySelectorAll('.approve-arg-v')).map((e) => e.textContent.trim()) : [],
    buttonsDisabled: modal ? Array.from(modal.querySelectorAll('.modal-foot button')).map((b) => b.disabled) : [],
    bubbleCount: bubbles.length,
    lastBubble: last ? last.textContent.trim() : '',
    toggle: toggle ? toggle.textContent.trim() : '',
    // 最后一条消息的"空白"判定：有气泡 div 才说明 content 非空
    hasBubble: !!last,
  }
})()`

async function openSession(evaluate, send, sid, label) {
  await evaluate(`localStorage.setItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous'), ${JSON.stringify(sid)})`)
  await send('Page.reload', {})
  await sleep(6500)
  let s = null
  for (let i = 0; i < 25; i++) {
    s = await evaluate(SNAP)
    if (s.popup) break
    await sleep(1000)
  }
  console.log(`   [${label}] popup=${s.popup} toggle=${s.toggle} bubbles=${s.bubbleCount} 末条气泡=${JSON.stringify(s.lastBubble.slice(0, 60))}`)
  return s
}

/** 在页面里直接调后端把某会话的挂起审批收尾（模拟"别处处理掉了"）。 */
const pageReject = (sid) => `(async () => {
  const r = await fetch('/api/v1/approvals/${sid}', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision: 'reject' }),
  })
  await r.text()
  return r.status
})()`

const fetchApprovalPending = (sid) => `fetch('/api/v1/approvals/${sid}').then(r => r.json()).then(d => !!d.pending)`
const fetchLastReply = (sid) => `(async () => {
  const d = await (await fetch('/api/v1/sessions/${sid}/messages')).json()
  const rows = Array.isArray(d) ? d : []
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].role === 'assistant' && (rows[i].content || '').trim()) return rows[i].content.trim()
  }
  return ''
})()`

;(async () => {
  const { ws, send, evaluate, errors, netErrors } = await connect()
  await send('Runtime.enable'); await send('Page.enable'); await send('Log.enable')
  await send('Page.navigate', { url })
  await ensureBrowserLoggedIn(evaluate, sleep)
  await sleep(2500)

  // ---------- 场景 1：挂起 → 弹窗 + 气泡里有话 → 点同意 ----------
  console.log('\n[1] 点同意（正常路径）')
  let s = await openSession(evaluate, send, S1, 'S1 打开')
  ok(s.popup, '打开挂起会话时弹窗自动恢复')
  ok(s.popupVals.some((v) => v.includes('/workspace/')), '弹窗里显示了操作对象（file_path）')
  ok(s.buttonsDisabled.length === 2 && s.buttonsDisabled.every((d) => !d), '两个按钮都可用')
  ok(s.hasBubble && s.lastBubble.length > 0, '停在审批时气泡里也有话（不是空白）')

  await evaluate(`Array.from(document.querySelectorAll('.approval-modal .modal-foot button')).find(b => b.textContent.includes('同意')).click()`)
  let gone = false
  for (let i = 0; i < 40; i++) {
    await sleep(1000)
    const t = await evaluate(SNAP)
    if (!t.popup) { gone = true }
    if (gone && !/处理中/.test(t.toggle)) break
  }
  ok(gone, '点同意后弹窗消失')
  ok((await evaluate(fetchApprovalPending(S1))) === false, '后端待审批已清空（不会再弹回来）')
  const reply1 = await evaluate(fetchLastReply(S1))
  ok(reply1.length > 0, `被批准的这轮有最终回复（${reply1.length} 字）`)
  ok((await evaluate(SNAP)).popup === false, '点同意后没有立刻又弹出来')

  // ---------- 场景 2：幽灵弹窗（前端记忆 vs 后端真相）----------
  // 必须放在"切换开关"之前：全局关闭开关会把**所有**会话的挂起中断一起收尾，
  // 那样 S3 就没有可测的挂起了。
  console.log('\n[2] 幽灵弹窗：别处处理掉后切走再切回')
  s = await openSession(evaluate, send, S3, 'S3 打开')
  ok(s.popup, 'S3 挂起会话打开后有弹窗（此时前端内存记住了它）')
  const st = await evaluate(pageReject(S3))
  console.log(`   页面内直接调后端收尾 → HTTP ${st}`)
  ok(st === 200, '页面内直接调后端收尾成功（HTTP 200）')
  await sleep(2000)
  // 切走：新对话（只清 approval.value，不动 sessionStates[sid].approval）
  await evaluate(`document.querySelector('.sb-new').click()`)
  await sleep(1200)
  // 切回：点侧边栏里那条会话
  const clicked = await evaluate(`(() => {
    const items = Array.from(document.querySelectorAll('.sb-item'))
    const it = items.find((e) => e.textContent.includes(${JSON.stringify(GHOST_MATCH)})) || items[0]
    if (!it) return false
    it.click()
    return true
  })()`)
  ok(clicked, '能在侧边栏点回该会话')
  await sleep(4500)
  s = await evaluate(SNAP)
  ok(!s.popup, '★ 切回后不再出现幽灵弹窗（旧实现会照搬内存里的旧弹窗）')
  ok((await evaluate(fetchApprovalPending(S3))) === false, '后端确认该会话已无待审批')

  // ---------- 场景 3：有挂起中断时切"自动执行" ----------
  console.log('\n[3] 开关：有挂起中断时切「自动执行」')
  s = await openSession(evaluate, send, S2, 'S2 打开')
  ok(s.popup, 'S2 挂起会话打开后有弹窗')
  ok(s.toggle === '需要审批', `开关初始为「需要审批」（实际「${s.toggle}」）`)

  await evaluate(`Array.from(document.querySelectorAll('button.tbtn.switchable')).find(b => /需要审批|自动执行/.test(b.textContent)).click()`)
  for (let i = 0; i < 60; i++) {
    await sleep(1000)
    const t = await evaluate(SNAP)
    if (t.toggle === '自动执行' && !t.popup) break
  }
  s = await evaluate(SNAP)
  ok(s.toggle === '自动执行', `开关已切到「自动执行」（实际「${s.toggle}」）`)
  ok(!s.popup, '切到自动执行后弹窗消失')
  ok((await evaluate(fetchApprovalPending(S2))) === false, '★ 后端确实把挂起中断收尾了（不是"看不见"）')
  const reply2 = await evaluate(fetchLastReply(S2))
  ok(reply2.length > 0, `被放弃的那一轮补上了最终回复（${reply2.length} 字）`)

  // 后端把 cleared 列表回给前端 → 前端立刻重载这些会话的历史。
  // 不刷新的话，用户仍然只看到"什么都没说的半轮"（后端已写好，界面停在旧快照）。
  let uiText = ''
  for (let i = 0; i < 20; i++) {
    uiText = (await evaluate(SNAP)).lastBubble
    if (uiText && !/没有留下文字回复/.test(uiText)) break
    await sleep(1000)
  }
  ok(uiText && !/没有留下文字回复/.test(uiText),
     `界面立刻显示了那轮的收尾回复（${JSON.stringify(uiText.slice(0, 44))}）`)

  // ---------- 场景 4：再切回"需要审批"：不许复活 ----------
  console.log('\n[4] 再切回「需要审批」')
  await evaluate(`Array.from(document.querySelectorAll('button.tbtn.switchable')).find(b => /需要审批|自动执行/.test(b.textContent)).click()`)
  for (let i = 0; i < 30; i++) {
    await sleep(1000)
    if ((await evaluate(SNAP)).toggle === '需要审批') break
  }
  await sleep(2500)
  s = await evaluate(SNAP)
  ok(s.toggle === '需要审批', `开关回到「需要审批」（实际「${s.toggle}」）`)
  ok(!s.popup, '★ 重开人工介入后旧弹窗不复活')
  ok((await evaluate(fetchApprovalPending(S2))) === false, '后端也确认没有待审批')

  // ---------- 收尾 ----------
  const shot = await send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/hitl_toggle_shot.png`, Buffer.from(shot.data, 'base64'))
  console.log(`\n截图: ${outDir}/hitl_toggle_shot.png`)

  console.log('\n[5] 控制台')
  if (netErrors.length) console.log(`   网络层 4xx 日志 ${netErrors.length} 条（预期噪音）: ${netErrors[0]}`)
  ok(errors.length === 0, `无 JS 报错${errors.length ? '：' + errors.slice(0, 3).join(' | ') : ''}`)

  const failed = results.filter((r) => !r[0]).length
  console.log(`\n==== ${results.length - failed}/${results.length} 通过 ====`)
  ws.close()
  process.exit(failed ? 1 : 0)
})().catch((e) => { console.log('异常:', e.message, e.stack); process.exit(2) })
