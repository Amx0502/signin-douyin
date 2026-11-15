<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="submit">
      <h1>抖音续火</h1>
      <p class="login-tip">登录后管理抖音账号、自动任务与运行记录</p>

      <label class="field">
        <span>用户名</span>
        <el-input v-model="form.username" placeholder="请输入用户名" autocomplete="username" />
      </label>
      <label class="field">
        <span>密码</span>
        <el-input
          v-model="form.password"
          type="password"
          show-password
          placeholder="请输入密码"
          autocomplete="current-password"
          @keyup.enter="submit"
        />
      </label>

      <el-button type="primary" native-type="submit" :loading="loading" class="login-btn">
        登录
      </el-button>
      <p v-if="error" class="login-error">{{ error }}</p>
    </form>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { instance } from '../api/index.js'

const route = useRoute()
const router = useRouter()
const form = reactive({ username: '', password: '' })
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!form.username.trim() || !form.password) {
    error.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await instance.post('/auth/login', {
      username: form.username.trim(),
      password: form.password,
    })
    const payload = res?.data || {}
    if (!payload.token) throw new Error('登录返回缺少 token')
    localStorage.setItem('access_token', payload.token)
    localStorage.setItem('user', JSON.stringify(payload.user || {}))
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    router.replace(redirect || '/douyin/overview')
  } catch (err) {
    error.value = err?.response?.data?.error || err?.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: radial-gradient(1200px 600px at 50% -10%, #e8f0ff 0%, #f6f8fc 55%, #f2f5fa 100%);
}
.login-card {
  width: min(380px, 100%);
  display: grid;
  gap: 14px;
  padding: 28px 24px 24px;
  border: 1px solid #e2e9f3;
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 24px 60px rgb(15 23 42 / 8%);
}
h1 {
  margin: 0;
  color: #0f172a;
  font-size: 20px;
  letter-spacing: 0.5px;
}
.login-tip {
  margin: -6px 0 4px;
  color: #7c8ba1;
  font-size: 12px;
  line-height: 1.6;
}
.field {
  display: grid;
  gap: 6px;
}
.field > span {
  color: #5b6b82;
  font-size: 12px;
}
.login-btn {
  margin-top: 4px;
  height: 40px;
  border-radius: 12px;
  font-weight: 650;
}
.login-error {
  margin: 0;
  color: #dc2626;
  font-size: 12px;
  line-height: 1.5;
}
</style>
