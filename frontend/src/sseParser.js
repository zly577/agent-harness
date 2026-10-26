/** Incremental SSE parsing, independent of fetch and application state. */
export function createSseParser(onEvent) {
  let buffer = ''

  function dispatch(chunk) {
    const data = []
    let eventName = 'message'
    for (const line of chunk.split(/\r?\n/)) {
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
      else if (line.startsWith('event:')) eventName = line.slice(6).trim()
    }
    if (!data.length || !data.join('\n').trim()) return
    const text = data.join('\n')
    let parsed
    try { parsed = JSON.parse(text) } catch { parsed = { raw: text } }
    parsed.event = eventName
    onEvent(parsed)
  }

  return {
    feed(text) {
      buffer += text
      const chunks = buffer.split(/\r?\n\r?\n/)
      buffer = chunks.pop()
      chunks.forEach(dispatch)
    },
    finish() {
      if (buffer.trim()) dispatch(buffer)
      buffer = ''
    },
  }
}
