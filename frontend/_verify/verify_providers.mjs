// verify_providers.mjs — P18 模型供应商界面回归（不需要跑模型）
//
// 覆盖：
//   1. 顶栏有模型选择器，且列出了可用供应商（含系统级）
//   2. 打开「模型」弹层 → 列表渲染出供应商，且**只显示密钥掩码**
//   3. 表单：模型名没填时「保存」是禁用的（避免存一条用不了的配置）
//   4. 「测试连接」指向一个必然连不上的地址 → 给出中文的"连接不上"提示
//   5. 新增 → 出现在列表里 → 删除 → 消失（完整闭环）
//
// ⚠️ 这个脚本会**真的创建一条供应商**（名字固定为 `__e2e_probe__`）然后删掉它。
//    因为"第一条私有供应商会被自动设为默认"，跑完必须确认它已被删除 ——
//    所以配套的清理命令要**无条件执行**（见 README/CI 里的用法），
//    别用 `&&` 串起来。
//
// 用法: node _verify/verify_providers.mjs <devtools端口> <页面地址> <邮箱> <口令> [输出目录]
import fs from 'node:fs'

const [, , portArg, url, email, password, outDir = '.'] = process.argv
if (!portArg || !url || !email || !password) {
  console.error('用法: node verify_providers.mjs <端口> <页面地址> <邮箱> <口令> [输出目录]')
  process.exit(2)
}

const PROBE_NAME = '__e2e_probe__'
const BAD_BASE_URL = 'http://127.0.0.1:9/v1'   // 保留端口，必然拒连
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const LOG_PATH = `${outDir}/verify_providers.log`
const log = (...parts) => {
  const line = parts.join(' ')
  try { fs.appendFileSync(LOG_PATH, line + '\n') } catch { /* 忽略 */ }
  console.log(line)
}

const WATCHDOG_MS = 120000
const watchdog = setTimeout(() => {
  log(`\n❌ 看门狗：${WATCHDOG_MS / 1000}s 内未完成，判定卡住`)
  process.exit(4)
}, WATCHDOG_MS)

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error('找不到可用的浏览器页面（Edge 是否带 --remote-debugging-port 启动？）')
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res) => ws.addEventListener('open', res))
let id = 0
const pending = new Map()
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m)
    pending.delete(m.id)
  }
})
const send = (method, params = {}) =>
  new Promise((res) => {
    const i = ++id
    pending.set(i, (msg) => res(msg.error ? { __cdpError: msg.error } : msg.result))
    ws.send(JSON.stringify({ id: i, method, params }))
  })
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r && r.__cdpError) throw new Error(`CDP 求值失败: ${r.__cdpError.message}`)
  if (r && r.exceptionDetails) {
    throw new Error(r.exceptionDetails.exception?.description || 'eval error')
  }
  return r ? r.result.value : undefined
}

const problems = []
const ok = (c, label, detail = '') => {
  log(`  ${c ? '✅' : '❌'} ${label}${detail && !c ? ' — ' + detail : ''}`)
  if (!c) problems.push(label)
}

async function waitFor(expr, timeout = 10000, step = 200) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeout) {
    try { if (await evaluate(expr)) return true } catch { /* 页面可能在导航中 */ }
    await sleep(step)
  }
  return false
}

const shot = async (name) => {
  try {
    const r = await Promise.race([send('Page.captureScreenshot', {}), sleep(3000).then(() => null)])
    if (r && r.data) fs.writeFileSync(`${outDir}/${name}`, Buffer.from(r.data, 'base64'))
  } catch { /* 截图失败不影响结论 */ }
}

const setInput = (selector, value) => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(selector)})
  if (!el) return false
  el.value = ${JSON.stringify(value)}
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
  return true
})()`)

/** 按钮按文案找（比 nth-child 稳，改布局不会误点）。 */
const clickByText = (selector, text) => evaluate(`(() => {
  const els = [...document.querySelectorAll(${JSON.stringify(selector)})]
  const el = els.find((e) => (e.textContent || '').trim().includes(${JSON.stringify(text)}))
  if (!el) return false
  el.click()
  return true
})()`)

process.on('unhandledRejection', (e) => {
  log(`\n❌ 未捕获的 Promise 异常：${e && e.stack ? e.stack : e}`)
  process.exit(1)
})

log('============ P18 模型供应商界面回归 ============\n')
await send('Runtime.enable')
await send('Page.enable')

// ---------- 登录 ----------
await send('Page.navigate', { url })
await sleep(1200)
await evaluate(`window.localStorage.clear()`)
await send('Page.reload', {})
await waitFor(`!!document.querySelector('.auth-card')`, 15000)
await setInput('.auth-field input[type=text]', email)
await setInput('.auth-field input[type=password]', password)
await evaluate(`document.querySelector('.auth-submit').click()`)
const entered = await waitFor(`!!document.querySelector('.app')`, 20000)
ok(entered, '登录成功进入主界面')
if (!entered) { log('  登录失败，无法继续'); clearTimeout(watchdog); process.exit(1) }

// 选择器要等 loadProviders 完成
await waitFor(`document.querySelectorAll('.model-select option').length > 1`, 12000)
await sleep(400)

// ---------- 1) 模型选择器 ----------
const sel = await evaluate(`(() => {
  const el = document.querySelector('.model-select')
  if (!el) return null
  return { count: el.options.length, texts: [...el.options].map(o => o.textContent.trim()), disabled: el.disabled }
})()`)
ok(sel !== null, '顶栏存在模型选择器')
ok(sel && !sel.texts.some((t) => t.includes('跟随默认')),
   `选择器里不再有「跟随默认」（实际：${sel ? sel.texts.join(' / ') : ''}）`)
ok(sel && sel.texts.some((t) => t.includes('DeepSeek')),
   `选择器列出了自己的供应商（${sel ? sel.texts.join(' / ') : ''}）`)
await shot('providers_selector_shot.png')

// 选择器的"未选中"状态必须**看得出来**：这个状态下发不出消息，不能藏着
const unsetSync = await evaluate(`(() => {
  const el = document.querySelector('.model-select')
  if (!el) return null
  return {
    unset: el.classList.contains('unset'),
    value: el.value,
    hasPlaceholder: [...el.options].some(o => o.textContent.trim() === '请选择模型'),
    selected: [...el.options].filter(o => o.selected).map(o => o.textContent.trim()),
  }
})()`)
ok(unsetSync && (unsetSync.value ? !unsetSync.unset : unsetSync.unset),
   `「未选中」高亮与真实选中状态一致（value=${unsetSync ? unsetSync.value : '?'}, unset=${unsetSync ? unsetSync.unset : '?'}）`)
ok(!unsetSync || unsetSync.value || unsetSync.hasPlaceholder,
   '未选中时显示「请选择模型」占位，而不是一个空白下拉框')

// ---------- 2) 打开弹层 ----------
await clickByText('.topbar-actions .tbtn', '模型')
const opened = await waitFor(`!!document.querySelector('.pv')`, 8000)
ok(opened, '点「模型」打开了供应商弹层')
await waitFor(`document.querySelectorAll('.pv-item').length > 0`, 10000)

const modal = await evaluate(`(() => {
  const el = document.querySelector('.pv')
  if (!el) return null
  const txt = el.textContent || ''
  return {
    items: document.querySelectorAll('.pv-item').length,
    hasSysTag: txt.includes('系统级'),   // 2026-09-18 起应当**没有**了（系统级已退役）
    masked: /\\*{4}/.test(txt),
    // 明文密钥的形状：sk- 后面跟 16 位以上
    leaksPlaintext: /sk-[A-Za-z0-9_-]{16,}/.test(txt),
    hasEmpty: !!document.querySelector('.pv-empty'),
  }
})()`)
ok(modal && modal.items > 0, `列表渲染出供应商（${modal ? modal.items : 0} 条）`)
ok(modal && !modal.hasSysTag,
   '列表里没有「系统级」—— 它已退役并收编为我的私有供应商（每个用户必须自己配模型）')
ok(modal && modal.masked, '密钥显示为掩码（含 ****）')
ok(modal && !modal.leaksPlaintext, '列表里没有明文密钥')
await shot('providers_list_shot.png')

// 点遮罩（弹层外的空白）**不该**关闭窗口 —— 填了一半的表单不能一下点丢
await evaluate(`(() => { document.querySelector('.pv-overlay').click(); return true })()`)
await sleep(400)
ok(await evaluate(`!!document.querySelector('.pv')`),
   '点弹层外的遮罩不会关闭窗口（只能靠「取消/关闭/✕」主动关）')

// ---------- 3) 新增表单 ----------
await clickByText('.pv-actions .pv-btn', '新增供应商')
const formOpen = await waitFor(`!!document.querySelector('.pv-form')`, 6000)
ok(formOpen, '点「新增供应商」打开了表单')

const saveDisabled = await evaluate(`(() => {
  const b = [...document.querySelectorAll('.pv-actions .pv-btn')].find(e => (e.textContent||'').includes('保存'))
  return b ? b.disabled : null
})()`)
ok(saveDisabled === true, '模型名为空时「保存」是禁用的（避免存一条用不了的配置）')

// ---------- 4) 测试连接（必然失败，验中文错误分类）----------
const fields = await evaluate(`(() => {
  const inputs = [...document.querySelectorAll('.pv-form .pv-field input')]
  return inputs.map(i => i.type)
})()`)
ok(Array.isArray(fields) && fields.length >= 6, `表单字段齐全（${fields ? fields.length : 0} 个输入）`)

await evaluate(`(() => {
  const inputs = [...document.querySelectorAll('.pv-form .pv-field input')]
  const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })) }
  // 顺序：name, base_url, model, api_key, temperature, timeout, max_tokens
  set(inputs[0], ${JSON.stringify(PROBE_NAME)})
  set(inputs[1], ${JSON.stringify(BAD_BASE_URL)})
  set(inputs[2], 'probe-model')
  set(inputs[3], 'sk-e2eprobe0123456789')
  return true
})()`)
await clickByText('.pv-form .pv-btn', '测试连接')
const gotResult = await waitFor(`!!document.querySelector('.pv-result')`, 30000)
ok(gotResult, '「测试连接」返回了结果')
const testMsg = await evaluate(`(() => {
  const el = document.querySelector('.pv-result')
  return el ? { text: (el.textContent || '').trim(), bad: el.classList.contains('bad') } : null
})()`)
ok(testMsg && testMsg.bad, '连不上时标记为失败状态')
ok(testMsg && (testMsg.text.includes('连接') || testMsg.text.includes('失败')), `给出中文原因：${testMsg ? testMsg.text.slice(0, 60) : ''}`)
ok(testMsg && !testMsg.text.includes('sk-e2eprobe0123456789'), '测试结果里没有泄漏明文密钥')
await shot('providers_test_fail_shot.png')

// ---------- 5) 保存 → 列表出现 → 删除 → 消失 ----------
await clickByText('.pv-form .pv-btn', '保存')
const saved = await waitFor(
  `[...document.querySelectorAll('.pv-item')].some(e => (e.textContent||'').includes(${JSON.stringify(PROBE_NAME)}))`,
  15000,
)
ok(saved, '保存后新供应商出现在列表里')

// 删除前的确认框：用 window.confirm 覆盖，避免依赖 CDP 的对话事件
await evaluate(`window.confirm = () => true`)
await evaluate(`(() => {
  const row = [...document.querySelectorAll('.pv-item')].find(e => (e.textContent||'').includes(${JSON.stringify(PROBE_NAME)}))
  if (!row) return false
  const btn = [...row.querySelectorAll('.pv-mini')].find(b => (b.textContent||'').includes('删除'))
  if (!btn) return false
  btn.click()
  return true
})()`)
const removed = await waitFor(
  `![...document.querySelectorAll('.pv-item')].some(e => (e.textContent||'').includes(${JSON.stringify(PROBE_NAME)}))`,
  15000,
)
ok(removed, '删除后它从列表里消失')
await shot('providers_after_delete_shot.png')

// 关闭弹层，回到对话界面
await clickByText('.pv-actions .pv-btn', '关闭')
await waitFor(`!document.querySelector('.pv')`, 6000)
ok(await evaluate(`!!document.querySelector('.app') && !document.querySelector('.pv')`), '弹层关闭后回到对话界面')

log(`\n============ 结果：${problems.length ? `❌ ${problems.length} 项未通过` : '✅ 全部通过'} ============`)
if (problems.length) problems.forEach((p) => log(`   - ${p}`))
clearTimeout(watchdog)
process.exit(problems.length ? 1 : 0)
