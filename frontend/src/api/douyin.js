import { instance } from './index.js'

const root = '/douyin'
const data = request => request.then(response => response.data)
export default {
  // 账号（后端需现场请求抖音做预检 + 资料回填，最坏 45s+，超时必须留足余量）
  createAccount: payload => data(instance.post(`${root}/accounts`, payload, { timeout: 90_000 })),
  listAccounts: () => data(instance.get(`${root}/accounts`)),
  getAccountAccess: () => data(instance.get(`${root}/accounts/access`)),
  updateAccount: (id, payload) => data(instance.put(`${root}/accounts/${id}`, payload)),
  deleteAccount: id => data(instance.delete(`${root}/accounts/${id}`)),
  verifyAccount: id => data(instance.post(`${root}/accounts/${id}/verify`, null, { timeout: 90_000 })),
  listFriendsCache: id => data(instance.get(`${root}/accounts/${id}/friends`)),
  listFriends: id => data(instance.post(`${root}/accounts/${id}/friends`, null, { timeout: 180_000 })),
  startFriendsRefresh: id => data(instance.post(`${root}/accounts/${id}/friends/refresh`, null, { timeout: 30_000 })),
  getFriendsJob: id => data(instance.get(`${root}/accounts/${id}/friends/job`, { timeout: 30_000 })),
  // 平台真实火花状态（只读会话列表，不发送消息）
  getSparkCache: id => data(instance.get(`${root}/accounts/${id}/spark`)),
  getSpark: id => data(instance.post(`${root}/accounts/${id}/spark`, null, { timeout: 120_000 })),
  // 扫码登录
  startLoginQr: (payload = {}) => data(instance.post(`${root}/login-qr`, payload, { timeout: 60_000 })),
  pollLoginQr: qrId => data(instance.get(`${root}/login-qr/${qrId}`, { timeout: 30_000 })),
  submitLoginVerification: (qrId, code) =>
    data(instance.post(`${root}/login-qr/${qrId}/verification`, { code }, { timeout: 30_000 })),
  closeLoginQr: qrId => data(instance.delete(`${root}/login-qr/${qrId}`)),
  // 手机号验证码登录（提交后需原手机 APP 扫码确认）
  startLoginSms: payload => data(instance.post(`${root}/login-sms`, payload, { timeout: 60_000 })),
  submitLoginSmsCode: (qrId, code) =>
    data(instance.post(`${root}/login-sms/${qrId}/code`, { code }, { timeout: 30_000 })),
  // 任务
  listTasks: () => data(instance.get(`${root}/tasks`)),
  createTask: payload => data(instance.post(`${root}/tasks`, payload)),
  updateTask: (id, payload) => data(instance.put(`${root}/tasks/${id}`, payload)),
  deleteTask: id => data(instance.delete(`${root}/tasks/${id}`)),
  batchRunTasks: () => data(instance.post(`${root}/tasks/batch-run`, null, { timeout: 30_000 })),
  runTask: id => data(instance.post(`${root}/tasks/${id}/run`, null, { timeout: 30_000 })),
  // 运行/日志/设置
  listRuns: (params = {}) => data(instance.get(`${root}/runs`, { params: { limit: 200, ...params } })),
  getRun: id => data(instance.get(`${root}/runs/${id}`)),
  // 单目标重试：只给运行记录里某个失败好友重发一次（起浏览器，最坏 90s+）
  retryRunTarget: (id, target) => data(
    instance.post(`${root}/runs/${id}/retry-target`, { target }, { timeout: 150_000 }),
  ),
  listLogs: (params = {}) => data(instance.get(`${root}/logs`, { params })),
  getSettings: () => data(instance.get(`${root}/settings`)),
  updateSettings: payload => data(instance.put(`${root}/settings`, payload)),
  getNotifySettings: () => data(instance.get(`${root}/notify-settings`)),
  updateNotifySettings: payload => data(instance.put(`${root}/notify-settings`, payload)),
  getBuiltinMessages: () => data(instance.get(`${root}/builtin-messages`)),
}
