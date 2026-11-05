import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { usePrefsStore } from './prefs'

/**
 * 主题（深色 / 浅色）。
 *
 * 以前存 localStorage —— 只跟着这一个浏览器走，换台机器打开就变回默认。
 * 现在存后端（settings 表的 prefs key 里的 ui.dark），跟其它偏好一致。
 */
export const useThemeStore = defineStore('theme', () => {
  const prefs = usePrefsStore()
  const dark = ref(false)
  let _synced = false

  function apply() {
    document.documentElement.classList.toggle('dark', dark.value)
  }
  function toggle() {
    dark.value = !dark.value
  }

  /** 从后端读一次偏好并应用。应用启动时调。 */
  async function load() {
    try {
      await prefs.load()
    } catch (_) {
      // 拉不到就用默认（浅色）
      apply()
      return
    }
    const v = prefs.prefs?.['ui.dark']
    if (v !== undefined && v !== null) {
      dark.value = !!v
      _synced = true
    }
    apply()
  }

  // 变更写回后端。
  // ⚠️ _synced 之前不写：初始化时把后端值灌进 dark 会触发 watch，
  //    那时如果后端还没加载完，会把默认值反写回去，覆盖掉真实设置。
  watch(dark, (v) => {
    apply()
    if (!_synced) return
    prefs.save({ 'ui.dark': !!v })
  })

  return { dark, toggle, apply, load }
})
