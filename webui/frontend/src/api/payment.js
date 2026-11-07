import http from './request'

// 单个账号的非敏感元数据：注册代理、可用的试用活动 id、是否已有 Codex RT
export const getPaymentAccount = (email) =>
  http.get(`/api/payment/account/${encodeURIComponent(email)}`)

// 支付区域预设（country/currency 组合，决定支付页币种和可用支付方式）
export const listPaymentProfiles = () => http.get('/api/payment/profiles')

// 为已注册账号生成 Plus/Team 支付链接。
// 只传邮箱和选项，access_token 由后端从本地库里取，不会经过浏览器。
export const createPaymentLink = (payload) => http.post('/api/payment/link', payload)

// 给「没有 Codex refresh_token」的老号补跑一次 Codex OAuth（登录 + 授权）。
// 耗时 30~90 秒，可能触发邮箱验证码和短信验证，所以单给一个长超时。
export const preparePaymentToken = (payload) =>
  http.post('/api/payment/prepare', payload, { timeout: 300000 })
