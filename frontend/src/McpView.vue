<template>
  <!--
    MCP 工具源管理。

    做成**全屏遮罩弹层**，与「模型」弹层同一套形态（没有 vue-router，
    为一个设置页引路由库不划算；模态还顺带保证"关掉即回到原对话"）。

    遮罩**刻意不绑定**点击关闭、也不绑 Esc：这里往往是刚填了一半的表单
    （URL / 命令 / 鉴权头），鼠标点到旁边就整窗消失且没有任何提示，
    是最招骂的一类交互。只能从「取消」「关闭」或右上角 ✕ 主动关。
  -->
  <div class="pv-overlay">
    <div class="pv" role="dialog" aria-label="MCP 工具源">
      <header class="pv-head">
        <div>
          <h2>MCP 工具源</h2>
          <p class="pv-sub">
            接入外部 MCP 服务，把它提供的工具直接挂给智能体（比如网页抓取、
            数据库查询、内部系统接口）。配置**按用户隔离**：别人配的你看不见，你也看不见别人的。
          </p>
        </div>
        <button class="pv-x" title="关闭" @click="$emit('close')">✕</button>
      </header>

      <!-- 状态提示：这两条都会让"配好了却没生效"，必须说在前面 -->
      <div v-if="status && !status.enabled" class="pv-banner pv-banner-warn">
        MCP 总开关当前是<strong>关闭</strong>的：下面的配置会保存，但不会加载。
        需要在后端 <code>backend/.env</code> 里设 <code>MCP_ENABLED=true</code> 并重启后端。
      </div>
      <div v-else-if="status && !status.adapters_installed" class="pv-banner pv-banner-warn">
        MCP 依赖未安装：请先
        <code>pip install "mcp&gt;=1.24,&lt;2.0" langchain-mcp-adapters</code>
        再重启后端，否则工具加载不到。
      </div>

      <div v-if="error" class="pv-banner pv-banner-error">{{ error }}</div>

      <!-- ===== 表单（新增 / 编辑）===== -->
      <section v-if="form.open" class="pv-form">
        <h3>{{ form.id ? '编辑工具源' : '新增工具源' }}</h3>

        <div class="pv-grid">
          <label class="pv-field">
            <span>名称<em>（同一账号下不能重复）</em></span>
            <input v-model="form.name" placeholder="如：fetch" :disabled="busy" />
          </label>

          <label class="pv-field">
            <span>连接方式</span>
            <select v-model="form.transport" :disabled="busy">
              <option value="streamable_http">Streamable HTTP（远程服务，推荐）</option>
              <option value="sse">SSE（远程服务，旧式）</option>
              <option value="stdio">Stdio（本机子进程）</option>
              <option value="websocket">WebSocket</option>
            </select>
          </label>

          <label v-if="needsUrl" class="pv-field pv-span2">
            <span>端点地址<em>（{{ form.transport }} 必填）</em></span>
            <input
              v-model="form.url"
              :placeholder="form.transport === 'websocket' ? 'ws://127.0.0.1:8765/ws' : 'https://.../mcp'"
              :disabled="busy"
            />
          </label>

          <template v-if="needsCommand">
            <label class="pv-field pv-span2">
              <span>启动命令<em>（stdio 必填）</em></span>
              <input v-model="form.command" placeholder="如 npx / python / node" :disabled="busy" />
            </label>
            <label class="pv-field pv-span2">
              <span>命令参数<em>（每行一个，可空）</em></span>
              <textarea
                v-model="form.argsText"
                rows="3"
                placeholder="-y&#10;@modelcontextprotocol/server-fetch"
                :disabled="busy"
              ></textarea>
            </label>
            <label class="pv-field">
              <span>工作目录<em>（可空）</em></span>
              <input v-model="form.cwd" placeholder="留空 = 后端进程目录" :disabled="busy" />
            </label>
          </template>

          <!-- HTTP 头：含密钥，所以编辑时默认"保留已保存的值"，值永不回显 -->
          <div v-if="supportsHeaders" class="pv-field pv-span2">
            <span>
              HTTP 头
              <em v-if="form.id">（值不会回显；默认保留已保存的，要改才重新填）</em>
              <em v-else>（可空，常用于 Authorization）</em>
            </span>

            <div v-if="form.id" class="pv-mode">
              <label><input type="radio" value="keep" v-model="form.headersMode" /> 保留已保存的</label>
              <label><input type="radio" value="replace" v-model="form.headersMode" /> 重新填写（覆盖）</label>
            </div>

            <div v-if="!form.id || form.headersMode === 'replace'" class="kv">
              <div v-for="(row, i) in form.headerRows" :key="'h' + i" class="kv-row">
                <input v-model="row.key" placeholder="Authorization" :disabled="busy" />
                <input v-model="row.value" placeholder="Bearer xxxx" type="password" autocomplete="off" :disabled="busy" />
                <button class="kv-del" :disabled="busy" @click="form.headerRows.splice(i, 1)">✕</button>
              </div>
              <button class="kv-add" :disabled="busy" @click="form.headerRows.push({ key: '', value: '' })">
                + 添加一项
              </button>
              <p class="kv-hint">空着不填 = 清除全部已保存的 HTTP 头</p>
            </div>
          </div>

          <div v-if="needsCommand" class="pv-field pv-span2">
            <span>
              环境变量
              <em v-if="form.id">（值不会回显；默认保留已保存的）</em>
              <em v-else>（可空，传给子进程）</em>
            </span>

            <div v-if="form.id" class="pv-mode">
              <label><input type="radio" value="keep" v-model="form.envMode" /> 保留已保存的</label>
              <label><input type="radio" value="replace" v-model="form.envMode" /> 重新填写（覆盖）</label>
            </div>

            <div v-if="!form.id || form.envMode === 'replace'" class="kv">
              <div v-for="(row, i) in form.envRows" :key="'e' + i" class="kv-row">
                <input v-model="row.key" placeholder="API_KEY" :disabled="busy" />
                <input v-model="row.value" placeholder="值" type="password" autocomplete="off" :disabled="busy" />
                <button class="kv-del" :disabled="busy" @click="form.envRows.splice(i, 1)">✕</button>
              </div>
              <button class="kv-add" :disabled="busy" @click="form.envRows.push({ key: '', value: '' })">
                + 添加一项
              </button>
            </div>
          </div>

          <label class="pv-field pv-check">
            <input type="checkbox" v-model="form.enabled" :disabled="busy" />
            <span>启用<em>（停用的不会加载）</em></span>
          </label>
        </div>

        <p v-if="form.message" class="pv-result" :class="form.ok ? 'ok' : 'bad'">
          {{ form.message }}
          <!-- 拿到的工具名是"真的能用"的证据：只说"连通"区分不出 0 个和 20 个 -->
          <span v-if="form.tools.length" class="kv-tools">
            <code v-for="t in form.tools" :key="t">{{ t }}</code>
          </span>
        </p>

        <div class="pv-actions">
          <button class="pv-btn" :disabled="busy || !canSubmit" @click="runTest">
            {{ form.testing ? '测试中…' : '测试连接' }}
          </button>
          <button class="pv-btn pv-btn-primary" :disabled="busy || !canSubmit" @click="save">
            {{ busy ? '保存中…' : '保存' }}
          </button>
          <button class="pv-btn" :disabled="busy" @click="closeForm">取消</button>
          <span class="pv-hint">
            建议先「测试连接」——它会真的连一次并把拿到的工具名列出来
          </span>
        </div>
      </section>

      <!-- ===== 列表 ===== -->
      <section v-else class="pv-list">
        <div v-if="loading" class="pv-empty">载入中…</div>

        <div v-else-if="!servers.length" class="pv-empty">
          <p>还没有任何 MCP 工具源。</p>
          <p class="pv-empty-tip">
            点「新增工具源」，填一个远程端点地址（或本机启动命令）即可。
            加完之后智能体下一次对话就能用到它提供的工具。
          </p>
        </div>

        <div v-for="s in servers" :key="s.id" class="pv-item" :class="{ disabled: !s.enabled }">
          <div class="pv-item-main">
            <div class="pv-item-title">
              <span class="pv-name">{{ s.name }}</span>
              <span class="pv-tag proto">{{ s.transport }}</span>
              <span v-if="!s.enabled" class="pv-tag off">已停用</span>
              <span v-if="s.has_headers" class="pv-tag">带鉴权头</span>
              <span v-if="s.has_env" class="pv-tag">带环境变量</span>
            </div>
            <div class="pv-item-meta">
              <span class="pv-url">{{ s.transport === 'stdio' ? s.command : s.url }}</span>
              <template v-if="s.transport === 'stdio' && s.args.length">
                <span class="pv-sep">·</span>
                <span class="pv-url">{{ s.args.join(' ') }}</span>
              </template>
              <template v-if="s.header_keys.length">
                <span class="pv-sep">·</span>
                <span>头：{{ s.header_keys.join('、') }}<em>（值不回显）</em></span>
              </template>
            </div>
          </div>

          <div class="pv-item-actions">
            <button class="pv-mini" :disabled="busy" @click="testSaved(s)">测试</button>
            <button class="pv-mini" :disabled="busy" @click="toggle(s)">
              {{ s.enabled ? '停用' : '启用' }}
            </button>
            <button class="pv-mini" :disabled="busy" @click="edit(s)">编辑</button>
            <button class="pv-mini danger" :disabled="busy" @click="remove(s)">删除</button>
          </div>
        </div>

        <div class="pv-actions">
          <button class="pv-btn pv-btn-primary" :disabled="busy" @click="addNew">新增工具源</button>
          <button class="pv-btn" :disabled="busy" title="丢掉已缓存的智能体，让刚改的配置立刻生效" @click="reload">
            重载
          </button>
          <button class="pv-btn" :disabled="busy" @click="$emit('close')">关闭</button>
          <span v-if="listMessage" class="pv-hint" :class="{ bad: !listOk }">{{ listMessage }}</span>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  createMcpServer,
  deleteMcpServer,
  fetchMcpServers,
  fetchMcpStatus,
  reloadMcp,
  testMcpServer,
  testMcpServerConfig,
  updateMcpServer,
} from './api'

const EMPTY_FORM = {
  open: false,
  id: '',
  name: '',
  transport: 'streamable_http',
  url: '',
  command: '',
  argsText: '',
  cwd: '',
  headersMode: 'keep',
  envMode: 'keep',
  headerRows: [],
  envRows: [],
  enabled: true,
  testing: false,
  message: '',
  tools: [],
  ok: false,
}

/** 把「键-值行」转成对象。键为空的行直接丢掉（那多半是用户多点了一下"添加"）。 */
function rowsToDict(rows) {
  const out = {}
  for (const r of rows || []) {
    const k = String(r.key || '').trim()
    if (!k) continue
    out[k] = String(r.value ?? '')
  }
  return out
}

function dictToRows(keys) {
  return (keys || []).map((k) => ({ key: k, value: '' }))
}

export default {
  name: 'McpView',
  emits: ['close'],
  setup() {
    const servers = ref([])
    const status = ref(null)
    const loading = ref(true)
    const busy = ref(false)
    const error = ref('')
    const listMessage = ref('')
    const listOk = ref(true)
    const form = reactive({ ...EMPTY_FORM })

    /** streamable_http / sse / websocket 靠 URL 连远程；stdio 靠本机命令。 */
    const needsUrl = computed(() => form.transport !== 'stdio')
    const needsCommand = computed(() => form.transport === 'stdio')
    /** 只有 http 类的连接认 headers（websocket 的 Connection 没有这个键）。 */
    const supportsHeaders = computed(() => form.transport === 'streamable_http' || form.transport === 'sse')

    /**
     * 保存按钮能不能点。
     *
     * 与「模型」弹层同一个道理：与其让用户撞一次 400/422，
     * 不如一开始就点不动 —— 而按钮变灰本身就是"这里还没填完"的提示。
     */
    const canSubmit = computed(() => {
      if (!form.name.trim()) return false
      if (needsUrl.value && !form.url.trim()) return false
      if (needsCommand.value && !form.command.trim()) return false
      return true
    })

    /** 组装请求体。headers/env 在"保留"模式下传 null（= 后端不改）。 */
    function buildPayload() {
      const p = {
        name: form.name.trim(),
        transport: form.transport,
        enabled: form.enabled,
      }
      if (needsUrl.value) p.url = form.url.trim()
      if (needsCommand.value) {
        p.command = form.command.trim()
        p.args = String(form.argsText || '')
          .split('\n')
          .map((x) => x.trim())
          .filter(Boolean)
        p.cwd = form.cwd.trim()
        // 编辑且选了"保留" → null（不动）；否则整体提交（清空就是 {}）
        p.env = form.id && form.envMode === 'keep' ? null : rowsToDict(form.envRows)
      }
      if (supportsHeaders.value) {
        p.headers = form.id && form.headersMode === 'keep' ? null : rowsToDict(form.headerRows)
      }
      return p
    }

    async function load() {
      loading.value = true
      error.value = ''
      try {
        // 列表与状态一起拉：只拉列表的话，"总开关没开"这件事界面上无从体现
        const [rows, st] = await Promise.all([fetchMcpServers(), fetchMcpStatus()])
        servers.value = rows
        status.value = st
      } catch (e) {
        error.value = `读取 MCP 工具源失败：${e.message}`
      } finally {
        loading.value = false
      }
    }

    function addNew() {
      Object.assign(form, { ...EMPTY_FORM, open: true })
    }

    function edit(s) {
      Object.assign(form, {
        ...EMPTY_FORM,
        open: true,
        id: s.id,
        name: s.name,
        transport: s.transport,
        url: s.url,
        command: s.command,
        // 参数数组按行展示回来（与表单"每行一个"的输入方式对应）
        argsText: (s.args || []).join('\n'),
        cwd: s.cwd,
        // 值**永远拿不到**（后端只回键名），所以只能给键名 + 空的输入框
        headerRows: dictToRows(s.header_keys),
        envRows: [],
        headersMode: 'keep',
        envMode: 'keep',
        enabled: s.enabled,
      })
    }

    function closeForm() {
      Object.assign(form, { ...EMPTY_FORM })
    }

    async function runTest() {
      form.testing = true
      form.message = ''
      form.tools = []
      try {
        const payload = buildPayload()
        // 编辑态且 headers/env 选了"保留"时，明文不在浏览器手里 ——
        // 只能测后端已保存的那条，否则会用空值去测，得到的失败是假的。
        const keepSecrets =
          form.id &&
          ((supportsHeaders.value && form.headersMode === 'keep') ||
            (needsCommand.value && form.envMode === 'keep'))
        const r = keepSecrets ? await testMcpServer(form.id) : await testMcpServerConfig(payload)
        form.ok = r.ok
        form.message = r.message
        form.tools = r.tools || []
        if (!r.ok && r.detail) form.message += ` —— ${r.detail}`
      } catch (e) {
        form.ok = false
        form.message = `测试失败：${e.message}`
      } finally {
        form.testing = false
      }
    }

    async function save() {
      busy.value = true
      form.message = ''
      try {
        const payload = buildPayload()
        if (form.id) {
          await updateMcpServer(form.id, payload)
        } else {
          await createMcpServer(payload)
        }
        closeForm()
        await load()
      } catch (e) {
        form.ok = false
        form.message = `保存失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    async function testSaved(s) {
      busy.value = true
      listMessage.value = ''
      try {
        const r = await testMcpServer(s.id)
        listOk.value = r.ok
        listMessage.value = `${s.name}：${r.message}`
        if (r.tools && r.tools.length) listMessage.value += `（工具：${r.tools.join('、')}）`
        if (!r.ok && r.detail) listMessage.value += ` —— ${r.detail}`
      } catch (e) {
        listOk.value = false
        listMessage.value = `测试失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    async function toggle(s) {
      busy.value = true
      listMessage.value = ''
      try {
        await updateMcpServer(s.id, { enabled: !s.enabled })
        await load()
      } catch (e) {
        listOk.value = false
        listMessage.value = `切换失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    async function remove(s) {
      if (!window.confirm(
        `删除 MCP 工具源「${s.name}」？\n\n` +
        `${s.transport === 'stdio' ? '命令：' + s.command : '地址：' + s.url}\n\n` +
        `已保存的鉴权头/环境变量会一起删掉，需要重新填写。`
      )) {
        return
      }
      busy.value = true
      listMessage.value = ''
      try {
        await deleteMcpServer(s.id)
        await load()
      } catch (e) {
        listOk.value = false
        listMessage.value = `删除失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    async function reload() {
      busy.value = true
      listMessage.value = ''
      try {
        const r = await reloadMcp()
        listOk.value = true
        listMessage.value = r.hint || '已重载：下一次对话会用上新配置'
      } catch (e) {
        listOk.value = false
        listMessage.value = `重载失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    onMounted(load)

    return {
      servers, status, loading, busy, error, form,
      listMessage, listOk,
      needsUrl, needsCommand, supportsHeaders, canSubmit,
      addNew, edit, closeForm, runTest, save, testSaved, toggle, remove, reload,
    }
  },
}
</script>

<style scoped>
/* 与 ProvidersView 共用同一套视觉 token（pv-*），观感与「模型」弹层一致 */
.pv-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  z-index: 50;
}
.pv {
  width: 100%;
  max-width: 780px;
  max-height: 88vh;
  overflow: auto;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 22px 18px;
}
.pv-head { display: flex; align-items: flex-start; gap: 12px; }
.pv-head h2 { margin: 0; font-size: 16px; font-weight: 600; }
.pv-sub { margin: 6px 0 0; font-size: 12px; line-height: 1.6; color: var(--text-3); max-width: 620px; }
.pv-x {
  margin-left: auto;
  flex: 0 0 auto;
  width: 26px; height: 26px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-2);
  border-radius: 6px;
  cursor: pointer;
}
.pv-x:hover { background: var(--hover); }

.pv-banner { margin: 12px 0 0; padding: 8px 10px; border-radius: 7px; font-size: 12.5px; line-height: 1.6; }
.pv-banner-error { background: var(--fill); color: var(--danger); border: 1px solid var(--border); }
.pv-banner-warn { background: var(--fill); color: var(--warn); border: 1px solid var(--border); }
.pv-banner code { font-family: var(--mono); font-size: 11.5px; }

.pv-form h3 { margin: 16px 0 10px; font-size: 13.5px; font-weight: 600; }
.pv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.pv-span2 { grid-column: span 2; }
.pv-field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--text-3); }
.pv-field em { font-style: normal; opacity: 0.8; }
.pv-field input, .pv-field select, .pv-field textarea {
  padding: 8px 10px;
  font-size: 13px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 7px;
  outline: none;
  font-family: inherit;
}
.pv-field textarea { resize: vertical; line-height: 1.5; font-family: var(--mono); font-size: 12px; }
.pv-field input:focus, .pv-field select:focus, .pv-field textarea:focus { border-color: var(--border-2); }
.pv-check { flex-direction: row; align-items: center; gap: 7px; }
.pv-check input { width: auto; }

.pv-mode { display: flex; gap: 14px; margin-bottom: 6px; }
.pv-mode label { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-2); }
.pv-mode input { width: auto; }

.kv { display: flex; flex-direction: column; gap: 6px; }
.kv-row { display: flex; gap: 6px; }
.kv-row input { flex: 1; padding: 7px 9px; font-size: 12.5px; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 6px; outline: none; }
.kv-row input:first-child { flex: 0 0 38%; font-family: var(--mono); }
.kv-del { width: 28px; border: 1px solid var(--border); background: transparent; color: var(--text-3); border-radius: 6px; cursor: pointer; }
.kv-del:hover:not(:disabled) { color: var(--danger); }
.kv-add { align-self: flex-start; padding: 4px 10px; font-size: 11.5px; color: var(--text-2); background: transparent; border: 1px dashed var(--border-2); border-radius: 6px; cursor: pointer; }
.kv-add:hover:not(:disabled) { background: var(--hover); }
.kv-hint { margin: 2px 0 0; font-size: 11px; color: var(--text-3); }
.kv-tools { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.kv-tools code { font-family: var(--mono); font-size: 11px; padding: 1px 6px; border: 1px solid var(--border); border-radius: 5px; background: var(--fill); color: var(--text-2); }

.pv-result { margin: 12px 0 0; font-size: 12.5px; line-height: 1.6; }
.pv-result.ok { color: var(--ok); }
.pv-result.bad { color: var(--danger); }

.pv-actions { display: flex; align-items: center; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.pv-btn { padding: 7px 14px; font-size: 13px; color: var(--text); background: transparent; border: 1px solid var(--border); border-radius: 7px; cursor: pointer; }
.pv-btn:hover:not(:disabled) { background: var(--hover); }
.pv-btn:disabled { opacity: 0.45; cursor: default; }
.pv-btn-primary { background: var(--btn); color: var(--btn-text); border-color: var(--btn); }
.pv-hint { font-size: 11.5px; color: var(--text-3); }
.pv-hint.bad { color: var(--danger); }

.pv-list { margin-top: 14px; }
.pv-empty { padding: 22px 4px; font-size: 13px; color: var(--text-2); }
.pv-empty-tip { font-size: 12px; color: var(--text-3); line-height: 1.6; }

.pv-item { display: flex; align-items: center; gap: 12px; padding: 11px 12px; border: 1px solid var(--border); border-radius: 9px; margin-bottom: 8px; }
.pv-item.disabled { opacity: 0.6; }
.pv-item-main { min-width: 0; flex: 1; }
.pv-item-title { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pv-name { font-size: 13.5px; font-weight: 500; }
.pv-tag { font-size: 11px; padding: 1px 6px; border-radius: 5px; border: 1px solid var(--border); color: var(--text-2); background: var(--fill); }
.pv-tag.proto { font-family: var(--mono); }
.pv-tag.off { color: var(--text-3); }
.pv-item-meta { margin-top: 5px; font-size: 11.5px; color: var(--text-3); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pv-item-meta em { font-style: normal; opacity: 0.75; }
.pv-url { font-family: var(--mono); word-break: break-all; }
.pv-sep { opacity: 0.5; }
.pv-item-actions { display: flex; gap: 6px; flex: 0 0 auto; }
.pv-mini { padding: 4px 9px; font-size: 11.5px; color: var(--text-2); background: transparent; border: 1px solid var(--border); border-radius: 6px; cursor: pointer; }
.pv-mini:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.pv-mini:disabled { opacity: 0.4; cursor: default; }
.pv-mini.danger:hover:not(:disabled) { color: var(--danger); }

@media (max-width: 640px) {
  .pv-grid { grid-template-columns: 1fr; }
  .pv-span2 { grid-column: span 1; }
  .pv-item { flex-direction: column; align-items: stretch; }
}
</style>
