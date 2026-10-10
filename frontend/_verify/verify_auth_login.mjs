// verify_auth_login.mjs — P18 登录态端到端回归（不需要跑模型）
//
// 验证整条链路：
//   1. 没有凭证时看到的是「登录视图」，而不是一个到处报错的主界面
//   2. 输错口令 → 就地中文错误提示，且**不会**把人踢出登录页
//   3. 正确口令 → 进入主界面（会话列表渲染出来）
//   4. 刷新页面 → 仍是已登录（token 从 localStorage 恢复 + /auth/me 校验）
//   5. 点「退出」→ 回到登录视图，localStorage 里的 token 被清掉
//   6. 手工把 localStorage 里的 token 换成伪造值 → 刷新后被判定为未登录
//      （而不是"看起来登录了、每个请求都静默失败"）
//
// 用法: node _verify/verify_auth_login.mjs <devtools端口> <页面地址> <邮箱> <口令> [输出目录]
import fs from 'node:fs'

const [, , portArg, url, email, password, outDir = '.'] = process.argv
if (!portArg || !url || !email || !password) {
  console.error('用法: node verify_auth_login.mjs <端口> <页面地址> <邮箱> <口令> [输出目录]')
  process.exit(2)
}
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// 日志同时写控制台和文件。
// 为什么必须落盘：这个脚本被上层工具超时 kill 时（SIGTERM/强杀），
// Node 对**管道/重定向**的 stdout 是有缓冲的，缓冲区会连同进程一起丢掉 ——
// 于是"卡住了"的表现是"什么输出都没有"，完全没法定位。用 appendFile 立刻落盘，
// 杀掉之后还能读到卡在哪一步。
const LOG_PATH = `${outDir}/verify_auth_login.log`
const log = (...parts) => {
  const line = parts.join(' ')
  try {
    fs.appendFileSync(LOG_PATH, line + '\n')
  } catch {
    /* 落盘失败不影响控制台输出 */
  }
  console.log(line)
}

// 看门狗：任何一步长时间不返回都要显式失败，不能悄无声息地挂着
const WATCHDOG_MS = 90000
const watchdog = setTimeout(() => {
  log(`\n❌ 看门狗：${WATCHDOG_MS / 1000}s 内未完成，判定卡住`)
  process.exit(4)
}, WATCHDOG_MS)
const finish = (code) => {
  clearTimeout(watchdog)
  process.exit(code)
}

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
    // 把**整条**响应交回去：CDP 在失败时返回的是 `{error}` 而不是 `{result}`，
    // 只取 m.result 会得到 undefined，然后在下面 `r.exceptionDetails` 上抛出
    // 一个和真实原因毫无关系的 TypeError。
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
  const r = await send('Runtime.evaluate', {
    expression: expr,
    returnByValue: true,
    awaitPromise: true,
  })
  if (r && r.__cdpError) {
    // 最常见的一种：表达式返回了 DOM 节点/函数等无法按值序列化的东西。
    // 报清楚一点，否则只会看到"条件一直不成立"这种误导性现象。
    throw new Error(`CDP 求值失败（表达式是否返回了不可序列化的值？）: ${r.__cdpError.message}`)
  }
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

/**
 * 轮询等待条件成立。
 *
 * ⚠️ 表达式**必须**返回布尔值。写成 `document.querySelector('.x')` 会返回一个
 * DOM 节点 —— CDP 在 `returnByValue: true` 下无法序列化它，只会回一个 error，
 * 于是这里永远等不到 true（表现是"等满超时、然后报条件不成立"，
 * 而不是任何可见的异常）。所以下面一律用 `!!` 包一层。
 */
async function waitFor(expr, timeout = 8000, step = 200) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeout) {
    try {
      if (await evaluate(expr)) return true
    } catch { /* 页面可能在导航中，忽略后重试 */ }
    await sleep(step)
  }
  return false
}

const shot = async (name) => {
  // 截图只是辅助证据，**绝不能**因为它卡住整个验证：
  // `Page.captureScreenshot` 在无头模式下某些状态（页面正在导航/未提交首帧）会
  // 迟迟不返回，直接 await 就会把脚本挂死。加 3s 竞速，超时就放弃。
  try {
    const r = await Promise.race([
      send('Page.captureScreenshot', {}),
      sleep(3000).then(() => null),
    ])
    if (r && r.data) fs.writeFileSync(`${outDir}/${name}`, Buffer.from(r.data, 'base64'))
    else log(`  （截图 ${name} 超时，跳过）`)
  } catch { /* 截图失败不影响结论 */ }
}

// 输入框赋值：必须 dispatch input 事件，否则 Vue 的 v-model 收不到
const setInput = (selector, value) => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(selector)})
  if (!el) return false
  el.value = ${JSON.stringify(value)}
  el.dispatchEvent(new Event('input', { bubbles: true }))
  return true
})()`)

const READ = `(() => ({
  hasLogin: !!document.querySelector('.auth-card'),
  hasApp: !!document.querySelector('.app'),
  hasAuthBoot: !!document.querySelector('.auth-boot'),
  error: (document.querySelector('.auth-error') || {}).textContent || '',
  hint: (document.querySelector('.auth-hint') || {}).textContent || '',
  sessionCount: document.querySelectorAll('.sb-item').length,
  userName: (document.querySelector('.sb-user-name') || {}).textContent || '',
  role: (document.querySelector('.sb-user-role') || {}).textContent || '',
  token: (window.localStorage.getItem('ah_auth_token') || '').slice(0, 12),
}))()`

// 未捕获异常必须显式报出来并退出 —— 否则进程会"静静地"活到看门狗超时，
// 而超时的现象和真正卡死一模一样，白费排查时间。
process.on('unhandledRejection', (e) => {
  log(`\n❌ 未捕获的 Promise 异常：${e && e.stack ? e.stack : e}`)
  finish(1)
})

log('============ P18 登录态端到端回归 ============\n')
log(`  页面地址: ${url}`)

await send('Runtime.enable')
await send('Page.enable')

// ---------- 1) 干净状态：应看到登录视图 ----------
await send('Page.navigate', { url })
await sleep(1200)
await evaluate(`window.localStorage.clear()`)
await send('Page.reload', {})
await waitFor(`!!(document.querySelector('.auth-card') || document.querySelector('.app'))`, 12000)
log('  [1] 首屏已渲染')

let s = await evaluate(READ)
ok(s.hasLogin && !s.hasApp, '未登录时显示登录视图（而不是主界面）', JSON.stringify(s))
ok(s.token === '', '登录视图下本地没有 token')
await shot('auth_login_shot.png')

// ---------- 2) 口令错误：就地报错，不跳走 ----------
log('  [2] 提交错误口令')
await setInput('.auth-field input[type=text]', email)
await setInput('.auth-field input[type=password]', 'DefinitelyWrong!123')
await evaluate(`document.querySelector('.auth-submit').click()`)
await waitFor(`!!document.querySelector('.auth-error')`, 10000)
s = await evaluate(READ)
ok(!s.hasApp, '口令错误不会进入主界面')
ok(s.error.includes('邮箱或口令不正确'), '口令错误给出中文提示', s.error)
ok(s.hasLogin, '口令错误后仍停在登录视图（没有被"登出"抖一下）')
await shot('auth_wrong_password_shot.png')

// ---------- 3) 登录成功：进入主界面 + 会话列表 ----------
log('  [3] 提交正确口令')
await setInput('.auth-field input[type=password]', password)
await evaluate(`document.querySelector('.auth-submit').click()`)
const entered = await waitFor(`!!document.querySelector('.app')`, 15000)
ok(entered, '正确口令后进入主界面')
// 会话列表是异步拉的，等它出现
await waitFor(`document.querySelectorAll('.sb-item').length > 0`, 12000)
s = await evaluate(READ)
ok(Number(s.sessionCount) > 0, `侧边栏渲染出会话（${s.sessionCount} 条）`)
ok(s.token.length > 0, 'token 已写入 localStorage')
ok(s.userName.length > 0, `侧边栏显示当前用户：${s.userName} / ${s.role}`)
await shot('auth_logged_in_shot.png')

// ---------- 4) 刷新：仍是已登录 ----------
log('  [4] 刷新页面，验证登录态保持')
await send('Page.reload', {})
const stillIn = await waitFor(`!!document.querySelector('.app')`, 15000)
await waitFor(`document.querySelectorAll('.sb-item').length > 0`, 12000)
s = await evaluate(READ)
ok(stillIn && !s.hasLogin, '刷新后保持登录（/auth/me 校验通过）')
ok(Number(s.sessionCount) > 0, '刷新后会话列表仍能恢复')

// ---------- 5) 伪造 token：应被判定为未登录 ----------
log('  [5] 伪造 token 后刷新')
await evaluate(`window.localStorage.setItem('ah_auth_token', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.forged')`)
await send('Page.reload', {})
const backToLogin = await waitFor(`!!document.querySelector('.auth-card')`, 15000)
s = await evaluate(READ)
ok(backToLogin && !s.hasApp, '伪造 token 刷新后被判为未登录（不是"看着登录了、请求全失败"）')
ok(s.token === '', '伪造 token 已被清出 localStorage')
await shot('auth_forged_token_shot.png')

// ---------- 6) 退出登录（P18 起要先确认）----------
log('  [6] 登录后点退出')
await setInput('.auth-field input[type=text]', email)
await setInput('.auth-field input[type=password]', password)
await evaluate(`document.querySelector('.auth-submit').click()`)
await waitFor(`!!document.querySelector('.app')`, 15000)
await waitFor(`!!document.querySelector('.sb-user-out')`, 8000)

// 第一次点：应弹出确认框，而不是直接退出
await evaluate(`document.querySelector('.sb-user-out').click()`)
ok(await waitFor(`!!document.querySelector('.confirm-modal')`, 6000),
   '点「退出」先弹出确认框（不直接退出）')
const confirmText = await evaluate(`(document.querySelector('.confirm-modal')||{}).textContent||''`)
ok(confirmText.includes('确认退出登录'), '确认框标题是「确认退出登录」')
ok(confirmText.includes(email), '确认框里写明了当前账号')
await shot('auth_logout_confirm_shot.png')

// 点「取消」：必须**不**退出（这条是防误触的关键断言）
await evaluate(`document.querySelector('.confirm-modal .btn-ghost').click()`)
ok(await waitFor(`!document.querySelector('.confirm-modal') && !!document.querySelector('.app')`, 6000),
   '点「取消」关闭弹窗且仍停留在主界面')
s = await evaluate(READ)
ok(s.token.length > 0, '取消退出后 token 仍在（没有被清掉）')

// 再点一次并确认，这次要真的退出
await evaluate(`document.querySelector('.sb-user-out').click()`)
await waitFor(`!!document.querySelector('.confirm-modal')`, 6000)
await evaluate(`document.querySelector('.confirm-modal .btn-danger').click()`)
const loggedOut = await waitFor(`!!document.querySelector('.auth-card')`, 10000)
s = await evaluate(READ)
ok(loggedOut && !s.hasApp, '确认后回到登录视图')
ok(s.token === '', '退出后 token 已清除')

log(`\n============ 结果：${problems.length ? `❌ ${problems.length} 项未通过` : '✅ 全部通过'} ============`)
if (problems.length) {
  problems.forEach((p) => log(`   - ${p}`))
  finish(1)
}
// 注意：通过时也必须显式退出并停掉看门狗，否则进程会一直挂着，
// 直到看门狗把一次"成功"的验证报成"超时失败"。
finish(0)
