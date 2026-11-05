<script setup>
/**
 * Remail 服务 —— Remail 订单管理。
 *
 * 「邮箱配置」页是填参数（Key / 项目 ID / 后缀 / 模式）；
 * 这一页是看订单：下了什么邮箱、什么类型、花了多少钱、能不能再收一次件。
 *
 * Remail 的长效（purchase）订单可以**重复收件** —— 注册超时、想查历史验证码
 * 时不用重新下单（重新下单要再花钱）。
 */
import { computed, onActivated, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listMailOrders, createMailOrder, getOrderMails, getWallet,
  setOrderDisabled, deleteOrder, bulkDeleteOrders,
} from '@/api/mail'
import { fmtTime, copyText } from '@/api/request'

const orders = ref([])
const wallet = ref({ balance: '-', enabled: true })
const loading = ref(false)
const creating = ref(false)
const selected = ref([])

// 手动下单表单
const form = ref({ suffix: '', service_mode: 'purchase', supply_policy: 'private_first' })

// 收件箱弹窗
const mailsVisible = ref(false)
const mailsLoading = ref(false)
const mailsEmail = ref('')
const mails = ref([])

const STAT = {
  pending_payment: { type: 'info', text: '待支付' },
  paid: { type: 'success', text: '已支付' },
  active: { type: 'success', text: '生效中' },
  completed: { type: 'info', text: '已完成' },
  // consumed = 短效（code）订单已消耗。
  // 短效的 serviceToken 是一次性凭证，用完就失效（实测取件 401），
  // 所以状态必须如实标成「已消耗」，而不是继续挂在「生效中」。
  consumed: { type: 'info', text: '已消耗' },
  refunded: { type: 'warning', text: '已退款' },
  failed: { type: 'danger', text: '失败' },
  closed: { type: 'info', text: '已关闭' },
}
function statOf(s) {
  return STAT[s] || { type: 'info', text: s || '-' }
}

/** 邮箱类型（后缀）→ 可读说明 */
function suffixText(row) {
  const s = row.suffix || ''
  if (!s) return '-'
  const known = {
    outlook: '微软邮箱（随机后缀）',
    'outlook.com': '微软 outlook.com',
    'gmail.com': 'Gmail 主邮箱',
    gmail_variant: 'Gmail 变种',
    'icloud.com': 'iCloud',
    proto: 'Proto',
    domain: '域名邮箱',
  }
  return known[s] ? `${known[s]}（${s}）` : s
}

async function load() {
  loading.value = true
  try {
    const data = await listMailOrders({ limit: 200 })
    orders.value = data.items || []
  } catch (e) {
    ElMessage.error('加载订单失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function loadWallet() {
  try {
    const data = await getWallet()
    wallet.value = data
  } catch (e) {
    wallet.value = { balance: '查询失败', enabled: true }
  }
}

onActivated(() => { load(); loadWallet() })

async function createOrder() {
  creating.value = true
  try {
    const payload = {}
    if (form.value.suffix.trim()) payload.suffix = form.value.suffix.trim()
    payload.service_mode = form.value.service_mode
    payload.supply_policy = form.value.supply_policy
    const r = await createMailOrder(payload)
    ElMessage.success(`下单成功：${r.email}`)
    load()
    loadWallet()
  } catch (e) {
    ElMessage.error('下单失败: ' + e.message)
  } finally {
    creating.value = false
  }
}

/** 取件 —— 长效订单可以反复收 */
async function openMails(row) {
  mailsVisible.value = true
  mailsLoading.value = true
  mailsEmail.value = row.email
  mails.value = []
  try {
    const data = await getOrderMails(row.order_no)
    mails.value = data.items || []
    if (!mails.value.length) {
      ElMessage.info('这个订单目前还没有收到邮件')
    }
  } catch (e) {
    ElMessage.error('取件失败: ' + e.message)
  } finally {
    mailsLoading.value = false
  }
}

async function refreshMails(row) {
  await openMails(row)
}

/** 启用 / 禁用订单。禁用后不再被自动复用，但仍可手动取件。 */
async function toggleDisabled(row) {
  const next = !row.disabled
  try {
    const data = await setOrderDisabled(row.order_no, next)
    orders.value = data.items || orders.value
    ElMessage.success(next
      ? `已禁用 ${row.email}，不会再被自动复用`
      : `已启用 ${row.email}`)
  } catch (e) {
    ElMessage.error('操作失败: ' + e.message)
  }
}

/** 删除本地订单记录 */
async function removeOrder(row) {
  try {
    await ElMessageBox.confirm(
      `删除订单 ${row.order_no.slice(0, 16)}…（${row.email}）的本地记录？\n\n` +
      `只删本地记录，不会去 Remail 退单或退款。\n` +
      `删除后这个邮箱不会再被复用，也看不到它了。`,
      '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
  } catch (_) { return }
  try {
    const data = await deleteOrder(row.order_no)
    orders.value = data.items || []
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  }
}

/** 批量删除 */
async function removeSelected() {
  const rows = selected.value
  if (!rows.length) { ElMessage.info('请先勾选订单'); return }
  try {
    await ElMessageBox.confirm(
      `删除选中的 ${rows.length} 条订单本地记录？\n\n` +
      `只删本地记录，不会去 Remail 退单或退款。`,
      '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
  } catch (_) { return }
  try {
    const data = await bulkDeleteOrders(rows.map((r) => r.order_no))
    orders.value = data.items || []
    selected.value = []
    ElMessage.success(`已删除 ${data.deleted || 0} 条`)
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  }
}

const totalCount = computed(() => orders.value.length)
const linkedCount = computed(() => orders.value.filter((o) => o.used_email).length)
const disabledCount = computed(() => orders.value.filter((o) => o.disabled).length)
const reusableCount = computed(() => orders.value.filter((o) => o.reusable).length)
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="9" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">状态与下单</span></template>

          <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px">
            <el-tag type="success" effect="light">余额 {{ wallet.balance }}</el-tag>
            <el-tag type="info" effect="light">共 {{ totalCount }} 单</el-tag>
            <el-tag type="primary" effect="light">已关联账号 {{ linkedCount }}</el-tag>
          </div>

          <el-divider content-position="left">手动下一单</el-divider>
          <p class="hint">
            正常注册会自动下单，这里是给「测试连通性」和「补单」用的。
            每次下单都会扣费。
          </p>
          <el-form label-position="top">
            <el-form-item label="邮箱后缀">
              <el-input v-model="form.suffix" placeholder="留空用「邮箱配置」里的默认值" />
              <div class="hint" style="margin-top: 4px">
                outlook = 微软白名单后缀随机；outlook.com = 精确匹配；
                domain = 域名邮箱；gmail_variant = Gmail 变种
              </div>
            </el-form-item>
            <el-form-item label="服务模式">
              <el-select v-model="form.service_mode" style="width: 100%">
                <el-option label="purchase（长效，可重复收件）" value="purchase" />
                <el-option label="code（短效接码，一次性）" value="code" />
              </el-select>
            </el-form-item>
            <el-form-item label="库存策略">
              <el-select v-model="form.supply_policy" style="width: 100%">
                <el-option label="private_first（优先自有库存）" value="private_first" />
                <el-option label="public_only（只用公共库存）" value="public_only" />
              </el-select>
            </el-form-item>
            <el-button type="primary" :loading="creating" @click="createOrder">下单</el-button>
          </el-form>

          <el-alert
            type="warning" :closable="false" show-icon style="margin-top: 14px"
            title="Remail 不交付邮箱密码。如果 OpenAI 把这个地址判定为「已有账号」，会要求密码登录 → 必然失败。自有域名后缀成功率最高。"
          />
        </el-card>
      </el-col>

      <el-col :md="15" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
              <span class="section-title" style="margin: 0">订单列表（{{ totalCount }}）</span>
              <div style="display: flex; gap: 8px">
                <el-button size="small" type="danger" plain
                           :disabled="!selected.length" @click="removeSelected">
                  删除选中 ({{ selected.length }})
                </el-button>
                <el-button size="small" @click="load">刷新</el-button>
              </div>
            </div>
          </template>

          <div style="display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
            <el-tag type="success" size="small" effect="plain">可复用 {{ reusableCount }}</el-tag>
            <el-tag type="info" size="small" effect="plain">已禁用 {{ disabledCount }}</el-tag>
            <el-tag type="primary" size="small" effect="plain">已关联账号 {{ linkedCount }}</el-tag>
          </div>

          <el-alert
            type="info" :closable="false" show-icon style="margin-bottom: 12px"
            title="长效（purchase）订单在没关联账号之前会被自动复用 —— 下次注册直接拿这个已付费的邮箱，不再重复下单扣费。禁用的订单不再被复用，但仍可手动收件。"
          />

          <el-table :data="orders" size="small" stripe max-height="520"
                    @selection-change="(rows) => selected = rows">
            <el-table-column type="selection" width="42" />
            <el-table-column prop="order_no" label="订单号" min-width="130" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ row.order_no }}</span></template>
            </el-table-column>
            <el-table-column prop="email" label="邮箱" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ row.email }}</span>
              </template>
            </el-table-column>
            <el-table-column label="邮箱类型" min-width="150">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" type="info">{{ suffixText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="模式" width="90">
              <template #default="{ row }">
                <el-tag size="small" effect="light"
                  :type="row.service_mode === 'purchase' ? 'success' : 'info'">
                  {{ row.service_mode === 'purchase' ? '长效' : '短效' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="140">
              <template #default="{ row }">
                <el-tag size="small" effect="light" :type="statOf(row.order_status).type">
                  {{ statOf(row.order_status).text }}
                </el-tag>
                <el-tag v-if="row.disabled" size="small" effect="plain" type="danger"
                        style="margin-left: 4px">已禁用</el-tag>
                <el-tag v-else-if="row.reusable" size="small" effect="plain" type="success"
                        style="margin-left: 4px">可复用</el-tag>
                <el-tag v-else-if="row.used_email" size="small" effect="plain" type="info"
                        style="margin-left: 4px">已用</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="尝试" width="80" align="center">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">
                  {{ row.attempt_count ?? 0 }} / {{ row.max_attempts ?? 10 }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="付费" width="80">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ row.pay_amount || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="关联账号" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.used_email" class="mono" style="font-size: 12px">{{ row.used_email }}</span>
                <span v-else class="hint">—</span>
              </template>
            </el-table-column>
            <el-table-column label="最近失败" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.last_error" class="hint" style="font-size: 12px">{{ row.last_error }}</span>
                <span v-else class="hint">—</span>
              </template>
            </el-table-column>
            <el-table-column label="下单时间" width="150">
              <template #default="{ row }">{{ fmtTime(row.received_at) }}</template>
            </el-table-column>
            <el-table-column label="启用" width="62" align="center">
              <template #default="{ row }">
                <el-switch
                  :model-value="!row.disabled"
                  @change="toggleDisabled(row)"
                />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="190" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="openMails(row)">收件箱</el-button>
                <el-button size="small" text @click="copyText(row.email)">复制邮箱</el-button>
                <el-button size="small" text type="danger" @click="removeOrder(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="还没有 Remail 订单。注册一个号或手动下一单就会出现。" :image-size="70" />
            </template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="mailsVisible" width="720px" top="8vh" :title="`收件箱 · ${mailsEmail}`">
      <div style="display: flex; justify-content: flex-end; margin-bottom: 10px">
        <el-button size="small" @click="refreshMails()">重新拉取</el-button>
      </div>
      <el-skeleton v-if="mailsLoading" :rows="4" animated />
      <el-table v-else :data="mails" size="small" stripe max-height="440">
        <el-table-column label="验证码" width="120">
          <template #default="{ row }">
            <span v-if="row.verificationCode" class="mono" style="font-weight: 600; font-size: 15px">
              {{ row.verificationCode }}
            </span>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="subject" label="主题" min-width="180" show-overflow-tooltip />
        <el-table-column prop="sender" label="发件人" min-width="150" show-overflow-tooltip />
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ row.receivedAt || '-' }}</template>
        </el-table-column>
        <el-table-column prop="bodyPreview" label="摘要" min-width="200" show-overflow-tooltip />
        <template #empty>这个订单还没有收到邮件</template>
      </el-table>
      <div class="hint" style="margin-top: 10px">
        长效（purchase）订单可以随时重复收件，不用重新下单。
      </div>
    </el-dialog>
  </div>
</template>
