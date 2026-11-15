import axios from 'axios'

// 与后端统一的响应包装：{ ok: true, data } / { ok: false, error }
export const instance = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
})

instance.interceptors.request.use(config => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

instance.interceptors.response.use(
  res => res.data,
  err => {
    if (err.response?.status === 401) {
      localStorage.removeItem('access_token')
      localStorage.removeItem('user')
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }
    return Promise.reject(err)
  },
)

export default instance
