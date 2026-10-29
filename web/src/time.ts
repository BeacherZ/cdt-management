/**
 * 界面上的时间显示统一走这里，全部以后端为准。
 *
 * 为什么不让各组件自己 `new Date()`：排班、事件、解析记录的时间都属于**后端时区**
 * （系统设置里的那个），而浏览器的本地时区是用户所在位置的。用户在国外打开面板时，
 * 两边能差几个小时甚至一整天 —— 排班表说「今天 0 点换班」，页面却按另一个时区
 * 显示成昨天，看着像数据错了。所以：
 *   · 时区（TZ）、今天（TODAY）、当前时刻（NOW_TS）都从 /api/v1/overview 下发；
 *   · 倒计时 / 「几分钟前」以收到后端时间的那个**浏览器时刻**为锚点递增，
 *     不用本机时钟 —— 用户把电脑时间调错也不会影响显示。
 */

let TZ = 'Asia/Shanghai'
/** 后端视角的「今天」，YYYY-MM-DD */
let TODAY = ''
/** 收到 overview 那一刻的后端秒级时间戳；0 表示还没收到过 */
let NOW_TS = 0
/** 收到 overview 那一刻的浏览器毫秒时间戳，用来算「又过去了多久」 */
let CLIENT_AT = 0

/** App 每次拿到 overview 都调一次，把后端时钟同步进来。 */
export function syncClock(clock: {
  timezone?: string
  today?: string
  now_ts?: number
}): void {
  if (clock.timezone) TZ = clock.timezone
  if (clock.today) TODAY = clock.today
  if (clock.now_ts) {
    NOW_TS = clock.now_ts
    CLIENT_AT = Date.now()
  }
}

/** 后端时区名，设置页之类需要显示的地方用。 */
export function backendTimezone(): string {
  return TZ
}

/** 后端此刻的秒级时间戳：以同步点为锚点 + 浏览器流逝的时间。 */
export function currentTs(): number {
  if (!NOW_TS) return Math.floor(Date.now() / 1000)
  return NOW_TS + Math.floor((Date.now() - CLIENT_AT) / 1000)
}

function parts(ts: number): Record<string, string> {
  const date = new Date(ts * 1000)
  const options: Intl.DateTimeFormatOptions = {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }
  let formatter: Intl.DateTimeFormat
  try {
    formatter = new Intl.DateTimeFormat('zh-CN', options)
  } catch {
    // 浏览器不认识这个时区名（极罕见）：退回本地时区，总比整页报错好
    formatter = new Intl.DateTimeFormat('zh-CN', { ...options, timeZone: undefined })
  }
  const result: Record<string, string> = {}
  for (const item of formatter.formatToParts(date)) {
    if (item.type !== 'literal') result[item.type] = item.value
  }
  return result
}

/** 后端时区下的 YYYY-MM-DD，用来判断「是不是同一天」。 */
export function dayKey(ts: number): string {
  if (!ts) return ''
  const p = parts(ts)
  return `${p.year}-${p.month}-${p.day}`
}

/** 后端时区下「今天」的 YYYY-MM-DD。 */
export function backendToday(): string {
  return TODAY || dayKey(currentTs())
}

/** 这个时间戳是不是后端时区里的今天。 */
export function isBackendToday(ts: number): boolean {
  if (!ts) return false
  return dayKey(ts) === backendToday()
}

/** HH:MM（事件进度、账单更新时间这类只关心时刻的地方）。 */
export function clockText(ts: number): string {
  if (!ts) return '—'
  const p = parts(ts)
  return `${p.hour}:${p.minute}`
}

/** 月-日 时:分，跨天时补日期用（例：11-01 00:00）。 */
export function monthDayTimeText(ts: number): string {
  if (!ts) return '—'
  const p = parts(ts)
  return `${p.month}-${p.day} ${p.hour}:${p.minute}`
}

/** 完整日期时间，YYYY-MM-DD HH:MM。 */
export function dateTimeText(ts: number): string {
  if (!ts) return '—'
  const p = parts(ts)
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`
}

/** 日期，YYYY-MM-DD。 */
export function dateText(ts: number): string {
  if (!ts) return '—'
  const p = parts(ts)
  return `${p.year}-${p.month}-${p.day}`
}

/** 相对时间：「刚刚 / N 分钟前 / N 小时前 / 日期」。基准是后端时间，不看本机时钟。 */
export function timeAgo(ts: number): string {
  if (!ts) return '—'
  const diff = currentTs() - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return dateText(ts)
}
