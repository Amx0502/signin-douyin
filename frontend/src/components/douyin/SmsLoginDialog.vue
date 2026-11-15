<template>
  <el-dialog
    :model-value="modelValue"
    class="douyin-sms-dialog"
    width="430px"
    align-center
    append-to-body
    @close="handleClose"
  >
    <template #header>
      <div class="sms-title">
        <span class="sms-title__icon"><el-icon><Iphone /></el-icon></span>
        <div><strong>手机号登录抖音</strong><small>验证码登录 + 原手机扫码确认</small></div>
      </div>
    </template>
    <div class="sms-body">
      <!-- 第一步：手机号 -->
      <template v-if="step === 'phone'">
        <el-form label-position="top" class="sms-form">
          <el-form-item label="抖音绑定手机号">
            <el-input v-model="phone" maxlength="13" placeholder="请输入 11 位手机号" size="large" clearable />
          </el-form-item>
        </el-form>
        <p class="sms-tip">提交后抖音会向该手机发送短信验证码；随后使用原设备扫码即可登录成功</p>
      </template>

      <!-- 第二步：验证码 -->
      <template v-else-if="step === 'code'">
        <p class="sms-sent">验证码已发送至 <strong>{{ maskPhone }}</strong>，请查收短信</p>
        <el-input
          v-model="smsCode"
          maxlength="6"
          size="large"
          placeholder="输入短信验证码"
          class="sms-code-input"
          @keyup.enter="submitCode"
        >
          <template #append>
            <el-button :disabled="resendSeconds > 0" @click="noop">
              {{ resendSeconds > 0 ? `${resendSeconds}s 后可重试` : '未收到？' }}
            </el-button>
          </template>
        </el-input>
        <el-alert v-if="pollMessage" type="info" :title="pollMessage" :closable="false" class="sms-alert" />
      </template>

      <!-- 第三步：原设备扫码确认 / 轮询结果 -->
      <template v-else-if="step === 'confirm'">
        <p class="sms-confirm-tip">
          <strong>抖音要求用原手机确认登录</strong>：请打开<strong>原手机抖音 APP</strong> 扫描下方二维码完成确认
        </p>
        <div class="qr-stage">
          <img v-if="confirmQr" :src="confirmQr" alt="原设备确认二维码" />
          <el-skeleton v-else animated><template #template><el-skeleton-item variant="image" class="qr-skeleton" /></template></el-skeleton>
          <div v-if="status === 'confirmed'" class="qr-overlay"><el-icon><CircleCheckFilled /></el-icon><strong>登录成功</strong></div>
        </div>
        <el-alert v-if="status === 'waiting' && pollMessage" type="info" :title="pollMessage" :closable="false" class="sms-alert" />
        <el-alert v-if="status === 'captcha' && verificationImage" type="warning" title="出现安全验证，请查看下方截图" :closable="false" class="sms-alert" />
        <img v-if="status === 'captcha' && verificationImage" :src="verificationImage" alt="安全验证截图" class="verify-shot" />
      </template>

      <!-- 成功 -->
      <template v-else-if="step === 'done'">
        <div class="sms-done">
          <el-icon class="done-icon"><CircleCheckFilled /></el-icon>
          <strong>账号已添加：{{ displayName }}</strong>
        </div>
      </template>

      <!-- 失败 -->
      <template v-else-if="step === 'error'">
        <el-alert type="error" :title="errorMessage || '登录失败'" :closable="false" />
        <p class="sms-tip">可关闭后重试，或改用扫码登录（触发滑块时短信通道走不通）。</p>
      </template>
    </div>
    <template #footer>
      <el-button @click="handleClose">关闭</el-button>
      <el-button
        v-if="step === 'phone'"
        type="primary"
        :loading="starting"
        @click="startSms"
      >获取验证码</el-button>
      <el-button
        v-else-if="step === 'code'"
        type="primary"
        :loading="submitting"
        @click="submitCode"
      >登录</el-button>
      <el-button
        v-else-if="step === 'error'"
        type="primary"
        @click="resetToPhone"
      >重新开始</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheckFilled, Iphone } from '@element-plus/icons-vue'
import api from '../../api/douyin.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'success'])

const step = ref('phone')      // phone / code / confirm / done / error
const phone = ref('')
const smsCode = ref('')
const starting = ref(false)
const submitting = ref(false)
const errorMessage = ref('')
const pollMessage = ref('')
const status = ref('')
const confirmQr = ref('')
const verificationImage = ref('')
const account = ref(null)
const resendSeconds = ref(0)
let qrId = null
let pollTimer = null
let tickTimer = null
// 轮询节奏：常规 2.5s；提交验证码后切 1s 快节奏（见 startRush）。
// 后端「建号 / 取原设备确认码」通常几秒内完成，固定 2.5s 会让用户白等一跳。
const POLL_INTERVAL_MS = 2500
const POLL_INTERVAL_RUSH_MS = 1000
const RUSH_WINDOW_MS = 90000
let rushUntil = 0
let polling = false

const maskPhone = computed(() => {
  const digits = phone.value.replace(/\D/g, '')
  return digits.length === 11 ? `${digits.slice(0, 3)} **** ${digits.slice(7)}` : phone.value
})
const displayName = computed(() =>
  account.value?.display_name || account.value?.remark || account.value?.nickname || '未命名账号',
)

function stopPolling() {
  polling = false
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null }
}

function scheduleNextPoll(delay) {
  if (!polling) return
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    pollTimer = null
    if (!polling) return
    await pollSession()
    // pollSession 命中终态时会 stopTimers()（polling=false），这里自然不再续排
    scheduleNextPoll()
  }, delay === undefined ? (Date.now() < rushUntil ? POLL_INTERVAL_RUSH_MS : POLL_INTERVAL_MS) : delay)
}

// 提交验证码后进入快节奏：立刻补一次，之后 1s 一跳，窗口 90s
function startRush() {
  rushUntil = Date.now() + RUSH_WINDOW_MS
  scheduleNextPoll(0)
}

function stopTimers() {
  stopPolling()
  if (tickTimer) { clearInterval(tickTimer); tickTimer = null }
}

function startPolling() {
  stopTimers()
  polling = true
  scheduleNextPoll()
  tickTimer = setInterval(() => {
    if (resendSeconds.value > 0) resendSeconds.value -= 1
  }, 1000)
}

function noop() {}

async function startSms() {
  const digits = phone.value.replace(/\D/g, '')
  if (!/^1\d{10}$/.test(digits)) {
    return ElMessage.warning('请输入 11 位大陆手机号')
  }
  starting.value = true
  errorMessage.value = ''
  try {
    const result = await api.startLoginSms({ phone: digits })
    qrId = result.qr_id
    step.value = 'code'
    resendSeconds.value = 60
    pollMessage.value = '正在发送验证码…'
    startPolling()
  } catch (error) {
    errorMessage.value = error?.response?.data?.error || '发起手机号登录失败'
    step.value = 'error'
  } finally {
    starting.value = false
  }
}

async function submitCode() {
  const code = smsCode.value.replace(/\D/g, '')
  if (!code || submitting.value || !qrId) return
  submitting.value = true
  try {
    await api.submitLoginSmsCode(qrId, code)
    step.value = 'confirm'
    pollMessage.value = '验证码已提交，等待二次验证…'
    startRush()
  } catch (error) {
    pollMessage.value = error?.response?.data?.error || '验证码提交失败'
  } finally {
    submitting.value = false
  }
}

async function pollSession() {
  if (!qrId) return
  try {
    const result = await api.pollLoginQr(qrId)
    status.value = result.status
    if (result.message) pollMessage.value = result.message
    if (result.qr_image) confirmQr.value = result.qr_image
    if (result.verification_image) verificationImage.value = result.verification_image
    if (result.status === 'confirmed') {
      account.value = result.account || {}
      step.value = 'done'
      stopTimers()
      emit('success', result.account)
    } else if (result.status === 'error') {
      stopTimers()
      errorMessage.value = result.message || '登录失败'
      step.value = 'error'
    }
  } catch (error) {
    stopTimers()
    errorMessage.value = error?.response?.data?.error || '登录会话已失效'
    step.value = 'error'
  }
}

function resetToPhone() {
  resetSession()
  step.value = 'phone'
}

function resetSession() {
  stopTimers()
  if (qrId) {
    api.closeLoginQr(qrId).catch(() => {})
    qrId = null
  }
  smsCode.value = ''
  status.value = ''
  confirmQr.value = ''
  verificationImage.value = ''
  pollMessage.value = ''
  errorMessage.value = ''
  account.value = null
  resendSeconds.value = 0
}

function handleClose() {
  emit('update:modelValue', false)
  resetSession()
  step.value = 'phone'
}

watch(() => props.modelValue, visible => {
  if (visible) {
    step.value = 'phone'
    phone.value = ''
    resetSession()
  }
})

onUnmounted(resetSession)
</script>

<style scoped>
.sms-title { display: flex; align-items: center; gap: 12px; }
.sms-title__icon { display: grid; width: 42px; height: 42px; place-items: center; color: #fff; border-radius: 14px; background: linear-gradient(135deg, #2563eb, #0ea5e9); box-shadow: 0 10px 24px rgb(37 99 235 / 24%); }
.sms-title strong, .sms-title small { display: block; }
.sms-title strong { color: #172033; font-size: 17px; }
.sms-title small { margin-top: 3px; color: #64748b; font-size: 12px; }
.sms-body { display: grid; justify-items: stretch; gap: 14px; padding: 8px 0 4px; }
.sms-form { display: grid; gap: 2px; }
.sms-tip { margin: 0; color: #94a3b8; font-size: 12px; line-height: 1.6; }
.sms-sent { margin: 0; color: #475569; font-size: 13px; }
.sms-sent strong { color: #172033; }
.sms-code-input :deep(input) { letter-spacing: 4px; font-size: 18px; }
.sms-alert { width: 100%; }
.sms-confirm-tip { margin: 0; color: #475569; font-size: 13px; line-height: 1.7; text-align: center; }
.sms-confirm-tip strong { color: #172033; }
.qr-stage { position: relative; width: 248px; height: 248px; margin: 0 auto; display: grid; place-items: center; padding: 13px; overflow: hidden; border: 1px solid #bfdbfe; border-radius: 24px; background: linear-gradient(145deg, #fff, #eff6ff); box-shadow: 0 18px 44px rgb(37 99 235 / 15%); }
.qr-stage img, .qr-skeleton { width: 220px; height: 220px; border-radius: 14px; object-fit: contain; }
.qr-overlay { position: absolute; inset: 0; display: grid; place-content: center; justify-items: center; gap: 10px; color: #fff; background: rgb(5 150 105 / 88%); backdrop-filter: blur(5px); }
.qr-overlay .el-icon { font-size: 52px; }
.verify-shot { width: 100%; border-radius: 10px; border: 1px solid #e2e8f0; }
.sms-done { display: grid; justify-items: center; gap: 12px; padding: 18px 0; }
.done-icon { color: #059669; font-size: 54px; }
.sms-done strong { color: #172033; font-size: 15px; }
</style>

<style>
.douyin-sms-dialog.el-dialog { max-width: calc(100vw - 28px); overflow: hidden; border: 1px solid rgb(191 219 254 / 80%); border-radius: 24px; background: rgb(255 255 255 / 96%); box-shadow: 0 30px 80px rgb(15 23 42 / 22%); }
</style>
