// SSE 流式对话消费：用 fetch + ReadableStream 解析 text/event-stream
// 事件格式（后端 /api/v1/chat/stream）：
//   data: {"type":"token"|"tool"|"approval"|"result"|"error", ...}
//   event: done

/**
 * 发起 SSE 流式对话。
 * @param {string} message 用户消息
 * @param {string|null} sessionId 会话 id（'new' 表示新建）
 * @param {AbortSignal} signal 取消信号
 * @param {(evt:object)=>void} onEvent 事件回调
 */
export async function streamChat(message, sessionId, signal, onEvent) {
  // 后端：session_id 缺省即新建；"new" 也让它缺省新建
  const body = sessionId && sessionId !== 'new'
    ? { message, session_id: sessionId }
    : { message }

  const resp = await fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try { const j = await resp.json(); detail = j.detail || detail } catch {}
    throw new Error(detail)
  }

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
