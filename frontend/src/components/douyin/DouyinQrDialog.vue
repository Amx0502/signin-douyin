<template>
  <el-dialog
    :model-value="modelValue"
    class="douyin-qr-dialog"
    width="430px"
    align-center
    append-to-body
    @close="handleClose"
  >
    <template #header>
      <div class="qr-title">
        <span class="qr-title__icon"><el-icon><Cellphone /></el-icon></span>
        <div><strong>扫码登录抖音</strong><small>使用抖音 App 扫描二维码</small></div>
      </div>
    </template>
    <div class="qr-body" :class="`is-${status || 'pending'}`">
      <div class="qr-stage">
        <img v-if="qrImage" :src="qrImage" alt="抖音登录二维码" />
        <el-skeleton v-else animated><template #template><el-skeleton-item variant="image" class="qr-skeleton" /></template></el-skeleton>
        <div v-if="status === 'confirmed'" class="qr-overlay"><el-icon><CircleCheckFilled /></el-icon><strong>登录成功</strong></div>
      </div>
      <div class="qr-state">
        <el-tag :type="statusMeta.type" effect="light">{{ statusMeta.label }}</el-tag>
        <span v-if="status === 'waiting' && qrImage">二维码将在 <b>{{ remainingSeconds }}</b> 秒后失效</span>
        <span v-else-if="status === 'waiting' && pollMessage">{{ pollMessage }}</span>
        <span v-else-if="status === 'verification_required' && pollMessage">{{ pollMessage }}</span>
        <span v-else>{{ statusMeta.tip }}</span>
      </div>
      <div v-if="status === 'confirmed' && account" class="qr-account-result">
        <strong>已添加账号：{{ displayName }}</strong>
        <small v-if="accountId">UID：{{ accountId }}</small>
      </div>
      <el-alert
        v-if="status === 'error' && errorMessage"
        type="error"
        :title="errorMessage"
        :closable="false"
        class="qr-error"
      />
      <el-progress
        v-if="status === 'waiting'"
        :percentage="countdownPercent"
        :show-text="false"
        :stroke-width="6"
      />
      <div v-if="status === 'verification_required'" class="qr-verification">
        <img v-if="verificationImage" :src="verificationImage" alt="安全验证截图" />
        <p v-else>页面出现安全验证，请在抖音 App 中查看验证码</p>
        <div class="qr-verification__row">
          <el-input
            v-model="verificationCode"
            maxlength="8"
            placeholder="输入验证码"
            @keyup.enter="submitVerification"
          />
          <el-button type="primary" :loading="verifying" @click="submitVerification">提交验证</el-button>
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="handleClose">关闭</el-button>
      <el-button
        v-if="status !== 'confirmed'"
        type="primary"
        :loading="starting"
        @click="startQr"
      >重新生成</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { Cellphone, CircleCheckFilled } from '@element-plus/icons-vue'
import api from '../../api/douyin.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'success'])

const status = ref('')       // '' / waiting / confirmed / expired / error
const qrImage = ref('')
const errorMessage = ref('')
const pollMessage = ref('')
const starting = ref(false)
const account = ref(null)
const verificationImage = ref('')
const verificationCode = ref('')
const verifying = ref(false)
let qrId = null
let pollTimer = null
let tickTimer = null
let deadline = 0
// 展示用倒计时固定 120s（对齐班级魔方扫码弹窗；后端会话实际 480s，超时后仍可继续等扫码）
const QR_COUNTDOWN_SECONDS = 120
let totalTtl = QR_COUNTDOWN_SECONDS
const nowTick = ref(0)

// 轮询节奏：常规 2.5s；提交验证码后切 1s 快节奏（见 startRush）。
// 后端建号通常只需几秒，固定 2.5s 会让「提交 → 看到账号」凭空多等最多 2.5s。
const POLL_INTERVAL_MS = 2500
const POLL_INTERVAL_RUSH_MS = 1000
const RUSH_WINDOW_MS = 90000
let rushUntil = 0
let polling = false

const statusMap = {
  '': { label: '正在获取', type: 'primary', tip: '正在获取二维码…' },
  waiting: { label: '等待扫码', type: 'primary', tip: '请在抖音 App 中确认登录' },
  verification_required: { label: '需要验证', type: 'warning', tip: '扫码触发了安全验证，请按提示输入验证码' },
  confirmed: { label: '登录成功', type: 'success', tip: '账号已添加并同步' },
  expired: { label: '二维码已过期', type: 'warning', tip: '请点击重新生成' },
  error: { label: '登录失败', type: 'danger', tip: '请点击重新生成' },
}
const statusMeta = computed(() => statusMap[status.value] || statusMap[''])
const displayName = computed(() =>
  account.value?.display_name || account.value?.remark || account.value?.nickname || '未命名账号',
)
const accountId = computed(() =>
  account.value?.remote_user_id || account.value?.remote_uid || account.value?.uid || '',
)
const remainingSeconds = computed(() => {
  void nowTick.value
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
})
const countdownPercent = computed(() => {
  void nowTick.value
  return Math.max(0, Math.min(100, Math.round(remainingSeconds.value / Math.max(1, totalTtl) * 100)))
})

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
    await pollQr()
    // pollQr 命中终态时会 stopTimers()（polling=false），这里自然不再续排
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

function startTimers() {
  stopTimers()
  polling = true
  scheduleNextPoll()
  tickTimer = setInterval(() => { nowTick.value += 1 }, 1000)
}

async function startQr() {
  starting.value = true
  errorMessage.value = ''
  qrImage.value = ''
  pollMessage.value = ''
  account.value = null
  status.value = ''
  stopTimers()
  try {
    const result = await api.startLoginQr({})
    qrId = result.qr_id
    totalTtl = QR_COUNTDOWN_SECONDS
    deadline = Date.now() + totalTtl * 1000
    status.value = 'waiting'
    startTimers()
  } catch (error) {
    status.value = 'error'
    errorMessage.value = error?.response?.data?.error || '发起扫码登录失败'
  } finally {
    starting.value = false
  }
}

async function pollQr() {
  if (!qrId) return
  try {
    const result = await api.pollLoginQr(qrId)
    status.value = result.status
    pollMessage.value = result.message || ''
    if (result.qr_image) qrImage.value = result.qr_image
    if (result.verification_image) verificationImage.value = result.verification_image
    if (result.status === 'confirmed') {
      account.value = result.account || {}
      stopTimers()
      emit('success', result.account)
    } else if (result.status === 'expired') {
      stopTimers()
    } else if (result.status === 'error') {
      stopTimers()
      errorMessage.value = result.message || '登录失败'
    }
  } catch (error) {
    stopTimers()
    status.value = 'error'
    errorMessage.value = error?.response?.data?.error || '登录会话已失效'
  }
}

async function submitVerification() {
  const code = verificationCode.value.trim()
  if (!code || verifying.value || !qrId) return
  verifying.value = true
  try {
    const result = await api.submitLoginVerification(qrId, code)
    pollMessage.value = result.message || ''
    startRush()
  } catch (error) {
    pollMessage.value = error?.response?.data?.error || '验证码提交失败'
  } finally {
    verifying.value = false
  }
}

function resetSession() {
  stopTimers()
  if (qrId) {
    api.closeLoginQr(qrId).catch(() => {})
    qrId = null
  }
  status.value = ''
  qrImage.value = ''
  errorMessage.value = ''
  pollMessage.value = ''
  account.value = null
  verificationImage.value = ''
  verificationCode.value = ''
}

function handleClose() {
  emit('update:modelValue', false)
  resetSession()
}

watch(() => props.modelValue, visible => {
  if (visible) startQr()
  else resetSession()
})

onUnmounted(resetSession)
</script>

<style scoped>
.qr-title { display: flex; align-items: center; gap: 12px; }
.qr-title__icon { display: grid; width: 42px; height: 42px; place-items: center; color: #fff; border-radius: 14px; background: linear-gradient(135deg, #2563eb, #0ea5e9); box-shadow: 0 10px 24px rgb(37 99 235 / 24%); }
.qr-title strong, .qr-title small { display: block; }
.qr-title strong { color: #172033; font-size: 17px; }
.qr-title small { margin-top: 3px; color: #64748b; font-size: 12px; }
.qr-body { display: grid; justify-items: center; gap: 18px; padding: 10px 0 4px; }
.qr-stage { position: relative; width: 248px; height: 248px; display: grid; place-items: center; padding: 13px; overflow: hidden; border: 1px solid #bfdbfe; border-radius: 24px; background: linear-gradient(145deg, #fff, #eff6ff); box-shadow: 0 18px 44px rgb(37 99 235 / 15%); }
.qr-stage img, .qr-skeleton { width: 220px; height: 220px; border-radius: 14px; object-fit: contain; }
.qr-overlay { position: absolute; inset: 0; display: grid; place-content: center; justify-items: center; gap: 10px; color: #fff; background: rgb(5 150 105 / 88%); backdrop-filter: blur(5px); }
.qr-overlay .el-icon { font-size: 52px; }
.qr-state { display: flex; align-items: center; gap: 10px; color: #64748b; font-size: 13px; }
.qr-state b { color: #2563eb; font-size: 18px; font-variant-numeric: tabular-nums; }
.qr-account-result { display: grid; gap: 4px; width: 248px; padding: 10px 12px; border: 1px solid #bbf7d0; border-radius: 10px; color: #166534; background: #f0fdf4; }
.qr-account-result strong { font-size: 13px; }
.qr-account-result small { color: #4d7c0f; font-size: 11px; overflow-wrap: anywhere; }
.qr-error { width: 248px; }
.qr-verification { display: grid; gap: 10px; width: 248px; padding: 10px; border: 1px solid #fde68a; border-radius: 10px; background: #fffbeb; }
.qr-verification img { width: 100%; border-radius: 6px; }
.qr-verification p { margin: 0; color: #92400e; font-size: 12px; line-height: 1.5; }
.qr-verification__row { display: flex; gap: 8px; }
.el-progress { width: 248px; }
</style>

<style>
.douyin-qr-dialog.el-dialog { max-width: calc(100vw - 28px); overflow: hidden; border: 1px solid rgb(191 219 254 / 80%); border-radius: 24px; background: rgb(255 255 255 / 96%); box-shadow: 0 30px 80px rgb(15 23 42 / 22%); }
</style>
