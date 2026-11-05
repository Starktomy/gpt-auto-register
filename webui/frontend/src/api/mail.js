import http from './request'

// ──────────────── Remail 订单（邮箱服务） ────────────────
//
// Remail 不交付账号密码，只交付「邮箱地址 + serviceToken」这一对取件凭证。
// 下单时全部写进本地 remail_orders 表，这里所有操作都只读本地 ——
// 不回查 Remail 的 projects / orders 接口。

export const listMailOrders = (params = {}) =>
  http.get('/api/mail/orders', { params }) // { limit, offset }

// 手动下一单（测试用 / 补单）。走的是和注册流程一样的下单逻辑。
export const createMailOrder = (payload) =>
  http.post('/api/mail/orders', payload) // { suffix?, project_id?, service_mode?, supply_policy? }

// 取件 —— 这就是「长效订单可以重复收件」的入口
export const getOrderMails = (orderNo) =>
  http.get(`/api/mail/orders/${encodeURIComponent(orderNo)}/mails`)

// 把订单关联到本地注册成功的账号
export const linkOrder = (orderNo, email) =>
  http.post(`/api/mail/orders/${encodeURIComponent(orderNo)}/link`, { email })

// 本地禁用 / 启用订单（禁用的不会被自动复用，但仍可手动取件）
export const setOrderDisabled = (orderNo, disabled) =>
  http.post(`/api/mail/orders/${encodeURIComponent(orderNo)}/disabled`, { disabled })

// 删除本地订单记录（不去 Remail 退单）
export const deleteOrder = (orderNo) =>
  http.delete(`/api/mail/orders/${encodeURIComponent(orderNo)}`)

// 批量删除本地订单记录
export const bulkDeleteOrders = (orderNos) =>
  http.post('/api/mail/orders/bulk_delete', { order_nos: orderNos })

// 查 Remail 余额
export const getWallet = () => http.get('/api/mail/wallet')
