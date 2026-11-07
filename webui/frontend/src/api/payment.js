import http from './request'

// 支付区域预设（country/currency 组合，决定支付页币种和可用支付方式）
export const listPaymentProfiles = () => http.get('/api/payment/profiles')

// 为已注册账号生成 Plus/Team 支付链接。
// 只传邮箱和选项，access_token 由后端从本地库里取，不会经过浏览器。
export const createPaymentLink = (payload) => http.post('/api/payment/link', payload)
