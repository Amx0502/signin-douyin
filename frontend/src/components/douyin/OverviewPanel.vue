<template>
  <div class="overview-stack">
    <el-card>
      <template #header>
        <div class="setting-head">
          <span>企业微信机器人通知</span>
          <el-tag :type="notify.webhook_configured ? 'success' : 'info'" size="small">
            {{ notify.webhook_configured ? '已配置' : '未配置' }}
          </el-tag>
        </div>
      </template>
      <el-form v-if="isAdmin" label-position="top" class="notify-form">
        <el-form-item label="机器人 Webhook">
          <el-input
            v-model="notify.douyin_webhook_url"
            type="text"
            placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
          />
        </el-form-item>
        <div class="notify-actions">
          <el-button type="primary" :loading="savingNotify" @click="saveNotify">保存配置</el-button>
          <span class="builtin-hint">任务里勾选「发送企业微信通知」后，每次运行结束会推送到该机器人</span>
        </div>
      </el-form>
      <el-alert
        v-else
        :type="notify.webhook_configured ? 'success' : 'info'"
        :closable="false"
        :title="notify.webhook_configured ? '管理员已配置企业微信机器人' : '管理员尚未配置企业微信机器人'"
      />
    </el-card>

    <MessageTemplateCard v-if="isAdmin" />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/douyin.js'
import MessageTemplateCard from './MessageTemplateCard.vue'

const loggedUser = JSON.parse(localStorage.getItem('user') || '{}')
const isAdmin = computed(() => loggedUser.role === 'admin')

const notify = reactive({ douyin_webhook_url: '', webhook_configured: false })
const savingNotify = ref(false)

async function loadNotify() {
  const result = await api.getNotifySettings()
  Object.assign(notify, {
    douyin_webhook_url: result?.douyin_webhook_url || '',
    webhook_configured: Boolean(result?.webhook_configured),
  })
}

async function saveNotify() {
  savingNotify.value = true
  try {
    const result = await api.updateNotifySettings({ douyin_webhook_url: notify.douyin_webhook_url })
    Object.assign(notify, {
      douyin_webhook_url: result?.douyin_webhook_url || '',
      webhook_configured: Boolean(result?.webhook_configured),
    })
    ElMessage.success('企微通知配置已保存')
  } catch (error) {
    ElMessage.error(error?.response?.data?.error || error?.message || '保存失败')
  } finally {
    savingNotify.value = false
  }
}

onMounted(() => {
  loadNotify().catch(() => {})
})
</script>

<style scoped>
.overview-stack{display:grid;gap:16px;grid-template-columns:minmax(0,1fr)}
.setting-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.setting-head span{font-weight:600}
.notify-form{margin:0}
.notify-actions{display:flex;align-items:center;gap:10px}
.builtin-hint{color:#94a3b8;font-size:11px}
</style>
