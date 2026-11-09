import type { Keepalive } from './types'

type Window = Pick<Keepalive, 'start_time' | 'end_time' | 'start_day' | 'end_day'>

/** 结束 00:00 就是「到当天午夜」，按面板习惯写成 24:00，避免被读成零长度 */
function endLabel(start: string, end: string): string {
  return start !== '00:00' && end === '00:00' ? '24:00' : end
}

/** 运行窗口的一句话摘要，空串 = 全天全月（等于没设窗口） */
export function windowSummary(item: Window): string {
  const parts: string[] = []
  if (item.start_time && item.end_time) {
    parts.push(`每天 ${item.start_time}–${endLabel(item.start_time, item.end_time)}`)
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
