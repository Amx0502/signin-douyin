<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-head">
        <div><strong>运行记录</strong><small>每次续火执行的账号、目标与发送回执</small></div>
        <div class="head-actions">
          <el-select v-model="statusFilter" clearable placeholder="全部状态" style="width:120px" @change="load">
            <el-option label="成功" value="success" />
            <el-option label="部分成功" value="partial" />
            <el-option label="失败" value="failed" />
            <el-option label="跳过" value="skipped" />
          </el-select>
          <el-select v-model="accountFilter" clearable placeholder="全部账号" style="width:150px" @change="load">
            <el-option v-for="row in accounts" :key="row.id" :value="row.id" :label="row.display_name" />
          </el-select>
          <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
    </template>

    <div class="run-list">
      <el-empty v-if="!runs.length" description="暂无运行记录" :image-size="90" />
      <article v-for="row in runs" :key="row.id" class="run-row" @click="openDetail(row)">
        <span class="run-mark" :class="row.status"><el-icon><component :is="statusIcon(row.status)" /></el-icon></span>
        <div class="run-main">
          <div class="run-title">
            <strong>{{ row.task_name || '续火任务' }}</strong>
            <el-tag :type="statusTag(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </div>
          <p class="run-message">{{ row.message || '—' }}</p>
          <small>{{ row.account_name }} · {{ triggerLabel(row.trigger) }} · {{ formatTime(row.started_at) }}<template v-if="durationText(row)"> · <span :class="{ 'live-duration': isRunning(row) }">耗时 {{ durationText(row) }}</span></template></small>
        </div>
        <div class="run-side">
          <small class="run-counts">{{ countsText(row) }}</small>
          <el-icon class="run-arrow"><ArrowRight /></el-icon>
        </div>
      </article>
    </div>
  </el-card>

  <el-drawer v-model="detailVisible" title="运行记录详情" size="min(520px, 92vw)">
    <div v-if="currentRun" class="run-detail">
      <div class="detail-row"><span>任务</span><strong>{{ currentRun.task_name || '续火任务' }}</strong></div>
      <div class="detail-row"><span>抖音账号</span><strong>{{ currentRun.account_name || '未知账号' }}</strong></div>
      <div class="detail-row"><span>状态</span><el-tag :type="statusTag(currentRun.status)">{{ statusLabel(currentRun.status) }}</el-tag></div>
      <div class="detail-row"><span>触发方式</span><strong>{{ triggerLabel(currentRun.trigger) }}</strong></div>
      <div class="detail-row"><span>开始时间</span><strong>{{ formatFullTime(currentRun.started_at) }}</strong></div>
      <div class="detail-row"><span>结束时间</span><strong>{{ formatFullTime(currentRun.finished_at) }}</strong></div>
      <div v-if="durationText(currentRun)" class="detail-row"><span>总耗时</span><strong>{{ durationText(currentRun) }}</strong></div>
      <div class="detail-row"><span>发送统计</span><strong>{{ countsText(currentRun) }}</strong></div>
      <div class="detail-row"><span>结果说明</span><p>{{ currentRun.message || '—' }}</p></div>

      <div v-if="detailSent.length" class="detail-row">
        <span>发送明细</span>
        <div class="sent-list">
          <div v-for="(entry, index) in detailSent" :key="index" class="sent-item">
            <div class="sent-item-head">
              <span class="sent-target">{{ entry.target || `目标 ${index + 1}` }}</span>
              <div class="sent-head-actions">
                <el-tag :type="entry.skipped ? 'info' : entry.success ? 'success' : 'danger'" size="small">
                  {{ entry.dry_run ? '演练' : entry.skipped ? '已续跳过' : entry.success ? '成功' : '失败' }}
                </el-tag>
                <el-tooltip
                  v-if="!entry.success && !entry.dry_run && !entry.skipped"
                  content="只给这个好友重发一次（沿用当时的文案）"
                  placement="top"
                >
                  <el-button
                    link
                    type="primary"
                    :icon="RefreshRight"
                    :loading="retryingTarget === entry.target"
                    @click="retryTarget(entry.target)"
                  >重试</el-button>
                </el-tooltip>
              </div>
            </div>
            <p v-if="!entry.success && !entry.dry_run && entry.reason" class="sent-reason">{{ entry.reason }}</p>
            <p v-if="entry.message" class="sent-message">{{ entry.message }}</p>
            <el-image
              v-if="shotSrc(entry.screenshot)"
              :src="shotSrc(entry.screenshot)"
              fit="cover"
              class="sent-shot sent-shot-click"
              @click="openShot(entry.screenshot)"
            >
              <template #error><span class="sent-shot-error">截图加载失败</span></template>
            </el-image>
            <span v-else-if="detailLoadingShot" class="sent-shot-loading">回执截图加载中…</span>
          </div>
        </div>
      </div>
      <div v-else class="detail-row"><span>发送明细</span><p>该记录未包含逐目标明细</p></div>
    </div>
    <!-- 回执截图查看器：与班级魔方同款（缩放/1:1/旋转/拖拽） -->
    <ImageViewerOverlay v-model="shotViewerVisible" :urls="shotViewerUrls" />
  </el-drawer>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowRight, CircleCheck, CircleClose, Clock, Minus, RefreshRight } from '@element-plus/icons-vue'
import ImageViewerOverlay from '../common/ImageViewerOverlay.vue'
import api from '../../api/douyin.js'

const props = defineProps({
  accounts: { type: Array, default: () => [] },
})

const loading = ref(false)
const runs = ref([])
const statusFilter = ref('')
const accountFilter = ref('')
const detailVisible = ref(false)
const currentRun = ref(null)
const detailLoadingShot = ref(false)
const retryingTarget = ref('')
// 回执截图查看器：与班级魔方同款（缩放 / 1:1 / 旋转 / 拖拽）
const shotViewerVisible = ref(false)
const shotViewerUrls = ref([])
// 实时时钟：驱动运行中记录的已耗时长跳动
const now = ref(Date.now())
let tickTimer = null

const detailSent = computed(() => {
  const sent = currentRun.value?.response_summary?.sent
  return Array.isArray(sent) ? sent : []
})

// 查看器 Teleport 到 body：抽屉关闭/切换记录时同步收起，避免残留遮罩
watch(detailVisible, visible => {
  if (!visible) shotViewerVisible.value = false
})

// 单目标重试：只给这个好友重发一次，成功后刷新列表即可看到新记录
async function retryTarget(target) {
  if (!currentRun.value?.id || !target) return
  retryingTarget.value = target
  try {
    const result = await api.retryRunTarget(currentRun.value.id, target)
    const ok = result?.response_summary?.sent?.[0]?.success
    if (ok) ElMessage.success(`「${target}」已重新发送成功`)
    else ElMessage.error(result?.message || `「${target}」重试未完成`)
    await load()
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || error?.message || '重试失败')
  } finally {
    retryingTarget.value = ''
  }
}

async function load() {
  loading.value = true
  try {
    // 空值不下发：后端 account_id 是 int 查询参数，空串会 422
    const params = { limit: 200 }
    if (statusFilter.value) params.status = statusFilter.value
    if (accountFilter.value) params.account_id = accountFilter.value
    runs.value = await api.listRuns(params)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  load()
  // 运行中记录的耗时需要每秒跳动；无运行记录时时钟照走，开销可忽略
  tickTimer = setInterval(() => { now.value = Date.now() }, 1000)
})
onBeforeUnmount(() => {
  if (tickTimer) clearInterval(tickTimer)
  tickTimer = null
})
defineExpose({ load })

function openDetail(row) {
  currentRun.value = row
  detailVisible.value = true
  // 列表接口剥离了回执截图（控制响应体积），打开抽屉时拉全量详情
  detailLoadingShot.value = true
  api.getRun(row.id)
    .then(full => { if (detailVisible.value) currentRun.value = full || row })
    .catch(() => {})
    .finally(() => { detailLoadingShot.value = false })
}

function shotSrc(value) {
  const shot = String(value || '')
  if (!shot) return ''
  return shot.startsWith('data:') || shot.startsWith('http') ? shot : `data:image/jpeg;base64,${shot}`
}

function openShot(value) {
  const src = shotSrc(value)
  if (!src) return
  shotViewerUrls.value = [src]
  shotViewerVisible.value = true
}

function countsText(row) {
  const summary = row.response_summary || {}
  if (summary.dry_run) return `演练 ${summary.sent?.length || 0} 个目标`
  return `成功 ${summary.sent_ok ?? 0} · 失败 ${summary.sent_fail ?? 0}`
}

function statusTag(status) {
  return { success: 'success', partial: 'warning', failed: 'danger', skipped: 'info', running: 'primary' }[status] || 'info'
}

function statusLabel(status) {
  return { success: '成功', partial: '部分成功', failed: '失败', skipped: '跳过', running: '运行中' }[status] || status
}

function statusIcon(status) {
  return { success: CircleCheck, partial: Clock, failed: CircleClose, skipped: Minus, running: Clock }[status] || Clock
}

function triggerLabel(trigger) {
  return { scheduler: '定时', manual: '手动', batch: '批量' }[trigger] || trigger
}

function isRunning(row) {
  return row?.status === 'running'
}

function durationText(row) {
  if (!row.started_at) return ''
  const start = new Date(row.started_at).getTime()
  if (!Number.isFinite(start)) return ''
  let end = row.finished_at ? new Date(row.finished_at).getTime() : NaN
  if (!Number.isFinite(end)) {
    // 运行中：以当前时刻为终点实时计算，随 now 每秒刷新
    if (!isRunning(row)) return ''
    end = now.value
  }
  const seconds = Math.round((end - start) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

function formatTime(value) {
  if (!value) return ''
  return String(value).replace('T', ' ').slice(5, 19)
}

function formatFullTime(value) {
  if (!value) return '—'
  return String(value).replace('T', ' ').slice(0, 19)
}
</script>

<style scoped>
.panel{border:1px solid rgb(191 219 254 / 58%);border-radius:22px;background:rgb(255 255 255 / 84%);box-shadow:0 18px 42px rgb(15 23 42 / 7%);backdrop-filter:blur(18px)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.panel-head strong,.panel-head small{display:block}
.panel-head small{margin-top:4px;color:#64748b;font-size:11px}
.head-actions{display:flex;gap:8px;align-items:center}
.head-actions .el-button+.el-button{margin-left:0}
.run-list{display:grid;gap:10px}
.run-row{display:flex;align-items:center;gap:13px;padding:13px 15px;border:1px solid #e2e8f0;border-radius:15px;background:linear-gradient(145deg,#fff,#f8fafc);cursor:pointer;transition:.18s ease}
.run-row:hover{border-color:#93c5fd;background:#f8fbff;transform:translateY(-1px)}
.run-mark{display:grid;width:42px;height:42px;flex:none;place-items:center;color:#64748b;border-radius:13px;background:#e2e8f0;font-size:20px}
.run-mark.success{color:#059669;background:#d1fae5}
.run-mark.partial,.run-mark.running{color:#d97706;background:#fef3c7}
.run-mark.failed{color:#dc2626;background:#fee2e2}
.run-mark.skipped{color:#64748b;background:#e2e8f0}
.run-main{min-width:0;flex:1}
.run-title{display:flex;align-items:center;gap:8px;min-width:0}
.run-title strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.run-message{margin:5px 0 4px;color:#475569;font-size:12px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.run-main small{color:#64748b;font-size:11px}
.live-duration{color:#2563eb;font-weight:600}
.live-duration::before{content:'';display:inline-block;width:6px;height:6px;margin-right:4px;border-radius:50%;background:#3b82f6;vertical-align:1px;animation:live-pulse 1.2s ease-in-out infinite}
@keyframes live-pulse{0%,100%{opacity:1}50%{opacity:.25}}
.run-side{display:flex;align-items:center;gap:10px;flex:none}
.run-counts{color:#64748b;font-size:11px;white-space:nowrap}
.run-arrow{flex:none;color:#94a3b8}
.run-detail{display:grid;gap:12px}
.detail-row{display:grid;grid-template-columns:90px minmax(0,1fr);align-items:start;gap:10px;padding:12px 14px;border:1px solid #e2e8f0;border-radius:13px;background:#f8fafc}
.detail-row>span{color:#64748b;font-size:12px}
.detail-row strong,.detail-row p{margin:0;overflow-wrap:anywhere;font-size:13px;color:#0f172a}
.detail-row .el-tag{justify-self:start}
.sent-list{display:grid;gap:8px;min-width:0}
.sent-item{padding:10px 12px;border:1px solid #e2e8f0;border-radius:10px;background:#fff}
.sent-item-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.sent-head-actions{display:flex;align-items:center;gap:8px;flex:none}
.sent-head-actions .el-button{margin:0}
.sent-target{font-weight:600;color:#0f172a;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sent-reason{margin:6px 0 0;padding:6px 9px;border-radius:8px;background:#fef2f2;color:#b91c1c;font-size:11px;line-height:1.5;overflow-wrap:anywhere}
.sent-message{margin:6px 0 0;color:#64748b;font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.sent-shot{display:block;width:100%;max-height:260px;margin-top:8px;border:1px solid #e2e8f0;border-radius:10px;cursor:zoom-in}
.sent-shot-click{cursor:zoom-in}
.sent-shot-error,.sent-shot-loading{display:block;margin-top:6px;color:#94a3b8;font-size:11px}
@media(max-width:750px){
  .panel-head{align-items:stretch;flex-direction:column}
  .head-actions .el-select{flex:1}
  .head-actions .el-button{flex:1;justify-content:center;margin-left:0}
  .run-row{display:grid;grid-template-columns:38px minmax(0,1fr);align-items:start;gap:10px;padding:11px 12px;border-radius:13px}
  .run-mark{width:38px;height:38px;border-radius:11px;font-size:18px}
  .run-main{grid-column:2}
  .run-side{grid-column:2;justify-content:space-between}
  .run-arrow{display:none}
  /* 详情抽屉：标签置顶、内容独占一行，避免 90px 标签列挤压溢出 */
  .run-detail{gap:8px;padding:0 2px}
  .detail-row{display:block;padding:10px 12px;border-radius:11px}
  .detail-row>span{display:block;margin-bottom:4px;font-size:11px}
  .detail-row strong,.detail-row p{font-size:13px;line-height:1.5}
  .sent-item{padding:9px 11px}
  .sent-item-head{flex-wrap:wrap;row-gap:4px}
  .sent-target{max-width:60%}
  .sent-message{font-size:12px}
}
</style>
