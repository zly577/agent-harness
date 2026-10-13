import assert from 'node:assert/strict'
import { Marked } from 'marked'
import { boundedUrlExtension, boundedUrlTokenizer, resizeComposer } from '../src/chatPresentation.js'
const parser = new Marked({ extensions: [boundedUrlExtension], tokenizer: boundedUrlTokenizer })
const cases = [
  ['给我讲一下https://github.com/zly577/agent-harness地址的docker部署步骤，不需要你帮我部署', 'https://github.com/zly577/agent-harness'],
  ['打开 https://example.com/path?x=1&y=2。然后继续', 'https://example.com/path?x=1&y=2'],
  ['访问www.example.com/path，后面不是链接', 'https://www.example.com/path'],
  ['(https://example.com/test) 后续文字', 'https://example.com/test'],
]
for (const [input, url] of cases) {
  const html = parser.parse(input)
  const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map(match => match[1].replace(/&amp;/g, '&'))
  assert.deepEqual(hrefs, [url], input)
  assert(!/<a[^>]*>[^<]*部署步骤/.test(html))
}
assert(parser.parse('[项目说明](https://example.com/说明)').includes('href="https://example.com/%E8%AF%B4%E6%98%8E"'), 'explicit Unicode links remain valid')
assert(!parser.parse('`https://example.com/path中文`').includes('<a'), 'inline code must remain literal')
assert(!parser.parse('```dos\necho https://example.com/path中文\n```').includes('<a'), 'commands must remain literal')
const element = { value: '长消息'.repeat(1000), scrollHeight: 900, style: {} }
resizeComposer(element)
assert.equal(element.style.height, '200px')
element.value = ''
resizeComposer(element)
assert.equal(element.style.height, '32px', 'sending a long message collapses the empty input')
element.value = '短消息'; element.scrollHeight = 32
resizeComposer(element)
assert.equal(element.style.height, '32px')
console.log('Chat presentation: URL boundaries and input resizing passed')
