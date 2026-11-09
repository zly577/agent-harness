<template>
  <DialogOverlay class="pv-overlay" :dismissible="!busy" @close="emit('close')">
    <section class="pv schedules" role="dialog" aria-modal="true" aria-labelledby="schedules-title">
      <header class="pv-head">
        <div class="pv-head-icon" aria-hidden="true">
          <svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="7" /><path d="M10 6v4l2.5 1.5" /></svg>
        </div>
        <div class="pv-heading">
          <h2 id="schedules-title">{{ heading }}</h2>
          <p class="pv-sub">{{ subtitle }}</p>
        </div>
        <button class="pv-x" type="button" aria-label="关闭定时任务" :disabled="busy" @click="emit('close')">×</button>
      </header>

      <p v-if="error" class="pv-banner pv-banner-error" role="alert">{{ error }}</p>
      <p v-if="openingSession || notice" class="pv-banner" role="status">{{ openingSession ? '正在打开会话…' : notice }}</p>

      <form v-if="form" class="pv-form" @submit.prevent="save">
        <div class="pv-content schedule-content">
          <fieldset class="pv-grid schedule-fields" :disabled="busy">
            <label class="pv-field pv-span2">
              <span>名称</span>
              <input v-model="form.title" required maxlength="128" placeholder="例如：汇总每日工作日志" />
            </label>
            <label class="pv-field pv-span2">
              <span>任务指令</span>
              <textarea v-model="form.prompt" required maxlength="8000" rows="4" placeholder="描述每次触发时要做的事。" />
            </label>
            <label class="pv-field">
              <span id="schedule-workspace-label">工作区</span>
              <select v-model="form.workspace_id" aria-labelledby="schedule-workspace-label" required>
                <option value="legacy">默认工作区</option>
                <option v-if="missingWorkspace" :value="form.workspace_id" disabled>工作区已不可用</option>
                <option v-for="workspace in workspaces" :key="workspace.id" :value="workspace.id">{{ workspace.name }}</option>
              </select>
            </label>
            <label class="pv-field">
              <span id="schedule-provider-label">模型</span>
              <select v-model="form.provider_id" aria-labelledby="schedule-provider-label">
                <option value="">跟随我的默认模型</option>
                <option v-if="missingProvider" :value="form.provider_id" disabled>模型已不可用</option>
                <option v-for="provider in providers" :key="provider.id" :value="provider.id">{{ provider.name }} · {{ provider.model }}</option>
              </select>
            </label>
            <label class="pv-field">
              <span id="schedule-frequency-label">频率</span>
              <select v-model="form.frequency" aria-labelledby="schedule-frequency-label">
                <option value="once">仅一次</option>
                <option value="daily">每天</option>
                <option value="weekly">每周</option>
              </select>
            </label>
            <label class="pv-field">
              <span>时区</span>
              <input v-model="form.timezone" required maxlength="128" list="schedule-timezones" placeholder="Asia/Shanghai" spellcheck="false" />
              <datalist id="schedule-timezones">
                <option v-for="timezone in timezones" :key="timezone" :value="timezone" />
              </datalist>
            </label>
            <label v-if="form.frequency === 'once'" class="pv-field">
              <span>执行日期与时间</span>
              <input v-model="form.once_at" type="datetime-local" :required="!preservesUnavailableTime" />
              <em v-if="preservesUnavailableTime && !form.once_at">浏览器无法显示原有的当地时间。留空会保留原有时间。</em>
            </label>
            <label v-else class="pv-field">
              <span>执行时间</span>
              <input v-model="form.time_of_day" type="time" required />
            </label>
            <fieldset v-if="form.frequency === 'weekly'" class="pv-span2 schedule-weekdays">
              <legend>执行星期</legend>
              <label v-for="(day, index) in weekdays" :key="day" class="pv-field pv-check">
                <input v-model="form.weekdays" type="checkbox" :value="index + 1" />{{ day }}
              </label>
            </fieldset>
          </fieldset>
        </div>
        <footer class="pv-actions pv-form-actions">
          <button class="pv-btn pv-btn-primary" :disabled="busy || (editingId && !hasChanges)">{{ busy ? '保存中…' : '保存任务' }}</button>
          <button class="pv-btn" type="button" :disabled="busy" @click="backToList">取消</button>
        </footer>
      </form>

      <template v-else>
        <div class="schedule-toolbar">
          <button v-if="historyTask" class="pv-btn" type="button" :disabled="busy" @click="backToList">返回任务</button>
          <button v-else class="pv-btn pv-btn-primary" type="button" :disabled="busy" @click="editTask()">新建定时任务</button>
          <button class="pv-btn" type="button" :disabled="busy || loading" @click="load()">刷新</button>
        </div>
        <div v-if="historyTask" class="pv-content schedule-content">
          <p v-if="loading && !runs.length" class="pv-caption" role="status">正在读取运行记录…</p>
          <p v-else-if="!runs.length && !error" class="pv-caption">暂无运行记录。</p>
          <ul v-if="runs.length" class="schedule-list">
            <li v-for="run in runs" :key="run.id" class="schedule-item">
              <div class="schedule-row">
                <strong>{{ run.trigger_type === 'manual' ? '立即执行' : '定时执行' }}</strong>
                <span class="schedule-badge" :class="badgeStyle(run.status)">{{ statusLabel(run.status) }}</span>
              </div>
              <p class="pv-caption">{{ run.scheduled_for ? '计划时间' : '提交时间' }} {{ formatScheduleTime(run.scheduled_for || run.created_at, historyTask.timezone) }}</p>
              <p v-if="run.started_at || run.finished_at" class="pv-caption">
                <span v-if="run.started_at">开始 {{ formatScheduleTime(run.started_at, historyTask.timezone) }}</span>
                <span v-if="run.finished_at">结束 {{ formatScheduleTime(run.finished_at, historyTask.timezone) }}</span>
              </p>
              <p v-if="run.reason" class="schedule-reason">{{ run.reason }}</p>
              <div v-if="run.session_id" class="schedule-actions">
                <button class="pv-btn" type="button" :disabled="busy" @click="emit('open-session', run.session_id)">
                  {{ run.status === 'awaiting_approval' ? '前往审批' : run.status === 'awaiting_input' ? '前往补充信息' : '查看会话' }}
                </button>
              </div>
            </li>
          </ul>
        </div>
        <div v-else class="pv-content schedule-content">
          <p v-if="loading && !tasks.length" class="pv-caption" role="status">正在读取定时任务…</p>
          <p v-else-if="!tasks.length && !error" class="pv-caption">暂无定时任务。创建一个任务，Agent 会在计划时间开启新会话。</p>
          <ul v-if="tasks.length" class="schedule-list">
            <li v-for="task in tasks" :key="task.id" class="schedule-item">
              <div class="schedule-row">
                <strong>{{ task.title }}</strong>
                <span class="schedule-badge" :class="{ ok: task.enabled && task.next_run_at }">
                  {{ !task.enabled ? '已暂停' : task.next_run_at ? '已启用' : '排期已结束' }}
                </span>
              </div>
              <p class="pv-caption">{{ describeSchedule(task) }} · {{ task.timezone }} · {{ workspaceName(task.workspace_id) }} · {{ providerName(task.provider_id) }}</p>
              <p class="pv-caption">
                <span>下次执行 {{ formatScheduleTime(task.next_run_at, task.timezone) || '—' }}</span>
                <span v-if="task.latest_run">最近运行 {{ statusLabel(task.latest_run.status) }}</span>
              </p>
              <div class="schedule-actions">
                <button class="pv-btn" type="button" :disabled="busy" @click="editTask(task)">编辑</button>
                <button class="pv-btn" type="button" :disabled="busy || (task.enabled && !task.next_run_at)" @click="changeTask(task, task.enabled ? 'pause' : 'resume')">{{ task.enabled ? '暂停' : '恢复' }}</button>
                <button class="pv-btn" type="button" :disabled="busy" @click="changeTask(task, 'run-now')">立即执行</button>
                <button class="pv-btn" type="button" :disabled="busy" @click="showHistory(task)">运行历史</button>
              </div>
            </li>
          </ul>
        </div>
      </template>
    </section>
  </DialogOverlay>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiGet, apiPatch, apiPost } from './api'
import DialogOverlay from './DialogOverlay.vue'
import { formatScheduleTime, scheduleChanges, scheduleLocalInput, schedulePayload } from './schedulePresentation'

const props = defineProps({
  workspaceId: { type: String, default: 'legacy' },
  workspaces: { type: Array, default: () => [] },
  providerId: { type: String, default: '' },
  providers: { type: Array, default: () => [] },
  openingSession: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'open-session'])
const schedulesPath = '/api/v1/schedules'
const taskPath = id => `${schedulesPath}/${encodeURIComponent(id)}`
const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
const timezones = [...new Set([localTimezone, 'UTC', 'Asia/Shanghai', ...(Intl.supportedValuesOf?.('timeZone') || [])])]
const statuses = {
  queued: '等待执行', starting: '启动中', running: '执行中', awaiting_approval: '等待审批',
  awaiting_input: '等待补充信息', succeeded: '已完成', failed: '失败', stopped: '已停止',
  skipped: '已跳过', missed: '已错过', interrupted: '已中断',
}

const tasks = ref([])
const runs = ref([])
const form = ref(null)
const editingId = ref(null)
const historyTask = ref(null)
const loading = ref(false)
const mutating = ref(false)
const busy = computed(() => mutating.value || props.openingSession)
const error = ref('')
const notice = ref('')
let original = null
let requestId = 0
let pendingLoad = false
let timer

const heading = computed(() => form.value ? (editingId.value ? '编辑定时任务' : '新建定时任务') : historyTask.value ? '运行历史' : '定时任务')
const subtitle = computed(() => historyTask.value
  ? `${historyTask.value.title} · ${historyTask.value.timezone}`
  : '每次执行都会在沙箱中开启独立会话。命令须在会话中审批后执行。')
const hasChanges = computed(() => !!form.value && !!original && Object.keys(scheduleChanges(original, schedulePayload(form.value))).length > 0)
const preservesUnavailableTime = computed(() => !!editingId.value && original?.frequency === 'once' && !original.once_at
  && form.value?.timezone.trim() === original.timezone)
const missingWorkspace = computed(() => form.value.workspace_id !== 'legacy' && !props.workspaces.some(item => item.id === form.value.workspace_id))
const missingProvider = computed(() => !!form.value.provider_id && !props.providers.some(item => item.id === form.value.provider_id))

function workspaceName(id) {
  return id && id !== 'legacy' ? props.workspaces.find(item => item.id === id)?.name || '工作区已不可用' : '默认工作区'
}

function providerName(id) {
  return id ? props.providers.find(item => item.id === id)?.name || '模型已不可用' : '跟随我的默认模型'
}

function statusLabel(status) {
  return statuses[status] || status
}

function badgeStyle(status) {
  if (status === 'succeeded') return 'ok'
  if (status === 'failed' || status === 'interrupted') return 'bad'
  if (status === 'awaiting_approval' || status === 'awaiting_input') return 'warn'
  return ''
}

function describeSchedule(task) {
  if (task.frequency === 'once') return `仅一次 · ${formatScheduleTime(task.once_at, task.timezone)}`
  if (task.frequency === 'weekly') return `每周 ${task.weekdays.map(day => weekdays[day - 1]).join('、')} ${task.time_of_day}`
  return `每天 ${task.time_of_day}`
}

async function load(silent = false) {
  const id = ++requestId
  const history = historyTask.value
  pendingLoad = true
  if (!silent) {
    loading.value = true
    error.value = ''
  }
  try {
    const data = await apiGet(history ? `${taskPath(history.id)}/runs` : schedulesPath)
    if (!Array.isArray(data)) throw new Error('服务返回了无效的定时任务数据。')
    if (id === requestId) {
      const target = history ? runs : tasks
      target.value = data
    }
  } catch (cause) {
    if (id === requestId && !silent) error.value = cause.message
  } finally {
    if (id === requestId) {
      loading.value = false
      pendingLoad = false
    }
  }
}

function editTask(task) {
  requestId += 1
  loading.value = false
  error.value = ''
  notice.value = ''
  editingId.value = task?.id ?? null
  form.value = {
    title: task?.title ?? '', prompt: task?.prompt ?? '',
    workspace_id: task ? task.workspace_id || 'legacy' : props.workspaceId,
    provider_id: task ? task.provider_id || '' : props.providerId,
    timezone: task?.timezone ?? localTimezone,
    frequency: task?.frequency ?? 'daily',
    once_at: scheduleLocalInput(task?.once_at, task?.timezone),
    time_of_day: task?.time_of_day ?? '09:00',
    weekdays: [...(task?.weekdays ?? [])],
  }
  original = task ? schedulePayload(form.value) : null
}

function showHistory(task) {
  notice.value = ''
  historyTask.value = task
  runs.value = []
  load()
}

function backToList() {
  form.value = null
  historyTask.value = null
  notice.value = ''
  load()
}

async function save() {
  const payload = schedulePayload(form.value)
  if (!payload.title || !payload.prompt.trim()) {
    error.value = '请填写任务名称和指令。'
    return
  }
  if (payload.frequency === 'weekly' && !payload.weekdays.length) {
    error.value = '请至少选择一个执行星期。'
    return
  }
  if (!payload.timezone) {
    error.value = '请输入有效的 IANA 时区，例如 Asia/Shanghai。'
    return
  }
  await mutate(async () => {
    if (editingId.value) await apiPatch(taskPath(editingId.value), scheduleChanges(original, payload))
    else await apiPost(schedulesPath, payload)
    form.value = null
    notice.value = '任务已保存。'
    await load()
  })
}

async function changeTask(task, action) {
  await mutate(async () => {
    const result = await apiPost(`${taskPath(task.id)}/${action}`)
    if (action === 'run-now') notice.value = result.reason || `立即执行：${statusLabel(result.status)}。`
    await load()
  })
}

async function mutate(action) {
  if (busy.value) return
  mutating.value = true
  error.value = ''
  notice.value = ''
  try {
    await action()
  } catch (cause) {
    error.value = cause.message
  } finally {
    mutating.value = false
  }
}

onMounted(() => {
  load()
  timer = setInterval(() => {
    if (!document.hidden && !pendingLoad && !busy.value && !form.value && !error.value) load(true)
  }, 10000)
})
onBeforeUnmount(() => {
  requestId += 1
  clearInterval(timer)
})
</script>

<style scoped>
.schedules { max-width: 720px; }
.schedule-content { padding-bottom: 24px; }
.schedule-toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 28px 16px; }
.schedule-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }
.schedule-item { padding: 12px 14px; border: 1px solid var(--border); border-radius: 12px; overflow-wrap: anywhere; }
.schedule-row { display: flex; justify-content: space-between; align-items: start; gap: 10px; }
.schedule-badge { flex: none; border-radius: 6px; padding: 2px 7px; background: var(--fill); color: var(--text-2); font-size: 12px; }
.schedule-badge.ok { color: var(--ok); }
.schedule-badge.bad { color: var(--danger); }
.schedule-badge.warn { color: var(--warn); }
.schedule-item p > span + span { margin-inline-start: 12px; }
.schedule-reason { margin: 7px 0 0; color: var(--text-2); font-size: 12px; white-space: pre-wrap; }
.schedule-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.schedule-actions .pv-btn { min-height: 30px; padding: 4px 9px; font-size: 12px; }
.schedule-fields, .schedule-weekdays { border: 0; padding: 0; margin: 0; min-width: 0; }
.schedule-weekdays { display: flex; flex-wrap: wrap; gap: 8px 14px; }
.schedule-weekdays legend { padding: 0; margin-bottom: 8px; color: var(--text-2); font-size: 13px; }
@media (max-width: 640px) {
  .schedule-toolbar { margin-inline: 18px; }
}
</style>
