<template>
  <div class="page-container">
    <LogConsole
      title="抖音日志"
      empty-text="暂无抖音日志"
      :logs="logs"
      :loading="loading"
      @refresh="refreshLogs"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import LogConsole from '../LogConsole.vue'
import api from '../../api/douyin.js'

const logs = ref([])
const loading = ref(false)

// 级别对齐 LogConsole 的 INFO/WARNING/ERROR 三档；success 归入 INFO
const LEVEL_MAP = { info: 'INFO', success: 'INFO', warning: 'WARNING', error: 'ERROR' }

async function refreshLogs() {
  loading.value = true
  try {
    const result = await api.listLogs({ limit: 500 })
    logs.value = (result.logs || []).map(row => ({
      time: String(row.created_at || '').replace('T', ' '),
      level: LEVEL_MAP[row.level] || 'INFO',
      message: row.message,
    }))
  } finally {
    loading.value = false
  }
}

onMounted(refreshLogs)
</script>
