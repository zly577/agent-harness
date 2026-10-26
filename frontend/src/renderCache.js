/** LRU with an approximate UTF-16 memory budget for source and rendered HTML. */
export function createRenderCache({ maxEntries = 128, maxBytes = 2 * 1024 * 1024 } = {}) {
  const entries = new Map()
  let bytes = 0
  return {
    get(key) {
      const entry = entries.get(key)
      if (!entry) return undefined
      entries.delete(key)
      entries.set(key, entry)
      return entry.value
    },
    set(key, value) {
      const old = entries.get(key)
      if (old) { bytes -= old.weight; entries.delete(key) }
      const weight = (key.length + value.length) * 2
      if (weight > maxBytes) return
      while (entries.size && (entries.size >= maxEntries || bytes + weight > maxBytes)) {
        const oldest = entries.keys().next().value
        bytes -= entries.get(oldest).weight
        entries.delete(oldest)
      }
      entries.set(key, { value, weight })
      bytes += weight
    },
    clear() { entries.clear(); bytes = 0 },
    get size() { return entries.size },
    get bytes() { return bytes },
  }
}
