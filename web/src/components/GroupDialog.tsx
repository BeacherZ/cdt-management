import { useState } from 'react'
import { ArrowDown, ArrowUp, Divide, GripVertical, Plus, Repeat, Trash2, TriangleAlert } from 'lucide-react'
import { post, SLOW_TIMEOUT_MS } from '../api'
import type { Server, ShiftGroup } from '../types'
import { Modal, Segmented, Switch } from './ui'
import ServerPicker from './ServerPicker'

type Cycle = 'day' | 'month'

type Row = {
  /** 0 = 还没选服务器 */
  server_id: number
  hours: number
  /** 一月一轮用：本月连上多少天 */
  days: number
  enabled: boolean
  slot_name: string
  start_time: string
  end_time: string
}

/** 新建时先摆两行空位：选择框留空，由用户自己挑，不自动塞前两台 */
function emptyRow(): Row {
  return {
    server_id: 0,
    hours: 12,
    days: 0,
    enabled: true,
    slot_name: '',
    start_time: '',
    end_time: '',
  }
}

/** 把 total 天分给 count 个人，除不尽的余数全给最后一位 */
function spreadDays(count: number, total: number): number[] {
  if (count <= 0) return []
  const base = Math.max(1, Math.floor(total / count))
  return Array.from({ length: count }, (_, index) =>
    index < count - 1 ? base : Math.max(1, total - base * (count - 1)),
  )
}

type MonthSpan = {
  startDay: number
  endDay: number
  span: number
  filled: boolean
}

/** 一月一轮：把每行天数换算成「几号到几号」。
 *
 * 1 号起按顺序铺，最后一位一直排到月末（每月天数变了会自动伸缩）；
 * 前面各按自己填的天数，但会给后面每行留至少 1 天，避免把整月吃光。
 *
 * 停用的成员不占日子：引擎的 active_members 会把他过滤掉，这里要是还给他排段，
 * 界面上写着「1–10 / 11–20 / 21–31」，实际跑起来是「1–10 / 11–31」，表里不一。
 */
function monthSpans(rows: Row[], monthDays: number): MonthSpan[] {
  const live = rows.filter((row) => row.enabled)
  let cursor = 1
  let liveIndex = 0
  return rows.map((row) => {
    if (!row.enabled) {
      return { startDay: 0, endDay: 0, span: 0, filled: false }
    }
    const last = liveIndex === live.length - 1
    const reserve = live.length - 1 - liveIndex
    const remain = Math.max(monthDays - cursor + 1, 0)
    const want = Math.max(0, Math.round(row.days || 0))
    const span = last ? remain : Math.min(want, Math.max(remain - reserve, 0))
    const startDay = cursor
    cursor += span
    liveIndex += 1
    return {
      startDay,
      endDay: span > 0 ? startDay + span - 1 : 0,
      span,
      filled: span > 0,
    }
  })
}

/** 把「HH:MM 起始 + 时长小时」换算成「HH:MM 结束」；算到 24:00 归零成 00:00（表示到午夜） */
function endFromStart(start: string, hours: number): string {
  if (!start || !hours) return ''
  const [sh, sm] = start.split(':').map(Number)
  if (Number.isNaN(sh) || Number.isNaN(sm)) return ''
  const total = (sh * 60 + sm + Math.round(hours * 60)) % (24 * 60)
  const h = Math.floor(total / 60)
  const m = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

/** 由起止时刻算时长（小时）；end 为 00:00 表示到午夜，按 +24 小时算 */
function slotDuration(start: string, end: string): number {
  if (!start || !end) return 0
  const [sh, sm] = start.split(':').map(Number)
  const [eh, em] = end.split(':').map(Number)
  if ([sh, sm, eh, em].some(Number.isNaN)) return 0
  let minutes = eh * 60 + em - (sh * 60 + sm)
  if (minutes <= 0) minutes += 24 * 60
  return Math.round((minutes / 60) * 2) / 2
}

/** 把「距 0 点的分钟数」转成 HH:MM，1440 归零 */
function minuteToHHMM(total: number): string {
  const t = ((total % 1440) + 1440) % 1440
  const h = Math.floor(t / 60)
  const m = t % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

type Props = {
  group: ShiftGroup | null
  servers: Server[]
  /** 今天所在月份的天数，由后端按排班时区算好下发：平摊、日期区间、"本月 N 天" 全用它 */
  monthDays: number
  onClose: () => void
  onSaved: (message: string) => void
  /** 在选择器里取消了某台机器的单机保活后，让父级刷新一次归属 */
  onKeepaliveCleared?: () => void
}

export default function GroupDialog({
  group,
  servers,
  monthDays,
  onClose,
  onSaved,
  onKeepaliveCleared,
}: Props) {
  const editing = Boolean(group && group.id)
  const [name, setName] = useState(group?.name ?? '联合换班')
  const [enabled, setEnabled] = useState(group?.enabled ?? true)
  const [failover, setFailover] = useState(group?.failover_enabled ?? true)
  const [cycle, setCycle] = useState<Cycle>(group?.cycle === 'month' ? 'month' : 'day')
  const [rows, setRows] = useState<Row[]>(() => {
    if (group?.members?.length) {
      return group.members.map((member) => ({
        server_id: member.server_id,
        hours: member.hours,
        days: member.days ?? 0,
        enabled: member.enabled,
        slot_name: member.slot_name ?? '',
        start_time: member.start_time ?? '',
        end_time: member.end_time ?? '',
      }))
    }
    return [emptyRow(), emptyRow()]
  })
  const [picking, setPicking] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // 日期框正在敲数字时的临时值：不然「9→20」敲到「2」就被夹到 9，第二位数字进不来
  const [dayDraft, setDayDraft] = useState<{ index: number; field: 'start' | 'end'; text: string } | null>(
    null,
  )

  const total = rows
    .filter((row) => row.enabled && row.server_id)
    .reduce((sum, row) => sum + (row.hours || 0), 0)

  // 月模式每行的实际日期段：最后一位排到月末，前面按天数顺延（停用行不占日子）
  const spans = monthSpans(rows, monthDays)
  // 「最后一位」指最后一个启用的成员：停用的行不参与排班，止日不该锁在他身上
  const isLastRow = (index: number) => !rows.slice(index + 1).some((row) => row.enabled)
  // 「第一位」同理：停用行排在前面时，起日锁在第一个启用的成员身上
  const firstLive = (index: number) =>
    Boolean(rows[index]?.enabled) && !rows.slice(0, index).some((row) => row.enabled)
  // 一个字都没填时不显示任何日期，避免看起来像已经分好了
  const anyDayFilled = rows.some((row) => row.enabled && (row.days || 0) > 0)

  /** 切周期：只切模式，不替用户预填上半月/下半月，日期由他自己选 */
  function changeCycle(next: Cycle) {
    setCycle(next)
    setError('')
  }

  function labelOf(serverId: number): string {
    return servers.find((server) => server.id === serverId)?.label ?? '（服务器已删除）'
  }

  /** 底部预览：把当月怎么分的直接写成日期段，月末伸缩一眼可见 */
  function monthPreview(): string {
    const parts = rows
      .map((row, index) => ({ row, span: spans[index] }))
      .filter((item) => item.span.span > 0 && item.row.server_id)
      .map((item) => `${item.span.startDay}–${item.span.endDay} 日 ${labelOf(item.row.server_id)}`)
    if (parts.length === 0) return '选好服务器后，这里显示当月怎么分'
    return `本月 ${monthDays} 天：${parts.join(' ｜ ')}`
  }

  function update(index: number, patch: Partial<Row>) {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function move(index: number, direction: -1 | 1) {
    setRows((current) => {
      const next = [...current]
      const target = index + direction
      if (target < 0 || target >= next.length) return current
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  function remove(index: number) {
    setRows((current) => current.filter((_, i) => i !== index))
  }

  function add() {
    const used = new Set(rows.map((row) => row.server_id))
    if (!servers.some((server) => !used.has(server.id))) {
      setError('所有服务器都已经在这个组里了')
      return
    }
    setRows((current) => [...current, emptyRow()])
    setError('')
  }

  /** 平摊工时：24 小时按成员数均分，组员 1 从 0 点起步，依次首尾相接 */
  function splitEvenly() {
    if (rows.length === 0) return
    if (rows.some((row) => !row.server_id)) {
      setError('先把每行的服务器选上，再平摊工时')
      return
    }
    const count = rows.length
    // 用分钟做整数均分，除不尽的余数补给最后一位，保证总和正好 24 小时
    const base = Math.floor((24 * 60) / count)
    const remainder = 24 * 60 - base * count
    let cursor = 0
    setRows((current) =>
      current.map((row, index) => {
        const start = minuteToHHMM(cursor)
        const thisMinutes = index === count - 1 ? 24 * 60 - cursor : base + (index < remainder ? 1 : 0)
        cursor += thisMinutes
        const end = minuteToHHMM(cursor)
        return { ...row, start_time: start, end_time: end, hours: slotDuration(start, end) }
      }),
    )
    setError('')
  }

  /** 平摊天数：当月天数按启用成员数均分，组员 1 从 1 号起步，余数给最后一位 */
  function splitDays() {
    if (rows.length === 0) return
    if (rows.some((row) => !row.server_id)) {
      setError('先把每行的服务器选上，再平摊天数')
      return
    }
    const live = rows.filter((row) => row.enabled)
    const spread = spreadDays(live.length, monthDays)
    let cursor = 0
    setRows((current) =>
      current.map((row) => {
        if (!row.enabled) return { ...row, days: 0 }
        const days = spread[cursor] ?? 0
        cursor += 1
        return { ...row, days }
      }),
    )
    setError('')
  }

  /** 改月模式的止日：本行吃到这一天，后面各行的区间自动顺延 */
  function setEndDay(index: number, value: number) {
    if (!rows[index]?.enabled) return
    const start = spans[index].startDay || 1
    // 后面还有几个启用的成员，就得给他留几天；停用的行不占位置
    const reserve = rows.slice(index + 1).filter((row) => row.enabled).length
    const max = Math.max(monthDays - reserve, start)
    const day = Math.min(Math.max(Math.round(value) || start, start), max)
    update(index, { days: day - start + 1 })
  }

  /** 改月模式的起始日：等价于让上一个启用的成员提前收工，本行的止日保持不动 */
  function setStartDay(index: number, value: number) {
    if (index <= 0 || !rows[index]?.enabled) return
    const prevIndex = rows
      .slice(0, index)
      .map((row, i) => (row.enabled ? i : -1))
      .filter((i) => i >= 0)
      .pop()
    if (prevIndex === undefined) return
    const prev = spans[prevIndex]
    const reserve = rows.slice(index + 1).filter((row) => row.enabled).length
    const endDay = isLastRow(index)
      ? monthDays
      : spans[index].filled
        ? spans[index].endDay
        : monthDays - reserve
    const lower = prev.startDay + 1
    const day = Math.min(Math.max(Math.round(value) || lower, lower), Math.max(endDay, lower))
    setRows((current) =>
      current.map((row, i) => {
        if (i === prevIndex) return { ...row, days: day - prev.startDay }
        if (i === index) return { ...row, days: endDay - day + 1 }
        return row
      }),
    )
  }

  /** 日期输入框显示值：正在输入时用草稿，否则用算出来的日期段 */
  function dayValue(index: number, field: 'start' | 'end'): string {
    if (dayDraft && dayDraft.index === index && dayDraft.field === field) return dayDraft.text
    const span = spans[index]
    if (!anyDayFilled || !span.filled) return ''
    return String(field === 'start' ? span.startDay : span.endDay)
  }

  /** 边敲边生效：草稿留原文，实际排班按数字走 */
  function typeDay(index: number, field: 'start' | 'end', raw: string) {
    const text = raw.replace(/[^0-9]/g, '').slice(0, 2)
    setDayDraft({ index, field, text })
    if (!text) return
    if (field === 'end') setEndDay(index, Number(text))
    else setStartDay(index, Number(text))
  }

  /** 改起止时刻 → 实时回填该行工时 */
  function updateTime(index: number, patch: { start_time?: string; end_time?: string }) {
    setRows((current) =>
      current.map((row, i) => {
        if (i !== index) return row
        const next = { ...row, ...patch }
        next.hours = slotDuration(next.start_time, next.end_time)
        return next
      }),
    )
  }

  /** 改工时 → 自动填该行起止时刻：起始 = 前一位的结束（首位从 00:00），结束 = 起始 + 工时 */
  function updateHours(index: number, hours: number) {
    setRows((current) => {
      const start = index === 0 ? '00:00' : current[index - 1]?.end_time || '00:00'
      return current.map((row, i) => {
        if (i !== index) return row
        const end = endFromStart(start, hours)
        return { ...row, hours, start_time: start, end_time: end }
      })
    })
  }

  async function save() {
    setError('')
    const blank = rows.findIndex((row) => !row.server_id)
    if (blank >= 0) {
      setError(`第 ${blank + 1} 行还没选服务器`)
      return
    }
    if (rows.length < 2) {
      setError('至少需要两台服务器')
      return
    }
    if (cycle === 'month') {
      // 一月一轮按日期排：每行都得落到具体日子，界面显示的是几号到几号
      // （只看启用的行：停用的成员本来就不参与排班）
      const hole = spans.findIndex((span, index) => rows[index].enabled && span.span < 1)
      if (hole >= 0) {
        setError(
          isLastRow(hole)
            ? '最后一位没有可排的日期了，先把前面成员的天数缩短一点'
            : `第 ${hole + 1} 行还没排到日期，填一下止日或点「平摊天数」`,
        )
        return
      }
    } else if (total <= 0) {
      setError('总时长必须大于 0')
      return
    }
    const ids = rows.map((row) => row.server_id)
    if (new Set(ids).size !== ids.length) {
      setError('同一台服务器不能重复添加')
      return
    }
    // 起止时刻要么全空（走老 hours 累加），要么成对填写；月模式根本不看时刻
    for (const row of cycle === 'month' ? [] : rows) {
      const hasStart = Boolean(row.start_time)
      const hasEnd = Boolean(row.end_time)
      if (hasStart !== hasEnd) {
        setError('班次起止时刻要成对填写，或都留空')
        return
      }
    }
    // 只有部分成员填了时刻会被引擎当成「没填」整组退回按小时轮换，
    // 界面上却还显示着时刻表 —— 直接拦住，别让它静默降级
    const enabledRows = cycle === 'month' ? [] : rows.filter((row) => row.enabled)
    const timedRows = enabledRows.filter((row) => row.start_time && row.end_time)
    if (timedRows.length > 0 && timedRows.length !== enabledRows.length) {
      setError('要么每台都填起止时刻（按时刻表轮换），要么全部留空（按班次时长轮换）')
      return
    }
    setBusy(true)
    try {
      const result = await post<{ id: number; bootstrap?: { error?: string } }>(
        '/api/v1/groups',
        {
          id: group?.id ?? 0,
          name,
          enabled,
          failover_enabled: failover,
          cycle,
          // 域名绑定不在这里改：由 DNS 页统一维护，避免两个入口写出不一致的绑定
          members: rows.map((row, index) => ({
            server_id: row.server_id,
            hours: row.hours,
            // 月模式提交界面上实际显示的日期段，所见即所存
            days: cycle === 'month' ? spans[index].span : 0,
            enabled: row.enabled,
            slot_name: row.slot_name,
            start_time: cycle === 'month' ? '' : row.start_time,
            end_time: cycle === 'month' ? '' : row.end_time,
          })),
        },
        // 新建组会立刻按排班表整队（等引擎锁 + 开机 + 停待机），普通 20 秒不够
        SLOW_TIMEOUT_MS,
      )
      if (editing) {
        onSaved('换班组已更新')
      } else if (result?.bootstrap?.error) {
        // 组本身已经存好，只是首轮整队没跑通 —— 说清楚，别让人以为组没建成
        onSaved('换班组已创建，但校正组员状态失败，详情见最近事件')
      } else {
        onSaved('换班组已创建，正在按排班表校正组员状态')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={editing ? '编辑联合换班组' : '新建联合换班组'}
      icon={<Repeat size={16} style={{ color: 'var(--green)' }} />}
      onClose={onClose}
      wide
      footer={
        <>
          <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
            {cycle === 'month'
              ? monthPreview()
              : total > 0
                ? `一个周期 ${total} 小时，按顺序轮换`
                : '选好服务器后，这里显示一个周期多长'}
          </span>
          <span className="spacer" />
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={save} disabled={busy}>
            保存
          </button>
        </>
      }
    >
      {servers.length < 2 && (
        <div className="auth-error">
          <TriangleAlert size={14} />
          先去「服务器」页添加至少两台服务器
        </div>
      )}

      <div className="field">
        <label htmlFor="gname">组名称</label>
        <input
          id="gname"
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="联合换班"
        />
      </div>

      <div className="field">
        <label>排班周期</label>
        <Segmented
          value={cycle}
          options={[
            { value: 'day', label: '一天一轮' },
            { value: 'month', label: '一月一轮' },
          ]}
          onChange={changeCycle}
        />
        <div className="hint">
          {cycle === 'month'
            ? '周期 1 个月：组员按天轮换，每月 1 号 0 点从第 1 位开始，最后一位排到月末。'
            : '周期 1 天：组员在 24 小时内轮换，填了起止时刻就按固定时刻表走。'}
        </div>
      </div>

      <div className="field">
        <label>
          {cycle === 'month'
            ? '轮换顺序（每台当班几号到几号）'
            : '轮换顺序（每台运行多少小时 / 班次时刻）'}
        </label>
        <div className="member-editor">
          {rows.map((row, index) => (
            <div className="member-row" key={`${row.server_id}-${index}`}>
              <GripVertical size={14} style={{ color: 'var(--muted-soft)' }} />
              <span className="member-order">{index + 1}</span>
              <button
                type="button"
                className={`select member-pick${row.server_id ? '' : ' is-empty'}`}
                title="点击选择服务器"
                onClick={() => setPicking(index)}
              >
                <span className="member-pick-text">
                  {row.server_id ? labelOf(row.server_id) : '选择服务器'}
                </span>
              </button>
              <input
                className="input member-slot"
                value={row.slot_name}
                placeholder="班次名称"
                onChange={(event) => update(index, { slot_name: event.target.value })}
              />
              {cycle === 'day' ? (
                <>
                  <div className="member-times">
                    <input
                      className="input"
                      type="time"
                      value={row.start_time}
                      onChange={(event) => updateTime(index, { start_time: event.target.value })}
                      title="起始时刻"
                    />
                    <span className="member-time-sep">–</span>
                    <input
                      className="input"
                      type="time"
                      value={row.end_time}
                      onChange={(event) => updateTime(index, { end_time: event.target.value })}
                      title="结束时刻（到午夜选 00:00）"
                    />
                  </div>
                  <div className="member-hours">
                    <input
                      className="input"
                      type="number"
                      min={0.5}
                      step={0.5}
                      value={row.hours}
                      onChange={(event) => updateHours(index, Number(event.target.value))}
                    />
                    <span>小时</span>
                  </div>
                </>
              ) : (
                <div className="member-days">
                  <input
                    className="input"
                    type="text"
                    inputMode="numeric"
                    value={dayValue(index, 'start')}
                    placeholder={firstLive(index) ? '1' : ''}
                    readOnly={firstLive(index) || !row.enabled}
                    title={
                      firstLive(index)
                        ? '每月从 1 号 0 点起算'
                        : '起始日：改了等于让上一个成员提前收工'
                    }
                    onChange={(event) => typeDay(index, 'start', event.target.value)}
                    onBlur={() => setDayDraft(null)}
                  />
                  <span className="member-time-sep">–</span>
                  <input
                    className="input"
                    type="text"
                    inputMode="numeric"
                    value={dayValue(index, 'end')}
                    placeholder={isLastRow(index) ? String(monthDays) : ''}
                    readOnly={isLastRow(index) || !row.enabled}
                    title={
                      !row.enabled
                        ? '这个成员已停用，本月不排班'
                        : isLastRow(index)
                          ? `最后一位自动排到月末（本月 ${monthDays} 天）`
                          : '结束日：本行上到这一天，后面的行顺延'
                    }
                    onChange={(event) => typeDay(index, 'end', event.target.value)}
                    onBlur={() => setDayDraft(null)}
                  />
                  <span>日</span>
                </div>
              )}
              <button
                type="button"
                className="btn ghost icon sm"
                title="上移"
                disabled={index === 0}
                onClick={() => move(index, -1)}
              >
                <ArrowUp size={13} />
              </button>
              <button
                type="button"
                className="btn ghost icon sm"
                title="下移"
                disabled={index === rows.length - 1}
                onClick={() => move(index, 1)}
              >
                <ArrowDown size={13} />
              </button>
              <button
                type="button"
                className="btn ghost icon sm"
                title="移除"
                onClick={() => remove(index)}
              >
                <Trash2 size={13} style={{ color: 'var(--red)' }} />
              </button>
            </div>
          ))}
        </div>
        <div className="member-toolbar">
          <button className="btn sm" onClick={add}>
            <Plus size={13} /> 添加成员
          </button>
          {cycle === 'month' ? (
            <button
              className="btn sm"
              title="当月天数按成员数均分，除不尽的余数给最后一位"
              onClick={splitDays}
            >
              <Divide size={13} /> 平摊天数
            </button>
          ) : (
            <button
              className="btn sm"
              title="24 小时按成员数均分，组员 1 从 0 点起步"
              onClick={splitEvenly}
            >
              <Divide size={13} /> 平摊工时
            </button>
          )}
        </div>
        <div className="hint">
          {cycle === 'month'
            ? '从 1 号 0 点起按日期依次轮换，不看小时边界；最后一位自动排到月末，2 月只有 28/29 天时会自己缩短。交班时先给下一位开机，确认运行后才把上一位节省停机。'
            : '填写「班次名称 + 起止时刻」后按固定时刻表每天自动换班；留空则按「小时数」顺序轮换。交接同样先开下一位、确认运行后才停上一位。'}
        </div>
      </div>

      <div className="field">
        <label>DDNS 记录</label>
        <div className="readonly-value">
          {group?.ddns_record ? (
            <code>{group.ddns_record}</code>
          ) : (
            <span className="muted">未绑定 —— 这个组换班时不会去动任何域名</span>
          )}
        </div>
        <div className="hint">
          绑定在左侧「DNS」页维护：一条域名只归一个组，两边不会抢。
        </div>
      </div>

      <div>
        <Switch label="启用该换班组" checked={enabled} onChange={setEnabled} />
        <Switch
          label="允许故障转移"
          hint="当班流量达标，或当班被抢占后一直起不来，自动切到另一台成员；流量全超且无人可接时全局熔断停机。一月一轮按天代班：每天 0 点重新尝试原当班，恢复了就交回排班"
          checked={failover}
          onChange={setFailover}
        />
      </div>

      {error && (
        <div className="auth-error">
          <TriangleAlert size={14} />
          {error}
        </div>
      )}

      {picking !== null && (
        <ServerPicker
          servers={servers}
          currentGroupId={group?.id || undefined}
          taken={
            new Set(
              rows
                .filter((row, index) => index !== picking && row.server_id)
                .map((row) => row.server_id),
            )
          }
          onClose={() => setPicking(null)}
          onKeepaliveCleared={onKeepaliveCleared}
          onPick={(server) => {
            const index = picking
            setRows((current) =>
              current.map((row, i) => (i === index ? { ...row, server_id: server.id } : row)),
            )
            setPicking(null)
            setError('')
          }}
        />
      )}
    </Modal>
  )
}
