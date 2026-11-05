import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import http from '@/api/request'

/**
 * 代理池。
 *
 * 原来代理只存在 localStorage（纯字符串数组），后端完全不知道每个代理用了
 * 几次 —— 既没法限流，也没法在某个代理被风控后自动换走。
 *
 * 现在池子由后端 SQLite（proxy_pool 表）持有：带启用状态、成功注册上限、
 * 用量计数。**不再使用 localStorage** —— 数据只认后端这一份，
 * 换台机器打开也是同一份代理池。
 */
export const useProxyStore = defineStore('proxy', () => {
  // 后端返回的完整对象（含 enabled / max_uses / used_count / status）
  const items = ref([])
  const stats = ref({ total: 0, available: 0, disabled: 0, exhausted: 0 })
  const loaded = ref(false)

  // 纯代理地址列表（下拉框用）
  const list = computed(() => items.value.map((i) => i.proxy))
  const text = computed(() => list.value.join('\n'))
  const count = computed(() => items.value.length)

  async function load() {
    const data = await http.get('/api/proxy/pool')
    items.value = data.items || []
    stats.value = data.stats || { total: 0, available: 0, disabled: 0, exhausted: 0 }
    loaded.value = true
    return data
  }

  /** 批量导入。mode: replace（覆盖，保留已有代理的用量）/ append（追加）。 */
  async function save(textValue, mode = 'replace') {
    const data = await http.post('/api/proxy/pool', { text: textValue, mode })
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  /** 局部更新（启用开关 / 上限 / 备注）。 */
  async function update(proxy, patch) {
    const data = await http.patch('/api/proxy/pool', { proxy, ...patch })
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  async function remove(proxy) {
    const data = await http.delete(`/api/proxy/pool/${encodeURIComponent(proxy)}`)
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  async function clear() {
    const data = await http.delete('/api/proxy/pool')
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  /** 重置用量计数（不删代理）。不传 proxy = 全部重置。 */
  async function resetCounts(proxy) {
    const data = await http.post('/api/proxy/pool/reset', { proxy: proxy || null })
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  /**
   * 清零 CF 拦截计数。
   *
   * 场景：静态代理被 CF 拉黑后自动停用了，主人换了 IP / 供应商修了之后
   * 手动点一下就能重新启用，不用删了重导。
   */
  async function resetCfBlocked(proxy) {
    const data = await http.post('/api/proxy/pool/cf_reset', { proxy: proxy || null })
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  /**
   * 检测代理池里哪些支持 IPv6 出口。
   *
   * IPv6 的地址空间比 IPv4 大几个数量级，同一 /64 段里的"邻居"少得多
   * → 被 Cloudflare 按 IP 段批量拉黑的概率低得多。
   */
  async function testIpv6(proxies, timeout = 10) {
    const data = await http.post('/api/proxy/pool/ipv6_test', { proxies, timeout })
    items.value = data.items || []
    stats.value = data.stats || stats.value
    return data
  }

  /** 找某个代理的详情。 */
  function find(proxy) {
    return items.value.find((i) => i.proxy === proxy) || null
  }

  /** 下拉框里显示的标签：带上用量，超限/停用的标出来。 */
  function labelOf(proxy) {
    const it = find(proxy)
    if (!it) return proxy
    const used = `${it.used_count}/${it.max_uses || '∞'}`
    if (it.status === 'disabled') return `${proxy}（已停用）`
    if (it.status === 'exhausted') return `${proxy}（已满 ${used}）`
    return `${proxy}（已用 ${used}）`
  }

  return {
    items, stats, list, text, count, loaded,
    load, save, update, remove, clear, resetCounts, resetCfBlocked, testIpv6,
    find, labelOf,
  }
})

/**
 * 判断代理格式是否合法：[scheme://][user:pass@]host:port
 * 协议可省略——省略时 curl 按 HTTP 代理处理，所以裸写 host:port 也算合法。
 */
export function isValidProxy(p) {
  return /^((socks5h?|socks4|https?):\/\/)?\S+:\d+$/i.test(p.trim())
}

/** 该代理生效的协议类型（用于提示：未写协议默认 http）。 */
export function proxyScheme(p) {
  const m = /^(socks5h?|socks4|https?):\/\//i.exec(p.trim())
  return m ? m[1].toLowerCase() : 'http(默认)'
}
