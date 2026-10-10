<template>
  <!--
    登录 / 注册视图（P18）。

    为什么做成一个独立组件而不是塞进 App.vue：App.vue 已经 3400+ 行，
    而"未登录"和"已登录"两套界面之间**没有共享状态**（登录成功后整个主界面
    重新挂载），拆开能让 App.vue 的改动收敛到"一行 v-if + 一个回调"。
  -->
  <div class="auth-wrap">
    <div class="auth-card">
      <div class="auth-brand">
        <span class="auth-mark">AH</span>
        <div>
          <h1>Agent Harness</h1>
          <p>DeepAgents 通用智能体</p>
        </div>
      </div>

      <div class="auth-tabs">
        <button
          type="button"
          class="auth-tab"
          :class="{ active: mode === 'login' }"
          @click="switchMode('login')"
        >登录</button>
        <button
          type="button"
          class="auth-tab"
          :class="{ active: mode === 'register' }"
          @click="switchMode('register')"
        >注册</button>
      </div>

      <form class="auth-form" @submit.prevent="submit">
        <label class="auth-field">
          <span>邮箱</span>
          <input
            v-model="email"
            type="text"
            autocomplete="username"
            placeholder="admin@local"
            :disabled="busy"
            autofocus
          />
        </label>

        <label class="auth-field">
          <span>口令</span>
          <input
            v-model="password"
            type="password"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            :placeholder="mode === 'register' ? '至少 8 位' : '请输入口令'"
            :disabled="busy"
          />
        </label>

        <label v-if="mode === 'register'" class="auth-field">
          <span>昵称<em>（可空）</em></span>
          <input v-model="displayName" type="text" placeholder="怎么称呼你" :disabled="busy" />
        </label>

        <!-- 错误提示：中文、就地展示（不用 alert，不打断输入） -->
        <p v-if="error" class="auth-error">{{ error }}</p>
        <p v-else-if="hintText" class="auth-hint">{{ hintText }}</p>

        <button class="auth-submit" type="submit" :disabled="busy || !canSubmit">
          {{ busy ? '请稍候…' : (mode === 'login' ? '登录' : '注册并登录') }}
        </button>
      </form>

      <p class="auth-foot">
        <template v-if="mode === 'login'">
          还没有账号？<a href="#" @click.prevent="switchMode('register')">注册一个</a>
        </template>
        <template v-else>
          已有账号？<a href="#" @click.prevent="switchMode('login')">去登录</a>
        </template>
      </p>
    </div>
  </div>
</template>

<script>
import { computed, ref } from 'vue'
import { ApiError, login as apiLogin, register as apiRegister } from './api'

export default {
  name: 'AuthView',
  emits: ['authed'],
  props: {
    /**
     * 由主界面传入的说明文字（如"登录已失效，请重新登录"）。
     * 为什么不用 alert：那是"打断式"提示，用户点掉之后就没有任何上下文了；
     * 就地显示在表单上方，用户一眼能明白刚才发生了什么。
     */
    notice: { type: String, default: '' },
  },
  setup(props, { emit }) {
    const mode = ref('login')
    const email = ref('')
    const password = ref('')
    const displayName = ref('')
    const busy = ref(false)
    const error = ref('')
    const hint = ref('')

    // 本地提示优先；没有就显示主界面传进来的原因（例如被踢回登录页）
    const hintText = computed(() => hint.value || props.notice || '')

    const canSubmit = computed(() => email.value.trim().length > 0 && password.value.length > 0)

    function switchMode(next) {
      if (mode.value === next) return
      mode.value = next
      error.value = ''
      hint.value = ''
    }

    /**
     * 注册成功后**顺手登录**。
     *
     * 后端把注册与登录做成两个语义清晰的接口（注册只回用户信息），
     * 但用户不该为这个设计多付一次操作 —— 所以这里自动串起来；
     * 万一自动登录失败（比如注册后立刻被禁用），退化成"请手动登录"的提示。
     */
    async function submit() {
      if (busy.value || !canSubmit.value) return
      busy.value = true
      error.value = ''
      hint.value = ''

      try {
        if (mode.value === 'register') {
          await apiRegister(email.value.trim(), password.value, displayName.value.trim())
          hint.value = '注册成功，正在登录…'
        }
        const data = await apiLogin(email.value.trim(), password.value)
        emit('authed', { token: data.access_token, user: data.user })
      } catch (e) {
        if (e instanceof ApiError) {
          error.value = e.message
          // 注册被关闭时引导用户找管理员，而不是让他反复重试
          if (mode.value === 'register' && e.status === 403) {
            hint.value = '如果是你自己部署的，请在 .env 里把 ALLOW_REGISTRATION 设为 true'
          }
        } else {
          // 网络层异常（后端没起来 / 端口不对）—— 说清楚可能的原因，
          // 否则用户只会看到一句 "Failed to fetch"。
          error.value = `无法连接后端服务（${e.message}）。请确认后端已在 8000 端口启动。`
        }
      } finally {
        busy.value = false
      }
    }

    return { mode, email, password, displayName, busy, error, hint, hintText, canSubmit, switchMode, submit }
  },
}
</script>

<style scoped>
.auth-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--sidebar);
}
.auth-card {
  width: 100%;
  max-width: 380px;
  padding: 28px 26px 22px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 12px;
}
.auth-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;
}
.auth-mark {
  width: 38px;
  height: 38px;
  border-radius: 9px;
  background: var(--btn);
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.3px;
}
.auth-brand h1 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.auth-brand p {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--text-3);
}
.auth-tabs {
  display: flex;
  gap: 4px;
  padding: 3px;
  background: var(--fill);
  border-radius: 8px;
  margin-bottom: 18px;
}
.auth-tab {
  flex: 1;
  padding: 7px 0;
  border: 0;
  background: transparent;
  border-radius: 6px;
  font-size: 13px;
  color: var(--text-3);
  cursor: pointer;
}
.auth-tab.active {
  background: var(--bg);
  color: var(--text);
  font-weight: 500;
}
.auth-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.auth-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-3);
}
.auth-field em {
  font-style: normal;
  opacity: 0.7;
}
.auth-field input {
  padding: 9px 11px;
  font-size: 14px;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border, #ddd);
  border-radius: 7px;
  outline: none;
}
.auth-field input:focus {
  border-color: var(--btn);
}
.auth-error,
.auth-hint {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.5;
}
.auth-error {
  color: #c0392b;
}
.auth-hint {
  color: var(--text-3);
}
.auth-submit {
  margin-top: 4px;
  padding: 10px 0;
  font-size: 14px;
  font-weight: 500;
  color: #fff;
  background: var(--btn);
  border: 0;
  border-radius: 8px;
  cursor: pointer;
}
.auth-submit:disabled {
  opacity: 0.45;
  cursor: default;
}
.auth-foot {
  margin: 16px 0 0;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
}
.auth-foot a {
  color: var(--btn);
}
</style>
