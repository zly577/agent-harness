// 验证"多操作审批弹窗"渲染：打开一个挂有多个待审批项的会话，
// 检查弹窗列出全部操作、路径都显示出来、按钮可用，并截图。
// 不做任何决策（不点同意/拒绝），所以不会触发模型调用。
// 用法: node verify_multi_approval.mjs <debugPort> <url> <outDir> <sessionId>
import fs from 'node:fs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const port = Number(process.argv[2] || 9333)
const url = process.argv[3] || 'http://127.0.0.1:5174/'
const outDir = process.argv[4] || '_verify'
const sid = process.argv[5]

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
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data)
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); return }
    if (m.method === 'Runtime.exceptionThrown') {
      errors.push(m.params.exceptionDetails?.exception?.description || 'exception')
    } else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      const t = m.params.entry.text || ''
      // favicon 之类噪音忽略
      if (!/favicon/.test(t)) errors.push(t.slice(0, 200))
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
  return { ws, send, evaluate, errors }
}

const SNAP = `(() => {
  const modal = document.querySelector('.approval-modal')
  if (!modal) return { open: false }
  const items = Array.from(modal.querySelectorAll('.approve-item'))
  return {
    open: true,
    title: modal.querySelector('h3')?.textContent || '',
    subtitle: modal.querySelector('.modal-head p')?.textContent?.trim() || '',
    note: modal.querySelector('.modal-note')?.textContent?.trim() || '',
    count: items.length,
    items: items.map((it) => ({
      tool: it.querySelector('.approve-tool')?.textContent?.trim() || '',
      index: it.querySelector('.approve-index')?.textContent?.trim() || '',
      keys: Array.from(it.querySelectorAll('.approve-arg-k')).map((e) => e.textContent.trim()),
      firstVal: it.querySelector('.approve-arg-v')?.textContent?.trim()?.slice(0, 60) || '',
      empty: !!it.querySelector('.approve-arg-empty'),
    })),
    buttons: Array.from(modal.querySelectorAll('.modal-foot button')).map((b) => ({
      text: b.textContent.trim(), disabled: b.disabled,
    })),
    bodyScrollable: (() => { const b = modal.querySelector('.modal-body'); return b ? b.scrollHeight > b.clientHeight : null })(),
    modalH: Math.round(modal.getBoundingClientRect().height),
    viewportH: window.innerHeight,
  }
})()`

;(async () => {
  const { ws, send, evaluate, errors } = await connect()
  await send('Runtime.enable'); await send('Page.enable'); await send('Log.enable')
  await send('Page.navigate', { url })
  await ensureBrowserLoggedIn(evaluate, sleep)
  await sleep(2500)
  await evaluate(`localStorage.setItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous'), ${JSON.stringify(sid)})`)
  await send('Page.reload', {})
  await sleep(7000)

  let s = null
  for (let i = 0; i < 20; i++) {
    s = await evaluate(SNAP)
    if (s && s.open) break
    await sleep(1000)
  }
  console.log('\n[1] 多操作审批弹窗')
  ok(s?.open, '会话打开后弹窗自动恢复出来（刷新/切会话不丢）')
  if (s?.open) {
    console.log(`  标题「${s.title}」/ 副标题「${s.subtitle}」`)
    console.log(`  说明「${s.note}」`)
    s.items.forEach((it, i) => console.log(`   [${i}] ${it.tool} ${it.index} keys=${it.keys.join(',')} val=${JSON.stringify(it.firstVal)} empty=${it.empty}`))
    console.log(`  按钮: ${JSON.stringify(s.buttons)}  body可滚动=${s.bodyScrollable}  弹窗高=${s.modalH}/${s.viewportH}`)
    ok(s.count >= 2, `弹窗列出了全部待审批操作（${s.count} 条，旧实现只显示第 1 条）`)
    ok(s.items.every((it) => it.tool), '每条都显示了工具名')
    ok(s.items.some((it) => it.keys.includes('file_path')), '显示了操作对象（file_path）')
    ok(s.count > 1 ? !!s.note : true, '多操作时给出了"已处理项再批准也无副作用"的说明')
    ok(s.buttons.length === 2 && s.buttons.every((b) => !b.disabled), '两个按钮都可用（不会停在"处理中…"）')
    ok(s.modalH <= s.viewportH, `弹窗没有超出视口（${s.modalH} ≤ ${s.viewportH}）`)
  }

  const shot = await send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/multi_approval_shot.png`, Buffer.from(shot.data, 'base64'))
  console.log(`\n截图: ${outDir}/multi_approval_shot.png`)

  await sleep(400)
  console.log('\n[2] 控制台')
  ok(errors.length === 0, `无 JS 报错${errors.length ? '：' + errors.slice(0, 3).join(' | ') : ''}`)

  const failed = results.filter((r) => !r[0]).length
  console.log(`\n==== ${results.length - failed}/${results.length} 通过 ====`)
  ws.close()
  process.exit(failed ? 1 : 0)
})().catch((e) => { console.log('异常:', e.message); process.exit(2) })
