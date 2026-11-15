<template>
  <el-card class="template-card">
    <template #header>
      <div class="setting-head">
        <span>消息模板设置</span>
        <el-tag :type="settings.using_builtin ? 'info' : 'success'" size="small">
          {{ settings.using_builtin ? '使用内置模板' : `自定义 ${settings.message_variants.length} 条` }}
        </el-tag>
      </div>
    </template>

    <p class="tip-text">
      从下面的文案列表<strong>逐个点选</strong>要使用的模板，再次点击取消；每次发送从中随机挑一条，并避开该好友上次收到的内容。
      每条文案右侧有<strong>编辑</strong>按钮，点击即可就地修改（支持多行，改内置文案会另存为自定义文案）；
      底部输入框支持一次添加多条（空行分隔）。一条不选则使用内置自然口语模板池。
    </p>

    <MessageTemplatePicker v-model="selectedVariants" :can-customize="isAdmin" />

    <div class="save-row">
      <el-button type="primary" :loading="saving" @click="saveSettings">保存模板</el-button>
      <el-button @click="resetSettings">放弃修改</el-button>
      <span class="builtin-hint">一条不选则使用内置自然口语模板池</span>
    </div>
  </el-card>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/douyin.js'
import MessageTemplatePicker from './MessageTemplatePicker.vue'

const settings = reactive({ message_variants: [], using_builtin: true })
const selectedVariants = ref([])
const saving = ref(false)
const loggedUser = JSON.parse(localStorage.getItem('user') || '{}')
const isAdmin = loggedUser.role === 'admin'

async function loadSettings() {
  const result = await api.getSettings()
  Object.assign(settings, result || {})
  selectedVariants.value = [...(settings.message_variants || [])]
}

async function saveSettings() {
  saving.value = true
  try {
    const variants = [...selectedVariants.value]
    const result = await api.updateSettings({ message_variants: variants, hitokoto_types: settings.hitokoto_types || [] })
    Object.assign(settings, result || {})
    selectedVariants.value = [...(result.message_variants || [])]
    ElMessage.success('消息模板已保存')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

function resetSettings() {
  selectedVariants.value = [...(settings.message_variants || [])]
}

onMounted(() => {
  loadSettings().catch(() => {})
})
</script>

<style scoped>
.template-card :deep(.el-card__body){display:grid;gap:0}
.setting-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.setting-head span{font-weight:600}
.tip-text{margin:0 0 10px;color:#64748b;font-size:12px}
.tip-text code{background:#f1f5f9;border-radius:4px;padding:1px 5px;color:#2563eb}
.save-row{display:flex;align-items:center;gap:10px;margin-top:12px}
.builtin-hint{color:#94a3b8;font-size:11px}
@media(max-width:750px){
  .save-row{flex-wrap:wrap}
  .save-row .el-button{flex:1}
  .builtin-hint{flex-basis:100%}
}
</style>
