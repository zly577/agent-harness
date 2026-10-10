// verify_run_persist.mjs — 验证「工具展示重构」+「回答可续传」两件事
//
// 分两段跑，因为两件事需要不同长度的任务：
//   A 段（工具展示）：发一条会连续用 ls/glob/read_file 的短请求，验证
//     A1 不再有会随状态反复开合的外层折叠块（旧 .tools-sum 已移除）
//     A2 同一时刻只有一条 .act（最新那条），前一条进 .act-history
//     A3 **.act-history 全程保持收起**，不因新工具出现而自动开合（旧实现的核心毛病）
//     A4 只读工具被合并成一条（"N 项"），不是十几条
//     A5 左列：跑完显示**工具本身**（工具名），正在跑才显示"正在查看/正在执行…"；
//        状态只在右侧徽标上说一次（左列不再出现"已完成"，避免一句话说两遍）
//   B 段（可续传）：另开一个会话，发一条**确定会跑很久**的委派请求，
//     在它真的还在跑的时候刷新页面，验证：
//     B1 刷新后回答仍在且继续增长（接回了运行，不是从头开始、也不是没了）
//     B2 没有重复的助手气泡
//     B3 点停止 → 后端那一轮真的停掉
//
// 用法: node _verify/verify_run_persist.mjs <devtools端口> <页面地址> <输出目录>
import fs from 'node:fs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// A 段：连续只读工具（触发合并），并让它"快"（短回答）
const ASK_TOOLS =
  '请严格按顺序做三件事：\n' +
  '1) 先用 write_todos 工具把你的工作计划列成待办清单（这一步必须做）；\n' +
  '2) 再用 ls 看工作区根目录，用 glob 找所有 .md 文件，并用 read_file 读其中 2 个（只读，不要写文件、不要执行 shell）；\n' +
  '3) 最后一句话总结你看到了什么，不要展开。'

// B 段：委派子智能体 —— 实测稳定跑 60~110 秒，足够在"还在跑"的时候刷新。
// 明确禁止写文件/执行命令：避免触发 HITL 审批中断（那属于 C 段要测的场景）
const ASK_LONG =
  '请委派 researcher 子智能体调研：本地智能体应该用 SQLite 还是 Postgres 存会话状态？' +
  '让它联网检索多个来源后给出 5 条结论。注意：全程只读，不要写文件、不要修改文件、不要执行 shell 命令。'

// C 段：故意触发 HITL 审批（写 /workspace 需要人工同意），测"刷新后审批弹窗还在不在"
const ASK_WRITE =
  '请用 write_file 把字符串 hello-approval 写进 /workspace/_probe_approval.txt，然后一句话确认。'

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

const SNAP = `(() => {
  const act = document.querySelector('.act')
  const hist = document.querySelector('.act-history')
  const meter = document.querySelector('.row:last-child .meter')
  const rows = document.querySelectorAll('.row')
  const assistantRows = document.querySelectorAll('.row.assistant')
  return {
    hasAct: !!act,
    // 用 textContent：innerText 对滚出视口/未渲染的元素会返回空串（会误报）
    actVerb: act ? (act.querySelector('.act-verb')?.textContent || '').trim() : '',
    actDetail: act ? (act.querySelector('.act-detail')?.textContent || '').trim() : '',
    actBadge: act ? (act.querySelector('.act-badge')?.textContent || '').trim() : '',
    actRunning: act ? act.classList.contains('running') : false,
    actGrouped: act ? /\\d+ 项/.test(act.querySelector('.act-detail')?.innerText || '') : false,
    oldToolsBlock: !!document.querySelector('.tools-sum'),
    histOpen: hist ? hist.hasAttribute('open') : null,
    histItems: document.querySelectorAll('.act-history .act-item').length,
    // 历史列表里左列也不能出现"已完成"（右侧徽标已经在说）
    histBadVerb: Array.from(document.querySelectorAll('.act-history .act-verb'))
      .map((e) => (e.textContent || '').trim()).find((t) => /已完成|已打开/.test(t)) || '',
    histLeftLabels: Array.from(document.querySelectorAll('.act-history .act-verb'))
      .map((e) => (e.textContent || '').trim()),
    subagentCards: document.querySelectorAll('.subagent').length,
    // 子智能体实时输出的总长度：委派期间主智能体没有正文，靠它判断"在长"
    subStreamLen: Array.from(document.querySelectorAll('.subagent-stream'))
      .reduce((n, e) => n + (e.innerText || '').length, 0),
    live: meter ? meter.classList.contains('live') : false,
    meter: meter ? (meter.innerText || '').replace(/\\n/g, ' | ') : null,
    thinking: !!document.querySelector('.thinking'),
    proseLen: (document.querySelector('.row.assistant:last-of-type .prose')?.innerText || '').length,
    rows: rows.length,
    assistantRows: assistantRows.length,
    stopBtn: !!document.querySelector('.btn-stop'),
    lastTextTail: (document.querySelector('.row.assistant:last-of-type')?.innerText || '').slice(-50),
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

  const send = async (text) => {
    await evaluate(cdp, `(() => {
      const t = document.querySelector('.composer textarea')
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
      setter.call(t, ${JSON.stringify(text)})
      t.dispatchEvent(new Event('input', { bubbles: true }))
    })()`)
    await sleep(250)
    await evaluate(cdp, `document.querySelector('.btn-send')?.click()`)
  }
  const runState = () => evaluate(cdp,
    `fetch('/api/v1/sessions/' + localStorage.getItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous')) + '/run').then(r => r.json())`)

  await cdp.send('Page.navigate', { url })
  await ensureBrowserLoggedIn((e) => evaluate(cdp, e), sleep)
  await sleep(2500)
  await evaluate(cdp, `localStorage.removeItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous'))`)
  await cdp.send('Page.navigate', { url })
  await sleep(2500)
  drain()

  // ================= A 段：工具展示 =================
  console.log('====== A. 工具展示重构 ======')
  await send(ASK_TOOLS)
  console.log('  已发送（短请求）')

  const samples = []
  let sawHistoryOpen = false, sawRunning = false, maxHist = 0
  let badLeftLabel = ''
  // 采样间隔取 300ms：工具调用的"进行中"窗口很短，700ms 会整段漏掉
  for (let i = 0; i < 70; i++) {
    await sleep(300)
    let s
    try { s = await evaluate(cdp, SNAP) } catch { break }
    samples.push(s)
    if (s.histOpen) sawHistoryOpen = true
    if (s.actRunning) sawRunning = true
    // 左列在跑完时必须显示工具名，不能是"已完成/已打开"（状态由右侧徽标负责）
    if (!s.actRunning && s.hasAct && /已完成|已打开/.test(s.actVerb)) {
      badLeftLabel = s.actVerb
    }
    if (s.histBadVerb) badLeftLabel = s.histBadVerb
    maxHist = Math.max(maxHist, s.histItems)
    if (maxHist >= 1 && s.proseLen > 20) break
  }
  drain()
  fs.writeFileSync(`${outDir}/run_persist_samples_a.json`, JSON.stringify(samples, null, 2), 'utf8')

  ok(!samples.some((s) => s.oldToolsBlock), '旧的外层折叠块 .tools-sum 已消失（不再有"整个块反复开合"）')
  ok(!sawHistoryOpen, '历史折叠块全程保持收起（新工具出现时不再自动开合）')
  ok(samples.some((s) => s.hasAct), '始终有一条"最新工具"常显（.act）')
  ok(sawRunning, `正在跑时左列显示动作文案（例：${samples.find((s) => s.actRunning)?.actVerb || ''}）`)
  ok(!badLeftLabel, `左列不再重复"已完成/已打开"（实际出现：${badLeftLabel || '无'}）`)
  // 跑完之后左列应当就是工具本身（工具名，形如 read_file / ls、glob、read_file）
  const doneSample = samples.find((s) => !s.actRunning && s.hasAct)
  console.log(`  跑完后最新一条：左列「${doneSample?.actVerb}」 右列「${doneSample?.actBadge}」`)
  ok(!!doneSample && doneSample.actVerb.length > 0, '跑完后左列有内容（工具本身）')
  ok(!!doneSample && /^[\w、，\s等个]+$/.test(doneSample.actVerb) && !/已完成|已打开/.test(doneSample.actVerb),
    `跑完后左列是工具名而不是状态（「${doneSample?.actVerb}」）`)
  ok(!!doneSample && /已完成|进行中/.test(doneSample.actBadge), `右列徽标负责说状态（「${doneSample?.actBadge}」）`)
  const grouped = samples.find((s) => s.actGrouped)
  ok(!!grouped, `只读工具被合并成一条（例：「${grouped?.actDetail || '（未采到）'}」）`)
  ok(maxHist >= 1, `前一条被挤进历史列表（最多 ${maxHist} 条）`)

  // ================= B 段：可续传 =================
  console.log('\n====== B. 回答可续传（另起一个会话）======')
  await evaluate(cdp, `document.querySelector('.sb-new')?.click()`)
  await sleep(600)
  await send(ASK_LONG)
  console.log('  已发送（会跑很久的委派请求）')

  // 等到确实在跑（委派期间主智能体没有正文，所以看的是子智能体卡片）
  let pre = null
  for (let i = 0; i < 40; i++) {
    await sleep(1000)
    pre = await evaluate(cdp, SNAP)
    if ((pre.proseLen > 0 || pre.subagentCards > 0) && (pre.live || pre.thinking)) break
  }
  const stateBefore = await runState()
  console.log(`  刷新前：正文 ${pre.proseLen} 字符，子智能体卡片 ${pre.subagentCards}，live=${pre.live}，后端 running=${stateBefore?.running}`)
  ok(stateBefore?.running === true, '刷新前后端确实还在跑（这是续传测试的前提）')

  await cdp.send('Page.reload', { ignoreCache: false })
  await sleep(4000)

  let after = null
  for (let i = 0; i < 30; i++) {
    try { after = await evaluate(cdp, SNAP) } catch { await sleep(500); continue }
    if (after.proseLen > 0 || after.subStreamLen > 0 || after.subagentCards > 0) break
    await sleep(700)
  }
  const stateAfter = await runState()
  const hasOutput = (s) => (s?.proseLen || 0) > 0 || (s?.subStreamLen || 0) > 0 || (s?.subagentCards || 0) > 0
  console.log(`  刷新后：正文 ${after?.proseLen}，子智能体输出 ${after?.subStreamLen}，卡片 ${after?.subagentCards}，live=${after?.live}，助手行 ${after?.assistantRows}，后端 running=${stateAfter?.running}`)
  ok(hasOutput(after), '刷新后这一轮的内容仍在（没有丢失）')
  ok(!!after && (after.live || after.thinking || stateAfter?.running), '刷新后仍在输出（接回了运行）')
  ok(!!after && after.assistantRows === 1, `没有重复的助手气泡（assistant 行数 ${after?.assistantRows}）`)

  const a = (after?.subStreamLen || 0) + (after?.proseLen || 0)
  await sleep(5000)
  const after2 = await evaluate(cdp, SNAP)
  const b2 = (after2.subStreamLen || 0) + (after2.proseLen || 0)
  ok(b2 > a, `刷新后内容继续增长（${a} → ${b2} 字符）`)

  // 停止（唯一会真正中断的方式）
  console.log('\n[停止]')
  const statePreStop = await runState()
  const liveNow = after2.live || after2.thinking || statePreStop?.running
  ok(liveNow, `停止前确实还在跑（后端 running=${statePreStop?.running}）`)
  ok(after2.stopBtn, '停止按钮在流式中可见')
  await evaluate(cdp, `document.querySelector('.btn-stop')?.click()`)
  await sleep(1500)
  let stopped = null
  for (let i = 0; i < 20; i++) {
    stopped = await evaluate(cdp, SNAP)
    if (!stopped.live && !stopped.thinking) break
    await sleep(700)
  }
  const statePostStop = await runState()
  drain()
  console.log(`  停止后：live=${stopped?.live}，后端 running=${statePostStop?.running} status=${statePostStop?.status}`)
  ok(statePostStop?.running === false, '后端那一轮已停止（不是只断了前端连接）')
  ok(!stopped.live && !stopped.thinking, '前端流式已结束')

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
  fs.writeFileSync(`${outDir}/run_persist_shot.png`, Buffer.from(shot.data, 'base64'))

  // ================= C 段：审批中断后刷新，弹窗要能恢复 =================
  console.log('\n====== C. 待审批弹窗的恢复 ======')
  await evaluate(cdp, `document.querySelector('.sb-new')?.click()`)
  await sleep(600)
  await send(ASK_WRITE)
  console.log('  已发送（会写 /workspace，预期触发人工审批）')

  let approvalSid = null
  let sawDialog = false
  for (let i = 0; i < 40; i++) {
    await sleep(1000)
    const dlg = await evaluate(cdp, `!!document.querySelector('.modal.approval-modal')`)
    if (dlg) { sawDialog = true; break }
  }
  approvalSid = await evaluate(cdp, `localStorage.getItem('ah.lastSessionId:' + (JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous'))`)
  const stateApproval = await runState()
  console.log(`  弹窗出现=${sawDialog}；后端状态=${JSON.stringify({ running: stateApproval?.running, status: stateApproval?.status })}`)
  drain()
  ok(sawDialog, '写操作触发了审批弹窗')
  ok(stateApproval?.status === 'awaiting_approval',
    `后端把这一轮标成"等审批"而不是"已完成"（实际 ${stateApproval?.status}）`)

  // 刷新：弹窗应当自己回来（不刷新的话这一轮就静默悬着了）
  console.log(`  刷新会话 ${approvalSid} …`)
  await cdp.send('Page.reload', { ignoreCache: false })
  await sleep(5000)
  let restored = false
  for (let i = 0; i < 20; i++) {
    const dlg = await evaluate(cdp, `!!document.querySelector('.modal.approval-modal')`)
    if (dlg) { restored = true; break }
    await sleep(700)
  }
  const actions = await evaluate(cdp,
    `Array.from(document.querySelectorAll('.approve-tool')).map(e => e.innerText)`)
  console.log(`  刷新后弹窗恢复=${restored}，待确认操作 ${JSON.stringify(actions)}`)
  ok(restored, '刷新后审批弹窗被恢复出来（这一轮不会静默卡住）')
  drain()

  console.log('\n[控制台错误]')
  ok(errors.length === 0, `无 JS 报错（${errors.length} 条）`, errors.slice(0, 3).join(' || '))

  fs.writeFileSync(`${outDir}/run_persist_result.json`,
    JSON.stringify({ errors, samplesA: samples, pre, stateBefore, after, stateAfter, after2, statePreStop, stopped, statePostStop }, null, 2), 'utf8')

  console.log('')
  if (problems.length) {
    console.log(`❌ ${problems.length} 项未通过：`)
    for (const p of problems) console.log('  - ' + p)
  } else {
    console.log('✅ 工具展示 + 可续传全部通过')
  }
  cdp.ws.close()
  process.exitCode = problems.length ? 1 : 0
}

main().catch((e) => { console.error('VERIFY FAILED:', e.message); process.exitCode = 1 })
