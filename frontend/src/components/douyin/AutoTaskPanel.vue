<template>
  <el-card class="panel" shadow="never">
    <template #header>
      <div class="panel-head">
        <div><strong>自动任务</strong><small>按计划时间给目标好友发送续火消息，模板池随机、相邻不重复</small></div>
        <div class="head-actions">
          <el-button :icon="Refresh" :loading="loading" @click="emit('refresh')">刷新</el-button>
          <el-button type="primary" :icon="Plus" @click="openCreate">新建任务</el-button>
        </div>
      </div>
    </template>

    <div class="task-list">
      <article v-for="row in tasks" :key="row.id" class="task-card">
        <div class="card-body">
          <div class="card-top">
            <div class="task-title">
              <span class="task-badge"><el-icon><Timer /></el-icon></span>
              <strong class="task-name" :class="{ 'is-disabled': !row.enabled }">{{ row.name }}</strong>
            </div>
            <el-switch
              size="small"
              :model-value="row.enabled"
              inline-prompt
              active-text="启"
              inactive-text="停"
              class="task-toggle"
              @change="value => toggle(row, value)"
            />
          </div>
          <div class="meta-row">
            <span class="chip chip-account"><el-icon><User /></el-icon>{{ accountName(row.account_id) }}</span>
            <span class="chip chip-time"><el-icon><Clock /></el-icon>{{ row.schedule_times.join('、') }}</span>
            <span class="chip chip-date"><el-icon><Calendar /></el-icon>{{ schedulePlanSummary(row) }}</span>
            <span class="chip chip-tpl"><el-icon><ChatLineRound /></el-icon>{{ row.message_variants.length ? `自定义 ${row.message_variants.length} 条` : '全局模板池' }}</span>
            <span v-if="row.notify_wecom" class="chip chip-notify"><el-icon><Bell /></el-icon>企微通知</span>
          </div>
          <div class="targets-line" :title="row.targets.join('、')">
            <el-icon><Aim /></el-icon>
            <span class="targets-text">{{ row.targets.join('、') }}</span>
          </div>
        </div>
        <div class="card-foot">
          <el-button size="small" type="primary" plain :icon="CaretRight" :loading="runningId === row.id" @click="run(row)">立即执行</el-button>
          <el-button size="small" :icon="EditPen" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" plain :icon="Delete" @click="remove(row)">删除</el-button>
        </div>
      </article>
      <el-empty class="task-empty" v-if="!tasks.length" :description="accounts.length ? '还没有任务，点击右上角新建' : '还没有任务，可先添加抖音账号再新建任务查看表单'" :image-size="70" />
    </div>

    <el-dialog v-model="editorVisible" :title="editingId ? '编辑任务' : '新建任务'" width="min(640px, 94vw)" align-center append-to-body>
      <el-form label-width="110px" class="editor-form">
        <el-form-item label="任务名称"><el-input v-model="form.name" placeholder="如：每日续火" /></el-form-item>
        <el-form-item label="抖音账号">
          <el-select v-model="form.account_id" style="width: 100%" no-data-text="暂无抖音账号，请先到「账号」页添加">
            <el-option v-for="row in accounts" :key="row.id" :value="row.id" :label="row.display_name" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标好友">
          <div class="targets-field">
            <el-select
              v-model="form.targets"
              multiple
              filterable
              allow-create
              default-first-option
              no-data-text="输入好友昵称后回车"
              placeholder="输入好友昵称/备注，回车添加"
            />
            <el-button
              :icon="User"
              :disabled="!form.account_id"
              @click="openFriends"
            >
              拉取好友
            </el-button>
          </div>
          <div v-if="form.targets.length" class="target-specs">
            <div class="target-specs__head">
              <span><el-icon><ChatDotRound /></el-icon>专属文案（可选）</span>
              <small>留空则走下方模板池随机；改名容错可在昵称里写「主名=别名」</small>
            </div>
            <div v-for="(row, index) in specRows" :key="index" class="target-spec">
              <span class="target-spec__index">{{ index + 1 }}</span>
              <el-input
                v-model="row.name"
                class="target-spec__name"
                placeholder="好友昵称（可写 主名=别名）"
                @input="syncSpec(index)"
              />
              <el-input
                v-model="row.message"
                class="target-spec__msg"
                type="textarea"
                :rows="1"
                :autosize="{ minRows: 1, maxRows: 4 }"
                placeholder="给这位好友的专属文案，留空走模板池"
                @input="syncSpec(index)"
              />
            </div>
          </div>
          <p class="tip-text">
            可从该账号的好友列表中勾选，也可手动输入昵称；建议先和该好友聊过一句，会话更易命中。
          </p>
        </el-form-item>
        <el-form-item label="每日执行时间">
          <div class="schedule-list">
            <div v-for="(_, index) in form.schedule_times" :key="index" class="schedule-row">
              <el-time-picker :editable="false" v-model="form.schedule_times[index]" value-format="HH:mm:ss" format="HH:mm:ss" placeholder="执行时间" />
              <el-button type="danger" plain :disabled="form.schedule_times.length === 1" @click="form.schedule_times.splice(index, 1)">删除</el-button>
            </div>
            <el-button plain @click="form.schedule_times.push('08:00:00')">添加执行时间</el-button>
          </div>
        </el-form-item>
        <el-form-item label="火花预检">
          <el-checkbox v-model="form.skip_sent_today">今日已续过的好友跳过</el-checkbox>
          <p class="tip-text">按本地记录判断：今天已经成功续过的好友不再重复打扰，只补发失败的。关闭则每次都照发。</p>
        </el-form-item>
        <el-form-item v-if="isAdmin" label="高级选项">
          <el-checkbox v-model="form.notify_wecom">发送企业微信通知</el-checkbox>
        </el-form-item>
        <el-form-item label="消息模板">
          <el-input
            v-model="templateText"
            type="textarea"
            :rows="5"
            placeholder="留空使用全局模板池；占位符 [API] 一言、[data] 日期、[weekday] 星期；每条模板空一行分隔，模板内可写多行"
          />
          <p class="tip-text">这里是任务通用文案池：没有配置专属文案的好友从这里随机选一条（相邻不重复、避开最近发过的）。模板之间<strong>空一行</strong>分隔，单条模板内部换行会原样发送；占位符 <code>[API]</code> 一言、<code>[data]</code> 日期、<code>[weekday]</code> 星期。想给指定好友单独配文案，直接在上方「专属文案」里填。</p>
          <div class="global-pool">
            <div class="global-pool__head">
              <span>全局模板池 · 点击选用，已选 {{ pickedPoolCount }} 条，内容同步到上方输入框</span>
              <el-button link type="primary" size="small" @click="loadGlobalPool">刷新</el-button>
            </div>
            <div class="pool-list">
              <button
                v-for="(item, index) in globalVariants"
                :key="index"
                type="button"
                class="pool-item"
                :class="{ 'is-selected': poolHas(item) }"
                :title="item"
                @click="togglePoolItem(item)"
              >
                <span class="pool-check">{{ poolHas(item) ? '✓' : '' }}</span>
                <span class="pool-text">{{ item }}</span>
              </button>
            </div>
            <p v-if="!globalVariants.length" class="tip-text">全局模板池暂无内容</p>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editorVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <FriendsDialog
      v-model="friendsVisible"
      :account="friendsAccount"
      mode="select"
      :selected="form.targets || []"
      @confirm="onFriendsConfirm"
    />
  </el-card>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Aim, Bell, Calendar, CaretRight, ChatDotRound, ChatLineRound,
  Clock, Delete, EditPen, Plus, Refresh, Timer, User,
} from '@element-plus/icons-vue'
import api from '../../api/douyin.js'
import FriendsDialog from './FriendsDialog.vue'
import { formatTemplates, parseTemplates } from '../../utils/douyinTemplates.js'
import { schedulePlanSummary } from '../../utils/taskSchedule.js'

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  accounts: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['refresh'])

const loggedUser = JSON.parse(localStorage.getItem('user') || '{}')
const isAdmin = loggedUser.role === 'admin'

const editorVisible = ref(false)
const editingId = ref(null)
const saving = ref(false)
const runningId = ref(null)
const templateText = ref('')
const globalVariants = ref([])
const form = reactive({})

const friendsVisible = ref(false)
const friendsAccount = ref(null)

function openFriends() {
  const row = props.accounts.find(item => item.id === form.account_id)
  if (!row) return ElMessage.warning('请先选择抖音账号')
  friendsAccount.value = row
  friendsVisible.value = true
}

function onFriendsConfirm(names) {
  const merged = [...(form.targets || [])]
  names.forEach(name => {
    if (name && !merged.includes(name)) merged.push(name)
  })
  form.targets = merged
  ElMessage.success(`已添加 ${names.length} 个好友到目标列表`)
}

function defaultForm() {
  return {
    account_id: props.accounts[0]?.id,
    name: '',
    enabled: true,
    targets: [],
    message_variants: [],
    schedule_times: ['08:00:00'],
    delay_min_seconds: 60,
    delay_max_seconds: 180,
    skip_sent_today: true,
    start_date: null,
    end_date: null,
    skip_weekends: false,
    date_mode: 'daily',
    run_dates: [],
    skip_dates: [],
    auto_disable_after_finish: false,
    notify_wecom: true,
  }
}

function openCreate() {
  Object.assign(form, defaultForm())
  editingId.value = null
  templateText.value = ''
  loadGlobalPool()
  editorVisible.value = true
}

function openEdit(row) {
  Object.assign(form, defaultForm(), { ...row, targets: [...(row.targets || [])] }, {
    schedule_times: row.schedule_times?.length ? [...row.schedule_times] : ['08:00:00'],
    delay_min_seconds: row.delay_min_seconds ?? null,
    delay_max_seconds: row.delay_max_seconds ?? null,
    skip_sent_today: row.skip_sent_today !== false,
    run_dates: [...(row.run_dates || [])],
    skip_dates: [...(row.skip_dates || [])],
  })
  editingId.value = row.id
  templateText.value = formatTemplates(row.message_variants)
  loadGlobalPool()
  editorVisible.value = true
}

async function loadGlobalPool() {
  try {
    const result = await api.getBuiltinMessages()
    globalVariants.value = result?.variants || []
  } catch {
    globalVariants.value = []
  }
}

// 输入框内容即任务文案来源；点选模板池条目 = 往解析结果里增删该条，再格式化回填
const parsedVariants = computed(() => parseTemplates(templateText.value))
const pickedPoolCount = computed(
  () => globalVariants.value.filter(item => parsedVariants.value.includes(item)).length,
)
function poolHas(item) {
  return parsedVariants.value.includes(item)
}
function togglePoolItem(item) {
  const list = parsedVariants.value
  const next = poolHas(item) ? list.filter(v => v !== item) : [...list, item]
  templateText.value = formatTemplates(next)
}

// -------------------------------------------------------------- 专属文案：form.targets 存「主名=别名|文案」合成串，
// specRows 把它拆成「名称 / 文案」两列便于编辑；外部（多选框增删/编辑回填）改 targets 时全量重建
const specRows = ref([])
let guardTargetsWatch = false

watch(
  () => (form.targets || []).slice(),
  () => {
    if (guardTargetsWatch) {
      guardTargetsWatch = false
      return
    }
    specRows.value = (form.targets || []).map(spec => {
      const text = String(spec || '')
      const cut = text.indexOf('|')
      return {
        name: cut >= 0 ? text.slice(0, cut) : text,
        message: cut >= 0 ? text.slice(cut + 1) : '',
      }
    })
  },
  { immediate: true },
)

function syncSpec(index) {
  const row = specRows.value[index]
  if (!row) return
  const name = String(row.name || '').trim()
  const message = String(row.message || '').trim()
  guardTargetsWatch = true
  form.targets[index] = message ? `${name}|${message}` : name
}

async function save() {
  if (!form.name?.trim()) return ElMessage.warning('请填写任务名称')
  if (!form.account_id) return ElMessage.warning('请选择抖音账号')
  form.targets = form.targets.map(t => String(t || '').trim()).filter(Boolean)
  if (!form.targets.length) return ElMessage.warning('至少添加一个目标好友')
  if (!form.schedule_times.length) return ElMessage.warning('至少设置一个执行时间')
  const low = Number(form.delay_min_seconds || 0)
  const high = Number(form.delay_max_seconds || 0)
  if (low > high) return ElMessage.warning('随机延迟的最小值不能大于最大值')
  form.message_variants = parseTemplates(templateText.value)
  saving.value = true
  try {
    if (editingId.value) {
      await api.updateTask(editingId.value, form)
    } else {
      await api.createTask(form)
    }
    ElMessage.success('任务已保存')
    editorVisible.value = false
    emit('refresh')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggle(row, value) {
  try {
    await api.updateTask(row.id, { ...row, enabled: value })
    emit('refresh')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || '操作失败')
  }
}

async function run(row) {
  runningId.value = row.id
  try {
    await api.runTask(row.id)
    ElMessage.success('已提交执行，可在运行记录查看结果')
    emit('refresh')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || '提交失败')
  } finally {
    runningId.value = null
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除任务「${row.name}」？`, '删除任务', { type: 'warning' })
  await api.deleteTask(row.id)
  ElMessage.success('已删除')
  emit('refresh')
}

function accountName(id) {
  const row = props.accounts.find(item => item.id === id)
  return row?.display_name || `账号 ${id}`
}
</script>

<style scoped>
.panel{border:1px solid rgb(191 219 254 / 58%);border-radius:22px;background:rgb(255 255 255 / 84%);box-shadow:0 18px 42px rgb(15 23 42 / 7%);backdrop-filter:blur(18px)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.panel-head strong,.panel-head small{display:block}
.panel-head small{margin-top:4px;color:#64748b;font-size:11px}
.head-actions{display:flex;gap:8px}
.head-actions .el-button+.el-button{margin-left:0}
/* 桌面端宽屏一行放多张卡，避免单卡被拉成整屏宽后大片留白 */
.task-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(400px,100%),1fr));gap:12px;align-items:stretch}
.task-empty{grid-column:1/-1}
.task-card{position:relative;display:flex;flex-direction:column;border:1px solid #e2e8f0;border-left:3px solid #3b82f6;border-radius:14px;background:#fff;overflow:hidden;transition:box-shadow .18s ease,transform .18s ease}
.task-card:hover{box-shadow:0 10px 26px rgb(59 130 246 / 14%);transform:translateY(-1px)}
.card-body{flex:1;display:grid;gap:8px;padding:12px 14px}
.card-top{display:flex;align-items:center;justify-content:space-between;gap:10px}
.task-title{display:flex;align-items:center;gap:9px;min-width:0}
.task-badge{display:inline-flex;flex:none;align-items:center;justify-content:center;width:30px;height:30px;border-radius:10px;background:#eff6ff;color:#2563eb}
.task-badge .el-icon{font-size:16px}
.task-name{font-size:14px;font-weight:600;color:#0f172a;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-name.is-disabled{color:#94a3b8}
.meta-row{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:4px;max-width:100%;padding:3px 9px;border-radius:999px;font-size:11px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .el-icon{font-size:12px;flex:none}
.chip-account{background:#eff6ff;color:#1d4ed8}
.chip-time{background:#f5f3ff;color:#6d28d9}
.chip-date{background:#ecfeff;color:#0e7490}
.chip-tpl{background:#fff7ed;color:#c2410c}
.chip-notify{background:#ecfdf5;color:#047857}
.targets-line{display:flex;align-items:center;gap:6px;padding:6px 10px;border-radius:9px;background:#f8fafc;font-size:12px;color:#334155;line-height:1.5}
.targets-line .el-icon{flex:none;color:#64748b;font-size:13px}
.targets-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-foot{display:flex;gap:6px;padding:8px 10px;border-top:1px solid #f1f5f9;background:#fbfdff}
.card-foot .el-button+.el-button{margin-left:0}
.target-specs{display:grid;gap:6px;width:100%;margin-top:8px;padding:10px;border:1px solid #e0eaff;border-radius:12px;background:#f8fbff}
.target-specs__head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;flex-wrap:wrap}
.target-specs__head span{display:inline-flex;align-items:center;gap:5px;color:#1d4ed8;font-size:12px;font-weight:600}
.target-specs__head span .el-icon{font-size:13px}
.target-specs__head small{color:#94a3b8;font-size:11px}
.target-spec{display:grid;grid-template-columns:auto minmax(0,2fr) minmax(0,3fr);align-items:center;gap:7px}
.target-spec__index{display:inline-flex;align-items:center;justify-content:center;flex:none;width:18px;height:18px;border-radius:50%;background:#dbeafe;color:#1d4ed8;font-size:11px}
.target-spec__name :deep(.el-input__wrapper),
.target-spec__msg :deep(.el-textarea__inner){background:#fff}
.schedule-list{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;width:100%;min-width:0}
.schedule-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;width:100%;min-width:0}
.schedule-row :deep(.el-date-editor.el-input){width:100%!important;min-width:0}
.targets-field{display:flex;align-items:center;gap:8px;width:100%}
.targets-field :deep(.el-select){flex:1;min-width:0}
.tip-text{margin:6px 0 0;color:#94a3b8;font-size:11px}
.global-pool{margin-top:10px;width:100%;border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;background:linear-gradient(145deg,#fff,#f8fafc)}
.global-pool__head{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#475569;font-size:12px;font-weight:600}
.pool-list{margin-top:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:6px;max-height:220px;overflow:auto;padding-right:2px}
.pool-item{display:flex;align-items:flex-start;gap:7px;text-align:left;border:1px solid #e2e8f0;background:#fff;border-radius:10px;padding:7px 10px;font-size:12px;color:#334155;cursor:pointer;line-height:1.5;width:100%;min-width:0}
.pool-item:hover{border-color:#60a5fa;background:#eff6ff}
.pool-item.is-selected{border-color:#3b82f6;background:#eff6ff;color:#1d4ed8}
.pool-check{flex:none;width:15px;height:15px;border:1px solid #cbd5e1;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:10px;color:#1d4ed8;margin-top:1px}
.pool-item.is-selected .pool-check{border-color:#3b82f6;background:#3b82f6;color:#fff}
.pool-text{flex:1;min-width:0;word-break:break-all;white-space:pre-wrap}
@media(max-width:750px){
  .panel-head{flex-direction:column;align-items:stretch;gap:10px}
  .panel-head small{display:none}
  .head-actions .el-button{flex:1;justify-content:center}
  .task-list{gap:8px}
  .card-body{padding:10px 12px;gap:6px}
  .card-foot{padding:7px 10px}
  .card-foot .el-button{flex:1;margin-left:0;padding:8px 0}
  .editor-form :deep(.el-form-item){display:block}
  .editor-form :deep(.el-form-item__label){display:block;width:auto!important;text-align:left;margin-bottom:4px}
  .editor-form :deep(.el-form-item__content){margin-left:0!important}
}
.task-toggle { --el-switch-width: 44px; --el-switch-off-color: #c9d5e5; }
.task-toggle :deep(.el-switch__inner) { font-size: 10px; font-weight: 600; }

.task-toggle { --el-switch-width: 44px; --el-switch-off-color: #c9d5e5; }
.task-toggle :deep(.el-switch__inner) { font-size: 10px; font-weight: 600; }

</style>
