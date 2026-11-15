import { createRouter, createWebHistory } from 'vue-router'

const Douyin = () => import('./views/Douyin.vue')
const Login = () => import('./views/Login.vue')

// Douyin.vue 按路由路径后缀切换面板（overview/accounts/auto/runs，其余为日志）
const routes = [
  { path: '/login', name: 'login', component: Login, meta: { public: true } },
  { path: '/', redirect: '/douyin/overview' },
  { path: '/douyin/overview', name: 'douyin-overview', component: Douyin },
  { path: '/douyin/accounts', name: 'douyin-accounts', component: Douyin },
  { path: '/douyin/auto', name: 'douyin-auto', component: Douyin },
  { path: '/douyin/runs', name: 'douyin-runs', component: Douyin },
  { path: '/douyin/logs', name: 'douyin-logs', component: Douyin },
  { path: '/:pathMatch(.*)*', redirect: '/douyin/overview' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(to => {
  if (to.meta.public) return true
  if (!localStorage.getItem('access_token')) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
