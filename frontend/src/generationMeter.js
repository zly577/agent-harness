// Recent stream speed is estimated because chunks have no exact token counts.
export const RATE_WINDOW_MS = 2000
export function recordDelta(message, text, at = Date.now()) {
  if (!text || message.endedAt) return
  const cjk = (text.match(/[\u3400-\u9fff]/g) || []).length
  const sample = { at, tokens: cjk + (text.length - cjk) / 4 }
  message.rateSamples = [...(message.rateSamples || []).filter(x => x.at > at - RATE_WINDOW_MS), sample]
}
export function instantaneousRate(message, at = Date.now()) {
  if (message.endedAt) return message.finalRate || 0
  const tokens = (message.rateSamples || []).reduce((sum, x) => x.at > at - RATE_WINDOW_MS && x.at <= at ? sum + x.tokens : sum, 0)
  const seconds = Math.max(0.5, Math.min(RATE_WINDOW_MS, at - (message.rateStartedAt ?? message.startedAt ?? at)) / 1000)
  return tokens / seconds
}
export function freezeMeter(message, at = Date.now()) {
  if (!message || message.endedAt) return
  message.finalRate = instantaneousRate(message, at)
  message.endedAt = at
  message.rateSamples = []
}
export function resumeMeter(message, at = Date.now()) {
  if (message.endedAt && message.startedAt) message.startedAt += at - message.endedAt
  message.endedAt = null
  message.rateStartedAt = at
  message.rateSamples = []
  message.finalRate = 0
}

export function elapsedSecondsLabel(startedAt, endedAt = null, now = Date.now()) {
  if (!startedAt) return ''
  const seconds = Math.max(0, Math.floor(((endedAt ?? now) - startedAt) / 1000))
  return `${seconds} 秒`
}

export function answerTiming(message, now = Date.now()) {
  let state = 'done'
  let label = '已完成'
  if (message.connectionLost) { state = 'interrupted'; label = '连接中断' }
  else if (message.error) { state = 'error'; label = '未完成' }
  else if (message.stopped) { state = 'stopped'; label = '已停止' }
  else if (message.planPaused) { state = 'paused'; label = '等待确认' }
  else if (message.streaming && !message.endedAt) { state = 'live'; label = '运行中' }
  const elapsed = elapsedSecondsLabel(message.startedAt, message.endedAt, now)
  return { state, text: `${label} · ${state === 'live' ? '' : '用时 '}${elapsed}` }
}
