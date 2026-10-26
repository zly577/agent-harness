import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick, reactive, ref } from 'vue'
import { createRenderCache } from '../src/renderCache.js'
import { scrollSignature, useMessageScroll } from '../src/composables/useMessageScroll.js'

test('scroll observation avoids walking old messages and tool arguments', () => {
  const old = { role: 'assistant' }
  Object.defineProperty(old, 'content', { get() { throw new Error('old history should not be traversed') } })
  const tool = { id: 't', status: 'running' }
  Object.defineProperty(tool, 'args', { get() { throw new Error('large arguments should not be traversed') } })
  const signature = scrollSignature([old, { content: 'tail', toolCalls: [tool] }])
  assert.ok(signature.includes('running'))
})

test('scroll follows content but stops when the user leaves the bottom', async () => {
  const scope = effectScope()
  const messages = reactive([{ content: '', toolCalls: [], delegations: [] }])
  const element = { scrollHeight: 1000, scrollTop: 500, clientHeight: 500 }
  let scroll
  scope.run(() => { scroll = useMessageScroll(messages, ref(element)) })
  messages[0].content = 'hello'
  await nextTick(); await nextTick()
  assert.equal(element.scrollTop, 1000)
  element.scrollTop = 0
  scroll.onMessagesScroll()
  messages[0].content += 'world'
  await nextTick(); await nextTick()
  assert.equal(element.scrollTop, 0)
  assert.equal(scroll.hasNewBelow.value, true)
  scope.stop()
})

test('render cache is bounded by memory and keeps recently used entries', () => {
  const cache = createRenderCache({ maxEntries: 2, maxBytes: 100 })
  cache.set('a', 'A')
  cache.set('b', 'B')
  assert.equal(cache.get('a'), 'A')
  cache.set('c', 'C')
  assert.equal(cache.get('b'), undefined)
  assert.equal(cache.get('a'), 'A')
  cache.set('large', 'x'.repeat(1000))
  assert.equal(cache.get('large'), undefined)
  assert.ok(cache.bytes <= 100)
  cache.clear()
  assert.equal(cache.size, 0)
  assert.equal(cache.bytes, 0)
})
