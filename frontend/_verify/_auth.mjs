// 共用：给验证脚本拿一个 access token（P18 起后端业务接口默认拒绝）。
//
// ## 为什么要有这个文件
//
// P18 之前，验证脚本都是直接 `fetch(backend + '/api/v1/...')` —— 后端没有任何鉴权，
// 所以能跑。加了"默认拒绝"之后这类调用一律 401，脚本会以一种很容易被误读为
// "服务挂了"的方式失败（拿不到数据 → 后面报"找不到会话"）。
//
// 这里把"登录拿 token + 拼请求头"收成一处，脚本只改几行。
//
// ## 凭证从环境变量读，不写进代码
//
//   AH_EMAIL=admin@local AH_PASSWORD=... node _verify/xxx.mjs ...
//
// 刻意不在脚本里放默认口令：这些文件是入库的，把口令写进去等于把口令提交到仓库。
// 缺少变量时**直接报错退出**，而不是静默用一个空 token 把每个请求都跑成 401 ——
// 那种失败方式让人以为是权限问题。

/**
 * 登录并返回 access token。
 * @param {string} backend 后端基址，如 http://127.0.0.1:8010
 * @returns {Promise<string>} token
 */
export async function loginToken(backend) {
  const email = process.env.AH_EMAIL
  const password = process.env.AH_PASSWORD
  if (!email || !password) {
    console.error(
      '缺少凭证：请用 AH_EMAIL / AH_PASSWORD 环境变量提供账号。\n' +
      '例：AH_EMAIL=admin@local AH_PASSWORD=*** node _verify/verify_xxx.mjs ...\n' +
      '（刻意不写默认口令：这些脚本会入库。）',
    )
    process.exit(2)
  }
  const resp = await fetch(`${backend}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) {
    console.error(`登录失败：HTTP ${resp.status} —— ${(await resp.text()).slice(0, 200)}`)
    process.exit(2)
  }
  const data = await resp.json()
  return data.access_token
}

/** 带凭证的请求头。 */
export function authHeaders(token, extra = {}) {
  return { ...extra, Authorization: `Bearer ${token}` }
}

/**
 * 带凭证地 GET JSON。
 * @param {string} url
 * @param {string} token
 */
export async function getJson(url, token) {
  const resp = await fetch(url, { headers: authHeaders(token) })
  if (!resp.ok) {
    throw new Error(`GET ${url} → HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`)
  }
  return resp.json()
}

/** 带凭证地 GET（要拿原始 Response 时用，比如检查状态码/响应头）。 */
export async function getRaw(url, token) {
  return fetch(url, { headers: authHeaders(token) })
}

/** 读环境变量里的凭证（同时给浏览器登录用）。 */
export function credentials() {
  const email = process.env.AH_EMAIL
  const password = process.env.AH_PASSWORD
  if (!email || !password) {
    console.error('缺少凭证：请用 AH_EMAIL / AH_PASSWORD 环境变量提供账号。')
    process.exit(2)
  }
  return { email, password }
}

/**
 * 确保浏览器处于已登录状态。
 *
 * ## 为什么每个验证脚本都需要它
 *
 * P18 之前这些脚本 `Page.navigate` 之后就直接断言界面 —— 那时后端没有鉴权，
 * 前端也没有登录页，所以能跑。现在打开页面先看到的是登录表单，
 * 后面所有 `document.querySelector('.xxx')` 都会拿到 null，
 * 而报错往往出现在**很后面**（点某个按钮时炸），看起来像"功能坏了"。
 *
 * 所以这里做成幂等的一步：看到登录页就登录，已经是主界面就直接返回。
 *
 * @param {(expr: string) => Promise<any>} evaluate 脚本自己的 CDP 求值函数
 * @param {(ms: number) => Promise<void>} sleep
 * @param {number} [timeoutMs] 等待进入主界面的上限
 * @returns {Promise<boolean>} 是否执行了登录动作
 */
export async function ensureBrowserLoggedIn(evaluate, sleep, timeoutMs = 20000) {
  const onLoginPage = await evaluate(`!!document.querySelector('.auth-card')`)
  if (!onLoginPage) return false

  const { email, password } = credentials()
  // ⚠️ 必须**分两步**（先填、等 Vue 把 `canSubmit` 重算完，再点）。
  // 在同一次求值里"填完立刻点"会踩到按钮还是 `disabled` 的那一帧 ——
  // `btn.click()` 对 disabled 按钮是**静默无效**，于是表现为"登录没反应"，
  // 再往上就变成"20s 内没进入主界面"，看起来像凭证错了。
  await evaluate(`(() => {
    const ins = [...document.querySelectorAll('.auth-field input')]
    if (ins.length < 2) return false
    const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })) }
    set(ins[0], ${JSON.stringify(email)})
    set(ins[1], ${JSON.stringify(password)})
    return true
  })()`)
  await sleep(150)
  await evaluate(`(() => {
    const btn = document.querySelector('.auth-submit')
    if (!btn || btn.disabled) return false
    btn.click()
    return true
  })()`)

  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    if (await evaluate(`!!document.querySelector('.app')`)) return true
    await sleep(250)
  }
  throw new Error(`浏览器登录失败（${timeoutMs}ms 内没进入主界面）—— 检查 AH_EMAIL/AH_PASSWORD 是否正确`)
}

/**
 * 把"上次在看的会话"写进 localStorage。
 *
 * P18 起这个 key 是**按用户分区**的（`ah.lastSessionId:<user_id>`），
 * 否则换账号登录会去开别人的会话。脚本里写死旧 key 的话，现象是
 * "设置了 key 但界面什么都没渲染"——很难联想到是 key 变了。
 */
export function setLastSessionExpr(sessionId) {
  const uid = "(JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous')"
  return `localStorage.setItem('ah.lastSessionId:' + ${uid}, ${JSON.stringify(sessionId)})`
}

/** 读"上次在看的会话"的表达式（同样要带用户分区）。 */
export function getLastSessionExpr() {
  const uid = "(JSON.parse(localStorage.getItem('ah_auth_user')||'{}').id||'anonymous')"
  return `localStorage.getItem('ah.lastSessionId:' + ${uid})`
}
