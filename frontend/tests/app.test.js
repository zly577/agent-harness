import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { useMessageEvents } from '../src/composables/useMessageEvents.js'

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
          return 'export const renderMarkdown = () => ""; export const renderTerminal = () => ""; export const clearMarkdownCache = () => {};'
        }
      },
    }],
  })
  try {
    const { default: App } = await server.ssrLoadModule('/src/App.vue')
    const html = await renderToString(createSSRApp(App))
    assert.match(html, /正在检查登录状态/)
    let toolCalls = [{ id: 't', name: 'read_file', args: { file_path: '/a' }, result: 'ok', status: 'done', ok: true }]
    const authedApp = {
      ...App,
      setup(...args) {
        const state = App.setup(...args)
        state.authBootstrapping.value = false
        state.user.value = { id: 'test', email: 'test@example.com', display_name: '测试用户' }
        state.messages.push({
          role: 'assistant', content: 'hello', thinking: '', streaming: false, startedAt: 1000, endedAt: 43210,
          toolCalls,
          delegations: [],
        })
        return state
      },
    }
    const chatHtml = await renderToString(createSSRApp(authedApp))
    assert.match(chatHtml, /Agent Harness/)
    assert.match(chatHtml, /测试用户/)
    assert.match(chatHtml, /read_file|读取文件/)
    assert.match(chatHtml, /已完成 · 用时 42 秒/)
    assert.doesNotMatch(chatHtml, /tok\/s|token\/s|meter-rate/)

    const events = useMessageEvents({ messages: [] })
    const message = { toolCalls: [], delegations: [] }
    toolCalls = message.toolCalls
    events.applyDataEvent(message, { type: 'tool_args', id: 'write', name: 'write_file', kb: 10.4, elapsed: 21 })
    const generatingHtml = await renderToString(createSSRApp(authedApp))
    assert.match(generatingHtml, /class="operation-notice" role="status"/)
    assert.match(generatingHtml, /尚未开始执行/)
    assert.match(generatingHtml, /10\.4 KB/)
    events.applyDataEvent(message, { type: 'tool_call', id: 'write', name: 'write_file', args: { file_path: '/report.json', content: '{}' } })
    const runningHtml = await renderToString(createSSRApp(authedApp))
    assert.match(runningHtml, /report\.json/)
    assert.match(runningHtml, /等待工具返回结果/)
    assert.doesNotMatch(runningHtml, /尚未开始执行/)
    events.applyDataEvent(message, { type: 'tool_result', id: 'write', content: '写入完成', ok: true })
    const doneHtml = await renderToString(createSSRApp(authedApp))
    assert.match(doneHtml, /写入完成/)
    assert.doesNotMatch(doneHtml, /class="operation-notice"/)

    // An empty older operation must also remain readable inside the archive.
    toolCalls = [{ id: 'empty', name: 'write_file', status: 'done', args: {} }, ...toolCalls]
    const archiveHtml = await renderToString(createSSRApp(authedApp))
    assert.match(archiveHtml, /此记录没有保存操作参数或文本结果/)
  } finally {
    await server.close()
  }
})
