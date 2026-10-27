import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const { chromium } = await import(pathToFileURL(process.env.HARNESS_PLAYWRIGHT_MODULE).href)
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
page.setDefaultTimeout(8000)
const errors = []
page.on('pageerror', error => errors.push(error.message))
const user = { id: 'fixture', email: 'fixture@example.com', display_name: '测试用户', role: 'admin' }
await page.addInitScript(user => {
  localStorage.setItem('ah_auth_token', 'fixture-only-token')
  localStorage.setItem('ah_auth_user', JSON.stringify(user))
}, user)
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  let data = {}
  if (path.endsWith('/auth/me')) data = user
  else if (path.endsWith('/sessions')) data = []
  else if (path.endsWith('/workspaces')) data = [{ id: 'w', name: '测试工作区', directory: '/workspace/demo' }]
  else if (path.includes('/llm/providers')) data = []
  else if (path.endsWith('/messages')) data = []
  else if (path.endsWith('/access/config')) data = { mode: 'sandbox', valid_modes: ['sandbox', 'full'] }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
})
try {
  await page.goto(process.env.HARNESS_UI_URL || 'http://127.0.0.1:5182')
  await page.getByRole('button', { name: '模型', exact: true }).waitFor()
  await page.evaluate(() => {
    const state = document.querySelector('#app')._vnode.component.setupState
    state.showTools = true
    state.messages.splice(0, state.messages.length, { role: 'assistant', content: '正在处理文件', thinking: '', streaming: true,
      toolCalls: [{ id: 'write', name: 'write_file', args: {}, result: '', status: 'generating',
        progress: { kb: 10.4, chars: 10650, elapsed: 21 }, startedAt: Date.now() - 21000 }], delegations: [] })
  })
  const details = page.locator('.act-call-wrap')
  await details.locator('summary').click()
  await page.getByText('操作内容正在生成，尚未开始执行。生成完成后会在这里显示完整操作。', { exact: true }).waitFor()
  assert.equal(await details.evaluate(el => el.open), true)
  assert.match(await details.innerText(), /10\.4 KB/)
  await details.locator('.operation-notice').scrollIntoViewIfNeeded()
  await page.locator('.act-block').screenshot({ path: 'frontend/_verify/operation-details-generating.png' })
  await page.evaluate(() => {
    const item = document.querySelector('#app')._vnode.component.setupState.messages[0].toolCalls[0]
    item.status = 'running'
    item.progress = null
    item.args = { file_path: '/report.json', content: '{"ok": true}' }
  })
  await page.getByText('等待工具返回结果，收到后会自动展示。', { exact: true }).waitFor()
  assert.equal(await details.evaluate(el => el.open), true)
  assert.match(await details.innerText(), /report\.json/)
  await page.evaluate(() => {
    const item = document.querySelector('#app')._vnode.component.setupState.messages[0].toolCalls[0]
    item.status = 'done'; item.ok = true; item.result = '写入完成'
  })
  await details.getByText('写入完成', { exact: true }).waitFor()
  assert.equal(await details.locator('.operation-notice').count(), 0)
  assert.equal(await details.evaluate(el => el.open), true)
  // Show the same generated-content notice at a phone width.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.evaluate(() => {
    const item = document.querySelector('#app')._vnode.component.setupState.messages[0].toolCalls[0]
    item.status = 'generating'; item.args = {}; item.result = ''; item.progress = { kb: 10.4, elapsed: 21 }
  })
  await page.getByText('操作内容正在生成，尚未开始执行。生成完成后会在这里显示完整操作。', { exact: true }).waitFor()
  await details.locator('.operation-notice').scrollIntoViewIfNeeded()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false)
  await page.screenshot({ path: 'frontend/_verify/operation-details-mobile.png' })
  assert.deepEqual(errors, [])
  console.log('PASS: click to expand, progress visible, live transition preserves expansion, final output visible, mobile has no horizontal overflow')
} finally {
  await browser.close()
}
