import { defineStore } from 'pinia'
import { ref } from 'vue'
import http from '@/api/request'

/**
 * 界面偏好（表单默认值、主题等）。
 *
 * 这些以前散在各页面的 localStorage 里 —— 只跟着**这一个浏览器**走，
 * 换台机器 / 清个缓存就没了，后端也完全看不到。
 * 现在统一存在后端 SQLite（settings 表的 prefs key），前后端同一份数据。
 *
 * 代理池另有专门的表（proxy_pool），不在这里。
 */
export const usePrefsStore = defineStore('prefs', () => {
  const prefs = ref({})
  const loaded = ref(false)
  let _saving = null
  let _pending = {}

  /** 从后端拉一次（应用启动时调） */
  async function load() {
    try {
      const data = await http.get('/api/settings/prefs')
      prefs.value = data.prefs || {}
      loaded.value = true
    } catch (e) {
      // 拉不到就用默认值，别因为偏好读不出来把整个界面卡住
      loaded.value = true
      throw e
    }
  }

  /** 合并写入（只传要改的 key）。后端是 merge 语义。 */
  function save(patch) {
    Object.assign(prefs.value, patch)
    Object.assign(_pending, patch)
    // 防抖：连续改多个字段（比如 el-input-number 连点）只发一次请求
    if (_saving) clearTimeout(_saving)
    _saving = setTimeout(async () => {
      const body = { ..._pending }
      _pending = {}
      try {
        await http.post('/api/settings/prefs', body)
      } catch (e) {
        // 保存失败下次再试：把值放回 pending
        Object.assign(_pending, body)
      }
    }, 400)
  }

  function get(key, fallback = undefined) {
    const v = prefs.value?.[key]
    return v === undefined ? fallback : v
  }

  return { prefs, loaded, load, save, get }
})
