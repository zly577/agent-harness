// Fixture-only visual QA: prose follows the answer font; source and terminals keep monospace.
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
const { chromium } = await import(process.env.HARNESS_PLAYWRIGHT_MODULE || 'playwright')
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1366, height: 1000 } })
page.setDefaultTimeout(8000)
const errors = []
page.on('pageerror', e => errors.push(e.message))
page.on('console', message => { if (message.type() === 'error' || message.text().startsWith('[Vue warn]')) errors.push(message.text()) })
const user = { id: 'font-fixture', email: 'font@example.com', display_name: '字体检查', role: 'admin' }
const provider = { id: 'demo', name: '测试模型', model: 'demo', enabled: true, is_default: true }
const text = '已重置 1 个工具的熔断状态，现在可以重新尝试之前失败的工具了。'
const messages = [{
  role: 'assistant', content: '这里是回答正文，工具说明应使用相同的字体。English text 123。',
  thinking: '这是一段自然语言的思考说明，应当与正文使用同一字体。',
  toolCalls: [
    { id: 'reset', name: 'reset_tool_breakers', args: { reason: '用户再次要求重试联网搜索' }, result: text, status: 'done', ok: true, intentLabel: '重置搜索工具熔断状态' },
    { id: 'read', name: 'read_file', args: { file_path: '/workspace/demo.py', description: '这是一段很长的任务说明，仍然应该使用正文字体。'.repeat(20) }, result: 'def greet():\n    print("你好，世界")', status: 'done', ok: true },
    { id: 'execute', name: 'execute', args: { command: 'python demo.py', description: '这是一段很长的任务说明，仍然应该使用正文字体。'.repeat(20) }, result: '你好，世界', status: 'done', ok: true },
  ], delegations: [], streaming: false,
}]
await page.addInitScript(user => {
  localStorage.setItem('ah_auth_token', 'fixture-only')
  localStorage.setItem('ah_auth_user', JSON.stringify(user))
}, user)
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  let data = {}
  if (path.endsWith('/auth/me')) data = user
  else if (path.includes('/llm/providers')) data = [provider]
  else if (path.endsWith('/sessions')) data = [{ id: 'font-session', title: '字体检查', workspace_id: 'legacy' }]
  else if (path.endsWith('/workspaces')) data = []
  else if (path.endsWith('/messages')) data = messages
  else if (path.endsWith('/hitl/config')) data = { enabled: true }
  else if (path.endsWith('/access/config')) data = { mode: 'sandbox', valid_modes: ['sandbox'] }
  else if (path.includes('/approvals/')) data = { pending: false }
  else if (path.endsWith('/run')) data = { running: false, status: 'idle' }
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
})
const family = selector => page.locator(selector).first().evaluate(el => getComputedStyle(el).fontFamily)
const report = []
try {
  await page.goto(process.env.HARNESS_UI_URL || 'http://127.0.0.1:5177')
  await page.locator('.sb-item').click()
  for (const selector of ['.reasoning', '.act-call-wrap', '.act-history', '.archived-step', '.operation-output', '.operation-long']) {
    await page.locator(selector).evaluateAll(elements => elements.forEach(el => { el.open = true }))
  }
  await page.locator('.operation-output .tool-code').first().waitFor()
  const prose = await family('.prose.md')
  for (const selector of ['.act-verb', '.operation-fields', '.operation-value', '.operation-long pre', '.operation-output .tool-code:not(.tool-code-source)', '.reasoning-text', '.operation-output > summary', '.step-number']) {
    assert.equal(await family(selector), prose, `${selector} should use the answer font`)
  }
  for (const selector of ['.tool-code-source', '.terminal-tool pre code', '.operation-code']) {
    assert.notEqual(await family(selector), prose, `${selector} should keep its code font`)
    assert.match(await family(selector), /Microsoft YaHei/, 'code fonts need a matching Chinese fallback')
  }
  assert.equal(await page.locator('.operation-output .tool-code:not(.tool-code-source)').textContent(), text)
  assert.equal(await page.locator('.operation-field dt').first().textContent(), '原因')
  for (const width of [1366, 390]) {
    for (const colorScheme of ['light', 'dark']) {
      await page.setViewportSize({ width, height: 1000 })
      await page.emulateMedia({ colorScheme })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false)
      const card = page.locator('.archived-step').first()
      await card.scrollIntoViewIfNeeded()
      await card.screenshot({ path: fileURLToPath(new URL(`./typography-${width}-${colorScheme}.png`, import.meta.url)) })
      report.push({ width, colorScheme, prose, code: await family('.tool-code-source') })
    }
  }
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ checks: 4, errors, report }, null, 2))
} finally { await browser.close() }
