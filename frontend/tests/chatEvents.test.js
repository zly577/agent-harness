import test from 'node:test'
import assert from 'node:assert/strict'
import { reactive, ref } from 'vue'
import { useMessageEvents } from '../src/composables/useMessageEvents.js'
import { useToolActivity } from '../src/composables/useToolActivity.js'
import { createSseParser } from '../src/sseParser.js'

function conversation() {
  const messages = reactive([])
  const sessionId = ref('one')
  const sessionStates = reactive({ one: {}, two: {} })
  const approval = ref(null)
  const events = useMessageEvents({ messages, sessionId, sessionStates, approval })
  const message = events.currentAssistantMessage(messages)
  return { ...events, message, messages, sessionId, sessionStates, approval }
}

test('tool parameter progress upgrades in place and trusts backend failure fields', () => {
  const c = conversation()
  c.applyDataEvent(c.message, { type: 'tool_args', id: 'tool', name: 'execute', kb: 2 })
  assert.equal(c.message.toolCalls[0].status, 'generating')
  c.applyDataEvent(c.message, { type: 'tool_call', id: 'tool', name: 'execute', args: { command: 'exit 7' }, intent_label: '检查退出码' })
  assert.equal(c.message.toolCalls.length, 1)
  assert.equal(c.message.toolCalls[0].status, 'running')
  c.applyDataEvent(c.message, { type: 'tool_result', id: 'tool', content: 'done', ok: false, exit_code: 7, duration_ms: 400, reason: '退出码 7' })
  assert.equal(c.message.toolCalls[0].status, 'error')
  assert.equal(c.message.toolCalls[0].exitCode, 7)
  assert.equal(c.message.toolCalls[0].intentLabel, '检查退出码')
})

test('parallel delegations keep separate text, thinking and usage', () => {
  const c = conversation()
  for (const id of ['a', 'b']) c.applyDataEvent(c.message, { type: 'subagent_start', id, summary: id })
  c.applyDataEvent(c.message, { type: 'subagent_token', id: 'b', delta: 'B' })
  c.applyDataEvent(c.message, { type: 'subagent_thinking', id: 'a', delta: 'A思考' })
  c.applyDataEvent(c.message, { type: 'subagent_usage', id: 'b', input: 20, output: 4 })
  assert.equal(c.message.delegations.length, 2)
  const [a, b] = c.message.delegations
  assert.equal(a.stream, '')
  assert.equal(a.thinking, 'A思考')
  assert.equal(b.stream, 'B')
  assert.equal(b.outTokens, 4)
  assert.equal(c.message.outTokens, 0)
})

test('approval for a background session does not replace the visible dialog', () => {
  const c = conversation()
  c.applyApprovalEvent(c.message, { type: 'clarification', clarifications: [{ id: 'q', question: '哪个文件？' }] }, 'two')
  assert.equal(c.approval.value, null)
  assert.equal(c.sessionStates.two.approval.clarifications[0].id, 'q')
  assert.equal(c.message.streaming, false)
  assert.equal(c.message.planPaused, true)
  assert.match(c.message.content, /哪个文件/)
})

test('an interrupted turn reports missing tool results and keeps completed failures', () => {
  const c = conversation()
  c.applyDataEvent(c.message, { type: 'tool_call', id: 'one', name: 'execute' })
  c.applyDataEvent(c.message, { type: 'tool_call', id: 'two', name: 'read_file' })
  c.applyDataEvent(c.message, { type: 'tool_result', id: 'two', content: 'failed', ok: false })
  assert.equal(c.finishTurn(c.message), 1)
  assert.equal(c.message.toolCalls[0].status, 'interrupted')
  assert.equal(c.message.toolCalls[1].status, 'error')
  assert.match(c.message.content, /1 个操作没有返回结果/)
})

test('final reply replaces deltas and freezes generation statistics', () => {
  const c = conversation()
  c.applyDataEvent(c.message, { type: 'token', content: '部分' })
  c.applyDataEvent(c.message, { type: 'thinking', scope: 'main', delta: '思考' })
  c.applyDataEvent(c.message, { type: 'result', content: '完整回复' })
  assert.equal(c.message.content, '完整回复')
  assert.equal(c.message.thinking, '思考')
  assert.equal(c.message.planFinished, true)
  assert.ok(c.message.endedAt)
  assert.equal(c.applyDataEvent(c.message, { type: 'stopped' }), false)
})

test('activity view keeps read groups and reads live and historical intent labels', () => {
  const c = conversation()
  const activity = useToolActivity({ showTools: ref(true), elapsedLabel: () => '1.0s', progressLabel: c.progressLabel, progressSize: c.progressSize })
  c.message.toolCalls.push(
    { id: 'a', name: 'ls', args: { path: '/' }, status: 'done', ok: true },
    { id: 'b', name: 'read_file', args: { file_path: '/a' }, status: 'done', ok: true, intent_label: '读取 a' },
  )
  assert.equal(activity.activityHistory(c.message).length, 0)
  assert.equal(activity.activityLatest(c.message).items.length, 2)
  assert.equal(activity.activityLatest(c.message).intent, '读取 a')
  c.message.toolCalls[1].intentLabel = '历史标签'
  assert.equal(activity.activityLatest(c.message).intent, '历史标签')
})

test('SSE parsing is invariant to every possible two-part network split', () => {
  const input = 'data: {"type":"token","content":"你好"}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n'
  for (let split = 0; split <= input.length; split++) {
    const events = []
    const parser = createSseParser(event => events.push(event))
    parser.feed(input.slice(0, split))
    parser.feed(input.slice(split))
    parser.finish()
    assert.deepEqual(events, [
      { type: 'token', content: '你好', event: 'message' },
      { event: 'done' },
    ])
  }
})

test('SSE parser handles comments, multiline data and final frame without newline', () => {
  const events = []
  const parser = createSseParser(event => events.push(event))
  parser.feed(': heartbeat\n\ndata: {"type":"result",\ndata: "content":"ok"}\n\ndata: broken')
  parser.finish()
  parser.finish()
  assert.deepEqual(events, [
    { type: 'result', content: 'ok', event: 'message' },
    { raw: 'broken', event: 'message' },
  ])
})
