<template>
  <div class="accounts-layout">
    <el-card class="panel" shadow="never">
      <template #header>
        <div class="panel-head">
          <div><strong>账号管理</strong><small>手机号或扫码添加抖音账号，自动校验登录态</small></div>
          <div class="head-actions">
            <el-button :icon="Refresh" :loading="loading" @click="emit('refresh')">刷新</el-button>
            <el-tooltip :disabled="canAddAccount" content="账号额度已用完，删除账号后才能重新添加" placement="bottom">
              <span class="add-wrap">
                <el-button type="primary" :icon="Cellphone" :disabled="!canAddAccount" @click="openSmsDialog">手机号登录</el-button>
              </span>
            </el-tooltip>
            <el-tooltip :disabled="canAddAccount" content="账号额度已用完，删除账号后才能重新添加" placement="bottom">
              <span class="add-wrap">
                <el-button type="success" :icon="Refresh" :disabled="!canAddAccount" @click="openQrDialog">扫码登录</el-button>
              </span>
            </el-tooltip>
          </div>
        </div>
        <p v-if="!canAddAccount" class="quota-hint">
          账号额度已用完（{{ accountAccess.account_count }}/{{ accountAccess.account_limit }}），删除账号后才能重新添加
        </p>
      </template>

    <div class="account-list">
      <article v-for="row in accounts" :key="row.id" class="account-row">
        <el-avatar :size="44" :src="row.avatar || ''" class="account-avatar">
          {{ (row.display_name || '抖').slice(0, 1) }}
        </el-avatar>
        <div class="account-main">
          <div class="account-title">
            <strong>{{ row.display_name }}</strong>
            <el-tag :type="row.status === 'active' ? 'success' : 'danger'" size="small">
              {{ row.status === 'active' ? '登录正常' : '登录失效' }}
            </el-tag>
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            <el-tag v-if="friendsJobs[row.id] === 'running'" size="small" type="primary">好友拉取中…</el-tag>
          </div>
          <small class="account-ids">
            <span v-if="row.douyin_id" class="id-item" :title="`抖音号 ${row.douyin_id}`">抖音号 {{ row.douyin_id }}</span>
            <span class="id-item" :title="`UID ${row.remote_user_id}`">UID {{ row.remote_user_id }}</span>
          </small>
          <small class="account-sub">
            <span>已缓存会话 {{ row.targets_cached }} 条</span>
            <span v-if="row.last_verified_at">校验于 {{ formatTime(row.last_verified_at) }}</span>
            <span v-if="row.cookies_refreshed_at" :title="`cookie 保活（刷新 ttwid/msToken 等短命字段）：${formatTime(row.cookies_refreshed_at)}`">
              保活于 {{ formatTime(row.cookies_refreshed_at) }}
            </span>
            <span v-if="row.profile_enabled" :title="`持久浏览器环境（设备指纹与登录态一起续命）：${row.profile_updated_at ? formatTime(row.profile_updated_at) : '尚未建立'}`">
              持久环境
            </span>
            <span v-if="row.last_error" class="err-text" :title="row.last_error">{{ row.last_error }}</span>
          </small>
        </div>
        <div class="account-side">
          <el-tooltip content="启用 / 停用该账号" placement="top">
            <el-switch :model-value="row.enabled" @change="value => toggle(row, value)" />
          </el-tooltip>
          <div class="account-actions">
            <el-button size="small" type="primary" plain :loading="verifyingId === row.id" @click="verify(row)">
              <el-icon v-if="verifyingId !== row.id"><CircleCheck /></el-icon>校验
            </el-button>
            <el-button size="small" :icon="User" :disabled="row.status !== 'active'" @click="openFriends(row)">好友</el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="remove(row)">删除</el-button>
          </div>
        </div>
      </article>
      <el-empty v-if="!accounts.length" description="还没有抖音账号，点击右上角添加" :image-size="90" />
    </div>

    <!-- 手机号验证码登录 -->
    <SmsLoginDialog v-model="smsVisible" @success="onSmsSuccess" />
    <!-- 扫码登录（对齐班级魔方二维码弹窗样式） -->
    <DouyinQrDialog v-model="qrVisible" @success="onQrSuccess" />
    <!-- 好友列表（只读）；auto-once：新账号首次打开自动拉好友+火花 -->
    <FriendsDialog
      v-model="friendsVisible"
      :account="friendsAccount"
      :auto-once="friendsAutoOnce"
      mode="view"
      @job-status="onJobStatus"
    />
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Cellphone, CircleCheck, Delete, Refresh, User } from '@element-plus/icons-vue'
import api from '../../api/douyin.js'
import FriendsDialog from './FriendsDialog.vue'
import DouyinQrDialog from './DouyinQrDialog.vue'
import SmsLoginDialog from './SmsLoginDialog.vue'

const props = defineProps({
  accounts: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['refresh'])

const loggedUser = JSON.parse(localStorage.getItem('user') || '{}')
const isAdmin = loggedUser.role === 'admin'

// 账号额度：普通用户额度满时禁用添加入口，删除账号后才恢复
const accountAccess = ref({ account_limit: null, account_count: 0, can_add_account: true })
const canAddAccount = computed(() => isAdmin || accountAccess.value.can_add_account)

async function loadAccountAccess() {
  try {
    accountAccess.value = await api.getAccountAccess()
  } catch {
    // 拉取失败按可添加处理，后端创建时仍会兜底校验
    accountAccess.value = { account_limit: null, account_count: 0, can_add_account: true }
  }
}

onMounted(loadAccountAccess)
watch(() => props.accounts, loadAccountAccess)

// 后台好友拉取任务状态（accountId -> 'running'），由 FriendsDialog 事件驱动
const friendsJobs = reactive({})

function onJobStatus({ accountId, status }) {
  if (status === 'running') {
    friendsJobs[accountId] = 'running'
  } else {
    delete friendsJobs[accountId]
  }
}

const smsVisible = ref(false)
const saving = ref(false)
const verifyingId = ref(null)

const qrVisible = ref(false)

const friendsVisible = ref(false)
const friendsAccount = ref(null)
const friendsAutoOnce = ref(false)
// 新账号添加成功后的一次性引导：首次点「好友」自动拉一次好友 + 一次火花
const autoOnceIds = new Set()

function openFriends(row) {
  friendsAccount.value = row
  friendsAutoOnce.value = autoOnceIds.delete(row.id)
  friendsVisible.value = true
}

function openSmsDialog() {
  smsVisible.value = true
}

function onSmsSuccess(account) {
  if (account?.id) autoOnceIds.add(account.id)
  emit('refresh')
}

async function verify(row) {
  verifyingId.value = row.id
  try {
    await api.verifyAccount(row.id)
    ElMessage.success(`「${row.display_name}」登录态正常`)
    emit('refresh')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || '校验失败')
    emit('refresh')
  } finally {
    verifyingId.value = null
  }
}

async function toggle(row, value) {
  try {
    await api.updateAccount(row.id, { remark: row.remark || '', enabled: value })
    emit('refresh')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || '操作失败')
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除账号「${row.display_name}」？其任务会一并删除。`, '删除账号', { type: 'warning' })
  await api.deleteAccount(row.id)
  ElMessage.success('已删除')
  emit('refresh')
}

// ---------------------------------------------------------------- 扫码登录

function openQrDialog() {
  qrVisible.value = true
}

function onQrSuccess(account) {
  if (account?.id) autoOnceIds.add(account.id)
  emit('refresh')
}

function formatTime(value) {
  if (!value) return ''
  return String(value).replace('T', ' ').slice(5, 16)
}
</script>

<style scoped>
.accounts-layout{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;align-items:start}
.panel{border:1px solid rgb(191 219 254 / 58%);border-radius:22px;background:rgb(255 255 255 / 84%);box-shadow:0 18px 42px rgb(15 23 42 / 7%);backdrop-filter:blur(18px)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.panel-head strong,.panel-head small{display:block}
.panel-head small{margin-top:4px;color:#64748b;font-size:11px}
.head-actions{display:flex;gap:8px}
.head-actions .el-button+.el-button{margin-left:0}
.add-wrap{display:inline-flex}
.quota-hint{margin:0;color:#dc2626;font-size:12px}
.account-list{display:grid;gap:10px}
/* 桌面端：头像 + 信息占满左区，开关与操作按钮收在右侧一列，避免按钮被拉满整行 */
.account-row{display:flex;align-items:flex-start;gap:12px;padding:13px 15px;border:1px solid #e2e8f0;border-radius:15px;background:linear-gradient(145deg,#fff,#f8fafc)}
.account-avatar{flex:none;margin-top:2px;background:#e0e7ff;color:#4338ca;font-weight:600}
.account-main{min-width:0;flex:1;display:grid;gap:4px}
.account-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.account-title strong{font-size:15px;color:#0f172a}
.account-ids{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:#475569;font-size:11px;font-family:ui-monospace,Consolas,monospace}
.id-item{padding:2px 7px;border-radius:6px;background:#f1f5f9;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.account-sub{display:flex;align-items:center;gap:6px;flex-wrap:wrap;color:#94a3b8;font-size:11px}
.err-text{color:#dc2626;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.account-side{flex:none;display:grid;justify-items:end;gap:9px}
.account-actions{display:flex;gap:6px}
.account-actions .el-button{margin-left:0}
.tip-text{margin:6px 0 0;color:#64748b;font-size:12px}
@media(max-width:1100px){
  .accounts-layout{grid-template-columns:minmax(0,1fr)}
}
@media(max-width:750px){
  .panel-head{flex-direction:column;align-items:stretch;gap:10px}
  .panel-head small{margin-top:0}
  .head-actions .el-button{flex:1;justify-content:center}
  .head-actions .add-wrap{flex:1}
  .head-actions .add-wrap .el-button{width:100%;margin-left:0}
  /* 手机端：开关与操作换行，按钮三等分铺满，保持与旧版一致的大点击区 */
  .account-row{flex-wrap:wrap;row-gap:10px;padding:12px 13px}
  .account-side{width:100%;display:flex;flex-direction:row-reverse;align-items:center;gap:10px}
  .account-actions{flex:1;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
  .account-actions .el-button{width:100%;justify-content:center}
}
</style>
