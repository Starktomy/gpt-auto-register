<script setup>
import { computed, onActivated, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'

const proxyStore = useProxyStore()
const { items, stats, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)
const saving = ref(false)

const rows = computed(() =>
  items.value.map((it, i) => ({
    index: i + 1,
    ...it,
    valid: isValidProxy(it.proxy),
    result: testResults.value[it.proxy] || null,
    // 上限 0 = 不限；进度条按比例算
    percent: it.max_uses ? Math.min(100, Math.round((it.used_count / it.max_uses) * 100)) : 0,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)

function statusOf(row) {
  if (!row.enabled) return { type: 'info', text: '已停用' }
  if (row.exhausted) return { type: 'warning', text: '已满' }
  return { type: 'success', text: '可用' }
}

async function reload() {
  try { await proxyStore.load() } catch (e) { ElMessage.error('加载代理池失败: ' + e.message) }
}
onActivated(reload)

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    // request.js 的拦截器已经解包了 resp.data，这里拿到的就是业务对象本身
    const data = await testProxies(targets)
    const results = data?.results || {}
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest(items.value.map((i) => i.proxy)) }
  finally { testingAll.value = false }
}

async function save(mode) {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  saving.value = true
  try {
    const data = await proxyStore.save(draft.value, mode)
    draft.value = ''
    ElMessage.success(`已保存 ${data.total} 个代理（现有 ${data.items.length} 个）`)
  } catch (e) {
    ElMessage.error('保存失败: ' + e.message)
  } finally {
    saving.value = false
  }
}
function editInDraft() {
  draft.value = proxyStore.text
  ElMessage.info('已把当前代理池载入编辑框，改完点「覆盖保存」')
}

async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(
      `确定清空全部 ${count.value} 个代理？用量记录也会一并清除。`,
      '确认', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
    await proxyStore.clear()
    ElMessage.success('已清空')
  } catch (e) {
    if (e?.message) ElMessage.error('清空失败: ' + e.message)
  }
}

/** 启用/停用开关 */
async function toggleEnabled(row) {
  try {
    await proxyStore.update(row.proxy, { enabled: !row.enabled })
    ElMessage.success(row.enabled ? `已停用 ${row.proxy}` : `已启用 ${row.proxy}`)
  } catch (e) {
    ElMessage.error('操作失败: ' + e.message)
  }
}

/** 改单个代理的注册数上限 */
async function changeLimit(row, value) {
  const n = Math.max(0, parseInt(value, 10) || 0)
  try {
    await proxyStore.update(row.proxy, { max_uses: n })
    ElMessage.success(`${row.proxy} 上限改为 ${n === 0 ? '不限' : n}`)
  } catch (e) {
    ElMessage.error('修改失败: ' + e.message)
  }
}

/**
 * 改代理类型：静态（固定出口 IP）/ 动态（每次请求换 IP）。
 *
 * ⚠️ 这个设置直接决定「被 Cloudflare 403 拦」时的处置：
 *    · 静态 → 出口 IP 固定，被拉黑后换多少次都一样
 *      → 连续被拦 2 次就自动停用
 *    · 动态 → 代理只是入口，换个出口 IP 可能就通过
 *      → 只记次数，不停用
 */
async function changeType(row, value) {
  const t = value === 'dynamic' ? 'dynamic' : 'static'
  try {
    await proxyStore.update(row.proxy, { proxy_type: t })
    ElMessage.success(`${row.proxy} 已设为${t === 'dynamic' ? '动态（每次换IP）' : '静态（固定IP）'}`)
  } catch (e) {
    ElMessage.error('修改失败: ' + e.message)
  }
}

/** 清零某个代理的 CF 拦截计数（换 IP / 供应商修了之后手动点） */
async function resetCf(row) {
  try {
    await proxyStore.resetCfBlocked(row.proxy)
    ElMessage.success(`${row.proxy} 的 CF 拦截计数已清零`)
  } catch (e) {
    ElMessage.error('重置失败: ' + e.message)
  }
}

/**
 * 批量检测代理池里哪些支持 IPv6 出口。
 *
 * IPv6 的地址空间比 IPv4 大几个数量级，同一 /64 段里的"邻居"少得多
 * → 被 Cloudflare 按 IP 段批量拉黑的概率低得多。
 */
const ipv6Testing = ref(false)
async function testIpv6All() {
  const targets = rows.value.map((r) => r.proxy)
  if (!targets.length) return
  ipv6Testing.value = true
  try {
    const data = await proxyStore.testIpv6(targets, 10)
    const res = data.results || {}
    const okCount = Object.values(res).filter((r) => r.ipv6).length
    ElMessage.success(`IPv6 检测完成：${okCount} / ${targets.length} 个支持`)
  } catch (e) {
    ElMessage.error('IPv6 检测失败: ' + e.message)
  } finally {
    ipv6Testing.value = false
  }
}

/** 重置某个代理的用量计数 */
async function resetOne(row) {
  try {
    await proxyStore.resetCounts(row.proxy)
    ElMessage.success(`${row.proxy} 用量已清零`)
  } catch (e) {
    ElMessage.error('重置失败: ' + e.message)
  }
}

/** 重置全部用量 */
async function resetAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm('确定把所有代理的用量计数清零？', '确认', { type: 'warning' })
    await proxyStore.resetCounts()
    ElMessage.success('已全部重置')
  } catch (e) {
    if (e?.message) ElMessage.error('重置失败: ' + e.message)
  }
}

async function removeOne(row) {
  try {
    await proxyStore.remove(row.proxy)
    ElMessage.success('已删除')
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  }
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="9" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">批量导入</span></template>
          <p class="hint">
            每行一个：<span class="mono">[协议://][user:pass@]host:port</span><br />
            不写协议默认按 <b>HTTP 代理</b>；SOCKS5 必须写 <span class="mono">socks5://</span>。<br />
            覆盖保存会保留已在池中的代理的<b>用量和启用状态</b>，只移除不在新列表里的。
          </p>
          <el-input
            v-model="draft" type="textarea" :rows="10" class="mono"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" :loading="saving" @click="save('replace')">覆盖保存</el-button>
            <el-button :loading="saving" @click="save('append')">追加合并</el-button>
            <el-button @click="editInDraft">载入当前池</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :md="15" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
              <span class="section-title" style="margin: 0">
                当前代理池（{{ count }} 个
                <template v-if="invalidCount">，<span style="color: var(--el-color-danger)">{{ invalidCount }} 个格式异常</span></template>）
              </span>
              <div style="display: flex; gap: 8px; flex-wrap: wrap">
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">测试全部</el-button>
                <el-button size="small" :loading="ipv6Testing" :disabled="!count" @click="testIpv6All">检测 IPv6</el-button>
                <el-button size="small" :disabled="!count" @click="resetAll">重置全部用量</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">复制全部</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">清空</el-button>
              </div>
            </div>
          </template>

          <div style="display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap">
            <el-tag type="success" size="small" effect="light">可用 {{ stats.available }}</el-tag>
            <el-tag type="info" size="small" effect="light">已停用 {{ stats.disabled }}</el-tag>
            <el-tag type="warning" size="small" effect="light">已满 {{ stats.exhausted }}</el-tag>
          </div>

          <el-table :data="rows" size="small" stripe max-height="480">
            <el-table-column prop="index" label="#" width="42" />
            <el-table-column prop="proxy" label="代理地址" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column label="生效协议" width="96">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxyScheme(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="118">
              <template #default="{ row }">
                <el-select
                  :model-value="row.proxy_type"
                  size="small"
                  style="width: 100%"
                  @change="(v) => changeType(row, v)"
                >
                  <el-option label="静态（固定IP）" value="static" />
                  <el-option label="动态（每次换IP）" value="dynamic" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="CF拦截" width="90" align="center">
              <template #default="{ row }">
                <el-tooltip
                  :content="row.cf_blocked
                    ? `被 Cloudflare 403 拦了 ${row.cf_blocked} 次` +
                      (row.proxy_type === 'static'
                        ? '（静态代理：连续 2 次会自动停用）'
                        : '（动态代理：只记录，不停用）')
                    : '未被 CF 拦截'"
                  placement="top"
                >
                  <span>
                    <el-tag v-if="row.cf_blocked" type="danger" size="small">
                      {{ row.cf_blocked }} 次
                    </el-tag>
                    <span v-else class="hint" style="font-size: 12px">—</span>
                  </span>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="IPv6" width="80" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.ipv6_status === 'ok'" type="success" size="small">支持</el-tag>
                <el-tag v-else-if="row.ipv6_status === 'fail'" type="info" size="small">不支持</el-tag>
                <span v-else class="hint" style="font-size: 12px">未测</span>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="62" align="center">
              <template #default="{ row }">
                <el-switch :model-value="row.enabled" @change="toggleEnabled(row)" />
              </template>
            </el-table-column>
            <el-table-column label="已用 / 上限" min-width="150">
              <template #default="{ row }">
                <el-progress
                  :percentage="row.percent"
                  :status="row.exhausted ? 'exception' : (row.percent > 70 ? 'warning' : 'success')"
                  :stroke-width="10"
                >
                  <span class="mono" style="font-size: 12px">
                    {{ row.used_count }} / {{ row.max_uses || '不限' }}
                  </span>
                </el-progress>
              </template>
            </el-table-column>
            <el-table-column label="上限" width="110">
              <template #default="{ row }">
                <el-input-number
                  :model-value="row.max_uses"
                  :min="0" :max="99999" :step="10" size="small"
                  controls-position="right"
                  @change="(v) => changeLimit(row, v)"
                />
              </template>
            </el-table-column>
            <el-table-column label="状态" width="76">
              <template #default="{ row }">
                <el-tag :type="statusOf(row).type" size="small" effect="light">{{ statusOf(row).text }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="连通性" min-width="130">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">未测</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">测试中…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">正常 {{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || '连接失败'" placement="top">
                  <el-tag type="danger" size="small">失败</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >测试</el-button>
                <el-button size="small" text @click="resetOne(row)">清零</el-button>
                <el-button
                  size="small" text
                  :disabled="!row.cf_blocked"
                  @click="resetCf(row)"
                >清CF</el-button>
                <el-button size="small" text type="danger" @click="removeOne(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>暂无代理，请在左侧批量导入</template>
          </el-table>

          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="全自动批量跑号时，每次注册都会从池里动态取一个可用代理（优先挑用得少的）。达到上限或被停用的代理会自动跳过；池里全都不可用时跑号会停下并提示，不会让所有 worker 挤同一个 IP。"
          />
          <el-alert
            type="warning" :closable="false" show-icon style="margin-top: 8px"
            title="上限只统计注册成功的号；失败的只累加失败次数，不占配额。在「注册结果」页把账号标注异常或删除时，该号占用的配额会自动还回池子。"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
