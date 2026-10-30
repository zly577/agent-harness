<template>
  <DialogOverlay class="pv-overlay" :dismissible="!busy && !dirty" @close="close">
    <section class="pv personal-settings" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <header class="pv-head">
        <div class="pv-heading">
          <h2 id="settings-title">个人设置</h2>
          <p class="pv-sub">管理用户记忆、账户安全和智能体运行偏好。</p>
        </div>
        <button class="pv-x" aria-label="关闭个人设置" :disabled="busy" @click="close">×</button>
      </header>
      <nav class="personal-tabs" aria-label="设置分类">
        <button v-for="item in tabs" :key="item.id" class="pv-btn" :class="{ selected: tab === item.id }"
          :aria-current="tab === item.id ? 'page' : undefined" :disabled="busy" @click="tab = item.id">{{ item.label }}</button>
      </nav>
      <div class="pv-content personal-content">
        <p v-if="message" class="pv-result ok" role="status">{{ message }}</p>
        <template v-if="tab === 'memory'">
          <h3>长期记忆</h3>
          <p class="pv-caption">用户记忆 · /memories/Agent.md</p>
          <p class="pv-caption">保存回答习惯、项目约定和工作偏好。同一账户的所有工作区和会话共享记忆，保存后从下一轮对话生效；项目约定请注明适用项目。</p>
          <p v-if="memory" class="pv-caption">每轮常驻加载前 {{ memory.core_limit?.toLocaleString() }} 字；详细经验保存在 {{ memory.detail_directory }}，按需读取。</p>
          <p v-if="memory && memoryContent.length > memory.core_limit" class="pv-result bad" role="status">当前内容超过常驻容量，超出部分仍保存在文件中。请把详细记录整理到按需记忆目录，核心偏好保持简短。</p>
          <p v-if="memoryError" class="pv-result bad" role="alert">{{ memoryError }}</p>
          <p v-if="memoryLoading">正在读取记忆…</p>
          <form v-else-if="memory" @submit.prevent="saveMemory">
            <label class="pv-field memory-field">
              <span>记忆内容 <em>支持 Markdown，最多 10 万字</em></span>
              <textarea v-model="memoryContent" rows="14" maxlength="100000" :disabled="busy" placeholder="例如：请用中文回答；修改代码后说明验证结果。" />
            </label>
            <p class="pv-caption">{{ memoryContent.length.toLocaleString() }} / 100,000 字<span v-if="!memory.exists"> · 当前账户尚未创建记忆文件</span></p>
            <div class="personal-actions">
              <button class="pv-btn pv-btn-primary" :disabled="busy || !memoryDirty">{{ busy ? '保存中…' : '保存记忆' }}</button>
              <button type="button" class="pv-btn" :disabled="busy" @click="reloadMemory">重新加载</button>
            </div>
          </form>
          <button v-else-if="!memoryLoading" class="pv-btn" @click="loadMemory">重试读取</button>
        </template>

        <template v-else-if="tab === 'account'">
          <h3>账户安全</h3>
          <dl class="account-details"><dt>登录邮箱</dt><dd>{{ user.email }}</dd><dt>昵称</dt><dd>{{ user.display_name || '未设置' }}</dd><dt>账户角色</dt><dd>{{ user.role === 'admin' ? '管理员' : '普通用户' }}</dd></dl>
          <p class="pv-caption">修改密码后，本页面会退出登录，请使用新密码重新登录。</p>
          <p v-if="passwordError" class="pv-result bad" role="alert">{{ passwordError }}</p>
          <form class="password-form" @submit.prevent="changePassword">
            <input class="account-username" :value="user.email" autocomplete="username" tabindex="-1" aria-hidden="true" />
            <label class="pv-field"><span>原密码</span><input v-model="password.old" type="password" autocomplete="current-password" required maxlength="128" :disabled="busy" /></label>
            <label class="pv-field"><span>新密码 <em>8–128 位</em></span><input v-model="password.next" type="password" autocomplete="new-password" required minlength="8" maxlength="128" :disabled="busy" /></label>
            <label class="pv-field"><span>确认新密码</span><input v-model="password.confirm" type="password" autocomplete="new-password" required minlength="8" maxlength="128" :disabled="busy" /></label>
            <div class="personal-actions"><button class="pv-btn pv-btn-primary" :disabled="busy">{{ busy ? '修改中…' : '修改密码并重新登录' }}</button></div>
          </form>
        </template>

        <template v-else>
          <h3>{{ tab === 'context' ? '上下文管理' : '任务执行预算' }}</h3>
          <p class="pv-caption">按账户保存，适用于你的所有工作区和会话；从下一轮对话生效，正在执行的任务保持原设置。</p>
          <p v-if="preferencesError" class="pv-result bad" role="alert">{{ preferencesError }}</p>
          <p v-if="preferencesLoading">正在读取设置…</p>
          <form v-else-if="preferences" @submit.prevent="savePreferences">
            <div v-if="tab === 'context'" class="pv-grid personal-grid">
              <label class="pv-field"><span>模型上下文窗口（token）</span><input v-model.number="form.context_window_tokens" type="number" min="0" max="10000000" step="1" required :disabled="busy" /><em>0 表示自动识别；无法识别时使用 {{ preferences.context_fallback_tokens?.toLocaleString() }}。自定义端点请填写实际窗口。</em></label>
              <label class="pv-field"><span>输出预留（token）</span><input v-model.number="form.context_output_reserve" type="number" min="256" max="1000000" step="1" required :disabled="busy" /></label>
              <label class="pv-field"><span>触发压缩的输入预算占比（%）</span><input v-model.number="form.context_trigger_percent" type="number" min="30" max="95" step="1" required :disabled="busy" /></label>
              <label class="pv-field"><span>压缩后保留的近期内容（token）</span><input v-model.number="form.context_keep_tokens" type="number" min="256" max="1000000" step="1" required :disabled="busy" /><em>预算不足时自动缩小，保留完整工具交互边界。</em></label>
              <p class="pv-caption pv-span2">预算包含系统提示、用户记忆、技能、工具定义和消息；扣除输出预留及安全余量。输入大小为保守估算，并参考供应商返回的实际用量校准。完整聊天记录仍保留。</p>
            </div>
            <div v-else class="pv-grid personal-grid">
              <label class="pv-field"><span>每轮工具调用上限</span><input v-model.number="form.tool_call_run_limit" type="number" min="0" max="100000" step="1" required :disabled="busy" /><em>0 表示不限制</em></label>
              <label class="pv-field"><span>每会话工具调用上限</span><input v-model.number="form.tool_call_thread_limit" type="number" min="0" max="100000" step="1" required :disabled="busy" /><em>同一工具范围跨轮次累计，保存不会清零该范围计数</em></label>
              <fieldset class="budget-scope pv-span2" :disabled="busy">
                <legend>预算适用范围</legend>
                <div class="budget-scope-options">
                  <label :class="{ selected: budgetScope === 'all' }">
                    <input v-model="budgetScope" type="radio" name="budget-scope" value="all" />
                    <span><strong>全部工具</strong><small>所有工具的调用次数合计计算</small></span>
                  </label>
                  <label :class="{ selected: budgetScope === 'single' }">
                    <input v-model="budgetScope" type="radio" name="budget-scope" value="single" />
                    <span><strong>指定工具</strong><small>只统计选中的工具，其他工具不计入</small></span>
                  </label>
                </div>
              </fieldset>
              <label v-if="budgetScope === 'single'" class="pv-field pv-span2">
                <span>选择工具</span>
                <select v-model="budgetToolChoice" aria-label="选择工具" :disabled="busy">
                  <option v-for="tool in budgetTools" :key="tool.value" :value="tool.value">{{ tool.label }}</option>
                  <option value="custom">其他工具（高级）</option>
                </select>
              </label>
              <label v-if="budgetScope === 'single' && budgetToolChoice === 'custom'" class="pv-field pv-span2">
                <span>工具标识</span><input v-model="customBudgetTool" aria-label="工具标识" maxlength="128" required :disabled="busy" placeholder="填写自定义或 MCP 工具的标识" />
                <em>仅自定义工具需要填写；常用工具可直接从上方选择。</em>
              </label>
              <p class="budget-summary pv-span2" aria-live="polite">{{ budgetSummary }}</p>
              <p class="pv-caption pv-span2">成功和失败的调用均计数。只统计主智能体调用，全部工具范围包含任务委派本身，不包含子智能体内部调用，也不代表费用上限。新启用的工具范围从启用后开始计数。</p>
            </div>
            <div class="personal-actions">
              <button class="pv-btn pv-btn-primary" :disabled="busy || !sectionDirty">{{ busy ? '保存中…' : '保存设置' }}</button>
              <button type="button" class="pv-btn" :disabled="busy || (!preferences.customized && !preferencesDirty)" @click="resetPreferences">恢复全局默认</button>
            </div>
            <p class="pv-caption">{{ preferences.customized ? '已使用个人设置；刷新页面和重启服务后仍保留。' : '当前跟随全局默认设置。' }}</p>
          </form>
          <button v-else-if="!preferencesLoading" class="pv-btn" @click="loadPreferences">重试读取</button>
        </template>
      </div>
    </section>
  </DialogOverlay>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import DialogOverlay from './DialogOverlay.vue'
import { apiDelete, apiGet, apiPost, apiPut } from './api'
import { useConfirmation } from './composables/useConfirmation'
import { toolNames } from './toolPresentation'

const props = defineProps({ user: { type: Object, required: true }, workspaceId: { type: String, required: true }, workspaceName: { type: String, default: '默认工作区' } })
const emit = defineEmits(['close', 'password-changed'])
const confirm = useConfirmation()
const tabs = [{ id: 'memory', label: '长期记忆' }, { id: 'account', label: '账户安全' }, { id: 'context', label: '上下文管理' }, { id: 'budget', label: '执行预算' }]
const contextKeys = ['context_window_tokens', 'context_output_reserve', 'context_trigger_percent', 'context_keep_tokens']
const budgetKeys = ['tool_call_run_limit', 'tool_call_thread_limit', 'tool_call_limit_tool']
const tab = ref('memory'), busy = ref(false), message = ref('')
const memory = ref(null), memoryContent = ref(''), memoryError = ref(''), memoryLoading = ref(true)
const preferences = ref(null), preferencesError = ref(''), preferencesLoading = ref(true), form = reactive({})
const budgetTools = ['web_search', 'execute', 'read_file', 'write_file', 'edit_file', 'ls', 'glob', 'grep', 'task', 'write_todos', 'ask_user', 'search_history', 'read_history', 'remember', 'update_task_state']
  .map(value => ({ value, label: toolNames[value] }))
const budgetScope = ref('all'), budgetToolChoice = ref('web_search'), customBudgetTool = ref('')
const budgetToolValue = computed(() => budgetScope.value === 'all' ? ''
  : budgetToolChoice.value === 'custom' ? customBudgetTool.value.trim() : budgetToolChoice.value)
const draftValue = key => key === 'tool_call_limit_tool' ? budgetToolValue.value : form[key]
const budgetSummary = computed(() => {
  const limits = []
  if (form.tool_call_run_limit > 0) limits.push(`每轮最多 ${form.tool_call_run_limit} 次`)
  if (form.tool_call_thread_limit > 0) limits.push(`每会话最多 ${form.tool_call_thread_limit} 次`)
  if (!limits.length) return '当前未设置次数上限。'
  const target = budgetScope.value === 'all' ? '全部工具合计' : budgetToolChoice.value === 'custom'
    ? '所选自定义工具' : budgetTools.find(tool => tool.value === budgetToolChoice.value)?.label
  return `${target}：${limits.join('，')}。`
})
const password = reactive({ old: '', next: '', confirm: '' }), passwordError = ref('')
const controller = new AbortController()
const opts = { signal: controller.signal }
const memoryUrl = '/api/v1/preferences/memory'
const memoryDirty = computed(() => !!memory.value && memoryContent.value !== memory.value.content)
const changed = keys => !!preferences.value && (keys.some(key => draftValue(key) !== preferences.value.values[key])
  || (keys.includes('tool_call_limit_tool') && budgetScope.value === 'single' && !budgetToolValue.value))
const preferencesDirty = computed(() => changed([...contextKeys, ...budgetKeys]))
const sectionDirty = computed(() => changed(tab.value === 'context' ? contextKeys : budgetKeys))
const dirty = computed(() => memoryDirty.value || preferencesDirty.value || !!(password.old || password.next || password.confirm))
watch(tab, () => { message.value = '' })

function syncBudgetTool(value) {
  budgetScope.value = value ? 'single' : 'all'
  budgetToolChoice.value = !value ? 'web_search' : budgetTools.some(tool => tool.value === value) ? value : 'custom'
  customBudgetTool.value = budgetToolChoice.value === 'custom' ? value : ''
}

async function close() {
  if (busy.value) return
  if (dirty.value && !await confirm({ title: '放弃未保存的设置？', description: '关闭后，尚未保存的修改将丢失。', confirmLabel: '放弃修改' })) return
  emit('close')
}
async function loadMemory() {
  memoryLoading.value = true
  memoryError.value = ''
  try { memory.value = await apiGet(memoryUrl, opts); memoryContent.value = memory.value.content }
  catch (error) { if (!controller.signal.aborted) memoryError.value = error.message }
  finally { memoryLoading.value = false }
}
async function reloadMemory() {
  if (memoryDirty.value && !await confirm({ title: '重新加载记忆？', description: '未保存的记忆修改将被丢弃。', confirmLabel: '重新加载' })) return
  await loadMemory()
}
async function saveMemory() {
  busy.value = true; memoryError.value = ''; message.value = ''
  try {
    memory.value = await apiPut(memoryUrl, { content: memoryContent.value, revision: memory.value.revision }, opts)
    message.value = '记忆已保存，从下一轮对话生效。'
  } catch (error) { if (!controller.signal.aborted) memoryError.value = error.message }
  finally { busy.value = false }
}
async function loadPreferences() {
  preferencesLoading.value = true; preferencesError.value = ''
  try { preferences.value = await apiGet('/api/v1/preferences', opts); Object.assign(form, preferences.value.values); syncBudgetTool(preferences.value.values.tool_call_limit_tool) }
  catch (error) { if (!controller.signal.aborted) preferencesError.value = error.message }
  finally { preferencesLoading.value = false }
}
async function savePreferences() {
  const keys = tab.value === 'context' ? contextKeys : budgetKeys
  const values = { ...preferences.value.values, ...Object.fromEntries(keys.map(key => [key, draftValue(key)])) }
  preferencesError.value = ''; message.value = ''
  if (tab.value === 'budget' && budgetScope.value === 'single' && !budgetToolValue.value) { preferencesError.value = '请选择工具，或填写自定义工具标识。'; return }
  if (!Number.isInteger(values.context_max_messages) || !Number.isInteger(values.context_keep_messages) || values.context_keep_messages >= values.context_max_messages) { preferencesError.value = '消息数须为整数，且保留消息数必须小于压缩阈值。'; return }
  if (contextKeys.some(key => !Number.isInteger(values[key]))) { preferencesError.value = '上下文设置须为整数。'; return }
  if (values.context_window_tokens && values.context_window_tokens < values.context_output_reserve + 2048) { preferencesError.value = '上下文窗口须大于输出预留至少 2048 token。'; return }
  if (values.tool_call_thread_limit && values.tool_call_run_limit > values.tool_call_thread_limit) { preferencesError.value = '每轮调用上限不能大于会话调用上限。'; return }
  busy.value = true
  try {
    preferences.value = await apiPut('/api/v1/preferences', values, opts)
    for (const key of keys) form[key] = preferences.value.values[key]
    if (keys.includes('tool_call_limit_tool')) syncBudgetTool(preferences.value.values.tool_call_limit_tool)
    message.value = '设置已保存，从下一轮对话生效。'
  } catch (error) { if (!controller.signal.aborted) preferencesError.value = error.message }
  finally { busy.value = false }
}
async function resetPreferences() {
  if (!await confirm({ title: '恢复全局默认？', description: '上下文管理和执行预算将一起恢复全局默认，覆盖这两项的未保存修改；长期记忆不受影响。', confirmLabel: '恢复默认' })) return
  busy.value = true; preferencesError.value = ''; message.value = ''
  try { preferences.value = await apiDelete('/api/v1/preferences', opts); Object.assign(form, preferences.value.values); syncBudgetTool(preferences.value.values.tool_call_limit_tool); message.value = '已恢复全局默认，从下一轮对话生效。' }
  catch (error) { if (!controller.signal.aborted) preferencesError.value = error.message }
  finally { busy.value = false }
}
async function changePassword() {
  passwordError.value = ''; message.value = ''
  if (password.next !== password.confirm) { passwordError.value = '两次输入的新密码不一致。'; return }
  if (password.old === password.next) { passwordError.value = '新密码不能与原密码相同。'; return }
  if ((memoryDirty.value || preferencesDirty.value) && !await confirm({ title: '修改密码并退出登录？', description: '长期记忆或运行设置还有未保存的修改，退出登录后会丢失。', confirmLabel: '修改密码并退出' })) return
  busy.value = true
  try {
    await apiPost('/api/v1/auth/change-password', { old_password: password.old, new_password: password.next }, opts)
    password.old = password.next = password.confirm = ''
    emit('password-changed')
  } catch (error) { if (!controller.signal.aborted) passwordError.value = error.message }
  finally { busy.value = false }
}
onMounted(() => { loadMemory(); loadPreferences() })
onBeforeUnmount(() => { controller.abort(); password.old = password.next = password.confirm = '' })
</script>

<style src="./styles/settings.css"></style>
<style scoped>
.personal-settings { max-width: 820px; }
.personal-tabs { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 28px 18px; border-bottom: 1px solid var(--border); }
.personal-tabs .selected { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-border); }
.personal-content { overflow-y: auto; padding-top: 10px; padding-bottom: 26px; }
.personal-content h3 { font-size: 16px; margin: 14px 0 8px; }
.personal-content .pv-caption { margin: 8px 0 14px; }
.memory-field, .personal-grid, .password-form { margin-top: 20px; }
.personal-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }
.budget-scope { min-width: 0; margin: 0; padding: 0; border: 0; }
.budget-scope legend { padding: 0; margin-bottom: 10px; color: var(--text-2); font-size: 13px; }
.budget-scope-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.budget-scope-options label { display: flex; align-items: flex-start; gap: 10px; padding: 14px; border: 1px solid var(--border-2); border-radius: 10px; cursor: pointer; }
.budget-scope-options label.selected { border-color: var(--accent-border); background: var(--accent-soft); }
.budget-scope-options input { flex: 0 0 auto; margin: 4px 0 0; accent-color: var(--accent-button); }
.budget-scope-options strong { display: block; font-size: 13px; font-weight: 500; }
.budget-scope-options small { display: block; margin-top: 4px; color: var(--text-2); font-size: 12px; }
.budget-summary { margin: 0; padding: 12px 14px; border-radius: 10px; background: var(--fill); color: var(--text-2); font-size: 13px; }
.password-form { display: grid; gap: 16px; max-width: 440px; }
.account-details { display: grid; grid-template-columns: 90px 1fr; gap: 10px; overflow-wrap: anywhere; }
.account-details dt { color: var(--text-2); }
.account-details dd { margin: 0; }
.account-username { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
@media (max-width: 640px) { .personal-tabs { padding-right: 18px; padding-left: 18px; } .budget-scope-options { grid-template-columns: minmax(0, 1fr); } }
</style>
