<template>
  <el-dialog
    :model-value="modelValue"
    :title="mode === 'select' ? '拉取好友并选择目标' : '好友列表'"
    width="min(720px, 94vw)"
    align-center
    append-to-body
    @update:model-value="value => emit('update:modelValue', value)"
  >
    <div class="friends-body">
      <div class="account-bar">
        <el-avatar :size="40" :src="account?.avatar || ''" class="account-avatar">
          {{ (account?.display_name || '抖').slice(0, 1) }}
        </el-avatar>
        <div class="account-meta">
          <div class="account-name">
            <strong>{{ account?.display_name || '—' }}</strong>
            <el-tag v-if="account?.douyin_id" size="small" type="info">抖音号 {{ account.douyin_id }}</el-tag>
          </div>
          <small class="account-uid">UID {{ account?.remote_user_id || '—' }}</small>
        </div>
        <div class="bar-right">
          <div class="account-stats">
            <template v-if="data">
              <span class="stat"><b>{{ data.count }}</b>会话</span>
              <span class="stat ok"><b>{{ data.sendable_count }}</b>可发送</span>
              <span v-if="data.unnamed_count" class="stat warn"><b>{{ data.unnamed_count }}</b>缺昵称</span>
            </template>
          </div>
          <el-button
            size="small"
            type="primary"
            :icon="Refresh"
            :loading="jobRunning"
            @click="refresh"
          >
            {{ jobRunning ? '拉取中…' : '拉取好友' }}
          </el-button>
        </div>
      </div>

      <div v-if="jobRunning || (data?.cached && data.fetched_at)" class="status-line">
        <el-tag v-if="data?.cached && data.fetched_at" size="small" type="info">
          缓存于 {{ formatTime(data.fetched_at) }}
        </el-tag>
        <el-tag v-if="jobRunning" size="small" type="primary">后台拉取中，可关闭窗口稍后回来</el-tag>
      </div>

      <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon />
      <p v-else-if="mode === 'select'" class="tip-text">
        默认展示上次保留的好友，勾选后写入任务目标；点「拉取好友」在后台刷新（好友与火花一次采样），完成后自动更新。缺昵称条目不可发送。
      </p>
      <p v-else class="tip-text">
        默认展示上次保留的好友（来源为抖音私信会话列表）；点「拉取好友」在后台刷新（好友与火花一次采样），可随时关闭窗口。
      </p>

      <el-input v-if="data" v-model="keyword" placeholder="搜索昵称或抖音号" clearable :prefix-icon="Search" />

      <div v-loading="loading && !jobRunning" class="friend-list">
        <template v-if="mode === 'select'">
          <div
            v-for="row in filtered"
            :key="row.key"
            class="friend-row"
            :class="{ disabled: !row.sendable }"
            @click="toggle(row.nickname, row.sendable)"
          >
            <el-checkbox
              :model-value="picked.includes(row.nickname)"
              :disabled="!row.sendable"
              @click.stop
              @change="value => toggle(row.nickname, row.sendable, value)"
            />
            <el-avatar :size="36" :src="row.avatar || ''" class="friend-avatar">
              {{ (row.nickname || '?').slice(0, 1) }}
            </el-avatar>
            <div class="friend-main">
              <strong>{{ row.nickname || '（未取到昵称，无法作为目标）' }}</strong>
              <small>
                <template v-if="row.unique_id">抖音号 {{ row.unique_id }}</template>
                <template v-else>会话 {{ row.conversation_short_id || row.conversation_id || '—' }}</template>
              </small>
            </div>
            <el-tag v-if="sparkOf(row)" size="small" :type="sparkOf(row).type" class="spark-tag" :title="sparkOf(row).title">
              <img :src="sparkOf(row).icon === 'on' ? sparkOn : sparkOff" alt="" class="spark-icon">{{ sparkOf(row).label }}
            </el-tag>
            <el-tag v-if="row.unique_id" size="small" type="info" class="uid-tag">UID {{ row.peer_user_id || shortSecUid(row.sec_uid) }}</el-tag>
          </div>
        </template>
        <template v-else>
          <div v-for="row in filtered" :key="row.key" class="friend-row">
            <el-avatar :size="36" :src="row.avatar || ''" class="friend-avatar">
              {{ (row.nickname || '?').slice(0, 1) }}
            </el-avatar>
            <div class="friend-main">
              <strong>{{ row.nickname || '（未取到昵称）' }}</strong>
              <small>
                <template v-if="row.unique_id">抖音号 {{ row.unique_id }}</template>
                <template v-else>会话 {{ row.conversation_short_id || row.conversation_id || '—' }}</template>
              </small>
            </div>
            <el-tag v-if="sparkOf(row)" size="small" :type="sparkOf(row).type" class="spark-tag" :title="sparkOf(row).title">
              <img :src="sparkOf(row).icon === 'on' ? sparkOn : sparkOff" alt="" class="spark-icon">{{ sparkOf(row).label }}
            </el-tag>
            <el-tag v-if="row.unique_id" size="small" type="info" class="uid-tag">UID {{ row.peer_user_id || shortSecUid(row.sec_uid) }}</el-tag>
            <el-button v-if="row.sendable" size="small" text type="primary" @click="copyOne(row.nickname)">复制</el-button>
          </div>
        </template>
        <el-empty v-if="!loading && !filtered.length" :description="data ? '没有匹配的好友' : '暂未拉取'" :image-size="80" />
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">关闭</el-button>
      <el-button v-if="mode === 'view'" type="primary" plain :disabled="!names.length" @click="copyAll">复制全部昵称</el-button>
      <el-button v-else type="primary" :disabled="!picked.length" @click="confirmPick">确定（已选 {{ picked.length }}）</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import api from '../../api/douyin.js'
import sparkOn from '../../assets/douyin-spark-on.png'
import sparkOff from '../../assets/douyin-spark-off.png'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  account: { type: Object, default: null },
  // view：只查看 + 复制；select：勾选后回传
  mode: { type: String, default: 'view' },
  selected: { type: Array, default: () => [] },
  // 首次打开自动拉一次好友 + 一次火花（新账号添加成功后的一次性引导），之后手动
  autoOnce: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'confirm', 'job-status'])

const loading = ref(false)
const error = ref('')
const data = ref(null)
const keyword = ref('')
const picked = ref([])
const jobRunning = ref(false)
const sparkMap = ref({})
let pollTimer = null

const filtered = computed(() => {
  const rows = (data.value?.friends || []).map(row => ({
    ...row,
    key: row.sec_uid || row.peer_user_id || row.nickname || row.conversation_id,
  }))
  const text = keyword.value.trim().toLowerCase()
  if (!text) return rows
  return rows.filter(row =>
    (row.nickname || '').toLowerCase().includes(text)
    || (row.unique_id || '').toLowerCase().includes(text)
    || (row.peer_user_id || '').toLowerCase().includes(text),
  )
})

const names = computed(() => (data.value?.friends || []).filter(row => row.sendable).map(row => row.nickname))

// 打开时先读本地保留的缓存（不请求抖音），并接管可能还在跑的后台拉取任务
watch(() => props.modelValue, value => {
  if (value) onOpen()
}, { immediate: true })

// 账号切换时清掉旧轮询，避免把 A 账号的任务结果写进 B 的列表
watch(() => props.account?.id, () => {
  stopPolling()
  jobRunning.value = false
  data.value = null
  error.value = ''
  sparkMap.value = {}
})

onBeforeUnmount(stopPolling)

function onOpen() {
  keyword.value = ''
  error.value = ''
  picked.value = []
  loadCache()
  // 新账号首次打开：一次合并采样同时拿好友与火花（同一数据源，只付一次协议初始化）
  if (props.autoOnce) {
    refresh()
    return
  }
  loadSparkCache()
  // 弹窗关了任务还在跑：重开时接上轮询
  syncJobStatus(true)
}

/** 读取火花快照缓存（不请求抖音），按 peer_user_id / sec_uid / 会话 / 昵称建索引。
 *  没有快照时角标不显示——点「拉取好友」会一次采样把火花一并拉回。 */
async function loadSparkCache() {
  if (!props.account?.id) return
  try {
    const result = await api.getSparkCache(props.account.id)
    sparkMap.value = buildSparkMap(result)
  } catch {
    // 火花快照只是辅助信息，读取失败不打扰主流程
    sparkMap.value = {}
  }
}

function buildSparkMap(result) {
  const map = {}
  for (const item of result?.sparks || []) {
    const keys = [item.peer_user_id, item.sec_uid, item.conversation_id,
      item.nickname && `n:${item.nickname}`].filter(Boolean)
    for (const key of keys) map[key] = item
  }
  return map
}

/** 好友行 → 火花角标；无进行中火花（state=0 / 无记录）返回 null 不显示。 */
function sparkOf(row) {
  const item = sparkMap.value[row.peer_user_id]
    || sparkMap.value[row.sec_uid]
    || sparkMap.value[row.conversation_id]
    || (row.nickname && sparkMap.value[`n:${row.nickname}`])
  if (!item || !item.active) return null
  const days = item.days || 0
  // 平台渲染文案优先（如「点亮中 1/3」「6 天后消失」），缺失时回退天数标签
  // icon: on=橙色火焰（今日已续）/ off=灰色火焰（未续与濒危）
  const text = String(item.text || '').trim()
  if (item.state === 3) {
    return { type: 'danger', icon: 'off', label: text || `濒危 ${days} 天`, title: text || '火花濒危，今日续上可恢复' }
  }
  if (item.state === 2) {
    return { type: 'warning', icon: 'off', label: text || `待续 ${days} 天`, title: '今日尚未续火花' }
  }
  return { type: 'success', icon: 'on', label: text || `点亮中 ${days} 天`, title: '今日已续火花' }
}

function applyResult(result, { warnUnnamed = false } = {}) {
  data.value = result
  const available = new Set((result.friends || []).filter(row => row.sendable).map(row => row.nickname))
  picked.value = (props.selected || []).filter(name => available.has(name))
  if (warnUnnamed && props.mode === 'select' && result.unnamed_count) {
    ElMessage.warning(`${result.unnamed_count} 个会话没有取到昵称，无法作为发送目标`)
  }
}

async function loadCache() {
  if (!props.account?.id) return
  loading.value = true
  error.value = ''
  try {
    const result = await api.listFriendsCache(props.account.id)
    if (!data.value) applyResult(result)
  } catch (err) {
    error.value = err?.response?.data?.error || '读取好友缓存失败'
  } finally {
    loading.value = false
  }
}

/** 启动后台拉取：立即返回，轮询任务状态；窗口关闭后任务照常执行。 */
async function refresh() {
  if (!props.account?.id || jobRunning.value) return
  error.value = ''
  try {
    await api.startFriendsRefresh(props.account.id)
    jobRunning.value = true
    emitJobStatus('running')
    ElMessage.success('已开始后台拉取好友，可关闭窗口稍后回来查看')
    startPolling()
  } catch (err) {
    error.value = err?.response?.data?.error || '发起拉取失败，请检查账号登录态'
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => syncJobStatus(), 2500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function syncJobStatus(initial = false) {
  if (!props.account?.id) return
  try {
    const job = await api.getFriendsJob(props.account.id)
    if (job.status === 'running') {
      jobRunning.value = true
      if (!pollTimer) startPolling()
      emitJobStatus('running')
      return
    }
    stopPolling()
    if (jobRunning.value || initial) {
      jobRunning.value = false
      emitJobStatus('idle')
    }
    if (job.status === 'success' && job.result) {
      applyResult(job.result, { warnUnnamed: true })
      if (job.result.spark) {
        // 合并采样：火花随好友一起返回，直接用结果建索引，不再额外请求抖音
        sparkMap.value = buildSparkMap(job.result.spark)
      } else {
        loadSparkCache()
      }
      if (job.finished_at && Date.now() - new Date(job.finished_at).getTime() < 10_000) {
        const spark = job.result.spark
        const sparkTip = spark && spark.count
          ? `，火花 ${spark.count} 个有记录（今日待续 ${spark.pending_today}）`
          : ''
        ElMessage.success(`已拉取 ${job.result.count} 个好友，已保留为缓存${sparkTip}`)
      }
    } else if (job.status === 'error') {
      error.value = job.error || '后台拉取好友失败'
    }
  } catch {
    // 轮询失败不打断界面，下一轮重试
  }
}

function emitJobStatus(status) {
  if (props.account?.id) {
    emit('job-status', { accountId: props.account.id, status })
  }
}

function formatTime(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = num => String(num).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function toggle(nickname, sendable, value) {
  if (!sendable || !nickname) return
  const checked = value === undefined ? !picked.value.includes(nickname) : value
  if (checked) {
    if (!picked.value.includes(nickname)) picked.value.push(nickname)
  } else {
    picked.value = picked.value.filter(item => item !== nickname)
  }
}

function confirmPick() {
  emit('confirm', [...picked.value])
  emit('update:modelValue', false)
}

function copyOne(name) {
  writeClipboard(name).then(() => ElMessage.success(`已复制：${name}`))
}

function copyAll() {
  writeClipboard(names.value.join('\n')).then(() => ElMessage.success(`已复制 ${names.value.length} 个昵称`))
}

function writeClipboard(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text))
  }
  return Promise.resolve(fallbackCopy(text))
}

function fallbackCopy(text) {
  const area = document.createElement('textarea')
  area.value = text
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    document.execCommand('copy')
  } finally {
    document.body.removeChild(area)
  }
}

function shortSecUid(value) {
  const text = String(value || '')
  return text.length > 14 ? `${text.slice(0, 14)}…` : text
}
</script>

<style scoped>
.friends-body{display:grid;gap:10px;grid-template-columns:minmax(0,1fr)}
.account-bar{display:flex;align-items:center;gap:12px;padding:11px 14px;border:1px solid #e2e8f0;border-radius:14px;background:linear-gradient(145deg,#fff,#f8fafc)}
.account-avatar{flex:none;background:#e0e7ff;color:#4338ca;font-weight:600}
.account-meta{min-width:0;flex:1;display:grid;gap:3px}
.account-name{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.account-name strong{font-size:14px;color:#0f172a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.account-uid{color:#94a3b8;font-size:11px;font-family:ui-monospace,Consolas,monospace}
.bar-right{display:flex;align-items:center;gap:12px;flex:none}
.account-stats{display:flex;gap:12px;flex:none}
.status-line{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.stat{display:flex;align-items:baseline;gap:3px;color:#64748b;font-size:11px}
.stat b{font-size:17px;color:#0f172a;font-weight:700}
.stat.ok b{color:#16a34a}
.stat.warn b{color:#d97706}
.tip-text{margin:0;color:#64748b;font-size:12px}
.friend-list{display:grid;gap:6px;max-height:50vh;overflow:auto;min-height:120px;align-content:start}
.friend-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border:1px solid #e2e8f0;border-radius:12px;background:linear-gradient(145deg,#fff,#f8fafc)}
.friend-row.disabled{opacity:.62}
.friend-avatar{flex:none;background:#ede9fe;color:#6d28d9;font-weight:600}
.friend-main{min-width:0;flex:1;display:grid;gap:2px}
.friend-main strong{font-size:13px;color:#0f172a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.friend-main small{color:#94a3b8;font-size:11px}
.uid-tag{flex:none;max-width:180px;overflow:hidden;text-overflow:ellipsis;font-family:ui-monospace,Consolas,monospace}
.spark-tag{flex:none;font-weight:600}
.spark-tag :deep(.el-tag__content){display:inline-flex;align-items:center}
.spark-icon{width:14px;height:14px;flex:none;min-width:14px;object-fit:contain;display:block;margin-right:3px}

/* 手机端：窄屏下单行塞不下「昵称 + 火花 + UID + 复制」，改成栅格——
   头像跨两行，昵称在上、火花/UID 在下，复制按钮独立占右上角，不再互相挤到截断 */
@media(max-width:560px){
  .account-bar{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:start;gap:8px 10px;padding:11px 12px}
  .account-avatar{grid-area:1/1/3/2}
  .account-meta{grid-area:1/2/3/3}
  .bar-right{grid-area:3/1/4/3;justify-content:space-between;width:100%}
  .account-stats{gap:10px}
  .tip-text{font-size:11px;line-height:1.55}
  .friend-list{max-height:54vh}
  .friend-row{display:grid;grid-template-columns:auto auto minmax(0,1fr) auto;align-items:center;gap:5px 9px;padding:9px 11px}
  .friend-row>.el-checkbox{grid-area:1/1/3/2}
  .friend-avatar{grid-area:1/2/3/3}
  .friend-main{grid-area:1/3/2/5}
  .uid-tag{grid-area:2/3/3/4;justify-self:start;max-width:100%}
  .spark-tag{grid-area:2/4/3/5;justify-self:end}
  .friend-row>.el-button{grid-area:1/4/2/5;justify-self:end;padding:0 10px}
}
</style>
