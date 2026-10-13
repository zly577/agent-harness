<template>
  <!--
    模型供应商管理。

    做成**全屏遮罩弹层**而不是独立路由：这个应用没有 vue-router（只有"登录/主界面"
    两种顶层形态），为一个设置页引一个路由库不划算。模态还顺带保证了"关掉即回到原
    对话"，不会丢掉正在流式输出的内容。
  -->
  <!--
    遮罩**刻意不绑定**点击关闭。
    以前是 `@click.self="$emit('close')"`，于是鼠标点到旁边一点就整个窗口消失 ——
    而这里往往是刚填了一半的表单（base_url / 密钥都白打了），而且没有任何提示。
    现在只能通过「取消」「关闭」或右上角 ✕ 主动关闭，与本应用其它确认框的行为一致。
    （同理不加 Esc 关闭：避免误触同样的问题。）
  -->
  <div class="pv-overlay">
    <div class="pv" role="dialog" aria-label="模型供应商">
      <header class="pv-head">
        <div>
          <h2>模型供应商</h2>
          <p class="pv-sub">
            支持任意 OpenAI 兼容服务（DeepSeek / Kimi / 通义 / 智谱 / OpenRouter /
            硅基流动 / vLLM / Ollama / LM Studio）与 Anthropic 协议服务。
            密钥加密保存，界面只显示掩码。
          </p>
        </div>
        <button class="pv-x" title="关闭" @click="$emit('close')">✕</button>
      </header>

      <div v-if="error" class="pv-banner pv-banner-error">{{ error }}</div>

      <!-- ===== 表单（新增 / 编辑）===== -->
      <section v-if="form.open" class="pv-form">
        <h3>{{ form.id ? '编辑供应商' : '新增供应商' }}</h3>

        <div class="pv-grid">
          <label class="pv-field">
            <span>展示名<em>（可空）</em></span>
            <input v-model="form.name" placeholder="如：我的 DeepSeek" :disabled="busy" />
          </label>

          <label class="pv-field">
            <span>协议</span>
            <select v-model="form.protocol" :disabled="busy">
              <option value="openai">OpenAI 兼容（Chat Completions）</option>
              <option value="anthropic">Anthropic（Messages）</option>
            </select>
          </label>

          <label class="pv-field pv-span2">
            <span>base_url<em>（留空用官方默认地址）</em></span>
            <input
              v-model="form.base_url"
              :placeholder="form.protocol === 'anthropic'
                ? 'https://api.anthropic.com'
                : 'https://api.deepseek.com'"
              :disabled="busy"
            />
          </label>

          <label class="pv-field pv-span2">
            <span>
              模型名
              <em>（必须支持 function/tool calling —— 智能体依赖工具调用）</em>
            </span>
            <input v-model="form.model" placeholder="如 deepseek-flash / gpt-4o-mini / claude-sonnet-4-..." :disabled="busy" />
          </label>

          <label class="pv-field pv-span2">
            <span>
              API Key
              <em v-if="form.id">（留空 = 不改动已保存的密钥）</em>
            </span>
            <input
              v-model="form.api_key"
              type="password"
              :placeholder="form.id ? (form.api_key_hint || '已保存') : 'sk-...'"
              autocomplete="off"
              :disabled="busy"
            />
          </label>

          <label class="pv-field">
            <span>图片识别能力</span>
            <select v-model="form.vision" :disabled="busy">
              <option value="auto">自动（已知模型；未知模型默认不支持）</option>
              <option value="yes">支持图片（多模态模型）</option>
              <option value="no">不支持图片（纯文本模型）</option>
            </select>
          </label>
          <label class="pv-field">
            <span>temperature<em>（可空）</em></span>
            <input v-model="form.temperature" placeholder="0" :disabled="busy" />
          </label>
          <label class="pv-field">
            <span>单次超时（秒）<em>（可空）</em></span>
            <input v-model="form.timeout" placeholder="60" :disabled="busy" />
          </label>
          <label class="pv-field">
            <span>max_tokens<em>（Anthropic 必填，可空则用 4096）</em></span>
            <input v-model="form.max_tokens" placeholder="留空" :disabled="busy" />
          </label>
          <label class="pv-field pv-check">
            <input type="checkbox" v-model="form.is_default" :disabled="busy" />
            <span>设为我的默认</span>
          </label>
        </div>

        <p v-if="form.message" class="pv-result" :class="form.ok ? 'ok' : 'bad'">
          {{ form.message }}
          <span v-if="form.reply" class="pv-reply">模型回复：{{ form.reply }}</span>
        </p>

        <div class="pv-actions">
          <button class="pv-btn" :disabled="busy || !form.model.trim()" @click="runTest">
            {{ form.testing ? '测试中…' : '测试连接' }}
          </button>
          <!--
            「保存」也要按"模型名是否填了"禁用：后端对 model 有非空校验，
            空着点保存只会换回一个 422 —— 与其让用户撞一次错误，
            不如一开始就点不动（而且按钮变灰本身就是"这里还没填完"的提示）。
          -->
          <button
            class="pv-btn pv-btn-primary"
            :disabled="busy || !form.model.trim()"
            @click="save"
          >
            {{ busy ? '保存中…' : '保存' }}
          </button>
          <button class="pv-btn" :disabled="busy" @click="closeForm">取消</button>
          <span class="pv-hint">建议先「测试连接」再保存 —— 密钥或地址写错时，它会明确告诉你是哪一种</span>
        </div>
      </section>

      <!-- ===== 列表 ===== -->
      <section v-else class="pv-list">
        <div v-if="loading" class="pv-empty">载入中…</div>

        <div v-else-if="!providers.length" class="pv-empty">
          <p>还没有任何可用的模型供应商。</p>
          <p class="pv-empty-tip">
            点下面「新增供应商」填上 base_url / api_key / 模型名即可；
            或者让管理员配置一个系统级供应商（所有用户共用）。
          </p>
        </div>

        <div v-for="p in providers" :key="p.id" class="pv-item" :class="{ disabled: !p.enabled }">
          <div class="pv-item-main">
            <div class="pv-item-title">
              <span class="pv-name">{{ p.name || p.model }}</span>
              <span class="pv-tag" :class="p.is_system ? 'sys' : 'own'">
                {{ p.is_system ? '系统级' : '我的' }}
              </span>
              <span class="pv-tag proto">{{ p.protocol }}</span>
              <span v-if="p.is_default" class="pv-tag def">默认</span>
              <span v-if="!p.enabled" class="pv-tag off">已停用</span>
              <span v-if="!p.has_api_key" class="pv-tag warn">未填密钥</span>
            </div>
            <div class="pv-item-meta">
              <code>{{ p.model }}</code>
              <span class="pv-sep">·</span>
              <span class="pv-url">{{ p.base_url }}</span>
              <span class="pv-sep">·</span>
              <span class="pv-key">{{ p.api_key_hint || '（无密钥）' }}</span>
            </div>
          </div>

          <div class="pv-item-actions">
            <button class="pv-mini" :disabled="busy" @click="testSaved(p)">测试</button>
            <button
              class="pv-mini"
              :disabled="busy || p.is_default"
              :title="p.is_system && !isAdmin ? '系统级供应商的默认设置是全局的，仅管理员可改' : ''"
              @click="makeDefault(p)"
            >设为默认</button>
            <button class="pv-mini" :disabled="busy" @click="edit(p)">编辑</button>
            <button class="pv-mini danger" :disabled="busy" @click="remove(p)">删除</button>
          </div>
        </div>

        <div class="pv-actions">
          <button class="pv-btn pv-btn-primary" :disabled="busy" @click="addNew">新增供应商</button>
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
  createProvider,
  deleteProvider,
  fetchProviders,
  setDefaultProvider,
  testProvider,
  testProviderConfig,
  updateProvider,
} from './api'

const EMPTY_FORM = {
  open: false,
  id: '',
  name: '',
  protocol: 'openai',
  base_url: '',
  model: '',
  api_key: '',
  api_key_hint: '',
  vision: 'auto',
  temperature: '',
  timeout: '',
  max_tokens: '',
  is_default: false,
  testing: false,
  message: '',
  reply: '',
  ok: false,
}

export default {
  name: 'ProvidersView',
  emits: ['close', 'changed'],
  props: {
    /** 当前用户是否管理员（决定"设为默认"能不能用在系统级供应商上） */
    isAdmin: { type: Boolean, default: false },
  },
  setup(props, { emit }) {
    const providers = ref([])
    const loading = ref(true)
    const busy = ref(false)
    const error = ref('')
    const listMessage = ref('')
    const listOk = ref(true)
    const form = reactive({ ...EMPTY_FORM })

    /** 把表单里的字符串参数转成后端要的类型；空串 = 不传（用默认值）。 */
    function buildParameters() {
      const p = {}
      if (form.vision !== 'auto') p.supports_vision = form.vision === 'yes'
      for (const key of ['temperature', 'timeout', 'max_tokens']) {
        const raw = String(form[key] ?? '').trim()
        if (raw === '') continue
        const num = Number(raw)
        if (Number.isNaN(num)) throw new Error(`${key} 必须是数字`)
        p[key] = num
      }
      return p
    }

    function buildPayload() {
      return {
        name: form.name.trim(),
        protocol: form.protocol,
        base_url: form.base_url.trim() || null,
        model: form.model.trim(),
        parameters: buildParameters(),
      }
    }

    async function load() {
      loading.value = true
      error.value = ''
      try {
        providers.value = await fetchProviders()
      } catch (e) {
        error.value = `读取供应商列表失败：${e.message}`
      } finally {
        loading.value = false
      }
    }

    function addNew() {
      Object.assign(form, { ...EMPTY_FORM, open: true })
    }

    function edit(p) {
      Object.assign(form, {
        ...EMPTY_FORM,
        open: true,
        id: p.id,
        name: p.name,
        protocol: p.protocol,
        base_url: p.base_url,
        model: p.model,
        api_key: '',                 // 绝不回填明文：留空 = 不改
        api_key_hint: p.api_key_hint,
        vision: typeof p.parameters?.supports_vision === 'boolean' ? (p.parameters.supports_vision ? 'yes' : 'no') : 'auto',
        temperature: p.parameters?.temperature ?? '',
        timeout: p.parameters?.timeout ?? '',
        max_tokens: p.parameters?.max_tokens ?? '',
        is_default: p.is_default,
      })
    }

    function closeForm() {
      Object.assign(form, { ...EMPTY_FORM })
    }

    /** 测试"当前表单里的临时配置"（不落库）。 */
    async function runTest() {
      form.testing = true
      form.message = ''
      form.reply = ''
      try {
        const payload = buildPayload()
        // 编辑模式且用户没重新填密钥时，明文不在手里 —— 这时只能测已保存的那条，
        // 否则会用一个空密钥去测，得到的"API Key 无效"是假的。
        if (form.id && !form.api_key) {
          const r = await testProvider(form.id)
          form.ok = r.ok
          form.message = r.message
          form.reply = r.reply || ''
          return
        }
        const r = await testProviderConfig({ ...payload, api_key: form.api_key })
        form.ok = r.ok
        form.message = r.message
        form.reply = r.reply || ''
      } catch (e) {
        form.ok = false
        form.message = e.message
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
          // api_key 留空 = 不改动已保存的密钥（这是刻意的，见表单提示）
          payload.api_key = form.api_key
          await updateProvider(form.id, payload)
        } else {
          payload.api_key = form.api_key
          payload.is_default = form.is_default
          await createProvider(payload)
        }
        closeForm()
        await load()
        emit('changed')
      } catch (e) {
        form.ok = false
        form.message = `保存失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    async function testSaved(p) {
      busy.value = true
      listMessage.value = ''
      try {
        const r = await testProvider(p.id)
        listOk.value = r.ok
        listMessage.value = `${p.name || p.model}：${r.message}` + (r.reply ? `（模型回复：${r.reply}）` : '')
        if (!r.ok && r.detail) listMessage.value += ` —— ${r.detail}`
      } catch (e) {
        listOk.value = false
        listMessage.value = `测试失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    async function makeDefault(p) {
      busy.value = true
      listMessage.value = ''
      try {
        await setDefaultProvider(p.id)
        await load()
        emit('changed')
      } catch (e) {
        listOk.value = false
        listMessage.value = e.message
      } finally {
        busy.value = false
      }
    }

    async function remove(p) {
      // 删除是不可逆的（密钥也一起没了），所以确认框里写清楚是哪一条
      if (!window.confirm(`删除供应商「${p.name || p.model}」？\n\n模型：${p.model}\n地址：${p.base_url}\n\n绑定它的会话会自动回退到默认供应商。`)) {
        return
      }
      busy.value = true
      listMessage.value = ''
      try {
        await deleteProvider(p.id)
        await load()
        emit('changed')
      } catch (e) {
        listOk.value = false
        listMessage.value = `删除失败：${e.message}`
      } finally {
        busy.value = false
      }
    }

    onMounted(load)

    return {
      providers, loading, busy, error, form,
      listMessage, listOk,
      addNew, edit, closeForm, runTest, save, testSaved, makeDefault, remove,
      isAdmin: computed(() => props.isAdmin),
    }
  },
}
</script>

<style scoped>
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

.pv-banner { margin: 12px 0 0; padding: 8px 10px; border-radius: 7px; font-size: 12.5px; }
.pv-banner-error { background: var(--fill); color: var(--danger); border: 1px solid var(--border); }

.pv-form h3 { margin: 16px 0 10px; font-size: 13.5px; font-weight: 600; }
.pv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.pv-span2 { grid-column: span 2; }
.pv-field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--text-3); }
.pv-field em { font-style: normal; opacity: 0.8; }
.pv-field input, .pv-field select {
  padding: 8px 10px;
  font-size: 13px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 7px;
  outline: none;
}
.pv-field input:focus, .pv-field select:focus { border-color: var(--border-2); }
.pv-check { flex-direction: row; align-items: center; gap: 7px; }
.pv-check input { width: auto; }

.pv-result { margin: 12px 0 0; font-size: 12.5px; line-height: 1.6; }
.pv-result.ok { color: var(--ok); }
.pv-result.bad { color: var(--danger); }
.pv-reply { display: block; color: var(--text-3); }

.pv-actions { display: flex; align-items: center; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.pv-btn {
  padding: 7px 14px;
  font-size: 13px;
  color: var(--text);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 7px;
  cursor: pointer;
}
.pv-btn:hover:not(:disabled) { background: var(--hover); }
.pv-btn:disabled { opacity: 0.45; cursor: default; }
.pv-btn-primary { background: var(--btn); color: var(--btn-text); border-color: var(--btn); }
.pv-hint { font-size: 11.5px; color: var(--text-3); }
.pv-hint.bad { color: var(--danger); }

.pv-list { margin-top: 14px; }
.pv-empty { padding: 22px 4px; font-size: 13px; color: var(--text-2); }
.pv-empty-tip { font-size: 12px; color: var(--text-3); line-height: 1.6; }

.pv-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  margin-bottom: 8px;
}
.pv-item.disabled { opacity: 0.6; }
.pv-item-main { min-width: 0; flex: 1; }
.pv-item-title { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pv-name { font-size: 13.5px; font-weight: 500; }
.pv-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid var(--border);
  color: var(--text-2);
  background: var(--fill);
}
.pv-tag.sys { color: var(--warn); }
.pv-tag.own { color: var(--text-2); }
.pv-tag.def { color: var(--ok); }
.pv-tag.off { color: var(--text-3); }
.pv-tag.warn { color: var(--danger); }
.pv-item-meta {
  margin-top: 5px;
  font-size: 11.5px;
  color: var(--text-3);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.pv-item-meta code { font-family: var(--mono); color: var(--text-2); }
.pv-url { font-family: var(--mono); }
.pv-key { font-family: var(--mono); }
.pv-sep { opacity: 0.5; }
.pv-item-actions { display: flex; gap: 6px; flex: 0 0 auto; }
.pv-mini {
  padding: 4px 9px;
  font-size: 11.5px;
  color: var(--text-2);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.pv-mini:hover:not(:disabled) { background: var(--hover); color: var(--text); }
.pv-mini:disabled { opacity: 0.4; cursor: default; }
.pv-mini.danger:hover:not(:disabled) { color: var(--danger); }

@media (max-width: 640px) {
  .pv-grid { grid-template-columns: 1fr; }
  .pv-span2 { grid-column: span 1; }
  .pv-item { flex-direction: column; align-items: stretch; }
}
</style>
