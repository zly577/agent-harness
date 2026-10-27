import test from 'node:test'
import assert from 'node:assert/strict'
import { taskPlan, taskPlanState } from '../src/toolPresentation.js'
import { useMessageEvents } from '../src/composables/useMessageEvents.js'

const todos = [
  { content: '安排菜单', status: 'in_progress' },
  { content: '核算预算', status: 'pending' },
]
const call = (items, status = 'done', ok = true) => ({ name: 'write_todos', args: { todos: items }, status, ok })

test('show the first submitted list before the tool result, without speculative checkmarks', () => {
  const message = { toolCalls: [] }
  const events = useMessageEvents({ messages: [] })
  events.applyDataEvent(message, { type: 'tool_args', id: 'plan', name: 'write_todos', kb: 1 })
  assert.equal(taskPlanState(message).hasCall, true)
  assert.equal(taskPlanState(message).updating, true)
  events.applyDataEvent(message, { type: 'tool_call', id: 'plan', name: 'write_todos', args: { todos: [{ content: '安排菜单', status: 'completed' }, todos[1]] } })
  assert.equal(taskPlan(message).length, 2)
  assert.equal(taskPlan(message)[0].status, 'pending')
  events.applyDataEvent(message, { type: 'tool_result', id: 'plan', content: 'saved', ok: true })
  assert.equal(taskPlan(message)[0].status, 'completed')
})

test('finish, pause and disconnect cannot hide or complete unfinished tasks', () => {
  const message = { streaming: false, planFinished: true, connectionLost: true, toolCalls: [call(todos)] }
  assert.deepEqual(taskPlan(message).map(item => item.status), ['running', 'pending'])
  assert.equal(taskPlanState(message).complete, false)
  message.planPaused = true
  assert.equal(taskPlan(message).length, 2)
})

test('failed and interrupted revisions preserve saved tasks and report the problem', () => {
  const finished = todos.map(item => ({ ...item, status: 'completed' }))
  const message = { toolCalls: [call(todos), call(finished, 'running')] }
  assert.deepEqual(taskPlan(message).map(item => item.status), ['running', 'pending'])
  message.toolCalls[1].status = 'error'
  message.toolCalls[1].ok = false
  assert.deepEqual(taskPlan(message).map(item => item.status), ['running', 'pending'])
  assert.match(taskPlanState(message).error, /失败/)
  assert.equal(taskPlanState(message).complete, false)
  message.toolCalls[1].status = 'interrupted'
  message.toolCalls[1].ok = null
  assert.match(taskPlanState(message).error, /中断/)
})

test('all-complete snapshots remain available so the final ticks can animate before hiding', () => {
  const message = { streaming: true, toolCalls: [call(todos)] }
  message.toolCalls.push(call(todos.map(item => ({ ...item, status: 'completed' }))))
  assert.equal(taskPlan(message).length, 2)
  assert.equal(taskPlanState(message).complete, true)
  message.toolCalls.push(call(todos))
  assert.equal(taskPlanState(message).complete, false, 'reopened plans cancel completion')
})

test('insertions and reordering preserve row identities, including duplicate task labels', () => {
  const original = taskPlan({ toolCalls: [call(todos)] })
  const revised = taskPlan({ toolCalls: [call([{ content: '采购食材', status: 'pending' }, todos[1], todos[0]])] })
  assert.equal(original[0].key, revised[2].key)
  assert.equal(original[1].key, revised[1].key)
  const duplicates = taskPlan({ toolCalls: [call([todos[0], todos[0]])] })
  assert.notEqual(duplicates[0].key, duplicates[1].key)
})

test('ignore malformed snapshots, support legacy task fields, and never invent a plan', () => {
  assert.equal(taskPlanState({ toolCalls: [{ name: 'execute' }] }).hasCall, false)
  const message = { toolCalls: [call(todos), call('invalid')] }
  assert.equal(taskPlan(message).length, 2)
  assert.deepEqual(taskPlan({ toolCalls: [{ name: 'write_todos', status: 'done', args: { tasks: [null, { title: '核算', status: 'done', id: 'a' }, {}] } }] }), [
    { key: 'id:a', text: '核算', status: 'completed' },
  ])
})

test('replay real 0/9 and 9/9 reports without inventing intermediate progress; rejected batches stay at 0', () => {
  const message = { toolCalls: [] }
  const events = useMessageEvents({ messages: [] })
  const initial = Array.from({ length: 9 }, (_, i) => ({ content: `阶段${i}`, status: i === 0 ? 'in_progress' : 'pending' }))
  const completed = initial.map(item => ({ ...item, status: 'completed' }))
  const update = (id, items, ok) => {
    events.applyDataEvent(message, { type: 'tool_call', id, name: 'write_todos', args: { todos: items } })
    events.applyDataEvent(message, { type: 'tool_result', id, name: 'write_todos', ok, content: ok ? 'saved' : 'Error: 本次清单未保存' })
  }
  const count = () => taskPlan(message).filter(item => item.status === 'completed').length
  update('initial', initial, true)
  assert.equal(count(), 0)
  update('batch-rejected', completed, false)
  assert.equal(count(), 0)
  assert.equal(taskPlanState(message).complete, false)
  const progress = initial.map(item => ({ ...item }))
  for (let i = 0; i < progress.length; i++) {
    progress[i].status = 'completed'
    if (i + 1 < progress.length) progress[i + 1].status = 'in_progress'
    update(`step-${i}`, progress.map(item => ({ ...item })), true)
    assert.equal(count(), i + 1)
  }
  assert.equal(taskPlanState(message).complete, true)
  // Old successful batch reports still render honestly; the UI does not pretend
  // that timestamps or intermediate updates existed when the model sent none.
  message.toolCalls = []
  update('legacy-initial', initial, true)
  update('legacy-batch', completed, true)
  assert.equal(count(), 9)
})
