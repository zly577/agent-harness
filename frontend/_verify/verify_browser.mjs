// verify_browser.mjs — 无头 Edge + CDP：真实渲染验证（几何 / 配色 / 交互 / 事件流）
// 用法: node verify_browser.mjs <port> <url> <outDir>
import fs from 'node:fs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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
  if (r.exceptionDetails) throw new Error('页面异常: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails))
  return r.result.value
}

const problems = []
const ok = (c, label, detail = '') => {
  console.log(`  ${c ? '✅' : '❌'} ${label}${detail && !c ? ' — ' + detail : ''}`)
  if (!c) problems.push(label)
}

const PROBE = `(() => {
  const $ = (s) => document.querySelector(s)
  const $$ = (s) => Array.from(document.querySelectorAll(s))
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } }
  const cs = (el, p) => el ? getComputedStyle(el).getPropertyValue(p) : null
  const out = {}
  out.viewport = { w: innerWidth, h: innerHeight }
  out.scheme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  out.shim = {
    app: !!$('.app'), sidebar: !!$('.sidebar'), resizer: !!$('.sb-resizer'),
    topbar: !!$('.topbar'), messages: !!$('.messages'), composer: !!$('.composer-inner'),
    sendBtn: !!$('.btn-send'), chips: $$('.chip').length, emptyTitle: !!$('.empty-title'),
  }
  out.color = { bodyBg: cs(document.body, 'background-color'), bodyText: cs(document.body, 'color') }
  out.geom = {
    sidebar: box($('.sidebar')),
    resizer: box($('.sb-resizer')),
    composer: box($('.composer-inner')),
    rowInner: box($('.row-inner')),
    emptyMark: box($('.empty-mark')),
  }
  if (out.geom.rowInner) {
    out.geom.rowInnerCx = out.geom.rowInner.x + Math.round(out.geom.rowInner.w / 2)
    out.geom.viewportCx = Math.round(innerWidth / 2)
  }
  out.overflowX = document.documentElement.scrollWidth > innerWidth + 1
  return out
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
        errors.push('log: ' + ev.params.entry.text.slice(0, 300))
      }
    }
  }

  await cdp.send('Page.navigate', { url })
  await ensureBrowserLoggedIn((e) => evaluate(cdp, e), sleep)
  await sleep(3500)
  drain()

  const result = { url, errors }
  const before = await evaluate(cdp, PROBE)
  result.before = before

  console.log('================ 浏览器端渲染验证 ================')
  console.log(`  视口 ${before.viewport.w}×${before.viewport.h}  配色模式=${before.scheme}`)
  console.log('')

  console.log('[1] 骨架与关键元素')
  ok(before.shim.app, '.app 挂载成功（Vue 已渲染）')
  ok(before.shim.sidebar && before.shim.resizer, '侧栏与拖拽手柄存在')
  ok(before.shim.topbar && before.shim.messages && before.shim.composer, '顶栏/消息区/输入框存在')
  ok(before.shim.emptyTitle && before.shim.chips === 4, `欢迎页渲染（建议词 ${before.shim.chips} 个）`)
  ok(!before.overflowX, '无横向溢出')

  console.log('\n[2] 布局几何')
  const g = before.geom
  ok(g.sidebar.w >= 200 && g.sidebar.w <= 460, `侧栏宽度 ${g.sidebar.w}px 在 [200,460] 内`)
  ok(g.resizer && g.resizer.h > 100, `拖拽手柄可命中（高 ${g.resizer?.h}px）`)
  ok(g.composer.w <= 780 && g.composer.w >= 400, `输入框宽度 ${g.composer.w}px 合理（居中列）`)
  ok(g.composer.x + g.composer.w / 2 === Math.round(before.viewport.w / 2) ||
     Math.abs((g.composer.x + g.composer.w / 2) - (g.sidebar.w + before.viewport.w) / 2) < 60,
     '输入框大致居中于主区')
  ok(!!g.emptyMark, '欢迎页图标已定位')

  console.log('\n[3] 配色（跟随系统）')
  ok(!!before.color.bodyBg, `body 背景 ${before.color.bodyBg}`)
  ok(!!before.color.bodyText, `body 前景 ${before.color.bodyText}`)

  console.log('\n[4] 交互：建议词')
  await evaluate(cdp, `document.querySelector('.chip')?.click()`)
  await sleep(400)
  const chip = await evaluate(cdp, `(() => { const t = document.querySelector('.composer textarea'); return { v: t?.value || '', focused: document.activeElement === t } })()`)
  ok(chip.v.length > 0, `点击建议词后输入框被填入：「${chip.v.slice(0, 20)}…」`)
  ok(chip.focused, '输入框获得焦点')

  console.log('\n[5] 计时器 + tok/s：发一条真实消息（走 SSE）')
  await evaluate(cdp, `(() => {
    const t = document.querySelector('.composer textarea')
    t.value = ''
    t.dispatchEvent(new Event('input', { bubbles: true }))
  })()`)
  await sleep(200)
  // 用原生 setter 让 v-model 同步，再点发送
  await evaluate(cdp, `(() => {
    const t = document.querySelector('.composer textarea')
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
    setter.call(t, '你好，请用一句话自我介绍')
    t.dispatchEvent(new Event('input', { bubbles: true }))
    return t.value
  })()`)
  await sleep(300)
  await evaluate(cdp, `document.querySelector('.btn-send')?.click()`)

  // 采样计时器在流式过程中的变化
  const samples = []
  for (let i = 0; i < 26; i++) {
    await sleep(700)
    const s = await evaluate(cdp, `(() => {
      const m = document.querySelector('.meter')
      const txt = document.querySelector('.prose')?.innerText || ''
      const thinking = !!document.querySelector('.thinking')
      return { meter: m ? m.innerText.replace(/\\n/g, ' | ') : null, proseLen: txt.length, thinking }
    })()`)
    samples.push(s)
    if (s.meter && s.proseLen > 0 && i > 3) break
  }
  drain()
  const meterSeen = samples.filter((s) => s.meter)
  result.meterSamples = samples.slice(-6)
  ok(meterSeen.length > 0, '计时器/速率元素已出现', JSON.stringify(samples.slice(0, 3)))
  if (meterSeen.length) {
    const first = meterSeen[0].meter
    const last = meterSeen[meterSeen.length - 1].meter
    ok(/\d+(\.\d+)?s|\d+m\d+s/.test(first), `计时格式正确：${first}`)
    ok(/tok\/s/.test(first), `含 tok/s 速率：${first}`)
    ok(first !== last, `计时在实时变化：${first}  →  ${last}`)
  }

  // 等这轮跑完，看最终态
  let finalState = null
  for (let i = 0; i < 30; i++) {
    await sleep(1000)
    finalState = await evaluate(cdp, `(() => {
      const meters = Array.from(document.querySelectorAll('.meter')).map((m) => m.innerText.replace(/\\n/g, ' '))
      const rows = document.querySelectorAll('.row').length
      const tools = !!document.querySelector('.tools')
      const toolsSum = document.querySelector('.tools-sum')?.innerText || null
      const subagents = document.querySelectorAll('.subagent').length
      const archived = document.querySelectorAll('.tool-subagent').length
      const thinking = !!document.querySelector('.thinking')
      const last = document.querySelector('.row:last-child .prose')
      return { meters, rows, tools, toolsSum, subagents, archived, thinking, prose: last ? last.innerText.slice(0, 80) : '' }
    })()`)
    if (!finalState.thinking) break
  }
  drain()
  result.finalState = finalState
  console.log('\n[6] 完成后状态')
  ok(finalState.rows >= 2, `消息行 ${finalState.rows} 条（用户 + 助手）`)
  ok(!!finalState.prose, `助手回复已渲染：「${(finalState.prose || '').slice(0, 40)}」`)
  ok(!finalState.thinking, '流式结束（打字指示消失）')
  ok(finalState.meters.length >= 1, `计时器保留在回复下方：${finalState.meters[0] || ''}`)
  ok(!finalState.subagents, '没有遗留的运行中子智能体卡片（该轮无委派）')

  console.log('\n[7] 侧栏拖拽')
  const drag = await evaluate(cdp, `(() => {
    const r = document.querySelector('.sb-resizer')
    const b = r.getBoundingClientRect()
    const before = document.querySelector('.sidebar').getBoundingClientRect().width
    const opts = { bubbles: true, clientX: b.left + 3, clientY: b.top + b.height / 2, button: 0, pointerId: 1, isPrimary: true }
    r.dispatchEvent(new PointerEvent('pointerdown', opts))
    window.dispatchEvent(new PointerEvent('pointermove', { ...opts, clientX: b.left + 123 }))
    const during = document.querySelector('.sidebar').getBoundingClientRect().width
    window.dispatchEvent(new PointerEvent('pointerup', { ...opts, clientX: b.left + 123 }))
    return { before, during, after: document.querySelector('.sidebar').getBoundingClientRect().width }
  })()`)
  result.drag = drag
  ok(drag.during > drag.before + 80, `拖动后侧栏变宽：${drag.before} → ${drag.during}px`)
  ok(drag.after === drag.during, '松手后宽度保持')

  // 双击复位
  await evaluate(cdp, `(() => { const r = document.querySelector('.sb-resizer'); r.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })) })()`)
  await sleep(300)
  const reset = await evaluate(cdp, `document.querySelector('.sidebar').getBoundingClientRect().width`)
  ok(Math.round(reset) === 260, `双击复位到默认宽度：${Math.round(reset)}px`)

  drain()
  result.errors = errors.slice()

  // 截图
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/shot.png`, Buffer.from(shot.data, 'base64'))

  console.log('\n[8] 控制台错误')
  ok(errors.length === 0, `无 JS 报错（${errors.length} 条）`, errors.slice(0, 3).join(' || '))

  fs.writeFileSync(`${outDir}/result.json`, JSON.stringify(result, null, 2), 'utf8')

  console.log('')
  if (problems.length) {
    console.log(`❌ ${problems.length} 项未通过：`)
    for (const p of problems) console.log('  - ' + p)
  } else {
    console.log('✅ 浏览器端全部通过')
  }
  cdp.ws.close()
  process.exitCode = problems.length ? 1 : 0
}

main().catch((e) => { console.error('VERIFY FAILED:', e.message); process.exitCode = 1 })
