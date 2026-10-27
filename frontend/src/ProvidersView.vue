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
    表单编辑期间也不通过 Esc 关闭，避免误触丢失输入；列表页可用 Esc 返回对话。
  -->
  <DialogOverlay class="pv-overlay" :dismissible="!form.open && !busy" @close="$emit('close')">
    <div class="pv" role="dialog" aria-modal="true" aria-labelledby="providers-title">
      <header class="pv-head">
        <span class="pv-head-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9" /></svg></span>
        <div class="pv-heading">
          <h2 id="providers-title">模型供应商</h2>
          <p class="pv-sub">
            连接你常用的模型服务，管理模型与默认设置。
          </p>
          <p class="pv-caption">支持 OpenAI 兼容与 Anthropic 协议 · 密钥加密保存，仅显示掩码</p>
        </div>
        <button class="pv-x" title="关闭" aria-label="关闭模型供应商" @click="$emit('close')"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg></button>
      </header>

      <div v-if="error" class="pv-banner pv-banner-error">{{ error }}</div>

      <!-- ===== 表单（新增 / 编辑）===== -->
      <Transition name="settings-panel" mode="out-in">
      <section v-if="form.open" key="form" class="pv-form">
        <div class="pv-content">
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

        </div>
        <div class="pv-actions pv-form-actions">
          <p v-if="form.message" class="pv-result pv-test-result" :class="form.ok ? 'ok' : 'bad'" role="status" aria-live="polite">
            <span class="pv-test-message">{{ form.message }}</span>
            <span v-if="form.reply" class="pv-reply">模型回复：{{ form.reply }}</span>
          </p>
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
      <section v-else key="list" class="pv-list">
        <div class="pv-content">
        <div v-if="!loading && providers.length" class="pv-section-label">已接入的模型 <span>{{ providers.length }}</span></div>
        <div v-if="loading" class="pv-empty">载入中…</div>

        <div v-else-if="!providers.length" class="pv-empty">
          <p>还没有任何可用的模型供应商。</p>
          <p class="pv-empty-tip">
            点下面「新增供应商」填上 base_url / api_key / 模型名即可；
            或者让管理员配置一个系统级供应商（所有用户共用）。
          </p>
        </div>

        <div v-for="p in providers" :key="p.id" class="pv-item" :class="{ disabled: !p.enabled, 'is-default': p.is_default }">
          <span class="pv-avatar" aria-hidden="true">{{ (p.name || p.model || 'M').slice(0, 1).toUpperCase() }}</span>
          <div class="pv-item-main">
            <div class="pv-item-title">
              <span class="pv-name">{{ p.name || p.model }}</span>
              <span class="pv-tag" :class="p.is_system ? 'sys' : 'own'">
                {{ p.is_system ? '系统级' : '我的' }}
              </span>
              <span class="pv-tag proto">{{ p.protocol === 'openai' ? 'OpenAI' : p.protocol === 'anthropic' ? 'Anthropic' : p.protocol }}</span>
              <span v-if="p.is_default" class="pv-tag def">默认</span>
              <span v-if="!p.enabled" class="pv-tag off">已停用</span>
              <span v-if="!p.has_api_key" class="pv-tag warn">未填密钥</span>
            </div>
            <div class="pv-item-meta">
              <code>{{ p.model }}</code>
              <div class="pv-detail">
                <span class="pv-url" :title="p.base_url">{{ p.base_url || '官方默认地址' }}</span>
                <span class="pv-key">{{ p.api_key_hint || '（无密钥）' }}</span>
              </div>
            </div>
          </div>

          <div class="pv-item-actions">
            <button class="pv-mini" :disabled="busy" @click="testSaved(p)">测试</button>
            <button
              v-if="!p.is_default"
              class="pv-mini"
              :disabled="busy || p.is_default"
              :title="p.is_system && !isAdmin ? '系统级供应商的默认设置是全局的，仅管理员可改' : ''"
              @click="makeDefault(p)"
            >设为默认</button>
            <button class="pv-mini" :disabled="busy" @click="edit(p)">编辑</button>
            <button class="pv-mini danger" :disabled="busy" @click="remove(p)">删除</button>
          </div>
        </div>

        </div>
        <div class="pv-actions">
          <button class="pv-btn pv-btn-primary" :disabled="busy" @click="addNew"><span class="pv-plus" aria-hidden="true">+</span>新增供应商</button>
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
  components: { DialogOverlay },
  emits: ['close', 'changed'],
  props: {
    /** 当前用户是否管理员（决定"设为默认"能不能用在系统级供应商上） */
    isAdmin: { type: Boolean, default: false },
  },
  setup(props, { emit }) {
    const confirm = useConfirmation()
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
      if (!await confirm({ title: '删除模型供应商', description: '供应商与已保存的密钥将被删除，绑定它的会话会自动回退到默认供应商。', details: `${p.name || p.model}\n${p.model}\n${p.base_url}`, confirmLabel: '删除供应商', danger: true })) {
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

<style src="./styles/settings.css"></style>
