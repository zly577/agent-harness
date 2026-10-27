// Fixture-only browser checks. No request reaches the user's backend or account.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

const { chromium } = await import(process.env.HARNESS_PLAYWRIGHT_MODULE || 'playwright')
const server = await createServer({ configFile: false, root: fileURLToPath(new URL('../', import.meta.url)),
  plugins: [vue()], cacheDir: path.join(os.tmpdir(), 'agent-settings-vite-cache'), server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1366, height: 900 } })
page.setDefaultTimeout(8000)
const errors = [], requests = []
page.on('pageerror', error => errors.push(error.message))
page.on('console', message => { if (message.text().startsWith('[Vue warn]')) errors.push(message.text()) })
const user = { id: 'fixture-user', email: 'test@example.com', display_name: '设置测试', role: 'user' }
const defaults = { context_max_messages: 80, context_keep_messages: 24, tool_call_run_limit: 0, tool_call_thread_limit: 0, tool_call_limit_tool: 'web_search' }
let values = { ...defaults }, customized = false
let memory = { content: '# 初始记忆\n请用中文回答。', revision: 'revision-1', exists: true }
let passwordChanged = false, conflict = false
let providerTest = { ok: true, message: '连接成功，可以正常使用', reply: 'pong' }
await page.addInitScript(user => {
  localStorage.setItem('ah_auth_token', 'fixture-token')
  localStorage.setItem('ah_auth_user', JSON.stringify(user))
}, user)
await page.route('**/api/**', async route => {
  const request = route.request(), url = new URL(request.url()), endpoint = url.pathname, method = request.method()
  const body = request.postDataJSON()
  requests.push({ endpoint, method, body, workspace: url.searchParams.get('workspace_id') })
  let data = {}, status = 200
  if (endpoint.endsWith('/auth/me')) data = user
  else if (endpoint.endsWith('/registration-config')) data = { allow_registration: false, email_verification_required: false }
  else if (endpoint.endsWith('/llm/providers/available') || endpoint.endsWith('/llm/providers')) data = [{ id: 'provider', name: '测试模型', model: 'fixture', enabled: true, is_default: true }]
  else if (endpoint.endsWith('/llm/providers/test')) data = providerTest
  else if (endpoint.endsWith('/workspaces')) data = [{ id: 'legacy', name: '测试工作区', directory: '/fixture' }]
  else if (endpoint.endsWith('/sessions')) data = []
  else if (endpoint.endsWith('/hitl/config')) data = { enabled: true }
  else if (endpoint.endsWith('/access/config')) data = { mode: 'sandbox', valid_modes: ['sandbox'] }
  else if (endpoint.endsWith('/preferences/memory')) {
    assert.equal(url.searchParams.get('workspace_id'), 'legacy')
    if (method === 'PUT') {
      if (conflict) { status = 409; data = { detail: '长期记忆已被其他页面或智能体修改，请重新加载后再编辑' } }
      else { assert.equal(body.revision, memory.revision); memory = { content: body.content, revision: 'revision-' + requests.length, exists: true }; data = memory }
    } else data = memory
  } else if (endpoint.endsWith('/preferences')) {
    if (method === 'PUT') { values = body; customized = true }
    if (method === 'DELETE') { values = { ...defaults }; customized = false }
    data = { values, defaults, customized }
  } else if (endpoint.endsWith('/auth/change-password')) {
    if (body.old_password !== 'Correct-old-password') { status = 400; data = { detail: '原密码不正确' } }
    else { passwordChanged = true; data = { ok: true } }
  } else throw new Error('Unexpected fixture request: ' + endpoint)
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
})
const output = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-settings-qa-'))
try {
  await page.goto(server.resolvedUrls.local[0])
  await page.getByRole('button', { name: '模型', exact: true }).click()
  const providersDialog = page.getByRole('dialog', { name: '模型供应商' })
  await providersDialog.getByRole('button', { name: '新增供应商', exact: true }).click()
  await providersDialog.getByRole('textbox', { name: /模型名/ }).fill('fixture-model')
  const formContent = providersDialog.locator('.pv-form .pv-content')
  const result = providersDialog.locator('.pv-form-actions .pv-test-result')
  for (const [name, viewport] of [['desktop', { width: 1366, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    await page.setViewportSize(viewport)
    for (const ok of [true, false]) {
      providerTest = { ok, message: ok ? '连接成功，可以正常使用' : '连接失败，请检查地址或密钥', reply: ok ? 'pong' : '' }
      await providersDialog.getByRole('button', { name: '测试连接', exact: true }).click()
      await result.getByText(providerTest.message, { exact: true }).waitFor()
      assert.equal(await result.evaluate(el => el.closest('.pv-content') === null), true)
      const positions = []
      for (const bottom of [false, true, false]) {
        const scroll = await formContent.evaluate((el, bottom) => {
          el.scrollTop = bottom ? el.scrollHeight : 0
          return { top: el.scrollTop, scrollable: el.scrollHeight > el.clientHeight }
        }, bottom)
        assert.equal(scroll.scrollable, true, `${name}: form must scroll`)
        if (bottom) assert(scroll.top > 0)
        const bounds = await result.boundingBox(), dialogBounds = await providersDialog.boundingBox()
        assert(bounds.y >= dialogBounds.y && bounds.y + bounds.height <= dialogBounds.y + dialogBounds.height)
        positions.push(bounds.y)
      }
      assert(Math.max(...positions) - Math.min(...positions) < 1, `${name}: result moved with the form`)
      if (ok) await page.screenshot({ path: path.join(output, `provider-test-${name}.png`) })
    }
    providerTest = { ok: true, message: '连接成功，可以正常使用', reply: '很长的模型测试回复。'.repeat(500) }
    await providersDialog.getByRole('button', { name: '测试连接', exact: true }).click()
    await result.getByText(providerTest.message, { exact: true }).waitFor()
    assert(await formContent.evaluate(el => el.clientHeight > 40), `${name}: reply consumed the form`)
    const footerBounds = await providersDialog.locator('.pv-form-actions').boundingBox()
    assert(footerBounds.y + footerBounds.height <= viewport.height)
  }
  await providersDialog.getByRole('button', { name: '取消', exact: true }).click()
  await providersDialog.getByRole('button', { name: '关闭模型供应商', exact: true }).click()
  await providersDialog.waitFor({ state: 'hidden' })
  for (const [name, viewport] of [['desktop', { width: 1366, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    await page.setViewportSize(viewport)
    const entry = page.getByRole('button', { name: '设置', exact: true })
    await entry.waitFor()
    assert.equal(await page.locator('.topbar').getByRole('button', { name: '设置', exact: true }).count(), 0)
    assert.equal(await entry.evaluate((el, name) => !!el.closest(name === 'desktop' ? '.sb-foot' : '.composer-footer'), name), true)
    const bounds = await entry.boundingBox()
    assert(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width && bounds.y + bounds.height <= viewport.height)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: path.join(output, `settings-entry-${name}.png`) })
    await entry.click()
    const settings = page.getByRole('dialog', { name: '个人设置' })
    await settings.getByRole('textbox', { name: /记忆内容/ }).waitFor()
    await settings.getByRole('button', { name: '关闭个人设置' }).click()
    await settings.waitFor({ state: 'hidden' })
  }
  await page.setViewportSize({ width: 1366, height: 900 })
  await page.getByRole('button', { name: '设置', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '个人设置' })
  const editor = dialog.getByRole('textbox', { name: /记忆内容/ })
  await editor.fill('# 我的偏好\n请先给出结论。')
  await dialog.getByRole('button', { name: '关闭个人设置' }).click()
  await page.getByRole('dialog', { name: '放弃未保存的设置？' }).getByRole('button', { name: '取消' }).click()
  assert.equal(await editor.inputValue(), '# 我的偏好\n请先给出结论。')
  await dialog.getByRole('button', { name: '保存记忆', exact: true }).click()
  await dialog.getByRole('status').filter({ hasText: '记忆已保存' }).waitFor()
  assert.equal(memory.content, '# 我的偏好\n请先给出结论。')
  conflict = true
  await editor.fill('未保存的过期编辑')
  await dialog.getByRole('button', { name: '保存记忆', exact: true }).click()
  await dialog.getByRole('alert').filter({ hasText: '重新加载' }).waitFor()
  assert.equal(await editor.inputValue(), '未保存的过期编辑')
  conflict = false
  await dialog.getByRole('button', { name: '重新加载', exact: true }).click()
  await page.getByRole('dialog', { name: '重新加载记忆？' }).getByRole('button', { name: '重新加载', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('textarea')?.value.startsWith('# 我的偏好'))

  await dialog.getByRole('button', { name: '上下文管理', exact: true }).click()
  await dialog.getByLabel(/触发压缩的消息数/).fill('100')
  await dialog.getByLabel(/压缩后保留的最近消息数/).fill('100')
  await dialog.getByRole('button', { name: '保存设置', exact: true }).click()
  await dialog.getByRole('alert').filter({ hasText: '小于压缩阈值' }).waitFor()
  await dialog.getByLabel(/压缩后保留的最近消息数/).fill('30')
  await dialog.getByRole('button', { name: '保存设置', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('[role="status"]')?.textContent.includes('设置已保存'))
  assert.equal(values.context_max_messages, 100)

  await dialog.getByRole('button', { name: '执行预算', exact: true }).click()
  assert.equal(await dialog.getByRole('radio', { name: /指定工具/ }).isChecked(), true)
  assert.equal(await dialog.getByLabel('选择工具', { exact: true }).inputValue(), 'web_search')
  assert.equal(await dialog.getByRole('textbox', { name: '工具标识', exact: true }).count(), 0)
  await dialog.getByLabel('选择工具', { exact: true }).selectOption({ label: '执行命令' })
  await dialog.getByRole('button', { name: '保存设置', exact: true }).click()
  await dialog.getByRole('status').filter({ hasText: '设置已保存' }).waitFor()
  assert.equal(values.tool_call_limit_tool, 'execute')
  await dialog.getByLabel(/每轮工具调用上限/).fill('3')
  await dialog.getByLabel(/每会话工具调用上限/).fill('10')
  await dialog.getByRole('radio', { name: /全部工具/ }).check()
  assert.equal(await dialog.getByLabel('选择工具', { exact: true }).count(), 0)
  await dialog.getByText('全部工具合计：每轮最多 3 次，每会话最多 10 次。', { exact: true }).waitFor()
  await dialog.getByRole('button', { name: '保存设置', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('button[type="submit"]')?.disabled || document.querySelector('[role="status"]')?.textContent.includes('设置已保存'))
  await dialog.getByRole('button', { name: '保存设置', exact: true }).waitFor({ state: 'visible' })
  await page.waitForTimeout(100)
  assert.deepEqual(values, { ...defaults, context_max_messages: 100, context_keep_messages: 30, tool_call_run_limit: 3, tool_call_thread_limit: 10, tool_call_limit_tool: '' })
  await page.screenshot({ path: path.join(output, 'budget-desktop.png') })
  await dialog.getByRole('button', { name: '关闭个人设置' }).click()
  await dialog.waitFor({ state: 'hidden' })
  await page.getByRole('button', { name: '设置', exact: true }).click()
  await dialog.getByRole('button', { name: '执行预算', exact: true }).click()
  assert.equal(await dialog.getByLabel(/每轮工具调用上限/).inputValue(), '3')
  assert.equal(await dialog.getByRole('radio', { name: /全部工具/ }).isChecked(), true)
  await dialog.getByRole('radio', { name: /指定工具/ }).check()
  await dialog.getByLabel('选择工具', { exact: true }).selectOption({ label: '联网搜索' })
  await dialog.getByText('联网搜索：每轮最多 3 次，每会话最多 10 次。', { exact: true }).waitFor()
  await page.screenshot({ path: path.join(output, 'budget-tool-desktop.png') })
  await dialog.getByLabel('选择工具', { exact: true }).selectOption({ label: '其他工具（高级）' })
  await dialog.getByRole('textbox', { name: '工具标识', exact: true }).fill('mcp__custom_search')
  await dialog.getByRole('button', { name: '保存设置', exact: true }).click()
  await dialog.getByRole('status').filter({ hasText: '设置已保存' }).waitFor()
  assert.equal(values.tool_call_limit_tool, 'mcp__custom_search')
  await dialog.getByRole('button', { name: '关闭个人设置' }).click()
  await dialog.waitFor({ state: 'hidden' })
  await page.getByRole('button', { name: '设置', exact: true }).click()
  await dialog.getByRole('button', { name: '执行预算', exact: true }).click()
  assert.equal(await dialog.getByRole('textbox', { name: '工具标识', exact: true }).inputValue(), 'mcp__custom_search')
  await dialog.getByRole('button', { name: '恢复全局默认', exact: true }).click()
  await page.getByRole('dialog', { name: '恢复全局默认？' }).getByRole('button', { name: '恢复默认', exact: true }).click()
  await dialog.getByRole('status').filter({ hasText: '已恢复全局默认' }).waitFor()
  assert.deepEqual(values, defaults)
  assert.equal(await dialog.getByRole('radio', { name: /指定工具/ }).isChecked(), true)
  assert.equal(await dialog.getByLabel('选择工具', { exact: true }).inputValue(), 'web_search')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.confirmation-overlay').waitFor({ state: 'hidden' })
  await page.waitForTimeout(250)
  await page.screenshot({ path: path.join(output, 'budget-mobile.png') })
  await dialog.getByRole('button', { name: '账户安全', exact: true }).click()
  await page.locator('.confirmation-overlay').waitFor({ state: 'hidden' })
  await page.waitForTimeout(250)
  await page.screenshot({ path: path.join(output, 'account-mobile.png') })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  const bounds = await dialog.boundingBox()
  assert(bounds.x >= 0 && bounds.x + bounds.width <= 391)
  await dialog.getByLabel('原密码', { exact: true }).fill('wrong-password')
  await dialog.getByLabel(/新密码 8/).fill('New-password-123')
  await dialog.getByLabel('确认新密码', { exact: true }).fill('Mismatch-123')
  await dialog.getByRole('button', { name: '修改密码并重新登录', exact: true }).click()
  await dialog.getByRole('alert').filter({ hasText: '不一致' }).waitFor()
  await dialog.getByLabel('确认新密码', { exact: true }).fill('New-password-123')
  await dialog.getByRole('button', { name: '修改密码并重新登录', exact: true }).click()
  await dialog.getByRole('alert').filter({ hasText: '原密码不正确' }).waitFor()
  assert(await page.evaluate(() => !!localStorage.getItem('ah_auth_token')))
  await dialog.getByLabel('原密码', { exact: true }).fill('Correct-old-password')
  await dialog.getByRole('button', { name: '修改密码并重新登录', exact: true }).click()
  await page.getByText('密码已更新，请使用新密码重新登录。', { exact: true }).waitFor()
  assert.equal(passwordChanged, true)
  assert.equal(await page.evaluate(() => localStorage.getItem('ah_auth_token')), null)
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, screenshots: output, mutations: requests.filter(r => r.method !== 'GET').length }))
} finally {
  await browser.close()
  await server.close()
}
