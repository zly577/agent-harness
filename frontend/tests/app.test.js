import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

test('App setup and login screen render after extracting composables', async () => {
  const server = await createServer({
    server: { middlewareMode: true }, appType: 'custom',
    plugins: [{
      name: 'test-browser-markdown',
      enforce: 'pre',
      load(id) {
        // Markdown sanitization requires a browser DOM. This smoke test checks
        // component setup and template bindings, so replace only that boundary.
        if (id.replaceAll('\\', '/').endsWith('/src/markdown.js')) {
          return 'export const renderMarkdown = () => ""; export const renderTerminal = () => "";'
        }
      },
    }],
  })
  try {
    const { default: App } = await server.ssrLoadModule('/src/App.vue')
    const html = await renderToString(createSSRApp(App))
    assert.match(html, /正在检查登录状态/)
    const authedApp = {
      ...App,
      setup(...args) {
        const state = App.setup(...args)
        state.authBootstrapping.value = false
        state.user.value = { id: 'test', email: 'test@example.com', display_name: '测试用户' }
        state.messages.push({
          role: 'assistant', content: 'hello', thinking: '', streaming: false,
          toolCalls: [{ id: 't', name: 'read_file', args: { file_path: '/a' }, result: 'ok', status: 'done', ok: true }],
          delegations: [],
        })
        return state
      },
    }
    const chatHtml = await renderToString(createSSRApp(authedApp))
    assert.match(chatHtml, /Agent Harness/)
    assert.match(chatHtml, /测试用户/)
    assert.match(chatHtml, /read_file|读取文件/)
  } finally {
    await server.close()
  }
})
