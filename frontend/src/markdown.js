// Markdown 渲染工具：marked 解析 + DOMPurify 消毒（防 XSS）
// + highlight.js 代码高亮 + KaTeX 公式（$$..$$ / $..$）
//
// 关键设计：公式在【消毒之后】才注入 DOM 字符串。
// 消毒永远看不到 KaTeX 的产物 ⇒ 不需要为 MathML 放宽 DOMPurify 策略，
// 也不会被它剥掉 <math> / style，安全性不受影响（注入内容全部由本地库生成）。
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import dos from 'highlight.js/lib/languages/dos'
import powershell from 'highlight.js/lib/languages/powershell'
import { boundedUrlExtension, boundedUrlTokenizer } from './chatPresentation'
import katex from 'katex'
import 'katex/dist/katex.min.css'

hljs.registerLanguage('dos', dos)
hljs.registerLanguage('powershell', powershell)

marked.use({ extensions: [boundedUrlExtension], tokenizer: boundedUrlTokenizer })

marked.setOptions({
  gfm: true, // 表格、删除线、任务列表等 GitHub 扩展
  breaks: true, // 换行即 <br>
})

// 链接统一新窗口打开，避免聊天页被导航走
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// ---------------------------------------------------------------------------
// 一、代码块渲染：语言标签 + 复制按钮 + 高亮
// ---------------------------------------------------------------------------
// 别名归一，让 ```html / ```js 这类常见写法都能命中高亮器
const LANG_ALIAS = {
  sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', py: 'python', py3: 'python', python3: 'python',
  yml: 'yaml', html: 'xml', htm: 'xml', vue: 'xml', svg: 'xml',
  'c++': 'cpp', 'c#': 'csharp', cs: 'csharp', rs: 'rust',
  golang: 'go', kt: 'kotlin', ps1: 'powershell', pwsh: 'powershell',
  cmd: 'dos', bat: 'dos', batch: 'dos', 'objective-c': 'objectivec',
  md: 'markdown', text: '', txt: '', plain: '', plaintext: '',
}

function resolveLang(raw) {
  const first = String(raw || '').trim().split(/\s+/)[0].toLowerCase()
  if (!first) return ''
  if (first in LANG_ALIAS) return LANG_ALIAS[first]
  return hljs.getLanguage(first) ? first : ''
}

const codeRenderer = {
  code({ text, lang, terminal = false, title = null }) {
    const language = resolveLang(lang)
    const body = text.replace(/\n$/, '')
    let highlighted
    if (language) {
      try {
        highlighted = hljs.highlight(body, { language, ignoreIllegals: true }).value
      } catch {
        highlighted = escapeHtml(body)
      }
    } else {
      highlighted = escapeHtml(body)
    }
    terminal = terminal || ['dos', 'powershell', 'bash'].includes(language)
    const label = title || ({ dos: 'Windows CMD', powershell: 'PowerShell', bash: 'Shell' }[language]) || language || 'text'
    // 复制内容由 JS 从 <pre><code> 的 textContent 读取，不写进属性（避免转义/体积问题）
    return (
      `<div class="code-block${terminal ? ' terminal-block' : ''}">` +
        '<div class="code-head">' +
          (terminal ? '<span class="terminal-dots" aria-hidden="true"><i></i><i></i><i></i></span>' : '') +
          `<span class="code-lang">${escapeHtml(label)}</span>` +
          '<button class="code-copy" type="button" aria-label="复制代码">复制</button>' +
        '</div>' +
        `<pre><code class="hljs${language ? ` language-${language}` : ''}">${highlighted}</code></pre>` +
      '</div>'
    )
  },
}

marked.use({ renderer: codeRenderer })

export function renderTerminal(text, title = '终端输出', lang = '') {
  // Escape untrusted output; strip terminal control sequences instead of interpreting HTML.
  const clean = String(text || '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
  return DOMPurify.sanitize(codeRenderer.code({ text: clean, lang, terminal: true, title }))
}

// ---------------------------------------------------------------------------
// 二、公式：源码级占位 → 消毒后注入 KaTeX
// ---------------------------------------------------------------------------
const CODE_TOKEN = (i) => `\u0001C${i}\u0001`
const MATH_BLOCK = (i) => `\n\n\u0001MB${i}\u0001\n\n`
const MATH_INLINE = (i) => `\u0001MI${i}\u0001`

/** 把 fenced / inline code 先挖走，避免公式规则误伤代码里的 $ */
function maskCode(src, codeStore) {
  const stash = (m) => {
    codeStore.push(m)
    return CODE_TOKEN(codeStore.length - 1)
  }
  // fenced：行首的 ``` / ~~~ 到下一个同类围栏（未闭合则吃到文末，流式输出时常见）
  let out = src.replace(
    /(^|\n)([ \t]*(?:`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:\n[ \t]*(?:`{3,}|~{3,})[^\n]*|$))/g,
    (_m, lead, body) => lead + stash(body),
  )
  out = out.replace(/`[^`\n]*`/g, stash)
  return out
}

function unmaskCode(src, codeStore) {
  // 迭代两次，兼容「被挖走的代码里又出现占位符」的极端情况
  return src
    .replace(/\u0001C(\d+)\u0001/g, (_m, i) => codeStore[Number(i)] ?? '')
    .replace(/\u0001C(\d+)\u0001/g, (_m, i) => codeStore[Number(i)] ?? '')
}

/**
 * 从（已屏蔽代码的）文本中抽出公式，替换成占位符。
 *
 * 支持四种写法 —— 模型并不统一，只认 $$ 会让一半公式漏渲染：
 *   块级: $$...$$   \[...\]   \begin{align}...\end{align}（白名单环境）
 *   行内: $...$     \(...\)
 * 顺序很重要：块级先跑。已经掏空的公式变成了占位符 token，不会再被后面的规则吃一遍。
 */
const MATH_ENVS =
  'align|aligned|alignat|gather|gathered|equation|multline|cases|dcases|array|' +
  'matrix|pmatrix|bmatrix|vmatrix|Vmatrix|smallmatrix|split'

function extractMath(src, mathStore) {
  const stash = (tex, display) => {
    mathStore.push({ tex: tex.trim(), display })
    const i = mathStore.length - 1
    return display ? MATH_BLOCK(i) : MATH_INLINE(i)
  }
  let out = src.replace(/\$\$([\s\S]+?)\$\$/g, (_m, tex) => stash(tex, true))
  out = out.replace(/\\\[([\s\S]+?)\\\]/g, (_m, tex) => stash(tex, true))
  // 独立环境块：KaTeX 需要 displayMode，这里补上
  out = out.replace(
    new RegExp(`\\\\begin\\{(${MATH_ENVS})\\}([\\s\\S]*?)\\\\end\\{\\1\\}`, 'g'),
    (_m, env, body) => stash(`\\begin{${env}}${body}\\end{${env}}`, true),
  )
  // 行内：$ 后不能是空格、内容不含 $、结尾不能是空格 —— 这样 "$5 和 $10" 不会被误判
  out = out.replace(/\$(?!\$)([^\s$](?:[^$]*[^\s$])?)\$/g, (_m, tex) => stash(tex, false))
  out = out.replace(/\\\(([\s\S]+?)\\\)/g, (_m, tex) => stash(tex, false))
  return out
}

function renderMathHtml(entry) {
  try {
    return katex.renderToString(entry.tex, {
      displayMode: entry.display,
      throwOnError: false,
      strict: 'ignore',
      trust: false,
      maxSize: 100,
      maxExpand: 2000,
      output: 'htmlAndMathml',
    })
  } catch {
    // 兜底：还原成原文，至少让用户看到自己写了什么
    const raw = `${entry.display ? '$$' : '$'}${entry.tex}${entry.display ? '$$' : '$'}`
    return `<code class="math-fallback">${escapeHtml(raw)}</code>`
  }
}

function injectMath(html, mathStore) {
  const rendered = mathStore.map(renderMathHtml)
  return html
    // 独占一行的块级公式：去掉 marked 补的 <p>，交给 .katex-display 自己撑开
    .replace(/<p>\s*\u0001MB(\d+)\u0001\s*<\/p>/g, (_m, i) => `<div class="katex-block">${rendered[Number(i)]}</div>`)
    .replace(/\u0001MB(\d+)\u0001/g, (_m, i) => rendered[Number(i)])
    .replace(/\u0001MI(\d+)\u0001/g, (_m, i) => rendered[Number(i)])
}

// ---------------------------------------------------------------------------
// 三、对外入口（带记忆化：流式输出会反复以同一段文本调用）
// ---------------------------------------------------------------------------
const cache = new Map()
const CACHE_MAX = 240

export function renderMarkdown(text) {
  if (!text) return ''
  const hit = cache.get(text)
  if (hit !== undefined) return hit

  const codeStore = []
  const mathStore = []

  let src = maskCode(String(text), codeStore)
  src = extractMath(src, mathStore)
  src = unmaskCode(src, codeStore)

  const html = DOMPurify.sanitize(marked.parse(src))
  const out = injectMath(html, mathStore)

  if (cache.size >= CACHE_MAX) cache.clear()
  cache.set(text, out)
  return out
}

// ---------------------------------------------------------------------------
// 四、复制按钮（事件委托，只装一次；v-html 出来的节点也能响应）
// ---------------------------------------------------------------------------
let copyInstalled = false

export function installMarkdownInteractions() {
  if (copyInstalled || typeof document === 'undefined') return
  copyInstalled = true

  document.addEventListener(
    'click',
    async (e) => {
      const btn = e.target?.closest?.('.code-copy')
      if (!btn) return
      // 别把点击冒泡给外层的 <details> / 折叠区
      e.preventDefault()
      e.stopPropagation()

      const codeEl = btn.closest('.code-block')?.querySelector('pre code')
      if (!codeEl) return
      const content = codeEl.textContent ?? ''

      let ok = false
      try {
        await navigator.clipboard.writeText(content)
        ok = true
      } catch {
        try {
          const ta = document.createElement('textarea')
          ta.value = content
          ta.setAttribute('readonly', '')
          ta.style.position = 'fixed'
          ta.style.top = '-1000px'
          ta.style.opacity = '0'
          document.body.appendChild(ta)
          ta.select()
          ok = document.execCommand('copy')
          ta.remove()
        } catch {
          ok = false
        }
      }

      btn.textContent = ok ? '已复制' : '复制失败'
      btn.classList.toggle('done', ok)
      setTimeout(() => {
        btn.textContent = '复制'
        btn.classList.remove('done')
      }, 1500)
    },
    true,
  )
}

installMarkdownInteractions()
