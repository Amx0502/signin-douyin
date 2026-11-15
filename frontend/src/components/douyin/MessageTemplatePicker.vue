<template>
  <div class="tpl-picker">
    <div class="tpl-head">
      <span class="tpl-title">点选要使用的文案<template v-if="selected.length">（已选 {{ selected.length }} 条）</template></span>
      <el-button link type="primary" size="small" :loading="loading" @click="loadBuiltin">刷新</el-button>
    </div>

    <div class="tpl-list">
      <template v-for="(item, index) in builtinVariants" :key="`b-${index}`">
        <button
          type="button"
          class="tpl-item"
          :class="{ 'is-selected': isSelected(item), 'is-editing': canCustomize && editingKey === `b-${index}` }"
          :title="item"
          @click="toggleBuiltin(item)"
        >
          <span class="tpl-check">{{ isSelected(item) ? '✓' : '' }}</span>
          <span class="tpl-text">{{ item }}</span>
          <span v-if="canCustomize" class="tpl-op" @click.stop="startEdit(item, `b-${index}`)">编辑</span>
        </button>
        <div v-if="canCustomize && editingKey === `b-${index}`" class="edit-block">
          <el-input
            v-model="editingDraft"
            type="textarea"
            :rows="4"
            placeholder="可写多行，换行会原样发送；占位符 [API] 一言、[data] 日期、[weekday] 星期"
          />
          <div class="edit-actions">
            <el-button size="small" type="primary" :disabled="!editingDraft.trim()" @click="saveEdit">保存修改</el-button>
            <el-button size="small" @click="cancelEdit">取消</el-button>
          </div>
        </div>
      </template>
      <span v-if="!builtinVariants.length && !loading" class="tpl-empty">暂无内置文案</span>
    </div>

    <div v-if="customVariants.length" class="custom-block">
      <div class="tpl-title custom-head">自定义文案</div>
      <div class="tpl-list">
        <template v-for="(item, index) in customVariants" :key="`c-${index}`">
          <button
            type="button"
            class="tpl-item"
            :class="{ 'is-selected': canCustomize || isSelected(item), 'is-editing': canCustomize && editingKey === `c-${index}` }"
            :title="canCustomize ? '点击编辑这条文案' : item"
            @click="canCustomize ? startEdit(item, `c-${index}`) : toggleBuiltin(item)"
          >
            <span class="tpl-check">{{ canCustomize || isSelected(item) ? '✓' : '' }}</span>
            <span class="tpl-text">{{ item }}</span>
            <span v-if="canCustomize" class="tpl-op" @click.stop="startEdit(item, `c-${index}`)">编辑</span>
            <span v-if="canCustomize" class="tpl-op tpl-op--danger" @click.stop="removeCustom(item)">移除</span>
          </button>
          <div v-if="canCustomize && editingKey === `c-${index}`" class="edit-block">
            <el-input
              v-model="editingDraft"
              type="textarea"
              :rows="4"
              placeholder="可写多行，换行会原样发送；占位符 [API] 一言、[data] 日期、[weekday] 星期"
            />
            <div class="edit-actions">
              <el-button size="small" type="primary" :disabled="!editingDraft.trim()" @click="saveEdit">保存修改</el-button>
              <el-button size="small" @click="cancelEdit">取消</el-button>
            </div>
          </div>
        </template>
      </div>
    </div>

    <div v-if="canCustomize" class="add-block">
      <div class="tpl-title custom-head">添加自定义文案</div>
      <el-input
        v-model="customDraft"
        type="textarea"
        :rows="4"
        placeholder="输入要添加的文案，可写多行（换行会原样发送）；多条文案之间空一行分隔，可一次批量添加。占位符 [API] 一言、[data] 日期、[weekday] 星期"
      />
      <div class="edit-actions">
        <el-button size="small" type="primary" plain :disabled="!customDraft.trim()" @click="addCustom">添加</el-button>
        <span class="add-hint">单条模板内部换行会原样发送，模板之间空一行分隔</span>
      </div>
    </div>
    <p v-else class="tpl-hint">自定义文案仅管理员可添加，如需新文案请联系管理员</p>
    <p v-if="hint" class="tpl-hint">{{ hint }}</p>
  </div>
</template>

<script setup>
// 消息模板点选器：内置文案池点选切换；管理员每行右侧有「编辑」按钮，就地展开
// 多行 textarea 修改文案（改内置文案会另存为自定义文案并选中），添加入口为多行
// textarea，多条模板空行分隔可批量添加。
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/douyin.js'
import { parseTemplates } from '../../utils/douyinTemplates.js'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  hint: { type: String, default: '' },
  // 仅管理员可添加/编辑/移除文案；普通用户只能点选文案池
  canCustomize: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue'])

const builtinVariants = ref([])
const loading = ref(false)
const customDraft = ref('')
const editingKey = ref('')
const editingOriginal = ref('')
const editingDraft = ref('')

const selected = computed(() => props.modelValue || [])
const customVariants = computed(() => selected.value.filter(item => !builtinVariants.value.includes(item)))

function isSelected(item) {
  return selected.value.includes(item)
}

function emitSelected(list) {
  emit('update:modelValue', list)
}

function toggleBuiltin(item) {
  emitSelected(isSelected(item) ? selected.value.filter(v => v !== item) : [...selected.value, item])
}

function removeCustom(item) {
  if (editingOriginal.value === item) cancelEdit()
  emitSelected(selected.value.filter(v => v !== item))
}

function startEdit(item, key) {
  if (editingKey.value === key) return
  editingKey.value = key
  editingOriginal.value = item
  editingDraft.value = item
}

function saveEdit() {
  const original = editingOriginal.value
  const text = editingDraft.value.replace(/\r\n?/g, '\n').trim()
  if (!text || !editingKey.value) return
  if (text !== original && selected.value.includes(text)) {
    ElMessage.warning('这条文案已经在列表里了')
    return
  }
  // 原文案在已选列表里 → 原位替换；否则（编辑未选中的内置文案）→ 保存为新文案并选中
  const next = selected.value.includes(original)
    ? selected.value.map(v => (v === original ? text : v))
    : [...selected.value, text]
  emitSelected(next)
  cancelEdit()
  ElMessage.success('文案已修改，记得点「保存模板」生效')
}

function cancelEdit() {
  editingKey.value = ''
  editingOriginal.value = ''
  editingDraft.value = ''
}

function addCustom() {
  const items = parseTemplates(customDraft.value)
  if (!items.length) return
  const next = [...selected.value]
  let added = 0
  for (const text of items) {
    if (next.includes(text)) continue
    next.push(text)
    added += 1
  }
  if (!added) return ElMessage.warning('这些文案都已经在列表里了')
  emitSelected(next)
  customDraft.value = ''
  ElMessage.success(`已添加 ${added} 条文案，记得点「保存模板」生效`)
}

async function loadBuiltin() {
  loading.value = true
  try {
    const result = await api.getBuiltinMessages()
    builtinVariants.value = result?.variants || []
  } catch {
    builtinVariants.value = []
  } finally {
    loading.value = false
  }
}

onMounted(loadBuiltin)
</script>

<style scoped>
.tpl-picker{display:grid;gap:10px;width:100%;min-width:0}
.tpl-head{display:flex;align-items:center;justify-content:space-between;color:#475569;font-size:12px;font-weight:600}
.tpl-title{font-size:12px;font-weight:600;color:#475569}
.tpl-list{display:grid;gap:6px;max-height:240px;overflow:auto;padding-right:2px}
.tpl-item{display:flex;align-items:flex-start;gap:7px;text-align:left;border:1px solid #e2e8f0;background:linear-gradient(145deg,#fff,#f8fafc);border-radius:10px;padding:7px 10px;font-size:12px;color:#334155;cursor:pointer;line-height:1.5}
.tpl-item:hover{border-color:#60a5fa;background:#eff6ff}
.tpl-item:hover .tpl-op{color:#2563eb}
.tpl-item.is-selected{border-color:#3b82f6;background:#eff6ff;color:#1d4ed8}
.tpl-item.is-editing{border-color:#6366f1;background:#eef2ff}
.tpl-check{flex:none;width:15px;height:15px;border:1px solid #cbd5e1;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:10px;color:#1d4ed8;margin-top:1px}
.tpl-item.is-selected .tpl-check{border-color:#3b82f6;background:#3b82f6;color:#fff}
.tpl-text{flex:1;min-width:0;word-break:break-all;white-space:pre-wrap}
.tpl-op{flex:none;color:#94a3b8;font-size:11px}
.tpl-op:hover{color:#2563eb}
.tpl-op--danger:hover{color:#ef4444}
.custom-block{display:grid;gap:6px}
.custom-head{color:#6366f1}
.tpl-empty{color:#94a3b8;font-size:12px}
.add-block{display:grid;gap:6px}
.edit-block{display:grid;gap:6px;padding:8px;border:1px dashed #6366f1;border-radius:10px;background:#f8f9ff}
.edit-actions{display:flex;align-items:center;gap:8px}
.add-hint{color:#94a3b8;font-size:11px}
.tpl-hint{margin:0;color:#94a3b8;font-size:11px}
</style>
