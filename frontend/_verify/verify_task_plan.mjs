// Fixture-only browser QA: no real account, model request or persisted mutation.
import assert from 'node:assert/strict'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'
const { chromium } = await import(process.env.HARNESS_PLAYWRIGHT_MODULE ? pathToFileURL(process.env.HARNESS_PLAYWRIGHT_MODULE).href : 'playwright')
const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1366, height: 1000 } })
page.setDefaultTimeout(5000)
const errors = []
page.on('pageerror', error => errors.push(error.message))
page.on('console', message => { if (message.type() === 'error' || message.text().startsWith('[Vue warn]')) errors.push(message.text()) })
const user = { id: 'fixture-user', email: 'fixture@example.com', display_name: '测试用户', role: 'admin' }
await page.addInitScript(user => {
  localStorage.setItem('ah_auth_token', 'fixture-only-token')
  localStorage.setItem('ah_auth_user', JSON.stringify(user))
}, user)
await page.route('**/api/**', async route => {
  assert.equal(route.request().method(), 'GET', 'fixture must not send a mutation')
  const path = new URL(route.request().url()).pathname
  let data = {}
  if (path.endsWith('/auth/me')) data = user
  else if (path.endsWith('/sessions') || path.endsWith('/messages') || path.includes('/llm/providers')) data = []
  else if (path.endsWith('/workspaces')) data = [{ id: 'w', name: '测试工作区', directory: '/workspace/demo' }]
  else if (path.endsWith('/access/config')) data = { mode: 'sandbox', valid_modes: ['sandbox', 'full'] }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
})

const todos = [
  { content: '安排两人份七天晚餐菜单', status: 'in_progress' },
  { content: '核算食材数量与 300 元采购预算', status: 'pending' },
  { content: '检查保存时间与每天的做饭顺序', status: 'pending' },
]
const emit = events => page.evaluate(events => {
  const { events: reducer, message } = window.planFixture
  events.forEach(event => reducer.applyDataEvent(message, event))
}, events)
let revision = 0
async function save(items, ok = true) {
  const id = `plan-${++revision}`
  await emit([
    { type: 'tool_call', id, name: 'write_todos', args: { todos: items } },
    { type: 'tool_result', id, content: ok ? 'saved' : 'failed', ok },
  ])
}
const shot = name => page.screenshot({ path: fileURLToPath(new URL(`./task-plan-${name}.png`, import.meta.url)) })
const shell = page.locator('.task-plan-shell')
const rows = page.locator('.task-plan li')

try {
  const address = server.httpServer.address()
  await page.goto(`http://127.0.0.1:${address.port}`)
  await page.getByRole('button', { name: '模型', exact: true }).waitFor()
  await page.evaluate(async () => {
    const state = document.querySelector('#app')._vnode.component.setupState
    state.messages.splice(0, state.messages.length,
      { role: 'user', content: '帮我制定七天晚餐计划' },
      { role: 'assistant', content: '正在整理菜单、采购预算和食材保存安排。', thinking: '', streaming: true, toolCalls: [], delegations: [] })
    const { useMessageEvents } = await import('/src/composables/useMessageEvents.js')
    window.planFixture = { message: state.messages[1], events: useMessageEvents({ messages: state.messages }) }
  })
  assert.equal(await shell.count(), 0)
  await emit([{ type: 'tool_args', id: 'initial', name: 'write_todos', kb: 1 }])
  await page.getByText('正在生成任务清单…', { exact: true }).waitFor()
  await emit([{ type: 'tool_call', id: 'initial', name: 'write_todos', args: { todos } }])
  await rows.last().waitFor()
  assert.equal(await rows.count(), 3, 'list appears before the tool result')
  assert.equal(await shell.evaluate(el => el === el.parentElement.firstElementChild), true, 'plan precedes answer content')
  await emit([{ type: 'tool_result', id: 'initial', content: 'saved', ok: true }])
  await page.waitForTimeout(450)
  const firstRow = await rows.first().elementHandle()

  const partial = todos.map((item, i) => ({ ...item, status: i === 0 ? 'completed' : i === 1 ? 'in_progress' : 'pending' }))
  await save(partial)
  await page.waitForTimeout(160)
  const animation = await rows.first().evaluate(el => ({
    check: parseFloat(getComputedStyle(el.querySelector('path')).strokeDashoffset),
    strike: parseFloat(getComputedStyle(el.querySelector('.task-plan-label')).backgroundSize),
  }))
  assert.ok(animation.check > 0 && animation.check < 1, `check must draw over time: ${JSON.stringify(animation)}`)
  assert.ok(animation.strike > 0 && animation.strike < 100, 'strike-through must animate')
  await page.waitForTimeout(400)
  assert.equal(await rows.first().locator('path').evaluate(el => parseFloat(getComputedStyle(el).strokeDashoffset)), 0)
  await shot('desktop')

  const revised = [{ content: '补充周三的采购安排', status: 'pending' }, ...partial]
  await save(revised)
  assert.equal(await firstRow.evaluate(el => el === document.querySelectorAll('.task-plan li')[1]), true, 'insertions preserve checked row nodes')
  await emit([{ type: 'result', content: '菜单已经整理，仍需核查采购安排。' }])
  await page.evaluate(() => { window.planFixture.message.streaming = false })
  await page.waitForTimeout(1750)
  assert.equal(await shell.count(), 1, 'unfinished plan survives end of answer')

  const finished = revised.map(item => ({ ...item, status: 'completed' }))
  await save(finished, false)
  await page.getByText('清单更新失败，未确认的完成状态不会打钩。', { exact: true }).waitFor()
  assert.equal(await page.locator('.task-plan li.completed').count(), 1)
  await save(finished)
  await page.getByText('任务全部完成', { exact: true }).waitFor()
  await page.waitForTimeout(600)
  assert.equal(await page.locator('.task-plan li.completed').count(), 4, 'show final ticks before collapse')
  await shot('complete')
  // Other tools must not postpone the hide timer.
  await emit([{ type: 'tool_call', id: 'unrelated', name: 'read_file', args: { file_path: '/demo' } }])
  await page.waitForFunction(() => document.querySelector('.task-plan-collapse-leave-active'), undefined, { timeout: 1200 })
  const fullHeight = await shell.evaluate(el => el.getBoundingClientRect().height)
  await page.waitForTimeout(120)
  const shrinkingHeight = await shell.evaluate(el => el.getBoundingClientRect().height)
  assert.ok(shrinkingHeight < fullHeight && shrinkingHeight > 0, 'card collapses gradually')
  await shell.waitFor({ state: 'detached' })

  await save(todos)
  await rows.last().waitFor()
  await save(todos.map(item => ({ ...item, status: 'completed' })))
  await page.waitForTimeout(600)
  await save(todos)
  await page.waitForTimeout(1600)
  assert.equal(await shell.count(), 1, 'reopened tasks cancel a pending collapse')

  await page.setViewportSize({ width: 390, height: 844 })
  await save([{ content: '核对所有食材数量与保存时间，确保两人份七天晚餐的全部采购成本不超过三百元，并补充实际执行步骤', status: 'completed' }, ...todos])
  await page.waitForTimeout(500)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'mobile page must not overflow')
  assert.equal(await shell.evaluate(el => { const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth }), true)
  await shot('mobile')

  await page.emulateMedia({ reducedMotion: 'reduce' })
  assert.ok(await shell.evaluate(el => parseFloat(getComputedStyle(el).transitionDuration) < 0.001))
  assert.equal(await page.locator('.task-plan-spinner').first().evaluate(el => getComputedStyle(el).animationName), 'none')

  // A reused message slot must not replay a completed historical plan or inherit
  // the previous message's hide timer.
  await page.evaluate(items => {
    const state = document.querySelector('#app')._vnode.component.setupState
    state.messages[1] = { role: 'assistant', content: '历史结果', streaming: false, toolCalls: [{ name: 'write_todos', status: 'done', ok: true, args: { todos: items } }], delegations: [] }
  }, finished)
  await shell.waitFor({ state: 'detached' })
  await page.evaluate(items => {
    const state = document.querySelector('#app')._vnode.component.setupState
    state.messages[1] = { role: 'assistant', content: '尚有任务未完成', streaming: false, toolCalls: [{ name: 'write_todos', status: 'done', ok: true, args: { todos: items } }], delegations: [] }
  }, todos)
  await shell.waitFor()
  await page.waitForTimeout(1600)
  assert.equal(await shell.count(), 1)
  assert.deepEqual(errors, [])
  console.log('PASS: immediate list at top, real check/strike animations, stable rows, unfinished/error retention, delayed animated collapse, reopen cancellation, history, mobile and reduced motion')
} finally {
  await browser.close()
  await server.close()
}
