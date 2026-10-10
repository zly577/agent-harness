// verify_file_delete.mjs — 文件面板"删除"功能的浏览器端验证（不跑模型）
//
// 覆盖：
//   1) 每个条目都有「删除」；文件同时有「下载」（目录没有下载）
//   2) 点删除先弹确认框，且**把完整路径摆出来**（不是只问"确定吗"）
//   3) 点「取消」→ 弹窗关闭、文件还在（不误删）
//   4) 点「删除」→ 列表刷新、该行消失、磁盘上文件真的没了
//   5) 目录也能删（连同内容一起）
//   6) 越界路径（工作区根 / 本机挂载 / 别名写法）被后端拒绝
//
// 用法: node _verify/verify_file_delete.mjs <devtools端口> <页面地址> <输出目录> <backendBase>
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { authHeaders, getJson, getRaw, loginToken } from './_auth.mjs'
import { ensureBrowserLoggedIn } from './_auth.mjs'

const [, , portArg, url, outDir = '.', backend = 'http://127.0.0.1:8010'] = process.argv
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const FILE = '/workspace/_verify_del_file.md'
const DIR = '/workspace/_verify_del_dir'

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res) => ws.addEventListener('open', res))
let id = 0
const pending = new Map()
const events = []
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id) }
  else if (m.method) events.push(m)
})
const send = (method, params = {}) => new Promise((res) => {
  const i = ++id
  pending.set(i, res)
  ws.send(JSON.stringify({ id: i, method, params }))
})
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval error')
  return r.result.value
}

const problems = []
const ok = (c, label, detail = '') => {
  console.log(`  ${c ? '✅' : '❌'} ${label}${detail && !c ? ' — ' + detail : ''}`)
  if (!c) problems.push(label)
}

// P18：这些接口默认拒绝，先登录拿 token（凭证走 AH_EMAIL / AH_PASSWORD 环境变量）。
// 注意要用**会话归属者**的账号 —— 文件接口按用户隔离，拿别人的 token 会什么都看不到。
const token = await loginToken(backend)

/**
 * 前置数据：直接把两个测试条目写进**该用户的工作区目录**。
 *
 * P18 起工作区是 `<repo>/workspace/<user_id>/`，所以这里先问后端"我是谁"，
 * 再按那个 id 拼路径 —— 不要手写死路径（换用户/换部署就失效）。
 *
 * 以前这两个文件是**手工准备**的，而且脚本会把它们删掉（那正是它要验证的行为），
 * 所以每跑一次都要重新造一遍。现在脚本自己造，可以随便重跑。
 */
const me = await getJson(`${backend}/api/v1/auth/me`, token)
const wsRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'workspace', me.id,
)
fs.mkdirSync(path.join(wsRoot, '_verify_del_dir'), { recursive: true })
fs.writeFileSync(path.join(wsRoot, '_verify_del_file.md'), '# 删除验证\n', 'utf-8')
fs.writeFileSync(path.join(wsRoot, '_verify_del_dir', 'inner.txt'), 'x\n', 'utf-8')

const exists = async (p) => {
  const r = await getRaw(`${backend}/api/v1/files/download?path=${encodeURIComponent(p)}`, token)
  return r.status === 200
}
const inList = async (p) => {
  const data = await getJson(`${backend}/api/v1/files?path=%2Fworkspace`, token)
  return (data.entries || []).some((e) => e.path === p)
}

// 行快照：名字 → 有哪些按钮
const ROWS = `(() => {
  const txt = (el) => (el ? (el.textContent || '').trim() : '')
  const rows = Array.from(document.querySelectorAll('.file-item'))
  return rows.map((r) => ({
    name: txt(r.querySelector('.file-name')),
    hasDl: !!r.querySelector('.file-dl'),
    hasDel: !!r.querySelector('.file-del'),
  }))
})()`

await send('Runtime.enable')
await send('Page.enable')
await send('Page.navigate', { url })
// P18：打开页面先看到的是登录页；不登录的话后面每个选择器都拿到 null，
// 而报错往往出现在很后面（点按钮时才炸），很容易被误判成界面坏了。
await ensureBrowserLoggedIn(evaluate, sleep)
await sleep(3000)

console.log('============ 文件面板 · 删除功能 ============')

// 前置：两个测试条目都在
ok(await exists(FILE), `前置：测试文件存在 ${FILE}`)
ok(await inList(DIR + '/'), `前置：测试目录存在 ${DIR}/`)

console.log('\n[1] 打开文件面板，看每行按钮')
await evaluate(`document.querySelector('.tbtn[title="浏览工作区文件"]')?.click()`)
await sleep(1200)
const rows = await evaluate(ROWS)
const fileRow = rows.find((r) => r.name === '_verify_del_file.md')
const dirRow = rows.find((r) => r.name === '_verify_del_dir')
console.log(`  行数 ${rows.length}；文件行=${JSON.stringify(fileRow)}；目录行=${JSON.stringify(dirRow)}`)
ok(!!fileRow, '看到测试文件行')
ok(!!fileRow?.hasDl, '文件行有「下载」')
ok(!!fileRow?.hasDel, '文件行有「删除」（本次新增）')
ok(!!dirRow?.hasDel, '目录行也有「删除」')
ok(!!dirRow && !dirRow.hasDl, '目录行没有「下载」（目录不能下载）')

console.log('\n[2] 点删除 → 确认框')
await evaluate(`(() => {
  const row = Array.from(document.querySelectorAll('.file-item'))
    .find((r) => (r.querySelector('.file-name')?.textContent || '').trim() === '_verify_del_file.md')
  row.querySelector('.file-del').click()
})()`)
await sleep(500)
const dialog = await evaluate(`(() => {
  const m = document.querySelector('.confirm-modal')
  if (!m) return null
  return {
    title: (m.querySelector('h3')?.textContent || '').trim(),
    desc: (m.querySelector('p')?.textContent || '').trim(),
    path: (m.querySelector('.confirm-path')?.textContent || '').trim(),
    buttons: Array.from(m.querySelectorAll('.modal-foot button')).map((b) => b.textContent.trim()),
  }
})()`)
console.log(`  ${JSON.stringify(dialog)}`)
ok(!!dialog, '弹出了确认框')
ok(dialog?.path === FILE, `确认框摆出了完整路径（${dialog?.path}）`)
ok(/不可恢复/.test(dialog?.desc || ''), `说明了后果不可恢复（「${dialog?.desc}」）`)
ok((dialog?.buttons || []).join(',') === '取消,删除', `按钮是 取消/删除（${dialog?.buttons}）`)

console.log('\n[3] 先点「取消」：不能误删')
await evaluate(`(() => {
  const b = Array.from(document.querySelectorAll('.confirm-modal .modal-foot button'))
    .find((x) => x.textContent.trim() === '取消')
  b.click()
})()`)
await sleep(500)
ok(!(await evaluate(`!!document.querySelector('.confirm-modal')`)), '弹窗已关闭')
ok(await exists(FILE), '文件还在（取消没有误删）')

console.log('\n[4] 再点「删除」并确认')
await evaluate(`(() => {
  const row = Array.from(document.querySelectorAll('.file-item'))
    .find((r) => (r.querySelector('.file-name')?.textContent || '').trim() === '_verify_del_file.md')
  row.querySelector('.file-del').click()
})()`)
await sleep(400)
await evaluate(`(() => {
  const b = Array.from(document.querySelectorAll('.confirm-modal .modal-foot button'))
    .find((x) => x.textContent.trim() === '删除')
  b.click()
})()`)
await sleep(1500)
const afterRows = await evaluate(ROWS)
ok(!(await evaluate(`!!document.querySelector('.confirm-modal')`)), '弹窗关闭')
ok(!afterRows.some((r) => r.name === '_verify_del_file.md'), '列表里该行已消失（已刷新）')
ok(!(await exists(FILE)), '磁盘上文件真的没了')
const toast = await evaluate(`(document.querySelector('.toast')?.textContent || '').trim()`)
ok(/已删除/.test(toast), `给了提示：${toast}`)

console.log('\n[5] 删除目录（连同内容）')
await evaluate(`(() => {
  const row = Array.from(document.querySelectorAll('.file-item'))
    .find((r) => (r.querySelector('.file-name')?.textContent || '').trim() === '_verify_del_dir')
  row.querySelector('.file-del').click()
})()`)
await sleep(400)
const dirDialog = await evaluate(`(document.querySelector('.confirm-modal p')?.textContent || '').trim()`)
ok(/全部内容/.test(dirDialog), `目录的确认框强调了"连同内容"（「${dirDialog}」）`)
await evaluate(`(() => {
  const b = Array.from(document.querySelectorAll('.confirm-modal .modal-foot button'))
    .find((x) => x.textContent.trim() === '删除')
  b.click()
})()`)
await sleep(1500)
ok(!(await inList(DIR + '/')), '目录已从列表消失')
ok(!(await exists(DIR + '/inner.txt')), '目录里的内容也一起删掉了')

console.log('\n[6] 越界路径必须被拒绝（直接打接口）')
for (const [label, p, want] of [
  ['工作区根', '/workspace', 400],
  ['本机挂载', '/mnt/d/Deepseek_Harness/agent-harness/README.md', 400],
  ['别名写法（不带前缀）', '/Agent.md', 400],
  ['相对路径', 'Agent.md', 400],
]) {
  const r = await fetch(`${backend}/api/v1/files?path=${encodeURIComponent(p)}`, { method: 'DELETE', headers: authHeaders(token) })
  const j = await r.json().catch(() => ({}))
  ok(r.status === want, `拒绝删除「${label}」→ HTTP ${r.status} ${j.detail ? '· ' + j.detail : ''}`)
}

const shot = await send('Page.captureScreenshot', { format: 'png' })
fs.writeFileSync(`${outDir}/file_delete_shot.png`, Buffer.from(shot.data, 'base64'))

console.log('\n[7] 控制台')
const errs = events.filter((e) => e.method === 'Runtime.exceptionThrown')
ok(errs.length === 0, `无 JS 异常（${errs.length} 条）`)

console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 项未通过：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ 文件删除功能全部通过')
}
ws.close()
process.exit(problems.length ? 1 : 0)
