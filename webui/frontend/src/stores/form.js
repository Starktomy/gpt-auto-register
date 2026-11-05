import { defineStore } from 'pinia'
import { nextTick, reactive, watch } from 'vue'
import { usePrefsStore } from './prefs'

// 跨页面共享的表单字段（proxy 在 注册 / 自动跑号 / Plus 检测 三处共用）。
//
// ⚠️ 以前这些值存在 localStorage —— 只跟着**这一个浏览器**走，换台机器 /
//    清个缓存就没了，后端也完全看不到。现在统一收到后端 SQLite
//    （settings 表的 prefs key），前后端同一份数据。
const defaults = {
  proxy: '',
  // OTP 等待秒数。
  // ⚠️ 原来是 10 —— 对直连邮箱够用，但对 Remail 这类**聚合转发**平台不够：
  //    邮件要经平台中转再投到目标邮箱，实测 20~30 秒才到（发码 18:34:41 → 到达 18:35:05）。
  //    10 秒必然超时，注册跑到最后一步全废。
  // 180 秒对所有 provider 都够（outlook / cf_temp 的等待时间本来就在 180 上下）。
  otpTimeout: 180,
  autoConcurrency: 1,
  autoCoolDown: 3,
  autoTargetCount: 0,
  // 注册后自动绑 2FA。单次 / 批量都**默认 true**：每个号都要 2FA。
  // 仍然拆成两个字段（而不是共用一个）：单次页是验 bug / 试流程的测试台，
  // 共用的话在那边临时关掉，回头批量跑几百个号就全裸奔了。
  want2fa: true,
  autoWant2fa: true,
  // 是否换取 refresh_token（Codex OAuth）。
  //
  // ⚠️ 默认**关闭**：
  //   1. 拿 rt 要额外跑一遍 Codex OAuth 授权链。这条链在某些情况下会被弹回
  //      /log-in，然后**再发一封验证码邮件**（auth_flow.py 的 need_otp 分支）。
  //      对长效邮箱无所谓，对**短效邮箱（code 模式，一次性）**就是致命的：
  //      第二封收不到 → 白等 180 秒 → 整个号白注册。
  //   2. 后端还会在**接码未启用**时自动跳过 Codex（add-phone 分支推进不动，
  //      跑了也是白跑）。
  //   3. access_token 的获取**不依赖** Codex OAuth —— 它走 get_auth_session。
  //      所以关掉这个开关，access_token 照拿。
  //
  // 取舍：chatgpt2api 靠 refresh_token 给 access_token 自动续期，没有 rt 的号
  // 到期后需要重新注册 —— 需要续期时手动打开这个开关。
  wantRefreshToken: false,
  autoWantRefreshToken: false,
}

// el-select 的 clearable 清空时把值写成 **undefined**（不是 ''），而 proxy 在三个
// 页面都是 `form.value.proxy.trim()` 直接调 —— 主人点一次叉，下次提交就
// "Cannot read properties of undefined (reading 'trim')"。这里统一兜底成字符串，
// 免得每个调用点各写各的可选链，也顺手挡住后端可能存下来的脏值。
export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  const prefs = usePrefsStore()
  const form = reactive({ ...defaults })

  // 用户在本会话里手动改过的字段。
  // ⚠️ 为什么需要它：load() 是异步的（要等后端返回），而页面上的开关
  //    用户可能已经动了。如果 load() 无条件覆盖所有字段，就会把用户的
  //    操作**冲掉** —— 表现就是"关掉 2FA 开关没反应"、"改了并发数又弹回去"。
  //    所以：只应用**用户还没动过**的字段。
  const dirty = new Set()

  // load() 正在往 form 里灌后端值。
  // ⚠️ 必须能区分"程序赋值"和"用户操作" —— 否则 load() 内部的赋值也会
  //    被 watch 当成用户修改，把所有字段标成 dirty，之后后端就再也
  //    同步不进来了（每次 load 都被跳过）。
  //    watch 是 pre-flush 的，会在下一个 tick 跑；所以灌完值要等 nextTick
  //    让 watch 跑完，再把标志复位。
  let _applying = 0

  // 从后端拉一次，用存下来的值覆盖默认值。
  // 没有存过的字段保持默认值 —— 主人清掉偏好后回到出厂设置，这是合理的。
  async function load() {
    try {
      await prefs.load()
    } catch (_) {
      // 拉不到就用默认值，不阻塞界面
      return
    }
    _applying += 1
    try {
      for (const key of Object.keys(defaults)) {
        // 用户已经动过这个字段 → 保住用户的值，别用后端的覆盖掉
        if (dirty.has(key)) continue
        const v = prefs.prefs?.[key]
        if (v !== undefined && v !== null) form[key] = v
      }
      await nextTick()   // 等 watch 跑完再复位标志
    } finally {
      _applying -= 1
    }
  }

  // clearable 清空后 proxy 会变成 undefined，这里回填成 ''，保证是干净字符串。
  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  // 变更写回后端（prefs.save 内部有防抖），同时记下用户动过哪些字段。
  watch(form, (v) => {
    const patch = {}
    for (const key of Object.keys(defaults)) {
      patch[key] = v[key]
      // _applying > 0 时是程序在灌值，不算用户操作
      if (!_applying) dirty.add(key)
    }
    prefs.save(patch)
  }, { deep: true })

  return { form, load }
})
