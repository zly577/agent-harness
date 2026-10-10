// verify_subagent_ui.mjs — 子智能体展示改造的真实渲染验证（无头 Edge + CDP）
//
// 验证四件事（对应本次需求）：
//   1) 委派卡片只展示**摘要**，完整 description 收在默认收起的 details 里
//   2) 子智能体的输出/推理**实时流式**出现在卡片上
//   3) 任务完成后卡片消失，内容**并入工具调用列表**（.tool-subagent），且默认收起
//   4) 回复下方有**计时器 + 每秒 token**，且流式期间数字在变
//
// 用法: node _verify/verify_subagent_ui.mjs <devtools端口> <页面地址> <输出目录>
import fs from 'node:fs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const ASK =
  '请委派 researcher 子智能体调研：2025 年国内大模型推理成本的变化趋势。' +
  '要求：1) 联网检索多个来源；2) 给出 3 条结论；3) 结论标注来源链接。'

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

// 一次性抓取页面上的关键状态
const SNAP = `(() => {
  const cards = Array.from(document.querySelectorAll('.subagent'))
  const card = cards[0]
  const full = card ? card.querySelector('.subagent-full') : null
  const stream = card ? card.querySelector('.subagent-stream') : null
  const reason = card ? card.querySelector('.reasoning') : null
  const meter = document.querySelector('.row:last-child .meter')
  const tools = document.querySelector('.row:last-child .tools')
  const cardInfo = cards.map((c) => ({
    type: (c.querySelector('.subagent-name')?.innerText || ''),
    summaryLen: (c.querySelector('.subagent-summary')?.innerText || '').length,
    streamLen: (c.querySelector('.subagent-stream')?.innerText || '').length,
    thinkLen: (c.querySelector('.reasoning-text')?.textContent || '').length,
    tools: c.querySelectorAll('.subagent-chip').length,
  }))
  return {
    subagentCards: cards.length,
    cardInfo,
    // 幽灵卡片检测：有正文/推理，却没有任务摘要 —— 说明内容挂到了别的 id 上
    ghostCards: cardInfo.filter((c) => (c.streamLen > 0 || c.thinkLen > 0) && c.summaryLen === 0).length,
    archived: document.querySelectorAll('.tool-subagent').length,
    summary: card ? (card.querySelector('.subagent-summary')?.innerText || '') : null,
    fullOpen: full ? full.hasAttribute('open') : null,
    fullTextLen: full ? (full.textContent || '').length : null,
    cardTextLen: card ? (card.innerText || '').length : null,
    streamLen: stream ? (stream.innerText || '').length : 0,
    hasReasoning: !!reason,
    reasoningMeta: reason ? (reason.querySelector('.reasoning-meta')?.innerText || '') : null,
    subagentState: card ? (card.querySelector('.subagent-state')?.innerText || '') : null,
    subagentElapsed: card ? (card.querySelector('.subagent-elapsed')?.innerText || '') : null,
    meter: meter ? (meter.innerText || '').replace(/\\n/g, ' | ') : null,
    meterLive: meter ? meter.classList.contains('live') : false,
    toolsOpen: tools ? tools.hasAttribute('open') : null,
    toolsSummary: tools ? (tools.querySelector('.tools-sum')?.innerText || '') : null,
    thinkingDots: !!document.querySelector('.thinking'),
    proseLen: (document.querySelector('.row:last-child .prose')?.innerText || '').length,
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
        // 带上 url，便于区分"应用报错"和"favicon 之类的环境噪音"
        const e = ev.params.entry
        errors.push(`log: ${e.text.slice(0, 200)}${e.url ? ' @' + e.url : ''}`)
      }
    }
  }

  await cdp.send('Page.navigate', { url })
  await ensureBrowserLoggedIn((e) => evaluate(cdp, e), sleep)
  await sleep(3500)
  drain()

  console.log('============ 子智能体展示改造 · 浏览器端验证 ============')

  console.log('\n[1] 发起一次会触发委派的消息')
  await evaluate(cdp, `(() => {
    const t = document.querySelector('.composer textarea')
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
    setter.call(t, ${JSON.stringify(ASK)})
    t.dispatchEvent(new Event('input', { bubbles: true }))
  })()`)
  await sleep(300)
  await evaluate(cdp, `document.querySelector('.btn-send')?.click()`)
  console.log('  已发送')

  // ---------- 运行中采样 ----------
  const samples = []
  let sawCard = false
  for (let i = 0; i < 90; i++) {
    await sleep(1000)
    let s
    try { s = await evaluate(cdp, SNAP) } catch (e) { drain(); continue }
    samples.push(s)
    if (s.subagentCards > 0) sawCard = true
    if (sawCard && s.subagentCards === 0) break    // 委派已收尾并归档
    if (!sawCard && s.archived > 0) break
  }
  drain()

  const during = samples.filter((s) => s.subagentCards > 0)
  const withMeter = samples.filter((s) => s.meter)
  fs.writeFileSync(`${outDir}/subagent_samples.json`, JSON.stringify(samples, null, 2), 'utf8')

  console.log(`\n[2] 运行中的委派卡片（采样 ${samples.length} 次，其中 ${during.length} 次看到卡片）`)
  ok(during.length > 0, '委派卡片出现过（子智能体过程可见）')

  // ---- 核心回归：不允许出现"有正文没摘要"的幽灵卡片 ----
  // （子图命名空间 id 与 task 调用 id 不一致时就会出现：卡片 A 只有摘要永远空白，
  //   卡片 B 只有正文没有摘要。实测原实现必现。）
  const ghostSamples = samples.filter((s) => s.ghostCards > 0)
  ok(ghostSamples.length === 0,
    `没有幽灵卡片（有正文无摘要的卡片）—— 出现 ${ghostSamples.length} 次`,
    ghostSamples.length ? JSON.stringify(ghostSamples[0].cardInfo) : '')

  const withContent = during.filter((s) =>
    s.cardInfo.some((c) => c.streamLen > 0 || c.thinkLen > 0))
  ok(withContent.length > 0, `有卡片同时展示了摘要与正文（${withContent.length}/${during.length} 次采样）`)
  if (withContent.length) {
    const c = withContent[0].cardInfo.find((x) => x.streamLen > 0 || x.thinkLen > 0)
    console.log(`  同一张卡片上：摘要 ${c.summaryLen} 字符 + 正文 ${c.streamLen} 字符 + 推理 ${c.thinkLen} 字符 + 工具 ${c.tools} 个`)
  }

  if (during.length) {
    const first = during[0]
    console.log(`  卡片摘要：「${(first.summary || '').slice(0, 70)}」`)
    ok(!!(first.summary || '').trim(), '卡片展示了任务摘要')
    ok((first.summary || '').length <= 200,
      `摘要是"压成一行"的（${(first.summary || '').length} 字符）`)
    ok(first.fullOpen === false, '完整任务描述默认收起（不糊用户一脸）')
    ok((first.fullTextLen || 0) > (first.summary || '').length,
      `完整描述确实存在且更长（${first.fullTextLen} vs ${(first.summary || '').length} 字符）`)
    ok(first.cardTextLen < first.fullTextLen + 400,
      '收起态卡片文本量受控（未把全文摊在卡片上）')

    // 流式增长：卡片正文/推理数字在变大
    const totals = during.map((s) => s.cardInfo.reduce((a, c) => a + c.streamLen + c.thinkLen, 0))
    ok(totals[totals.length - 1] > totals[0],
      `子智能体输出在实时增长（${totals[0]} → ${totals[totals.length - 1]} 字符）`)
    const elapsedSet = new Set(during.map((s) => s.subagentElapsed))
    ok(elapsedSet.size > 1, `卡片上的耗时在走（${[...elapsedSet].slice(0, 3).join(' → ')} …）`)
    // 中间态就应该已经有内容（而不是等到结束才一次性出现）
    const mid = during[Math.floor(during.length / 2)]
    const midLen = mid.cardInfo.reduce((a, c) => a + c.streamLen + c.thinkLen, 0)
    ok(midLen > 0, `跑到一半时卡片已有内容（${midLen} 字符）——不是结束时才一次性出现`)
  }

  console.log('\n[3] 计时器 + 每秒 token（实时）')
  ok(withMeter.length > 0, '回复下方出现了计时器/速率元素')
  if (withMeter.length) {
    const first = withMeter[0].meter
    const last = withMeter[withMeter.length - 1].meter
    console.log(`  首次：${first}`)
    console.log(`  末次：${last}`)
    ok(/\d+(\.\d+)?s|\d+m\d+s/.test(first), `含计时：${first}`)
    ok(/tok\/s/.test(first), `含每秒 token：${first}`)
    ok(new Set(withMeter.map((s) => s.meter)).size > 1,
      `计量数字在实时变化（${withMeter.length} 次采样中有 ${new Set(withMeter.map((s) => s.meter)).size} 种取值）`)
  }

  // ---------- 收尾态 ----------
  // 注意：委派归档只说明"子任务结束"，主智能体此时还要生成最终回复。
  // 必须等到整轮真的停下来（折叠条显示"已完成" + 打字指示消失），否则会把
  // "还没轮到写回复"误判成"回复没渲染"。
  let fin = samples[samples.length - 1]
  for (let i = 0; i < 90; i++) {
    fin = await evaluate(cdp, SNAP)
    const settled = fin.subagentCards === 0 && fin.archived > 0
      && /已完成/.test(fin.toolsSummary || '') && !fin.thinkingDots
    if (settled) break
    await sleep(1000)
  }
  drain()

  console.log('\n[4] 完成后：并入工具调用列表')
  ok(fin.subagentCards === 0, '运行中卡片已消失')
  ok(fin.archived >= 1, `委派已归档进工具列表（.tool-subagent ×${fin.archived}）`)
  ok(fin.toolsOpen === false, '工具调用块默认收起（内容"隐匿在后面"）')
  ok(/委派子智能体/.test(fin.toolsSummary || ''), `折叠条摘要说明了委派：${fin.toolsSummary}`)
  ok(fin.proseLen > 0, `助手回复已渲染（${fin.proseLen} 字符）`)
  ok(!!fin.meter && /tok\/s/.test(fin.meter), `收尾后计量保留在回复下方：${fin.meter}`)

  // 展开归档项，确认能查到完整内容（先展开外层工具块，否则 innerText 取不到隐藏子树）
  await evaluate(cdp, `(() => { const t = document.querySelector('.row:last-child .tools'); if (t) t.setAttribute('open','') })()`)
  await sleep(200)
  const expanded = await evaluate(cdp, `(() => {
    const d = document.querySelector('.tool-subagent')
    if (!d) return null
    d.setAttribute('open', '')
    const t = d.textContent || ''
    return { len: t.length, hasTask: /任务/.test(t), hasReport: /回报/.test(t) }
  })()`)
  ok(expanded && expanded.len > 200 && expanded.hasTask,
    `展开归档项能看到完整任务（${expanded?.len} 字符，"任务"=${expanded?.hasTask}，"回报"=${expanded?.hasReport}）`)

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/subagent_shot.png`, Buffer.from(shot.data, 'base64'))

  console.log('\n[5] 控制台错误')
  ok(errors.length === 0, `无 JS 报错（${errors.length} 条）`, errors.slice(0, 3).join(' || '))

  fs.writeFileSync(`${outDir}/subagent_result.json`,
    JSON.stringify({ errors, samples, final: fin, expanded }, null, 2), 'utf8')

  console.log('')
  if (problems.length) {
    console.log(`❌ ${problems.length} 项未通过：`)
    for (const p of problems) console.log('  - ' + p)
  } else {
    console.log('✅ 子智能体展示改造全部通过')
  }
  cdp.ws.close()
  process.exitCode = problems.length ? 1 : 0
}

main().catch((e) => { console.error('VERIFY FAILED:', e.message); process.exitCode = 1 })
