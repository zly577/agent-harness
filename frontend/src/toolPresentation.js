const labels = {
  file_path: '文件', path: '路径', paths: '路径', pattern: '匹配规则', query: '搜索内容', command: '命令',
  content: '文件内容', old_string: '原内容', new_string: '替换内容', replace_all: '全部替换',
  offset: '起始行', limit: '数量上限', glob: '文件范围', recursive: '包含子目录',
  description: '任务说明', subagent_type: '执行智能体', timeout: '超时时间', timeout_seconds: '超时（秒）',
  max_results: '结果数量', url: '地址', urls: '地址', cwd: '执行目录', encoding: '编码',
  todos: '任务', tasks: '任务', status: '状态', name: '名称', title: '标题', id: '标识',
}
export const toolNames = { ls: '浏览目录', glob: '查找文件', grep: '搜索内容', read_file: '读取文件', write_file: '写入文件', edit_file: '修改文件', execute: '执行命令', task: '委派任务', write_todos: '更新计划', ask_user: '询问用户', web_search: '联网搜索', delete: '删除文件' }
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
export function taskPlan(message) {
  if ((!message.streaming && !message.planPaused) || message.planFinished) return []
  const calls = (message.toolCalls || []).filter(call => call.name === 'write_todos' && call.status === 'done' && call.ok !== false)
  const latest = calls[calls.length - 1]
  const items = latest?.args?.todos || latest?.args?.tasks
  if (!Array.isArray(items)) return []
  const plan = items.filter(item => item && typeof item === 'object').map((item, i) => ({
    key: item.id || `${i}:${item.content || item.description || item.title || ''}`,
    text: String(item.content || item.description || item.title || item.name || '').trim(),
    status: item.status === 'completed' || item.status === 'done' ? 'completed' : item.status === 'in_progress' || item.status === 'running' ? 'running' : 'pending',
  })).filter(item => item.text)
  return plan.length && plan.some(item => item.status !== 'completed') ? plan : []
}


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
