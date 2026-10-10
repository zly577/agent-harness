/**
 * 统一 API 请求封装。
 *
 * ## 为什么要有这一层
 *
 * 改造前 `fetch` 散在 13 处（`sse.js` 6 处 + `App.vue` 7 处），每处都自己拼
 * URL、自己判断 `resp.ok`。加认证之后这变成三个必须**处处一致**的问题：
 *
 * 1. 每个请求都要带 `Authorization`（漏一处就是一类难查的 401）；
 * 2. 任何请求拿到 401 都要"清 token + 回登录页"（漏一处就是白屏或死循环）；
 * 3. 错误提示要统一（FastAPI 的 `detail` 有字符串、也有 422 的数组两种形状）。
 *
 * 把它们收进这里之后，业务代码只关心"拿数据"，不再关心凭证与错误体形状。
 *
 * ## token 存 localStorage 的取舍
 *
 * 存 localStorage 意味着 XSS 能偷到 token（httpOnly Cookie 则不能）。选它的
 * 理由是：SSE 走 `fetch` + `ReadableStream`，用 Cookie 需要额外处理
 * `credentials`/CORS/CSRF，而这个项目是单机自用、无第三方脚本注入面。
 * 真要加强：改成 httpOnly Cookie + CSRF token，改动集中在**这一个文件**。
 */

const TOKEN_KEY = 'ah_auth_token'
const USER_KEY = 'ah_auth_user'

/** 401 的订阅者（App.vue 用它切回登录页） */
const unauthorizedHandlers = new Set()

let _token = ''
let _user = null

function safeParse(raw) {
  try {
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

// 模块加载时恢复上次登录态（localStorage 在隐私模式下可能抛异常）
try {
  _token = window.localStorage.getItem(TOKEN_KEY) || ''
  _user = safeParse(window.localStorage.getItem(USER_KEY))
} catch {
  _token = ''
  _user = null
}

/** 带状态码的 API 错误，供调用方区分 401 / 404 / 409 等。 */
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export function getToken() {
  return _token
}

export function getUser() {
  return _user
}

export function isLoggedIn() {
  return Boolean(_token)
}

/** 登录成功 / 校验通过后写入。 */
export function setSession(token, user) {
  _token = token || ''
  _user = user || null
  try {
    if (_token) window.localStorage.setItem(TOKEN_KEY, _token)
    if (_user) window.localStorage.setItem(USER_KEY, JSON.stringify(_user))
  } catch {
    /* localStorage 不可用（隐私模式）时只保留内存态 */
  }
}

/** 只更新用户信息（如改完口令/改名后刷新展示）。 */
export function setUser(user) {
  setSession(_token, user)
}

/** 清掉登录态。 */
export function clearSession() {
  _token = ''
  _user = null
  try {
    window.localStorage.removeItem(TOKEN_KEY)
    window.localStorage.removeItem(USER_KEY)
  } catch {
    /* 忽略 */
  }
}

/**
 * 订阅"凭证失效"。返回取消订阅函数。
 *
 * 为什么要订阅而不是让每个调用点自己处理：401 可能来自任意一个请求，
 * 包括用户没在看的那个 SSE 流。集中通知 → 应用只在一个地方决定"回登录页"。
 */
export function onUnauthorized(handler) {
  unauthorizedHandlers.add(handler)
  return () => unauthorizedHandlers.delete(handler)
}

function notifyUnauthorized(detail) {
  clearSession()
  for (const h of unauthorizedHandlers) {
    try {
      h(detail)
    } catch {
      /* 单个订阅者出错不能影响其它 */
    }
  }
}

/**
 * 把 FastAPI 的错误响应整理成一句能展示的中文。
 *
 * `detail` 的形状有三种，不处理会直接给用户看一个 `[object Object]`：
 * - 字符串：我们自己的 HTTPException（已经写好中文了）→ 原样用；
 * - 数组：422 校验错误 → `[{loc, msg, type}]`，取第一条拼成人话；
 * - 其它：兜底成 HTTP 状态码。
 */
function describeError(status, body) {
  const detail = body && typeof body === 'object' ? body.detail : null
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] || {}
    const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : ''
    const msg = first.msg || '参数不合法'
    return field ? `${field}：${msg}` : msg
  }
  if (status === 401) return '登录已失效，请重新登录'
  if (status === 403) return '没有权限执行该操作'
  if (status === 404) return '请求的资源不存在'
  if (status === 409) return '操作冲突，请刷新后重试'
  if (status >= 500) return `服务端错误（HTTP ${status}）`
  return `请求失败（HTTP ${status}）`
}

/**
 * 发一个请求，返回原始 `Response`（**已处理 401**）。
 *
 * 流式端点（SSE）需要拿到 Response 自己读 body，所以单独暴露这一层；
 * 普通调用请用 `apiFetch`。
 *
 * @param {string} path 以 `/api/v1/...` 开头的路径
 * @param {object} [opts]
 * @param {string} [opts.method] 默认 GET
 * @param {any}    [opts.body] 有值则 JSON 序列化并带 Content-Type
 * @param {object} [opts.headers] 额外请求头
 * @param {AbortSignal} [opts.signal]
 * @param {boolean} [opts.skipAuth] 不带 token（登录/注册用）
 * @param {boolean} [opts.silent401] 401 时不触发全局登出（用于 /auth/me 的启动探测）
 */
export async function apiFetchRaw(path, opts = {}) {
  const { method = 'GET', body, headers = {}, signal, skipAuth = false, silent401 = false } = opts

  const finalHeaders = { ...headers }
  if (!skipAuth && _token) finalHeaders.Authorization = `Bearer ${_token}`

  const init = { method, signal, headers: finalHeaders }
  if (body !== undefined && body !== null) {
    finalHeaders['Content-Type'] = 'application/json'
    init.body = typeof body === 'string' ? body : JSON.stringify(body)
  }

  const resp = await fetch(path, init)

  if (resp.status === 401) {
    // 先读出 detail（用于提示），再决定要不要动全局登录态
    let detail = '登录已失效，请重新登录'
    try {
      const clone = resp.clone()
      const j = await clone.json()
      detail = describeError(401, j)
    } catch {
      /* 响应不是 JSON（比如网关返回的 HTML）→ 用默认文案 */
    }
    if (skipAuth) {
      // 登录/注册接口自己返回的 401（例如口令错）**不是**"凭证失效"，
      // 绝不能顺手把全局登录态清掉 —— 否则用户在登录页输错一次口令，
      // 会被"登出"一次，页面跟着抖一下。
    } else if (silent401) {
      clearSession()
    } else {
      notifyUnauthorized(detail)
    }
    throw new ApiError(detail, 401, detail)
  }

  if (!resp.ok) {
    // 先按文本读（而不是直接 json()），因为"响应体是不是空的"本身就是一条重要线索：
    // - **空 body 的 5xx** 几乎一定是**开发代理连不上后端**：Vite 的 proxy 在上游
    //   ECONNREFUSED 时直接回 500 且不带 body。用户看到的"服务端错误"其实是
    //   "后端没在跑"，光看那句话根本想不到。
    // - 后端自己抛异常时，FastAPI 的 ServerErrorMiddleware 会回 **文本**
    //   "Internal Server Error"（不是 JSON），所以两者可以区分开。
    let text = ''
    try {
      text = await resp.text()
    } catch {
      /* 读不出来就当空 */
    }
    let body = null
    if (text) {
      try {
        body = JSON.parse(text)
      } catch {
        body = null // 非 JSON（HTML/纯文本）：沿用状态码兜底文案
      }
    }
    const message =
      resp.status >= 500 && !text.trim()
        ? `连不上后端（HTTP ${resp.status}）：后端进程可能已停止或正在重启，请确认后端已启动`
        : describeError(resp.status, body)
    throw new ApiError(message, resp.status, body)
  }

  return resp
}

/**
 * 发一个请求并解析 JSON 响应（401 已统一处理）。
 *
 * @returns {Promise<any>}
 * @throws {ApiError} 非 2xx
 */
export async function apiFetch(path, opts = {}) {
  const resp = await apiFetchRaw(path, opts)
  if (resp.status === 204) return null
  const text = await resp.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export const apiGet = (path, opts) => apiFetch(path, { ...opts, method: 'GET' })
export const apiPost = (path, body, opts) => apiFetch(path, { ...opts, method: 'POST', body })
export const apiPut = (path, body, opts) => apiFetch(path, { ...opts, method: 'PUT', body })
export const apiPatch = (path, body, opts) => apiFetch(path, { ...opts, method: 'PATCH', body })
export const apiDelete = (path, opts) => apiFetch(path, { ...opts, method: 'DELETE' })

// ============================================================================
// 认证相关接口
// ============================================================================

/** 启动时校验本地 token 是否还有效；有效则同时刷新用户信息。 */
export async function fetchMe() {
  return apiFetch('/api/v1/auth/me', { silent401: true })
}

export async function login(email, password) {
  const data = await apiFetch('/api/v1/auth/login', {
    method: 'POST',
    body: { email, password },
    skipAuth: true,
  })
  setSession(data.access_token, data.user)
  return data
}

export async function register(email, password, displayName) {
  return apiFetch('/api/v1/auth/register', {
    method: 'POST',
    body: { email, password, display_name: displayName || '' },
    skipAuth: true,
  })
}

export function logout() {
  clearSession()
  notifyUnauthorized('已退出登录')
}

// ============================================================================
// 模型供应商
// ============================================================================

/** 列出可用供应商（系统级 + 自己的）。**只回密钥掩码，不回明文。** */
export async function fetchProviders() {
  return apiFetch('/api/v1/llm/providers')
}

/** 供对话区「模型选择器」用的列表（只要启用的）。 */
export async function fetchAvailableProviders() {
  return apiFetch('/api/v1/llm/providers/available')
}

/**
 * 新增供应商。
 * @param {object} payload {name, protocol, base_url, api_key, model, parameters, is_default, scope}
 */
export async function createProvider(payload) {
  return apiFetch('/api/v1/llm/providers', { method: 'POST', body: payload })
}

/** 修改供应商。`api_key` 留空 = 不改动已保存的密钥。 */
export async function updateProvider(id, payload) {
  return apiFetch(`/api/v1/llm/providers/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
  })
}

export async function deleteProvider(id) {
  return apiFetch(`/api/v1/llm/providers/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export async function setDefaultProvider(id) {
  return apiFetch(`/api/v1/llm/providers/${encodeURIComponent(id)}/set-default`, {
    method: 'POST',
  })
}

/** 测试**已保存**的配置（后端解密后用一次）。 */
export async function testProvider(id) {
  return apiFetch(`/api/v1/llm/providers/${encodeURIComponent(id)}/test`, { method: 'POST' })
}

/**
 * 测试**临时**配置（"先测后存"，不落库）。
 *
 * 编辑表单里点"测试连接"走这条 —— 用户想先确认能用再保存，
 * 而不是被迫先存一个可能是错的配置。
 */
export async function testProviderConfig(payload) {
  return apiFetch('/api/v1/llm/providers/test', { method: 'POST', body: payload })
}

/** 拉取供应商的模型列表（很多兼容实现没有这个端点，失败不报错）。 */
export async function fetchProviderModels(id) {
  return apiFetch(`/api/v1/llm/providers/${encodeURIComponent(id)}/models`)
}

// ============================================================================
// MCP 工具源
// ============================================================================

/**
 * MCP 总开关与依赖状态。
 *
 * `enabled=false` 时数据库里的配置**都在**、只是不加载 —— 界面要据此提示，
 * 否则用户会以为自己配错了（表现为"配好了但工具没出现"）。
 */
export async function fetchMcpStatus() {
  return apiFetch('/api/v1/mcp/status')
}

/** 列出**我自己**的 MCP 工具源。headers/env 只回键名，值永不回显。 */
export async function fetchMcpServers() {
  return apiFetch('/api/v1/mcp/servers')
}

/**
 * 新增一个 MCP 工具源。
 * @param {object} payload {name, transport, url, headers, command, args, env, cwd, enabled}
 */
export async function createMcpServer(payload) {
  return apiFetch('/api/v1/mcp/servers', { method: 'POST', body: payload })
}

/**
 * 修改。**所有字段可选**：不传的字段不动。
 * 注意 `headers` / `env`：`null` = 不改，空对象 `{}` = 清空。
 */
export async function updateMcpServer(id, payload) {
  return apiFetch(`/api/v1/mcp/servers/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
  })
}

export async function deleteMcpServer(id) {
  return apiFetch(`/api/v1/mcp/servers/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

/** 测试**已保存**的配置（后端解密后用一次，明文不回浏览器）。 */
export async function testMcpServer(id) {
  return apiFetch(`/api/v1/mcp/servers/${encodeURIComponent(id)}/test`, { method: 'POST' })
}

/** 测试**临时**配置（先测后存，不落库）。返回的 `tools` 是拿到的工具名列表。 */
export async function testMcpServerConfig(payload) {
  return apiFetch('/api/v1/mcp/servers/test', { method: 'POST', body: payload })
}

/**
 * 丢掉我的 agent 缓存，让刚改的配置立刻生效。
 * 增删改接口后端已经自动做了这个；这里留给"改了 YAML 文件"之类的场景。
 */
export async function reloadMcp() {
  return apiFetch('/api/v1/mcp/reload', { method: 'POST', body: {} })
}

export { describeError }
