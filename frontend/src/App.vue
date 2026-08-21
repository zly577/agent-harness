<template>
  <div class="app">
    <header class="header">
      <h1>Agent Harness</h1>
      <span class="status" :class="{ live: sessionId }">
        {{ sessionId ? '会话运行中' : '新会话' }}
      </span>
    </header>

    <!-- 消息区 -->
    <main class="messages">
      <div
        v-for="(m, i) in messages"
        :key="i"
        class="msg"
        :class="m.role"
      >
        {{ m.content }}
        <span v-if="m.role === 'assistant' && browsing" class="cursor">▋</span>
      </div>
    </main>

    <!-- 审批弹窗 -->
    <div v-if="approval" class="approval-overlay">
      <div class="approval-box">
        <h3>⛔ 待人工审批</h3>
        <p>该操作需要你的批准才能继续执行：</p>
        <div v-for="(a, i) in approval.actions" :key="i" class="approval-action">
          <code>{{ a.tool || a.type }}</code>
          <pre>{{ JSON.stringify(a.args, null, 2) }}</pre>
        </div>
        <div class="approval-btns">
          <button class="btn approve" @click="decide(true)">✅ 批准</button>
          <button class="btn reject" @click="decide(false)">⛔ 拒绝</button>
        </div>
      </div>
    </div>

    <!-- 输入区 -->
    <footer class="inputbar">
      <textarea
        v-model="input"
        placeholder="输入消息…"
        @keydown.enter.exact.prevent="send"
        :disabled="streaming"
      ></textarea>
      <button class="btn" @click="send" :disabled="streaming || !input.trim()">
        {{ streaming ? '生成中…' : '发送' }}
      </button>
      <button v-if="streaming" class="btn ghost" @click="stopStream">停止</button>
    </footer>
  </div>
</template>

<script>
import { onBeforeUnmount, reactive, ref } from 'vue'
import { streamChat } from './sse'

export default {
  setup() {
    const sessionId = ref(null)
    const messages = reactive([
      { role: 'assistant', content: '你好！我是 Agent Harness，请问有什么可以帮你？' },
    ])
    const input = ref('')
    const streaming = ref(false)
    const browsing = ref(false)
    const approval = ref(null)
    const activeController = ref(null)

    async function send() {
      const text = input.value.trim()
      if (!text || streaming.value) return
      input.value = ''
      const controller = new AbortController()
      activeController.value = controller

      messages.push({ role: 'user', content: text })
      const assistantMsg = { role: 'assistant', content: '' }
      messages.push(assistantMsg)
      streaming.value = true
      browsing.value = true

      try {
        await streamChat(text, sessionId.value, controller.signal, (evt) => {
          if (evt.type === 'session') {
            sessionId.value = evt.session_id
          } else if (evt.type === 'token') {
            assistantMsg.content += evt.content
          } else if (evt.type === 'tool') {
            assistantMsg.content += `[调用工具: ${evt.name}] `
          } else if (evt.type === 'approval') {
            approval.value = { ...evt }
          } else if (evt.type === 'result') {
            if (evt.content) assistantMsg.content = evt.content
          } else if (evt.type === 'error') {
            assistantMsg.content = `⚠️ ${evt.detail}`
          }
        })
      } catch (e) {
        if (e.name !== 'AbortError') {
          assistantMsg.content = `⚠️ ${e.message}`
        }
      }
      browsing.value = false
      streaming.value = false
      activeController.value = null
    }

    async function decide(approve) {
      if (!sessionId.value || !approval.value) return
      const decision = approve ? 'approve' : 'reject'
      try {
        const resp = await fetch(`/api/v1/approvals/${sessionId.value}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision }),
        })
        const data = await resp.json()
        if (!resp.ok) throw new Error(data.detail || '审批失败')
        messages.push({ role: 'assistant', content: `[已${approve ? '批准' : '拒绝'}] ${data.reply}` })
      } catch (e) {
        messages.push({ role: 'assistant', content: `⚠️ 审批失败：${e.message}` })
      }
      approval.value = null
      // 审批后可继续发起一个空操作让会话继续？这里简化：审批恢复已由后端完成。
    }

    function stopStream() {
      if (activeController.value) activeController.value.abort()
    }

    onBeforeUnmount(() => stopStream())

    return { sessionId, messages, input, send, streaming, browsing, approval, decide, stopStream }
  },
}
</script>

<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f5f7fa; }
.app { max-width: 820px; margin: 0 auto; height: 100vh; display: flex; flex-direction: column; }
.header { padding: 14px 20px; background: #fff; border-bottom: 1px solid #e5e7eb; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 18px; margin: 0; }
.status { font-size: 12px; color: #6b7280; background: #f3f4f6; padding: 2px 10px; border-radius: 12px; }
.status.live { color: #059669; background: #ecfdf5; }

.messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
.msg { max-width: 78%; padding: 10px 14px; border-radius: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; font-size: 14px; }
.msg.user { align-self: flex-end; background: #2563eb; color: #fff; }
.msg.assistant { align-self: flex-start; background: #fff; border: 1px solid #e5e7eb; }
.cursor { animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: 0; } }

.approval-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex; align-items: center; justify-content: center; z-index: 100; }
.approval-box { background: #fff; border-radius: 12px; padding: 24px; max-width: 520px; width: 90%; }
.approval-box h3 { margin: 0 0 12px; color: #b45309; }
.approval-action { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; margin: 8px 0; }
.approval-action code { color: #b45309; font-weight: 600; }
.approval-action pre { margin: 6px 0 0; font-size: 12px; white-space: pre-wrap; }
.approval-btns { display: flex; gap: 10px; margin-top: 16px; }

.inputbar { display: flex; gap: 10px; padding: 14px 20px; background: #fff; border-top: 1px solid #e5e7eb; }
.inputbar textarea { flex: 1; resize: none; min-height: 44px; max-height: 120px; border: 1px solid #d1d5db; border-radius: 8px; padding: 10px 12px; font-size: 14px; }
.btn { padding: 10px 18px; border: none; border-radius: 8px; cursor: pointer; background: #2563eb; color: #fff; font-size: 14px; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.ghost { background: #f3f4f6; color: #4b5563; }
.btn.approve { background: #059669; }
.btn.reject { background: #dc2626; }
</style>
