// Browser QA with fixture-only APIs; never reads or changes the user's account.
// HARNESS_PLAYWRIGHT_MODULE may point to a bundled playwright/index.mjs.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const { chromium } = await import(process.env.HARNESS_PLAYWRIGHT_MODULE || 'playwright')
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1366, height: 900 } })
page.setDefaultTimeout(8000)
const errors = [], mutations = [], results = []
page.on('pageerror', error => errors.push(error.message))
page.on('console', message => { if (message.type() === 'error' || message.text().startsWith('[Vue warn]')) errors.push(message.text()) })
page.on('dialog', async dialog => { errors.push(`Native dialog: ${dialog.message()}`); await dialog.dismiss() })
const user = { id: 'fixture-user', email: 'admin@example.com', display_name: '测试用户', role: 'admin' }
const provider = { id: 'fixture-provider', name: '测试模型', protocol: 'openai', model: 'demo', base_url: 'https://example.com/v1', is_default: true, enabled: true, parameters: {} }
await page.addInitScript(user => {
  localStorage.setItem('ah_auth_token', 'fixture-only-token')
  localStorage.setItem('ah_auth_user', JSON.stringify(user))
}, user)
await page.route('**/api/**', async route => {
  const request = route.request(), path = new URL(request.url()).pathname
  if (!['GET', 'HEAD'].includes(request.method())) mutations.push(`${request.method()} ${path}`)
  let data = {}
  if (path.endsWith('/auth/me')) data = user
  else if (path.endsWith('/registration-config')) data = { allow_registration: true, email_verification_required: true }
  else if (path.endsWith('/llm/providers') || path.endsWith('/llm/providers/available')) data = [provider]
  else if (path.endsWith('/mcp/servers')) data = [{ id: 'mcp-demo', name: '测试工具源', transport: 'streamable_http', url: 'https://example.com/mcp', enabled: true, header_keys: [], env_keys: [] }]
  else if (path.endsWith('/mcp/status')) data = { enabled: true, adapters_installed: true }
  else if (path.endsWith('/hitl/config')) data = { enabled: true }
  else if (path.endsWith('/access/config')) data = { mode: 'sandbox', valid_modes: ['sandbox', 'full'] }
  else if (path.endsWith('/sessions')) data = [{ id: 's-demo', title: '测试会话', workspace_id: 'w-demo' }]
  else if (path.endsWith('/workspaces')) data = [{ id: 'w-demo', name: '测试工作区', directory: '/workspace/demo' }]
  else if (path.endsWith('/files')) data = { path: '/workspace', files: [] }
  else if (path.endsWith('/messages')) data = []
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
})
const state = async values => page.evaluate(values => Object.assign(document.querySelector('#app').__vue_app__._instance.setupState, values), values)
const pause = () => page.waitForTimeout(260)
const shot = async name => page.screenshot({ path: fileURLToPath(new URL(`./dialogs-${name}.png`, import.meta.url)) })
async function check(name, selector = '[role="dialog"]') {
  await page.locator(selector).last().waitFor()
  await pause()
  const metrics = await page.locator(selector).last().evaluate(el => {
    const r = el.getBoundingClientRect(), css = getComputedStyle(el)
    return { radius: css.borderRadius, font: css.fontFamily, x: r.x, y: r.y, width: r.width, height: r.height,
      visible: r.x >= -1 && r.y >= -1 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1,
      overflow: document.documentElement.scrollWidth > innerWidth + 1 }
  })
  assert.equal(metrics.visible, true, `${name}: clipped dialog`)
  assert.equal(metrics.overflow, false, `${name}: page overflow`)
  assert.ok(['20px', '16px'].includes(metrics.radius), `${name}: radius ${metrics.radius}`)
  results.push({ name, ...metrics })
}
try {
  await page.goto(process.env.HARNESS_UI_URL || 'http://127.0.0.1:5177')
  await page.getByRole('button', { name: '模型', exact: true }).waitFor()
  await page.getByRole('button', { name: '模型', exact: true }).click()
  await check('providers')
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await check('provider-delete', '.confirmation-overlay [role="dialog"]')
  await page.keyboard.press('Escape')
  await pause()
  assert.equal(await page.locator('.pv').count(), 1)
  assert.equal(mutations.length, 0, 'cancelled deletion sent a request')
  await page.getByRole('button', { name: '新增供应商', exact: true }).click()
  await pause()
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('.pv-form').count(), 1, 'Escape discarded a model form')
  await check('provider-form')
  await page.getByRole('button', { name: '关闭模型供应商', exact: true }).click()
  await pause()
  await page.getByRole('button', { name: '工具源', exact: true }).click()
  await check('mcp')
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await check('mcp-delete', '.confirmation-overlay [role="dialog"]')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await pause()
  await page.keyboard.press('Escape')
  await pause()
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await check('logout')
  await shot('logout-desktop')
  await page.keyboard.press('Shift+Tab')
  assert.equal(await page.locator(':focus').textContent(), '退出登录', 'Shift+Tab should stay in the dialog')
  await page.keyboard.press('Tab')
  assert.equal(await page.locator(':focus').textContent(), '取消', 'Tab should wrap to cancel')
  await page.keyboard.press('Escape')
  await pause()
  assert.equal(await page.locator(':focus').textContent(), '退出', 'focus should return to the opener')
  await page.getByRole('button', { name: '删除会话', exact: true }).click()
  await check('session-delete')
  await page.keyboard.press('Escape'); await pause()
  await page.getByRole('button', { name: '删除工作区', exact: true }).click()
  await check('workspace-delete')
  await page.keyboard.press('Escape'); await pause()
  await page.getByRole('button', { name: '沙箱内', exact: true }).click()
  await check('access-confirmation')
  await page.keyboard.press('Escape'); await pause()
  assert.equal(mutations.length, 0, 'cancelled confirmations must not mutate APIs')
  await page.getByRole('button', { name: '删除会话', exact: true }).click()
  await page.locator('.confirmation-overlay .btn-danger').click()
  await pause()
  assert.deepEqual(mutations, ['DELETE /api/v1/sessions/s-demo'], 'accepted confirmation must execute exactly once')
  if (!process.env.HARNESS_UI_SMOKE) {
  await state({ showFilePanel: true })
  await check('files')
  await state({ pendingDelete: { path: '/workspace/目录/很长的文件名称.txt', is_dir: false } })
  await check('file-delete')
  await page.keyboard.press('Escape'); await pause()
  assert.equal(await page.locator('.file-modal').count(), 1, 'closing file confirmation closed the file list')
  await page.keyboard.press('Escape'); await pause()
  const picker = { directory: '/workspace/demo', parent: '/workspace', entries: [{ name: '文档目录', directory: '/workspace/demo/docs' }] }
  await state({ workspacePicker: picker })
  await check('workspace-picker')
  await page.keyboard.press('Escape'); await pause()
  await state({ nativeWorkspaceTicket: 'fixture-ticket' })
  await check('native-picker')
  await page.keyboard.press('Escape'); await pause()
  await state({ approval: { session_id: 's-demo', actions: [{ tool: 'write_file', args: { file_path: '/workspace/test.txt', content: '测试' } }], clarifications: [] } })
  await check('approval')
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('.approval-modal').count(), 1, 'Escape dismissed an approval')
  await state({ approval: { session_id: 's-demo', actions: [], clarifications: [{ id: 'q-demo', question: '需要怎样的页面风格？', options: ['跟随模型页面的效果', '保留当前效果'] }] } })
  await check('clarification')
  await state({ approval: null }); await pause()
  for (const colorScheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme })
    await page.setViewportSize({ width: 390, height: 844 })
    for (const [key, value] of [['showProviders', true], ['showMcp', true], ['workspacePicker', picker], ['logoutConfirm', true]]) {
      await state({ [key]: value }); await check(`${key}-mobile-${colorScheme}`)
      if (key === 'logoutConfirm') await shot(`logout-mobile-${colorScheme}`)
      await state({ [key]: key === 'workspacePicker' ? null : false }); await pause()
    }
  }
  await page.setViewportSize({ width: 640, height: 360 })
  await state({ showProviders: true })
  await page.getByRole('button', { name: '新增供应商', exact: true }).click()
  await check('provider-form-short-landscape')
  await state({ showProviders: false }); await pause()
  await state({ showMcp: true })
  await page.getByRole('button', { name: '新增工具源', exact: true }).click()
  await check('mcp-form-short-landscape')
  await state({ showMcp: false }); await pause()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await state({ logoutConfirm: true })
  await check('logout-reduced-motion')
  const motion = await page.locator('.overlay').evaluate(el => getComputedStyle(el).transitionDuration)
  assert.ok(parseFloat(motion) <= 0.001, 'reduced motion should disable opening transitions')
  await state({ logoutConfirm: false }); await pause()
  await state({ user: null }); await pause()
  await check('login-dark-mobile', '.auth-card')
  await page.getByRole('button', { name: '注册', exact: true }).click()
  await check('register-dark-mobile', '.auth-card')
  await shot('register-mobile-dark')
  }
  assert.deepEqual(errors, [], 'browser errors')
  console.log(JSON.stringify({ checks: results.length, mutations, errors, screens: results.map(r => r.name) }, null, 2))
  await fs.writeFile(fileURLToPath(new URL('./dialogs-report.json', import.meta.url)), JSON.stringify({ results, mutations, errors }, null, 2))
} catch (error) {
  await shot('failure')
  console.error('Browser errors:', errors)
  throw error
} finally { await browser.close() }
