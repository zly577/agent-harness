import assert from 'node:assert/strict'
import { taskPlan, argumentFields, argumentText, toolName } from '../src/toolPresentation.js'
const tasks = [{ content: '检查项目', status: 'completed' }, { content: '实现修改', status: 'in_progress' }, { content: '验证结果', status: 'pending' }]
const call = (todos, status = 'done', ok = true) => ({ name: 'write_todos', args: { todos }, status, ok })
const message = { streaming: true, toolCalls: [call(tasks)] }
assert.deepEqual(taskPlan(message).map(x => x.status), ['completed', 'running', 'pending'])
message.toolCalls.push(call(tasks.map(x => ({ ...x, status: 'completed' })), 'error', false))
assert.equal(taskPlan(message).length, 3, 'a failed update must not complete the checklist')
message.toolCalls.push(call(tasks.map(x => ({ ...x, status: 'completed' }))))
assert.deepEqual(taskPlan(message), [], 'all completed tasks hide the plan')
message.toolCalls = [call(tasks)]
message.streaming = false
assert.deepEqual(taskPlan(message), [], 'finished turn hides unfinished checklist without falsely ticking tasks')
message.planPaused = true
assert.equal(taskPlan(message).length, 3, 'approval pause retains pending tasks')
message.planFinished = true
assert.deepEqual(taskPlan(message), [])
assert.deepEqual(taskPlan({ streaming: true, toolCalls: [{ name: 'task', args: { description: '任务' }, status: 'done' }] }), [], 'delegation text is not invented into a plan')
const fields = argumentFields({ file_path: '/workspace/demo.txt', replace_all: false, settings: { timeout: 30 } })
assert.equal(fields[0].label, '文件')
assert.equal(argumentText(fields[1].value), '否')
assert.equal(fields[2].nested, true)
assert.equal(toolName('read_file'), '读取文件')
assert.equal(toolName('custom_tool'), 'custom_tool')
console.log('Tool presentation: planning transitions, failure handling and argument fields passed')
