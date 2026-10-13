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
