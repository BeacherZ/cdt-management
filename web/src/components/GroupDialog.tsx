import { useState } from 'react'
import { ArrowDown, ArrowUp, Divide, GripVertical, Plus, Repeat, Trash2, TriangleAlert } from 'lucide-react'
import { post, SLOW_TIMEOUT_MS } from '../api'
import type { Server, ShiftGroup } from '../types'
import { Modal, Switch } from './ui'
import ServerPicker from './ServerPicker'

type Row = {
  /** 0 = 还没选服务器 */
  server_id: number
  hours: number
  enabled: boolean
  slot_name: string
  start_time: string
  end_time: string
}

/** 新建时先摆两行空位：选择框留空，由用户自己挑，不自动塞前两台 */
function emptyRow(): Row {
  return { server_id: 0, hours: 12, enabled: true, slot_name: '', start_time: '', end_time: '' }
}

/** 把「HH:MM 起始 + 时长小时」换算成「HH:MM 结束」，自动处理跨天（次日 00:00 归零） */
function endFromStart(start: string, hours: number): string {
  if (!start || !hours) return ''
  const [sh, sm] = start.split(':').map(Number)
  if (Number.isNaN(sh) || Number.isNaN(sm)) return ''
  const total = (sh * 60 + sm + Math.round(hours * 60)) % (24 * 60)
  const h = Math.floor(total / 60)
  const m = total % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

/** 由起止时刻算时长（小时），支持跨天（end 早于等于 start 视为次日） */
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
  onClose: () => void
  onSaved: (message: string) => void
  /** 在选择器里取消了某台机器的单机保活后，让父级刷新一次归属 */
  onKeepaliveCleared?: () => void
}

export default function GroupDialog({ group, servers, onClose, onSaved, onKeepaliveCleared }: Props) {
  const editing = Boolean(group && group.id)
  const [name, setName] = useState(group?.name ?? '联合换班')
  const [enabled, setEnabled] = useState(group?.enabled ?? true)
  const [failover, setFailover] = useState(group?.failover_enabled ?? true)
  const [rows, setRows] = useState<Row[]>(() => {
    if (group?.members?.length) {
      return group.members.map((member) => ({
        server_id: member.server_id,
        hours: member.hours,
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

  const total = rows
    .filter((row) => row.enabled && row.server_id)
    .reduce((sum, row) => sum + (row.hours || 0), 0)

  function labelOf(serverId: number): string {
    return servers.find((server) => server.id === serverId)?.label ?? '（服务器已删除）'
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
    if (total <= 0) {
      setError('总时长必须大于 0')
      return
    }
    const ids = rows.map((row) => row.server_id)
    if (new Set(ids).size !== ids.length) {
      setError('同一台服务器不能重复添加')
      return
    }
    // 起止时刻要么全空（走老 hours 累加），要么成对填写
    for (const row of rows) {
      const hasStart = Boolean(row.start_time)
      const hasEnd = Boolean(row.end_time)
      if (hasStart !== hasEnd) {
        setError('班次起止时刻要成对填写，或都留空')
        return
      }
    }
    // 只有部分成员填了时刻会被引擎当成「没填」整组退回按小时轮换，
    // 界面上却还显示着时刻表 —— 直接拦住，别让它静默降级
    const enabledRows = rows.filter((row) => row.enabled)
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
          // 域名绑定不在这里改：由 DNS 页统一维护，避免两个入口写出不一致的绑定
          members: rows.map((row) => ({
            server_id: row.server_id,
            hours: row.hours,
            enabled: row.enabled,
            slot_name: row.slot_name,
            start_time: row.start_time,
            end_time: row.end_time,
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
            {total > 0 ? `一个周期 ${total} 小时，按顺序轮换` : '选好服务器后，这里显示一个周期多长'}
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
        <label>轮换顺序（每台运行多少小时 / 班次时刻）</label>
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
                  title="结束时刻（可跨天）"
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
          <button
            className="btn sm"
            title="24 小时按成员数均分，组员 1 从 0 点起步"
            onClick={splitEvenly}
          >
            <Divide size={13} /> 平摊工时
          </button>
        </div>
        <div className="hint">
          填写「班次名称 + 起止时刻」后按固定时刻表每天自动换班；留空则按「小时数」顺序轮换。
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
          label="允许流量故障转移"
          hint="当班流量达标时自动切到下一台没超的成员；找不到没超的成员（含状态读不到）就全局熔断停机"
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
