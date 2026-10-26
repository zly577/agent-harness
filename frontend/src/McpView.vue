<template>
  <!--
    MCP 工具源管理。

    做成**全屏遮罩弹层**，与「模型」弹层同一套形态（没有 vue-router，
    为一个设置页引路由库不划算；模态还顺带保证"关掉即回到原对话"）。

    遮罩**刻意不绑定**点击关闭，表单编辑时也不通过 Esc 关闭：这里往往是刚填了一半的表单
    （URL / 命令 / 鉴权头），鼠标点到旁边就整窗消失且没有任何提示，
    是最招骂的一类交互。列表页可用 Esc 关闭，表单通过「取消」「关闭」或右上角 ✕ 主动关。
  -->
  <DialogOverlay class="pv-overlay" :dismissible="!form.open && !busy" @close="$emit('close')">
    <div class="pv" role="dialog" aria-modal="true" aria-labelledby="mcp-title">
      <header class="pv-head">
        <span class="pv-head-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3" /></svg></span>
        <div class="pv-heading">
          <h2 id="mcp-title">MCP 工具源</h2>
          <p class="pv-sub">
            为智能体接入网页抓取、数据库和更多外部工具。
          </p>
          <p class="pv-caption">配置仅对当前账号可见 · 鉴权信息不会回显</p>
        </div>
        <button class="pv-x" title="关闭" aria-label="关闭 MCP 工具源" @click="$emit('close')"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg></button>
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
      <Transition name="settings-panel" mode="out-in">
      <section v-if="form.open" key="form" class="pv-form">
        <div class="pv-content">
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

        </div>
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
      <section v-else key="list" class="pv-list">
        <div class="pv-content">
        <div v-if="!loading && servers.length" class="pv-section-label">已连接的工具源 <span>{{ servers.length }}</span></div>
        <div v-if="loading" class="pv-empty">载入中…</div>

        <div v-else-if="!servers.length" class="pv-empty">
          <p>还没有任何 MCP 工具源。</p>
          <p class="pv-empty-tip">
            点「新增工具源」，填一个远程端点地址（或本机启动命令）即可。
            加完之后智能体下一次对话就能用到它提供的工具。
          </p>
        </div>

        <div v-for="s in servers" :key="s.id" class="pv-item" :class="{ disabled: !s.enabled }">
          <span class="pv-avatar" aria-hidden="true">{{ (s.name || 'M').slice(0, 1).toUpperCase() }}</span>
          <div class="pv-item-main">
            <div class="pv-item-title">
              <span class="pv-name">{{ s.name }}</span>
              <span class="pv-tag proto">{{ s.transport }}</span>
              <span v-if="!s.enabled" class="pv-tag off">已停用</span>
              <span v-if="s.has_headers" class="pv-tag">带鉴权头</span>
              <span v-if="s.has_env" class="pv-tag">带环境变量</span>
            </div>
            <div class="pv-item-meta">
              <span class="pv-url" :title="s.transport === 'stdio' ? s.command : s.url">{{ s.transport === 'stdio' ? s.command : s.url }}</span>
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

        </div>
        <div class="pv-actions">
          <button class="pv-btn pv-btn-primary" :disabled="busy" @click="addNew"><span class="pv-plus" aria-hidden="true">+</span>新增工具源</button>
          <button class="pv-btn" :disabled="busy" title="丢掉已缓存的智能体，让刚改的配置立刻生效" @click="reload">
            重载
          </button>
          <button class="pv-btn" :disabled="busy" @click="$emit('close')">关闭</button>
          <span v-if="listMessage" class="pv-hint" :class="{ bad: !listOk }">{{ listMessage }}</span>
        </div>
      </section>
      </Transition>
    </div>
  </DialogOverlay>
</template>

<script>
import { computed, onMounted, reactive, ref } from 'vue'
import DialogOverlay from './DialogOverlay.vue'
import { useConfirmation } from './composables/useConfirmation'
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
  components: { DialogOverlay },
  emits: ['close'],
  setup() {
    const confirm = useConfirmation()
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
      if (!await confirm({ title: '删除 MCP 工具源', description: '已保存的鉴权头和环境变量会一并删除，再次接入时需要重新填写。', details: `${s.name}\n${s.transport === 'stdio' ? s.command : s.url}`, confirmLabel: '删除工具源', danger: true })) {
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

<style src="./styles/settings.css"></style>
<style scoped>
.pv-mode { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-bottom: 6px; }
.pv-mode label { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-2); }
.pv-mode input { width: auto; }

.kv { display: flex; flex-direction: column; gap: 6px; }
.kv-row { display: flex; gap: 6px; }
.kv-row input { flex: 1; min-width: 0; padding: 9px 10px; font-size: 12.5px; color: var(--text); background: var(--bg); border: 1px solid var(--border-2); border-radius: 10px; outline: none; }
.kv-row input:first-child { flex: 0 0 38%; font-family: var(--mono); }
.kv-del { width: 30px; flex: 0 0 auto; border: 1px solid var(--border); background: transparent; color: var(--text-2); border-radius: 8px; cursor: pointer; }
.kv-del:hover:not(:disabled) { color: var(--danger); }
.kv-add { align-self: flex-start; padding: 4px 10px; font-size: 11.5px; color: var(--text-2); background: transparent; border: 1px dashed var(--border-2); border-radius: 6px; cursor: pointer; }
.kv-add:hover:not(:disabled) { background: var(--hover); }
.kv-hint { margin: 2px 0 0; font-size: 11px; color: var(--text-3); }
.kv-tools { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.kv-tools code { font-family: var(--mono); font-size: 11px; padding: 1px 6px; border: 1px solid var(--border); border-radius: 5px; background: var(--fill); color: var(--text-2); }

</style>
