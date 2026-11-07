<script setup>
/**
 * 生成支付链接弹窗。
 *
 * 独立的按钮功能，不和检测/导出流程混在一起：
 * 选一个账号 + 选一个支付区域 → 后端建 checkout session → 返回三条链接。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { listPaymentProfiles, createPaymentLink } from '@/api/payment'
import { copyText } from '@/api/request'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  email: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const profiles = ref([])
const exitCountry = ref('')
const plan = ref('plus')
const profileId = ref('paypal_de')
const seats = ref(2)
const interval = ref('month')
const workspaceName = ref('')
const promoCode = ref('')
const loading = ref(false)
const result = ref(null)

const LINKS = [
  { key: 'openai', label: 'OpenAI 长链', hint: 'pay.openai.com · 独立浏览器可开' },
  { key: 'stripe', label: 'Stripe 镜像', hint: 'checkout.stripe.com · 同一 host 的镜像' },
  { key: 'internal', label: 'ChatGPT 内部链接', hint: 'chatgpt.com/checkout · 需该账号自己登录' },
]
// OpenAI 校验 billing_details.country 必须等于出口 IP 的国家，
// 选错区域会直接被拒（"Billing country must match request country"）。
const currentProfile = computed(
  () => profiles.value.find((p) => p.id === profileId.value) || null,
)
const countryMismatch = computed(
  () => !!exitCountry.value && !!currentProfile.value && currentProfile.value.country !== exitCountry.value,
)

const linkRows = computed(() =>
  LINKS.map((item) => ({ ...item, url: result.value?.urls?.[item.key] || '' })),
)

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    result.value = null
    loading.value = false
    loadProfiles()
  },
)

async function loadProfiles() {
  try {
    const { items, exit_country } = await listPaymentProfiles()
    profiles.value = items || []
    exitCountry.value = exit_country || ''
    // 默认选出口 IP 所在国家 —— 选别的国家 OpenAI 会直接拒。
    const match = profiles.value.find((p) => p.country === exitCountry.value)
    if (match) profileId.value = match.id
    else if (!profiles.value.find((p) => p.id === profileId.value)) {
      profileId.value = profiles.value[0]?.id || ''
    }
  } catch (e) {
    ElMessage.error('读取支付区域失败: ' + e.message)
  }
}

async function generate() {
  if (!props.email) { ElMessage.warning('没有选中账号'); return }
  loading.value = true
  result.value = null
  try {
    const { data } = await createPaymentLink({
      email: props.email,
      plan: plan.value,
      profile: profileId.value,
      seats: seats.value,
      interval: interval.value,
      workspace_name: workspaceName.value,
      promo_code: promoCode.value,
    })
    result.value = data
    ElMessage.success('支付链接已生成')
  } catch (e) {
    ElMessage.error('生成失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

function openUrl(url) {
  if (!url) return
  window.open(url, '_blank', 'noopener,noreferrer')
}
</script>

<template>
  <el-dialog v-model="visible" width="720px" top="8vh" destroy-on-close>
    <template #header>
      <div style="display: flex; align-items: center; gap: 12px">
        <span style="font-weight: 600">生成支付链接</span>
        <el-tag size="small" type="info">{{ email || '未选择账号' }}</el-tag>
      </div>
    </template>

    <el-form label-width="96px" size="default">
      <el-form-item label="套餐">
        <el-radio-group v-model="plan">
          <el-radio-button value="plus">Plus</el-radio-button>
          <el-radio-button value="team">Team</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item label="支付区域">
        <el-select v-model="profileId" style="width: 100%" placeholder="选择国家/币种">
          <el-option
            v-for="p in profiles" :key="p.id" :value="p.id"
            :label="`${p.label} · ${p.country}/${p.currency}`"
          >
            <div class="hint" style="line-height: 1.4">
              {{ p.label }} · {{ p.country }}/{{ p.currency }}
              <span v-if="p.note" style="margin-left: 6px">{{ p.note }}</span>
            </div>
          </el-option>
        </el-select>
        <div class="hint" style="margin-top: 4px">
          country/currency 决定支付页的币种、界面语言和可用支付方式；试用资格只看请求出口 IP。
        </div>
      </el-form-item>

      <template v-if="plan === 'team'">
        <el-form-item label="工作区名">
          <el-input v-model="workspaceName" placeholder="我的工作区" style="width: 260px" />
        </el-form-item>
        <el-form-item label="周期/席位">
          <el-radio-group v-model="interval" size="small">
            <el-radio-button value="month">按月</el-radio-button>
            <el-radio-button value="year">按年</el-radio-button>
          </el-radio-group>
          <el-input-number v-model="seats" :min="1" :max="100" size="small" style="margin-left: 12px" />
          <span class="hint" style="margin-left: 6px">席位</span>
        </el-form-item>
      </template>

      <el-form-item label="优惠码">
        <el-input v-model="promoCode" placeholder="可留空" style="width: 260px" />
      </el-form-item>
    </el-form>

    <el-alert
      v-if="countryMismatch" type="warning" :closable="false" show-icon
      :title="`账单国家必须与出口 IP 的国家一致`"
      :description="`当前出口 IP 在 ${exitCountry}，而所选区域是 ${currentProfile?.country}。OpenAI 会拒绝该请求，请改成 ${exitCountry} 对应的区域，或先让该号走对应地区的代理。`"
      style="margin-bottom: 12px"
    />

    <el-alert
      v-if="result?.warnings?.length" type="warning" :closable="false" show-icon
      :title="'部分步骤降级：' + result.warnings.join('、')"
      style="margin-bottom: 12px"
    />

    <div v-if="result" class="pay-links">
      <div v-for="row in linkRows" :key="row.key" class="pay-link">
        <div class="pay-link-head">
          <span class="pay-link-label">{{ row.label }}</span>
          <span class="hint">{{ row.hint }}</span>
        </div>
        <div v-if="row.url" class="pay-link-url mono">{{ row.url }}</div>
        <div v-else class="hint">—（本次没有生成）</div>
        <div v-if="row.url" style="margin-top: 6px">
          <el-button size="small" text type="primary" @click="copyText(row.url)">
            <el-icon><CopyDocument /></el-icon>复制
          </el-button>
          <el-button size="small" text @click="openUrl(row.url)">打开</el-button>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :loading="loading" @click="generate">
        <el-icon><Link /></el-icon>生成链接
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pay-links { display: flex; flex-direction: column; gap: 10px; }
.pay-link {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
}
.pay-link-head { display: flex; align-items: baseline; gap: 10px; }
.pay-link-label { font-weight: 600; font-size: 13px; }
/* 链接很长，min-width:0 + break-all 才不会把弹窗撑破 */
.pay-link-url {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
  max-height: 64px;
  overflow: hidden;
}
</style>
