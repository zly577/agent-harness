const labels = {
  file_path: '文件', path: '路径', paths: '路径', pattern: '匹配规则', query: '搜索内容', command: '命令',
  content: '文件内容', old_string: '原内容', new_string: '替换内容', replace_all: '全部替换',
  offset: '起始位置', limit: '数量上限', glob: '文件范围', recursive: '包含子目录',
  description: '任务说明', subagent_type: '执行智能体', timeout: '超时时间', timeout_seconds: '超时（秒）',
  reason: '原因',
  max_results: '结果数量', url: '地址', urls: '地址', cwd: '执行目录', encoding: '编码',
  todos: '任务', tasks: '任务', status: '状态', name: '名称', title: '标题', id: '标识',
  snapshot: '任务状态', expected_revision: '预期版本', source: '来源', tier: '记忆层级',
  message_ids: '消息标识', scope: '检索范围', max_chars: '字符预算', key: '记忆标识',
}
export const toolNames = { ls: '浏览目录', glob: '查找文件', grep: '搜索内容', read_file: '读取文件', write_file: '写入文件', edit_file: '修改文件', execute: '执行命令', task: '委派任务', write_todos: '更新计划', ask_user: '询问用户', web_search: '联网搜索', delete: '删除文件', update_task_state: '保存任务状态', search_history: '搜索历史', read_history: '读取历史', remember: '保存长期记忆' }
export function toolName(name) { return toolNames[name] || name || '工具' }
export function argumentFields(args) {
  if (args == null) return []
  const entries = Array.isArray(args) ? args.map((v, i) => [String(i + 1), v]) : typeof args === 'object' ? Object.entries(args) : [['内容', args]]
  return entries.map(([key, value]) => ({ key, label: labels[key] || key.replace(/_/g, ' '), value, nested: value !== null && typeof value === 'object' }))
}
export function argumentText(value) {
  if (value === true) return '是'
  if (value === false) return '否'
  if (value == null) return '未设置'
  return String(value)
}
function todoItems(call) {
  const items = call?.args?.todos ?? call?.args?.tasks
  return Array.isArray(items) ? items : null
}

function normalizePlan(items) {
  const occurrences = new Map()
  return (items || []).filter(item => item && typeof item === 'object').map(item => {
    const text = String(item.content || item.description || item.title || item.name || '').trim()
    const occurrence = occurrences.get(text) || 0
    occurrences.set(text, occurrence + 1)
    return {
      // Index-based keys remount checked rows when the model inserts a task.
      key: item.id != null ? `id:${item.id}` : `text:${text}:${occurrence}`,
      text,
      status: item.status === 'completed' || item.status === 'done' ? 'completed' : item.status === 'in_progress' || item.status === 'running' ? 'running' : 'pending',
    }
  }).filter(item => item.text)
}

export function taskPlanState(message) {
  const calls = (message.toolCalls || []).filter(call => call.name === 'write_todos')
  const latest = calls[calls.length - 1]
  const saved = calls.filter(call => call.status === 'done' && call.ok !== false && todoItems(call) !== null).at(-1)
  const updating = latest?.status === 'running' || latest?.status === 'generating'
  const error = latest?.ok === false || latest?.status === 'error' ? '清单更新失败，未确认的完成状态不会打钩。'
    : latest?.status === 'interrupted' ? '清单更新已中断，未确认的完成状态不会打钩。' : ''
  const items = normalizePlan(todoItems(saved || latest))
  // Display the first submitted list immediately, but only acknowledge completion
  // after the tool succeeds. Failed later updates retain the last saved list.
  if (!saved) items.forEach(item => { if (item.status === 'completed') item.status = 'pending' })
  return {
    hasCall: calls.length > 0,
    items,
    updating,
    error,
    complete: items.length > 0 && items.every(item => item.status === 'completed') && !updating && !error,
  }
}

// Parsing does not hide completed plans; the component owns the exit animation.
export function taskPlan(message) { return taskPlanState(message).items }


/** Show the physical destination without changing the tool's virtual argument. */
export function workspacePath(key, value, directory) {
  if (!directory || typeof value !== 'string' || !/^(file_path|path|paths|cwd|\d+)$/.test(key)) return ''
  if (value !== '/workspace' && !value.startsWith('/workspace/')) return ''
  const parts = value.slice('/workspace'.length).split('/').filter(Boolean)
  if (parts.some(part => part === '..' || part === '.')) return ''
  const separator = directory.includes('\\') ? '\\' : '/'
  const root = directory.replace(/[\\/]+$/, '')
  return parts.length ? root + separator + parts.join(separator) : directory
}
