<template>
  <div class="douyin-page" v-loading="loading">
    <DouyinOverviewPanel v-if="isOverview" />

    <DouyinAccountsPanel
      v-else-if="isAccounts"
      :accounts="accounts"
      :loading="loading"
      @refresh="loadAll"
    />

    <DouyinAutoTaskPanel
      v-else-if="isAuto"
      :tasks="tasks"
      :accounts="accounts"
      :loading="loading"
      @refresh="loadAll"
    />

    <DouyinRunHistoryPanel
      v-else-if="isRuns"
      ref="runsPanel"
      :accounts="accounts"
    />

    <DouyinLogsPanel v-else />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import api from "../api/douyin.js";
import DouyinOverviewPanel from "../components/douyin/OverviewPanel.vue";
import DouyinAccountsPanel from "../components/douyin/AccountsPanel.vue";
import DouyinAutoTaskPanel from "../components/douyin/AutoTaskPanel.vue";
import DouyinRunHistoryPanel from "../components/douyin/RunHistoryPanel.vue";
import DouyinLogsPanel from "../components/douyin/LogsPanel.vue";

const route = useRoute();
const path = computed(() => route.path);
const isOverview = computed(() => path.value.endsWith("/overview"));
const isAccounts = computed(() => path.value.endsWith("/accounts"));
const isAuto = computed(() => path.value.endsWith("/auto"));
const isRuns = computed(() => path.value.endsWith("/runs"));

const loading = ref(false);
const accounts = ref([]);
const tasks = ref([]);
const runsPanel = ref(null);

let pollTimer = null;

async function loadAll() {
  loading.value = true;
  try {
    if (isAccounts.value) {
      accounts.value = await api.listAccounts();
    } else if (isAuto.value) {
      [accounts.value, tasks.value] = await Promise.all([api.listAccounts(), api.listTasks()]);
    } else if (isRuns.value) {
      runsPanel.value?.load();
    }
  } finally {
    loading.value = false;
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

onMounted(() => {
  loadAll();
  // 执行/日志页轻量轮询，让后台提交的运行结果自动出现
  pollTimer = setInterval(() => {
    if (isAuto.value || isRuns.value) {
      loadAll().catch(() => {});
    }
  }, 10_000);
});
onUnmounted(stopPolling);
watch(path, () => {
  loadAll();
});
</script>

<style scoped>
.douyin-page {
  display: grid;
  gap: 16px;
  grid-template-columns: minmax(0, 1fr);
}
</style>
