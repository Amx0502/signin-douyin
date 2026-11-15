<template>
  <div class="app-shell">
    <header class="app-bar">
      <div class="brand">
        <span class="brand__dot" aria-hidden="true"></span>
        <strong>抖音续火</strong>
      </div>
      <nav class="tabs" aria-label="主导航">
        <RouterLink to="/douyin/overview">概览</RouterLink>
        <RouterLink to="/douyin/accounts">账号</RouterLink>
        <RouterLink to="/douyin/auto">自动任务</RouterLink>
        <RouterLink to="/douyin/runs">运行记录</RouterLink>
        <RouterLink to="/douyin/logs">日志</RouterLink>
      </nav>
      <button v-if="loggedIn" class="logout" type="button" @click="logout">退出</button>
    </header>
    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const loggedIn = computed(() => route.name !== 'login')

function logout() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('user')
  router.push({ name: 'login' })
}
</script>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr;
}
.app-bar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 16px;
  border-bottom: 1px solid #e5e9f0;
  background: rgb(255 255 255 / 92%);
  backdrop-filter: blur(12px);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
  color: #172033;
}
.brand__dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: linear-gradient(135deg, #22d3ee, #2563eb);
  box-shadow: 0 0 0 3px rgb(37 99 235 / 12%);
}
.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.tabs::-webkit-scrollbar { display: none; }
.tabs a {
  flex: none;
  padding: 6px 12px;
  border-radius: 999px;
  color: #5b6b82;
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
  transition: background 0.15s ease, color 0.15s ease;
}
.tabs a:hover { background: #eff4fb; color: #1d4ed8; }
.tabs a.router-link-active {
  background: #e6efff;
  color: #1d4ed8;
  font-weight: 600;
}
.logout {
  flex: none;
  margin-left: auto;
  padding: 5px 12px;
  border: 1px solid #dbe3ee;
  border-radius: 999px;
  background: #fff;
  color: #64748b;
  font-size: 12px;
  cursor: pointer;
}
.logout:hover { border-color: #bfd3ef; color: #1d4ed8; }
.app-main { padding: 16px; }

@media (max-width: 640px) {
  .app-bar { gap: 10px; padding: 8px 12px; flex-wrap: wrap; }
  .tabs { order: 3; width: 100%; }
  .app-main { padding: 10px; }
}
</style>
