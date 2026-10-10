// SSE 流式对话消费：用 fetch + ReadableStream 解析 text/event-stream
// 事件格式（后端 /api/v1/chat/stream 与 /api/v1/approvals/{id}）：
//   data: {"type":"session"|"decision"|"token"|"thinking"|"tool_args"|"tool_call"|"tool_result"|"approval"|"result"|"error", ...}
//   event: done
//
// 其中 `tool_args` 是**工具参数还在生成**时的节流心跳（大参数实测要几十秒到几分钟，
// 而 `tool_call` 必须等参数 JSON 拼完整才发得出来）。旧前端收到它会走
// `applyDataEvent` 的 default 分支被忽略，所以前后端可以独立上线。
//
// P18：所有请求统一走 ./api 的封装 —— 它会自动注入 Authorization 头，
// 并在收到 401 时清凭证 + 通知界面回登录页。**SSE 流里的 401 也走同一条路径**，
// 所以"后端重启导致凭证失效"时用户看到的是回登录页，而不是一个静默断掉的流。

import { apiFetch, apiFetchRaw } from './api'

/**
 * 发起 SSE 流式对话。
 * @param {string} message 用户消息
 * @param {string|null} sessionId 会话 id（'new' 表示新建）
 * @param {AbortSignal} signal 取消信号
 * @param {(evt:object)=>void} onEvent 事件回调
 * @param {string|null} [providerId] 本轮使用的模型供应商 id（P18）。
 *   传空 = 不指定，由后端按「会话已绑定 → 用户默认 → 系统默认」解析。
 *   注意它会**写回会话**（后续轮次沿用），不是"只影响这一轮"的临时值。
 */
export async function streamChat(message, sessionId, signal, onEvent, providerId = null) {
  // 后端：session_id 缺省即新建；"new" 也让它缺省新建
  const body = sessionId && sessionId !== 'new'
    ? { message, session_id: sessionId }
    : { message }
  if (providerId) body.provider_id = providerId

  await consumeStream('/api/v1/chat/stream', body, signal, onEvent)
}

/**
 * 提交审批决策（approve / reject）。后端以同一套 SSE 事件流回恢复执行的过程，
 * 所以这里复用同一个解析器实时渲染工具调用与最终回复。
 * @param {string} sessionId 会话 id
 * @param {'approve'|'reject'} decision 决策
 * @param {AbortSignal} signal 取消信号
 * @param {(evt:object)=>void} onEvent 事件回调
 */
export async function streamApproval(sessionId, decision, signal, onEvent) {
  await consumeStream(
    `/api/v1/approvals/${encodeURIComponent(sessionId)}`,
    { decision },
    signal,
    onEvent,
  )
}

/**
 * 读取人工介入（HITL）开关状态。
 * @param {string} [sessionId] 传了则看该会话的开关（可能被单独覆盖）
 * @returns {Promise<{enabled:boolean, scope:string, global_default:boolean}>}
 */
export async function fetchHitlConfig(sessionId) {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  return apiFetch(`/api/v1/hitl/config${qs}`)
}

/**
 * 热插拔人工介入开关。
 * @param {boolean} enabled true = 访问本机文件需人工同意；false = 全程自动执行
 * @param {string} [sessionId] 只对该会话生效；不传则改自己的默认值
 */
export async function setHitlConfig(enabled, sessionId) {
  return apiFetch('/api/v1/hitl/config', {
    method: 'PUT',
    body: sessionId ? { enabled, session_id: sessionId } : { enabled },
  })
}

/**
 * 读取权限模式（P21）。
 *
 * 两种模式：
 * - `sandbox` 默认权限：文件读写落工作区，execute 进沙箱；访问本机文件需审批。
 * - `full`    完全访问：execute 直接在本机 shell 跑（deepagents LocalShellBackend），不审批。
 *
 * 后端保存的是**内存态**（重启回到安全默认 sandbox），所以每次进入界面都重新拉一次。
 * @param {string} [sessionId] 传了则看该会话的有效值
 * @returns {Promise<{mode:string,label:string,full_access:boolean,scope:string,valid_modes:string[]}>}
 */
export async function fetchAccessConfig(sessionId) {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  return apiFetch(`/api/v1/access/config${qs}`)
}

/**
 * 切换权限模式（P21）。
 *
 * 后端会重建该用户的 Agent 缓存（backend / permissions 是编译期输入），
 * 所以切换后**下一轮对话**才用新模式 —— 正在跑的那一轮不受影响。
 * @param {'sandbox'|'full'} mode 目标模式
 * @param {string} [sessionId] 只对该会话生效；不传则改自己的默认值
 */
export async function setAccessConfig(mode, sessionId) {
  return apiFetch('/api/v1/access/config', {
    method: 'PUT',
    body: sessionId ? { mode, session_id: sessionId } : { mode },
  })
}

/**
 * 查询某会话后端是否还有正在进行的回答。
 *
 * 运行跑在后端（与页面无关），所以刷新 / 关标签页 / 切会话之后，
 * 靠这个接口 + 下面的 attach 接口就能"接回去"继续看，而不用重跑。
 * @param {string} sessionId 会话 id
 * @returns {Promise<{running:boolean,status:string,user_message:string,error:string,started_at?:number}>}
 */
export async function fetchRunState(sessionId) {
  return apiFetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/run`)
}

/**
 * 查询某会话是否有待审批项。
 *
 * 用于刷新/切会话后**把审批弹窗恢复出来**：审批中断发生时如果用户恰好不在页面上
 * （刷新、关了标签页），回来时必须还能看到"有一个操作在等你确认"，
 * 否则这一轮就静默卡在中断态了。
 * @param {string} sessionId 会话 id
 * @returns {Promise<{session_id:string,pending:boolean,actions:Array}>}
 */
export async function fetchApproval(sessionId) {
  return apiFetch(`/api/v1/approvals/${encodeURIComponent(sessionId)}`)
}

/**
 * 接回该会话正在进行的回答：后端先回放已产生的事件，再接上直播。
 * 与 streamChat 共用同一套事件格式，所以消费端可以复用同一个事件处理器。
 * @param {string} sessionId 会话 id
 * @param {AbortSignal} signal 取消信号
 * @param {(evt:object)=>void} onEvent 事件回调
 */
export async function streamRunAttach(sessionId, signal, onEvent) {
  await consumeStream(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/run/stream`,
    null,
    signal,
    onEvent,
    'GET',
  )
}

/**
 * **用户明确停止**本轮回答。
 *
 * 这是除"关闭服务"之外唯一会中断运行的方式 —— 刷新页面、关标签页、
 * 切会话都只是断开连接，后端那一轮会继续跑完。
 * @param {string} sessionId 会话 id
 */
export async function stopRun(sessionId) {
  return apiFetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/stop`, {
    method: 'POST',
  })
}

/** POST（或 GET）一个 SSE 端点并逐条派发事件。 */
async function consumeStream(url, body, signal, onEvent, method = 'POST') {
  // apiFetchRaw 已经处理了 401（清凭证 + 通知回登录页）与非 2xx 的中文错误文案；
  // 这里只需要拿到 Response 自己读流。抛出的 ApiError 带 .status，调用方照旧可用。
  const resp = await apiFetchRaw(url, { method, body, signal })
  if (!resp.body) throw new Error('响应无数据流')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // 按 SSE 空行切分
    const chunks = buffer.split('\n\n')
    buffer = chunks.pop() // 最后一段可能不完整
    for (const chunk of chunks) {
      parseChunk(chunk, onEvent)
    }
  }
  // 处理剩余 buffer
  if (buffer.trim()) {
    parseChunk(buffer, onEvent)
  }
}

function parseChunk(chunk, onEvent) {
  let dataLine = null
  let eventName = 'message'
  for (const line of chunk.split('\n')) {
    if (line.startsWith('data:')) {
      dataLine = line.slice(5).trim()
    } else if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    }
  }
  if (dataLine === null || dataLine === '') return
  let parsed = null
  try { parsed = JSON.parse(dataLine) } catch { parsed = { raw: dataLine } }
  parsed.event = eventName
  onEvent(parsed)
}
