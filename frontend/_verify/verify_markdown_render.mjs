// verify_markdown_render.mjs — Markdown 代码块 / 公式渲染回归（不需要跑模型、不需要登录）
//
// ## 为什么这样设计
//
// 1. 直接 `import('/src/markdown.js')`（Vite dev server 就是 ESM 服务）——
//    测的是**真实模块**，不是复制一份逻辑；改完 markdown.js 立刻能验。
// 2. 把渲染结果塞进一个真正带 `.md` 类的容器，用**真实样式表**（App.vue 的 <style> 是全局的）
//    ⇒ 覆盖"渲染对不对"和"样式吃没吃上"两件事。
// 3. 用 `getComputedStyle` 读回**实际生效的颜色/边框**，而不是只断言 DOM 结构 ——
//    样式写错选择器时 DOM 照样正确，只有计算样式能测出来。
// 4. 浅色 / 深色各截一张。本项目跟随系统配色，深色只在媒体查询里，
//    只截浅色等于没验深色。
//
// 不登录：登录页与主界面共用同一份全局样式，渲染层不依赖鉴权。
// 少一步登录 ⇒ 少一处会因凭证缺失而误报的失败点。
//
// 用法: node _verify/verify_markdown_render.mjs <devtools端口> <页面地址> <输出目录>
import fs from 'node:fs'

const [, , portArg, url, outDir = '.'] = process.argv
if (!portArg || !url) {
  console.error('用法: node _verify/verify_markdown_render.mjs <devtools端口> <页面地址> <输出目录>')
  process.exit(2)
}
const port = Number(portArg)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
if (!page) throw new Error(`端口 ${port} 上没有可用页面，先起一个带 --remote-debugging-port 的浏览器`)
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res) => ws.addEventListener('open', res))
let id = 0
const pending = new Map()
const events = []
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pending.has(m.id)) {
    const { res, rej } = pending.get(m.id)
    pending.delete(m.id)
    // ⚠️ 别把 CDP 的 error 当成功吞掉：那样 res 收到 undefined，
    // 报错会推迟到「读 xxx.data」时才炸，且完全看不出是哪个命令失败。
    if (m.error) rej(new Error(`${m.error.code} ${m.error.message}`))
    else res(m.result)
  } else if (m.method) events.push(m)
})
const send = (method, params = {}, timeoutMs = 15000) => new Promise((res, rej) => {
  const i = ++id
  const timer = setTimeout(() => {
    pending.delete(i)
    rej(new Error(`CDP 超时 ${timeoutMs}ms: ${method}`))
  }, timeoutMs)
  pending.set(i, {
    res: (v) => { clearTimeout(timer); res(v) },
    rej: (e) => { clearTimeout(timer); rej(e) },
  })
  ws.send(JSON.stringify({ id: i, method, params }))
})
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text || 'eval error')
  }
  return r.result.value
}
const shot = async (name) => {
  const r = await send('Page.captureScreenshot', { format: 'png' }, 20000)
  if (!r?.data) throw new Error(`截图失败（${name}）：CDP 未返回 data`)
  const buf = Buffer.from(r.data, 'base64')
  fs.writeFileSync(`${outDir}/${name}`, buf)
  return buf.length
}

const problems = []
const ok = (c, label, detail = '') => {
  console.log(`  ${c ? '✅' : '❌'} ${label}${detail && !c ? ' — ' + detail : ''}`)
  if (!c) problems.push(label)
}

await send('Runtime.enable')
await send('Page.enable')
await send('Emulation.setDeviceMetricsOverride', { width: 900, height: 1400, deviceScaleFactor: 1, mobile: false })
// 无头环境默认不给剪贴板权限 ⇒ navigator.clipboard 会 reject、execCommand 也会失败，
// 看起来像"复制功能坏了"（实际是环境限制）。这里显式授权，才能验到真实写入内容。
try {
  await send('Browser.grantPermissions', {
    origin: new URL(url).origin,
    permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'],
  })
} catch (e) {
  console.log(`  （剪贴板授权失败，跳过内容比对：${e.message}）`)
}

await send('Page.navigate', { url })
await sleep(4000)

// ---------------------------------------------------------------------------
// 灌入样例：覆盖 语言高亮 / 无语言 / 行内代码 / 代码里的 $ / 行内公式 / 块级公式 / 表格
// ---------------------------------------------------------------------------
const SAMPLE = [
  '# 渲染样例',
  '',
  '行内代码 `npm run dev` 与行内公式 $E = mc^2$，还有**加粗**和[链接](https://example.com)。',
  '',
  '反斜杠写法行内公式：\\(a^2 + b^2 = c^2\\)。价格 $100 和 $200 不该变成公式。',
  '',
  '块级公式：',
  '',
  '$$',
  '\\int_{0}^{\\infty} e^{-x^{2}}\\,dx = \\frac{\\sqrt{\\pi}}{2}',
  '$$',
  '',
  '另一种块级写法：',
  '',
  '\\[',
  '\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}',
  '\\]',
  '',
  '不带定界符的环境：',
  '',
  '\\begin{align}',
  'x &= a + b \\\\',
  'y &= c - d',
  '\\end{align}',
  '',
  '```python',
  '# 一段带注释的 Python',
  'def fib(n: int) -> list[int]:',
  '    """前 n 个斐波那契数"""',
  '    a, b, out = 0, 1, []',
  '    for _ in range(n):',
  '        out.append(a)',
  '        a, b = b, a + b',
  '    return out',
  '```',
  '',
  '```javascript',
  'const price = "$100";           // 代码里的 $ 不该变成公式',
  'export function render(x) { return `$${x}` }',
  '```',
  '',
  '```bash',
  'export PATH="/usr/bin:$PATH"',
  'curl -s --noproxy "*" http://127.0.0.1:8000/health',
  '```',
  '',
  '```json',
  '{ "model": "deepseek-chat", "temperature": 0.2, "max_tokens": 4096 }',
  '```',
  '',
  '```',
  '无语言标记的纯文本块',
  '   缩进保留',
  '```',
  '',
  '| 项目 | 值 |',
  '| --- | --- |',
  '| 模型 | DeepSeek |',
  '| 温度 | 0.2 |',
  '',
  '> 引用块，用来确认左边框还在。',
].join('\n')

const injected = await evaluate(`(async () => {
  try {
    const mod = await import('/src/markdown.js')
    if (typeof mod.renderMarkdown !== 'function') return { error: 'renderMarkdown 未导出' }
    const old = document.getElementById('__mdcheck')
    if (old) old.remove()
    const el = document.createElement('div')
    el.id = '__mdcheck'
    el.className = 'md'
    el.style.cssText = 'position:fixed;inset:0;z-index:99999;background:var(--bg);color:var(--text);' +
      'overflow:auto;padding:22px 26px;font-size:15px;line-height:1.6;'
    el.innerHTML = mod.renderMarkdown(${JSON.stringify(SAMPLE)})
    document.body.appendChild(el)
    return { ok: true, bytes: el.innerHTML.length }
  } catch (e) {
    return { error: String(e && e.stack || e) }
  }
})()`)

if (!injected?.ok) {
  console.error('\n❌ 注入失败：\n' + (injected?.error || '未知错误'))
  ws.close()
  process.exit(1)
}
await sleep(600)

// ---------------------------------------------------------------------------
// 断言：结构
// ---------------------------------------------------------------------------
const DOM = `(() => {
  const root = document.getElementById('__mdcheck')
  if (!root) return { missing: true }
  const q = (s) => root.querySelector(s)
  const qa = (s) => Array.from(root.querySelectorAll(s))
  return {
    codeBlocks: qa('.code-block').length,
    heads: qa('.code-head').length,
    langs: qa('.code-lang').map((e) => e.textContent.trim()),
    copies: qa('.code-copy').length,
    copyLabel: qa('.code-copy')[0]?.textContent.trim() || '',
    hljsCode: qa('pre code.hljs').length,
    langClass: qa('pre code[class*="language-"]').length,
    // 高亮是否真的产生了 token
    tokenCount: qa('[class^="hljs-"], [class*=" hljs-"]').length,
    keywords: qa('.hljs-keyword').length,
    strings: qa('.hljs-string').length,
    comments: qa('.hljs-comment').length,
    katexAll: qa('.katex').length,
    katexDisplay: qa('.katex-display').length,
    // 公式是否带着 KaTeX 字体（字体没加载会被 fallback 成衬线体）
    katexFam: (() => {
      const k = q('.katex')
      if (!k) return ''
      return getComputedStyle(k).fontFamily || ''
    })(),
    // 代码块里被误当公式的痕迹：转义串 $ 应原样留在代码里
    codeText: qa('.code-block pre code').map((e) => e.textContent).join('\\n'),
    inlineCodeBg: (() => {
      const c = qa('p code')[0]
      return c ? getComputedStyle(c).backgroundColor : ''
    })(),
    blockBg: (() => {
      const b = q('.code-block')
      return b ? getComputedStyle(b).backgroundColor : ''
    })(),
    headBorder: (() => {
      const h = q('.code-head')
      return h ? getComputedStyle(h).borderBottomWidth + ' ' + getComputedStyle(h).borderBottomColor : ''
    })(),
    copyBorder: (() => {
      const c = q('.code-copy')
      return c ? getComputedStyle(c).borderTopWidth : ''
    })(),
    katexDisplayOverflow: (() => {
      const d = q('.katex-display')
      return d ? getComputedStyle(d).overflowX : ''
    })(),
    // marked 补的 <p> 有没有被去掉（留着会多出一层上下间距）
    strayP: qa('p').filter((p) => p.querySelector('.katex-block, .katex-display')).length,
    // 美元金额那段，用来确认没被公式规则误伤
    priceText: qa('p').map((e) => e.textContent).find((t) => t.includes('价格')) || '',
  }
})()`

const light = await evaluate(DOM)

console.log('\n[1] 代码块')
ok(light.codeBlocks === 5, `5 个代码块都变成卡片（实际 ${light.codeBlocks}）`)
ok(light.heads === light.codeBlocks, `每块都有卡头（${light.heads}/${light.codeBlocks}）`)
ok(light.copies === light.codeBlocks, `每块都有复制按钮（${light.copies}/${light.codeBlocks}）`)
console.log(`  语言标签：${JSON.stringify(light.langs)}`)
ok(
  light.langs.slice(0, 4).join(',') === 'python,javascript,bash,json' && light.langs[4] === 'text',
  '语言标签按围栏标注归一（js→javascript 别名生效；无标注回落 text）',
  JSON.stringify(light.langs),
)
ok(light.hljsCode === 5, `所有代码块挂了 hljs class（实际 ${light.hljsCode}）`)
ok(light.langClass === 4, `只有 4 个有语言的块带 language-*（实际 ${light.langClass}）`)
ok(light.tokenCount > 20, `高亮真的产生 token（${light.tokenCount} 个）`)
ok(light.keywords > 0 && light.strings > 0 && light.comments > 0,
  `关键字/字符串/注释三类 token 都有（${light.keywords}/${light.strings}/${light.comments}）`)
ok(light.blockBg && light.blockBg !== 'rgba(0, 0, 0, 0)', `代码块有独立底色（${light.blockBg}）`)
ok(/^1px/.test(light.headBorder), `卡头有分隔线（${light.headBorder}）`)

console.log('\n[2] 行内代码')
ok(light.inlineCodeBg && light.inlineCodeBg !== 'rgba(0, 0, 0, 0)', `行内代码有底色（${light.inlineCodeBg}）`)

console.log('\n[3] 公式')
ok(light.katexAll === 5, `四种写法都渲染成 KaTeX（${light.katexAll} 个 .katex，期望 5）`)
ok(light.katexDisplay === 3, `三个块级公式走 display 模式（${light.katexDisplay}）`)
ok(/KaTeX/.test(light.katexFam), `KaTeX 字体已生效（${light.katexFam.split(',')[0]}）`)
ok(light.katexDisplayOverflow === 'auto', `块级公式可横向滚动，宽公式不会撑破布局（${light.katexDisplayOverflow}）`)
ok(light.strayP === 0, `块级公式外层没有残留 <p>（${light.strayP} 个）`)
ok(
  light.priceText.includes('$100') && light.priceText.includes('$200'),
  `美元金额没被误当公式（「${light.priceText.trim()}」）`,
)

console.log('\n[4] 代码与公式互不误伤')
ok(light.codeText.includes('"$100"') && light.codeText.includes('`$${x}`'),
  '代码里的 $ 保持原样（没被当公式吃掉）')
ok(!/katex/.test(light.codeText), '代码块内没有混入公式产物')

console.log('\n[5] 复制按钮可用')
await send('Page.bringToFront').catch(() => {})
// 点击处理是 async（要 await 剪贴板）⇒ 点完必须等一下再读文案，
// 否则读到的是点击前的旧值，看起来像"按钮没反应"。
const clicked = await evaluate(`(() => {
  const btn = document.querySelector('#__mdcheck .code-copy')
  if (!btn) return { error: '没有复制按钮' }
  btn.click()
  return { before: btn.textContent.trim() }
})()`)
ok(!clicked.error, '点击不抛异常', clicked.error || '')
await sleep(400)
const after = await evaluate(`(() => {
  const btn = document.querySelector('#__mdcheck .code-copy')
  return { text: btn?.textContent.trim() || '', done: !!btn?.classList.contains('done') }
})()`)
ok(after.text !== clicked.before || after.done, `点击后立刻有反馈（「${clicked.before}」→「${after.text}」）`)

// 真读回来比对，才算验过"复制的是这段代码"
const clip = await evaluate(`(async () => {
  try {
    const t = await navigator.clipboard.readText()
    return { len: t.length, head: t.slice(0, 40) }
  } catch (e) { return { error: String(e && e.name || e) } }
})()`)
// 第一个复制按钮对应样例里的 Python 块
const expectHead = 'def fib(n: int)'
if (clip.error) {
  console.log(`  （剪贴板读回不可用：${clip.error} —— 无头环境限制，跳过内容比对）`)
} else {
  ok(clip.head.includes(expectHead.slice(0, 20)), `剪贴板内容是该代码块（${JSON.stringify(clip.head)}）`)
}
await sleep(1600)
const restored = await evaluate(`document.querySelector('#__mdcheck .code-copy')?.textContent.trim() || ''`)
ok(restored === '复制', `1.5s 后文案复位（「${restored}」）`)

const lightBytes = await shot('md_render_light_shot.png')

// ---------------------------------------------------------------------------
// 深色：只验"变了且可读"，不复刻浅色那套断言
// ---------------------------------------------------------------------------
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] })
await sleep(400)
const dark = await evaluate(DOM)
ok(dark.blockBg !== light.blockBg, `深色下代码块底色跟着变（${light.blockBg} → ${dark.blockBg}）`)
ok(dark.inlineCodeBg !== light.inlineCodeBg, `深色下行内代码底色跟着变（${light.inlineCodeBg} → ${dark.inlineCodeBg}）`)
ok(dark.katexAll === light.katexAll, '深色下公式依然全部渲染')
const darkBytes = await shot('md_render_dark_shot.png')
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] })

// ---------------------------------------------------------------------------
// 控制台
// ---------------------------------------------------------------------------
const errors = events.filter((e) => e.method === 'Runtime.exceptionThrown')
console.log('\n[6] 控制台')
ok(errors.length === 0, `无 JS 异常（${errors.length} 条）`,
  errors.slice(0, 2).map((e) => e.params?.exceptionDetails?.text).join(' | '))

console.log(`\n  浅色截图 ${outDir}/md_render_light_shot.png（${(lightBytes / 1024) | 0}KB）`)
console.log(`  深色截图 ${outDir}/md_render_dark_shot.png（${(darkBytes / 1024) | 0}KB）`)

console.log('')
if (problems.length) {
  console.log(`❌ ${problems.length} 项未通过：`)
  for (const p of problems) console.log('  - ' + p)
} else {
  console.log('✅ Markdown 代码块 / 公式渲染全部通过')
}
ws.close()
process.exit(problems.length ? 1 : 0)
