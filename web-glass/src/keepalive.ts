import type { Keepalive } from './types'

type Window = Pick<Keepalive, 'start_time' | 'end_time' | 'start_day' | 'end_day'>

/** 结束标签：当天内写原值；跨天写「次日 HH:MM」；结束 00:00 写 24:00；起止相同返回空（等于全天） */
function endLabel(start: string, end: string): string {
  const s = minutesOf(start)
  const e = minutesOf(end)
  if (s < 0 || e < 0 || s === e) return ''
  if (e > s) return end
  return e === 0 ? '24:00' : `次日 ${end}`
}

/** 运行窗口的一句话摘要，空串 = 全天全月（等于没设窗口） */
export function windowSummary(item: Window): string {
  const parts: string[] = []
  if (item.start_time && item.end_time) {
    const label = endLabel(item.start_time, item.end_time)
    if (label) parts.push(`每天 ${item.start_time}–${label}`)
  }
  if (item.start_day && item.end_day) parts.push(`每月 ${item.start_day}–${item.end_day} 日`)
  return parts.join(' · ')
}

/** HH:MM → 分钟数，格式不对返回 -1 */
export function minutesOf(value: string): number {
  const match = /^(\d{2}):(\d{2})$/.exec(value)
  if (!match) return -1
  return Number(match[1]) * 60 + Number(match[2])
}
