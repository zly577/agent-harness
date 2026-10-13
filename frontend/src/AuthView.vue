<template>
  <div class="auth-wrap">
    <div class="auth-card">
      <div class="auth-brand"><span class="auth-mark">AH</span><div><h1>Agent Harness</h1><p>你的 AI 工作助手</p></div></div>
      <div class="auth-tabs">
        <button v-for="item in [{key: 'login', label: '登录'}, {key: 'register', label: '注册'}]" :key="item.key" type="button" class="auth-tab" :class="{ active: mode === item.key }" :disabled="busy || sending" @click="switchMode(item.key)">{{ item.label }}</button>
      </div>
      <form class="auth-form" @submit.prevent="submit">
        <label class="auth-field"><span>邮箱</span><input v-model="email" type="text" inputmode="email" autocomplete="username" placeholder="请输入邮箱地址" maxlength="255" required :disabled="busy || sending" autofocus /></label>
        <label class="auth-field"><span>密码</span><input v-model="password" type="password" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" :placeholder="mode === 'register' ? '请设置 8–128 位密码' : '请输入密码'" :minlength="mode === 'register' ? 8 : 1" maxlength="128" required :disabled="busy" /></label>
        <template v-if="mode === 'register'">
          <div v-if="emailVerificationRequired" class="auth-field">
            <label for="registration-code">邮箱验证码</label>
            <div class="auth-code-row">
              <input id="registration-code" v-model="code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="6 位数字验证码" pattern="[0-9]{6}" maxlength="6" required :disabled="busy" />
              <button type="button" class="auth-code-button" :disabled="busy || sending || cooldown > 0 || !validEmail" @click="sendCode">{{ sending ? '发送中…' : cooldown > 0 ? `${cooldown} 秒后重发` : '获取验证码' }}</button>
            </div>
            <span>验证码 10 分钟内有效，未收到时请检查垃圾邮件。</span>
          </div>
          <label class="auth-field"><span>昵称<em>（选填）</em></span><input v-model="displayName" type="text" autocomplete="nickname" maxlength="128" placeholder="希望我们怎么称呼你" :disabled="busy" /></label>
        </template>
        <p v-if="mode === 'register' && registrationConfigLoaded && !registrationAllowed" class="auth-hint">暂未开放注册，请联系管理员。</p>
        <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
        <p v-else-if="hintText" class="auth-hint" role="status">{{ hintText }}</p>
        <button class="auth-submit" type="submit" :disabled="busy || sending || !canSubmit">{{ busy ? (mode === 'login' ? '正在登录…' : '正在注册…') : (mode === 'login' ? '登录' : '注册并登录') }}</button>
      </form>
      <p class="auth-foot"><template v-if="mode === 'login'">还没有账号？<a href="#" @click.prevent="switchMode('register')">立即注册</a></template><template v-else>已有账号？<a href="#" @click.prevent="switchMode('login')">去登录</a></template></p>
    </div>
  </div>
</template>

<script>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ApiError, apiGet, login as apiLogin, register as apiRegister, sendRegistrationCode } from './api'

export default {
  name: 'AuthView',
  emits: ['authed'],
  props: { notice: { type: String, default: '' } },
  setup(props, { emit }) {
    const mode = ref('login'), email = ref(''), password = ref(''), displayName = ref(''), code = ref('')
    const busy = ref(false), sending = ref(false), error = ref(''), hint = ref(''), now = ref(Date.now())
    const registrationAllowed = ref(false), emailVerificationRequired = ref(true), registrationConfigLoaded = ref(false)
    onMounted(async () => {
      try {
        const config = await apiGet('/api/v1/auth/registration-config')
        registrationAllowed.value = config.allow_registration
        emailVerificationRequired.value = config.email_verification_required
        registrationConfigLoaded.value = true
      } catch (e) { error.value = '无法读取注册设置，请刷新页面重试' }
    })
    const resendAt = ref({})
    const normalizedEmail = computed(() => email.value.trim().toLowerCase())
    const cooldown = computed(() => Math.max(0, Math.ceil(((resendAt.value[normalizedEmail.value] || 0) - now.value) / 1000)))
    const timer = setInterval(() => { now.value = Date.now() }, 500)
    onUnmounted(() => clearInterval(timer))
    watch(email, () => { code.value = ''; error.value = ''; hint.value = '' })
    const validEmail = computed(() => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(normalizedEmail.value))
    const hintText = computed(() => hint.value || props.notice || '')
    const canSubmit = computed(() => normalizedEmail.value && password.value.length > 0 && (mode.value === 'login' || (registrationConfigLoaded.value && registrationAllowed.value && validEmail.value && password.value.length >= 8 && (!emailVerificationRequired.value || /^[0-9]{6}$/.test(code.value)))))
    const errorMessage = e => e instanceof ApiError ? e.message : '暂时无法连接服务，请检查网络后重试'
    function switchMode(next) {
      if (busy.value || sending.value || mode.value === next) return
      mode.value = next
      error.value = ''; hint.value = ''; code.value = ''
    }
    async function sendCode() {
      if (busy.value || sending.value || cooldown.value || !validEmail.value) return
      sending.value = true; error.value = ''; hint.value = ''
      try {
        const result = await sendRegistrationCode(normalizedEmail.value)
        resendAt.value[normalizedEmail.value] = Date.now() + result.retry_after * 1000
        now.value = Date.now()
        hint.value = result.detail
      } catch (e) { error.value = errorMessage(e) }
      finally { sending.value = false }
    }
    async function submit() {
      if (busy.value || sending.value || !canSubmit.value) return
      busy.value = true; error.value = ''; hint.value = ''
      let registered = false
      try {
        if (mode.value === 'register') {
          await apiRegister(normalizedEmail.value, password.value, displayName.value.trim(), emailVerificationRequired.value ? code.value : undefined)
          registered = true
          hint.value = '注册成功，正在登录…'
        }
        const data = await apiLogin(normalizedEmail.value, password.value)
        emit('authed', { token: data.access_token, user: data.user })
      } catch (e) {
        if (registered) { mode.value = 'login'; code.value = ''; error.value = '注册成功，但自动登录失败，请重新登录' }
        else error.value = errorMessage(e)
      } finally { busy.value = false }
    }
    return { emailVerificationRequired, registrationAllowed, registrationConfigLoaded, mode, email, password, displayName, code, busy, sending, error, hintText, cooldown, validEmail, canSubmit, switchMode, sendCode, submit }
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
.auth-code-row { display: flex; gap: 8px; }
.auth-code-row input { min-width: 0; flex: 1; }
.auth-code-button { padding: 0 10px; border: 1px solid var(--border); border-radius: 7px; background: var(--fill); color: var(--text); white-space: nowrap; cursor: pointer; }
.auth-code-button:disabled { opacity: 0.5; cursor: default; }
</style>
