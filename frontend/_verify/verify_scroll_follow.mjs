// verify_scroll_follow.mjs — 验证"流式输出时用户能自由上下滑动"
//
// 要证明的行为：
//   A) 用户没碰滚动条时，输出持续增长仍自动贴底（原有能力不能丢）
//   B) 用户往上滚 → 立即停止跟随；之后模型再吐多少字，**视口都不动**
//   C) 停手后出现"回到底部"按钮；下方来了新内容时按钮带提示点
//   D) 点按钮 → 平滑回到底部、按钮消失、跟随恢复
//
// 用法: node _verify/verify_scroll_follow.mjs <devtools端口> <页面地址> <输出目录>
import fs from 'node:fs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// 要一段**较长**的流式输出，才好在中间做"上滑"动作
const ASK =
  '请用中文写一份较长的技术说明，主题是「浏览器渲染管线」。' +
  '分成 12 个小节，每节用 3~4 句话展开。不要调用任何工具，直接输出正文。'

async function waitEndpoint() {
  for (let i = 0; i < 75; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
      if (page) return page
    } catch {}
    await sleep(400)
  }
  throw new Error('devtools endpoint 未就绪')
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []
    ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data)
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id)
        this.pending.delete(m.id)
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result)
      } else if (m.method) this.events.push(m)
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise((res, rej) => this.pending.set(id, { resolve: res, reject: rej }))
  }
}

const connect = (u) => new Promise((res, rej) => {
  const ws = new WebSocket(u)
  ws.addEventListener('open', () => res(new CDP(ws)))
  ws.addEventListener('error', (e) => rej(new Error('ws: ' + (e.message || '?'))))
})

async function evaluate(cdp, expr) {
  const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) {
    throw new Error('页面异常: ' + (r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails)))
  }
  return r.result.value
}

const problems = []
const ok = (c, label, detail = '') => {
  console.log(`  ${c ? '✅' : '❌'} ${label}${detail && !c ? ' — ' + detail : ''}`)
  if (!c) problems.push(label)
}

const SCROLL_STATE = `(() => {
  const el = document.querySelector('.messages')
  if (!el) return null
  const btn = document.querySelector('.jump-bottom')
  return {
    top: Math.round(el.scrollTop),
    max: Math.round(el.scrollHeight - el.clientHeight),
    gap: Math.round(el.scrollHeight - el.scrollTop - el.clientHeight),
    proseLen: (document.querySelector('.row:last-child .prose')?.innerText || '').length,
    btn: !!btn,
    btnFresh: btn ? btn.classList.contains('fresh') : false,
    smooth: getComputedStyle(el).scrollBehavior,
  }
})()`

const main = async () => {
  const page = await waitEndpoint()
  const cdp = await connect(page.webSocketDebuggerUrl)
  await cdp.send('Runtime.enable')
  await cdp.send('Page.enable')
  await cdp.send('Log.enable')

  const errors = []
  const drain = () => {
    for (const ev of cdp.events.splice(0)) {
      if (ev.method === 'Runtime.exceptionThrown') {
        errors.push('exception: ' + (ev.params.exceptionDetails?.exception?.description || '').slice(0, 300))
      } else if (ev.method === 'Runtime.consoleAPICalled' && ev.params.type === 'error') {
        errors.push('console.error: ' + (ev.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 300))
      } else if (ev.method === 'Log.entryAdded' && ev.params.entry.level === 'error') {
        const e = ev.params.entry
        errors.push(`log: ${e.text.slice(0, 200)}${e.url ? ' @' + e.url : ''}`)
      }
    }
  }

  await cdp.send('Page.navigate', { url })
  await ensureBrowserLoggedIn((e) => evaluate(cdp, e), sleep)
  await sleep(3500)
  drain()

  console.log('============ 流式输出 · 自由滚动验证 ============')

  const init = await evaluate(cdp, SCROLL_STATE)
  ok(init && init.smooth === 'auto',
    `消息区不用 CSS 平滑滚动（scroll-behavior=${init?.smooth}）——否则动画会和手动滚动抢控制权`)

  console.log('\n[1] 发一条会长输出的消息')
  await evaluate(cdp, `(() => {
    const t = document.querySelector('.composer textarea')
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
    setter.call(t, ${JSON.stringify(ASK)})
    t.dispatchEvent(new Event('input', { bubbles: true }))
  })()`)
  await sleep(250)
  await evaluate(cdp, `document.querySelector('.btn-send')?.click()`)
  console.log('  已发送')

  // ---------- A) 不碰滚动条：应当持续贴底 ----------
  const phaseA = []
  for (let i = 0; i < 40; i++) {
    await sleep(500)
    const s = await evaluate(cdp, SCROLL_STATE)
    if (!s) continue
    phaseA.push(s)
    if (s.proseLen > 400 && s.max > 200) break
  }
  drain()
  const aGrown = phaseA.length > 1 && phaseA[phaseA.length - 1].proseLen > phaseA[0].proseLen
  const aGaps = phaseA.map((s) => s.gap)
  const aMaxGap = Math.max(...aGaps)
  console.log(`\n[2] 未干预时自动贴底（正文 ${phaseA[0]?.proseLen} → ${phaseA[phaseA.length - 1]?.proseLen} 字符）`)
  ok(aGrown, '正文在持续增长（流式生效）')
  ok(aMaxGap <= 80, `增长过程中始终贴底（最大离底 ${aMaxGap}px）`)
  ok(!phaseA[phaseA.length - 1].btn, '贴底时不显示"回到底部"按钮')

  // ---------- B) 用户上滑：必须立刻停手 ----------
  console.log('\n[3] 用户往上滑（关键回归）')
  // 先用滚轮事件测"手势一上来就解除跟随"
  await evaluate(cdp, `(() => {
    const el = document.querySelector('.messages')
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: -140, bubbles: true, cancelable: true }))
  })()`)
  await sleep(200)
  const afterWheel = await evaluate(cdp, SCROLL_STATE)
  ok(afterWheel.btn, '滚轮向上后立即出现"回到底部"（手势先于滚动生效）')

  // 再真的把视口挪到历史位置
  await evaluate(cdp, `(() => {
    const el = document.querySelector('.messages')
    el.scrollTop = Math.max(0, el.scrollTop - 500)
  })()`)
  await sleep(300)
  const scrolled = await evaluate(cdp, SCROLL_STATE)
  const anchorTop = scrolled.top
  console.log(`  已上滑到 scrollTop=${anchorTop}（可滚动范围 ${scrolled.max}）`)

  // 观察 8 秒：模型还在吐字，视口应当纹丝不动
  const phaseB = []
  for (let i = 0; i < 16; i++) {
    await sleep(500)
    const s = await evaluate(cdp, SCROLL_STATE)
    if (s) phaseB.push(s)
  }
  drain()
  const last = phaseB[phaseB.length - 1]
  const grewWhilePaused = last.proseLen > scrolled.proseLen
  const maxTop = Math.max(...phaseB.map((s) => s.top))
  const jumpedToBottom = phaseB.some((s) => s.gap <= 10)
  console.log(`  期间正文 ${scrolled.proseLen} → ${last.proseLen} 字符`)
  ok(grewWhilePaused, `上滑期间内容仍在增长（+${last.proseLen - scrolled.proseLen} 字符）`)
  ok(!jumpedToBottom && maxTop <= anchorTop + 8,
    `视口没有被拽回底部（停在 ${maxTop}px，初始 ${anchorTop}px）`)
  ok(!!last.btn, '"回到底部"按钮保持可见')
  ok(last.btnFresh, '按钮带上了"下方有新内容"的提示点')

  // 把这个状态截下来（这才是用户视角的"我在翻历史，模型在下面继续写"）
  const shotPaused = await cdp.send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/scroll_paused_shot.png`, Buffer.from(shotPaused.data, 'base64'))

  // ---------- D) 点按钮回到底部 ----------
  console.log('\n[4] 点"回到底部"')
  await evaluate(cdp, `document.querySelector('.jump-bottom')?.click()`)
  await sleep(1200)
  const afterJump = await evaluate(cdp, SCROLL_STATE)
  ok(afterJump.gap <= 80, `视口回到底部（离底 ${afterJump.gap}px）`)
  ok(!afterJump.btn, '按钮消失')

  // 恢复跟随后应继续贴底
  await sleep(1500)
  const resumed = await evaluate(cdp, SCROLL_STATE)
  ok(resumed.gap <= 80 || !resumed.btn,
    `恢复跟随后继续贴底（离底 ${resumed.gap}px，正文 ${resumed.proseLen} 字符）`)

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/scroll_shot.png`, Buffer.from(shot.data, 'base64'))

  console.log('\n[5] 控制台错误')
  ok(errors.length === 0, `无 JS 报错（${errors.length} 条）`, errors.slice(0, 3).join(' || '))

  fs.writeFileSync(`${outDir}/scroll_result.json`,
    JSON.stringify({ errors, init, phaseA, scrolled, phaseB, afterJump, resumed }, null, 2), 'utf8')

  console.log('')
  if (problems.length) {
    console.log(`❌ ${problems.length} 项未通过：`)
    for (const p of problems) console.log('  - ' + p)
  } else {
    console.log('✅ 自由滚动全部通过')
  }
  cdp.ws.close()
  process.exitCode = problems.length ? 1 : 0
}

main().catch((e) => { console.error('VERIFY FAILED:', e.message); process.exitCode = 1 })
