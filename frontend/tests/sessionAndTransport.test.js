import test from 'node:test'
import assert from 'node:assert/strict'
import { apiFetch, apiFetchRaw, clearSession, getToken, setSession, setUser, getSessionVersion, onUnauthorized, login } from '../src/api.js'
import { streamChat } from '../src/sse.js'

const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}

test('old 401 cannot clear or notify a new login', async t => {
  const response = deferred()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  let notices = 0
  const unsubscribe = onUnauthorized(() => notices++)
  try {
    setSession('old-token', { id: 'old' })
    const pending = apiFetchRaw('/test')
    setSession('new-token', { id: 'new' })
    response.resolve(json({ detail: 'expired' }, 401))
    await assert.rejects(pending, { name: 'StaleSessionError' })
    assert.equal(getToken(), 'new-token')
    assert.equal(notices, 0)
  } finally { unsubscribe(); clearSession() }
})

test('login change while reading a 401 body is checked again', async t => {
  const entered = deferred()
  const body = deferred()
  t.mock.method(globalThis, 'fetch', async () => ({ status: 401, clone: () => ({ json: () => { entered.resolve(); return body.promise } }) }))
  setSession('old-token', { id: 'old' })
  const pending = apiFetchRaw('/test', { silent401: true })
  await entered.promise
  setSession('new-token', { id: 'new' })
  body.resolve({ detail: 'expired' })
  await assert.rejects(pending, { name: 'StaleSessionError' })
  assert.equal(getToken(), 'new-token')
  clearSession()
})

test('old successful JSON cannot update the new user interface', async t => {
  const body = deferred()
  const entered = deferred()
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, status: 200, text: () => { entered.resolve(); return body.promise } }))
  setSession('old-token', { id: 'old' })
  const pending = apiFetch('/test')
  await entered.promise
  setSession('new-token', { id: 'new' })
  body.resolve(JSON.stringify({ privateData: 'old-user' }))
  await assert.rejects(pending, { name: 'StaleSessionError' })
  clearSession()
})

test('current 401 still signs out, but invalid login does not', async t => {
  t.mock.method(globalThis, 'fetch', async () => json({ detail: 'expired' }, 401))
  setSession('current-token', { id: 'user' })
  await assert.rejects(apiFetchRaw('/test'), { status: 401 })
  assert.equal(getToken(), '')
  setSession('current-token', { id: 'user' })
  await assert.rejects(login('user@example.com', 'wrong'), { status: 401 })
  assert.equal(getToken(), 'current-token')
  clearSession()
})

test('profile updates preserve the authentication generation', () => {
  setSession('current-token', { id: 'user' })
  const version = getSessionVersion()
  setUser({ id: 'user', display_name: 'new name' })
  assert.equal(getSessionVersion(), version)
  clearSession()
  setSession('current-token', { id: 'user' })
  assert.notEqual(getSessionVersion(), version)
  clearSession()
})

function streamed(text) {
  return new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(text)); controller.close() } }))
}

test('SSE comments are ignored and explicit done completes the stream', async t => {
  t.mock.method(globalThis, 'fetch', async () => streamed(': keep-alive\n\ndata: {"type":"token","content":"hello"}\n\nevent: done\ndata: {}\n\n'))
  const events = []
  await streamChat('hi', null, undefined, event => events.push(event))
  assert.equal(events.length, 2)
  assert.equal(events[0].content, 'hello')
  assert.equal(events[1].event, 'done')
})

test('premature SSE EOF is distinguishable from successful completion', async t => {
  t.mock.method(globalThis, 'fetch', async () => streamed('data: {"type":"token","content":"partial"}\n\n'))
  const events = []
  await assert.rejects(streamChat('hi', null, undefined, event => events.push(event)), { name: 'StreamDisconnectedError' })
  assert.equal(events[0].content, 'partial')
})

test('SSE events from a previous login are rejected', async t => {
  let source
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({ start(controller) { source = controller } })))
  setSession('old-token', { id: 'old' })
  const events = []
  const pending = streamChat('hi', null, undefined, event => events.push(event))
  await new Promise(resolve => setImmediate(resolve))
  setSession('new-token', { id: 'new' })
  source.enqueue(new TextEncoder().encode('data: {"type":"token","content":"old-data"}\n\n'))
  await assert.rejects(pending, { name: 'StaleSessionError' })
  assert.deepEqual(events, [])
  clearSession()
})
