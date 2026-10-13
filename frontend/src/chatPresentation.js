export const boundedUrlExtension = {
  name: 'boundedUrl',
  level: 'inline',
  start(src) { return src.search(/https?:\/\/|www\./i) },
  tokenizer(src) {
    const match = /^(?:https?:\/\/|www\.)[^\s<>"'`*\[\]\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]+/i.exec(src)
    if (!match) return
    let raw = match[0].replace(/[.,;:!?]+$/, '')
    while (raw.endsWith(')') && (raw.match(/\)/g) || []).length > (raw.match(/\(/g) || []).length) raw = raw.slice(0, -1)
    if (!raw || /^(?:https?:\/\/|www\.)$/i.test(raw)) return
    return { type: 'link', raw, href: /^www\./i.test(raw) ? 'https://' + raw : raw, text: raw, tokens: [{ type: 'text', raw, text: raw }] }
  },
}

export const boundedUrlTokenizer = {
  url(src) {
    // Explicit Markdown links are unaffected; stop the GFM fallback swallowing CJK prose.
    if (/^(?:https?:\/\/|www\.)/i.test(src)) return undefined
    return false
  },
}

export function resizeComposer(element) {
  if (!element) return
  element.style.height = 'auto'
  if (!element.value) {
    element.style.height = '32px'
    return
  }
  element.style.height = `${Math.min(element.scrollHeight, 200)}px`
}
