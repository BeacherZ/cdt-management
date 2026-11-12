import { useState } from 'react'
import { HeartPulse, TriangleAlert } from 'lucide-react'
import { post } from '../api'
import type { Keepalive, Server } from '../types'
import { Modal, Segmented, Switch } from './ui'

type Props = {
  /** 新建时必传：从实例卡片「加入保活」或选机器之后进来 */
  server?: Server | null
  /** 编辑时必传 */
  item?: Keepalive | null
  onClose: () => void
  onSaved: (message: string) => void
}

const MODE_OPTIONS: { value: 'all' | 'custom'; label: string }[] = [
  { value: 'all', label: '不限' },
  { value: 'custom', label: '指定' },
]

/**
 * 单机保活设置页：开关 + 可选运行时段 / 运行日期。
 *
 * 版式套用联合换班设置页（.field / .hint / .segmented），两个维度各用一个
 * 「不限 / 指定」切换：没设过窗口的人看到的是两个「不限」，不会被空输入框
 * 误导成「必须填」。
 */
export default function KeepaliveDialog({ server, item, onClose, onSaved }: Props) {
  const editing = Boolean(item?.id)
  const target = (item ?? server) as { label?: string; instance_id?: string } | null
  const [enabled, setEnabled] = useState(item ? item.enabled : true)

  const [timeMode, setTimeMode] = useState<'all' | 'custom'>(
    item?.start_time && item?.end_time ? 'custom' : 'all',
  )
  const [startTime, setStartTime] = useState(item?.start_time ?? '')
  const [endTime, setEndTime] = useState(item?.end_time ?? '')

  const [dayMode, setDayMode] = useState<'all' | 'custom'>(
    item?.start_day && item?.end_day ? 'custom' : 'all',
  )
  const [startDay, setStartDay] = useState(item?.start_day ? String(item.start_day) : '')
  const [endDay, setEndDay] = useState(item?.end_day ? String(item.end_day) : '')

  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function save() {
    setError('')
    let start_time = ''
    let end_time = ''
    if (timeMode === 'custom') {
      if (!startTime || !endTime) {
        setError('运行时段要填完整的起止时刻，或者切回「不限」')
        return
      }
      // 允许跨天：结束早于开始 = 跨天到次日；起止相同 = 全天（等于不限，界面已提示）
      start_time = startTime
      end_time = endTime
    }

    let start_day = 0
    let end_day = 0
    if (dayMode === 'custom') {
      const from = Number(startDay)
      const to = Number(endDay)
      if (![from, to].every((value) => Number.isInteger(value) && value >= 1 && value <= 31)) {
        setError('运行日期要填 1–31 的数字（2 月等小月会自动截到月末）')
        return
      }
      if (to < from) {
        setError('结束日不能早于开始日（不支持跨月）')
        return
      }
      start_day = from
      end_day = to
    }

    setBusy(true)
    try {
      if (editing) {
        await post(`/api/v1/keepalives/${item!.id}`, {
          enabled,
          start_time,
          end_time,
          start_day,
          end_day,
        })
        onSaved('单机保活已更新')
      } else {
        await post('/api/v1/keepalives', {
          server_id: server!.id,
          enabled,
          start_time,
          end_time,
          start_day,
          end_day,
        })
        onSaved('已加入单机保活')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={editing ? '编辑单机保活' : '单机保活设置'}
      icon={<HeartPulse size={16} style={{ color: 'var(--green)' }} />}
      onClose={onClose}
      footer={
        <>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            窗口外自动停机、窗口内自动拉起，两端都不推通知，只在事件里留痕
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
      <div className="field">
        <label>实例</label>
        <div className="meta-line" style={{ marginTop: 0 }}>
          <strong style={{ fontSize: 13 }}>{target?.label || '未知实例'}</strong>
          <code style={{ marginLeft: 8 }}>{target?.instance_id || '未填实例 ID'}</code>
        </div>
        <div className="hint">机器固定不可改；要换机器就移出保活后重新添加。</div>
      </div>

      <Switch
        checked={enabled}
        onChange={setEnabled}
        label="启用保活"
        hint="关掉后不再自动拉起，实例状态保持不变"
      />

      <div className="field">
        <label>运行时段</label>
        <Segmented value={timeMode} options={MODE_OPTIONS} onChange={setTimeMode} />
        {timeMode === 'custom' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
            <input
              className="input"
              type="time"
              style={{ flex: '1 1 0', width: 'auto' }}
              value={startTime}
              onChange={(event) => setStartTime(event.target.value)}
              title="开始时刻"
            />
            <span className="member-time-sep">–</span>
            <input
              className="input"
              type="time"
              style={{ flex: '1 1 0', width: 'auto' }}
              value={endTime}
              onChange={(event) => setEndTime(event.target.value)}
              title="结束时刻"
            />
          </div>
        )}
        {timeMode === 'custom' && startTime && endTime && startTime === endTime && (
          <div className="auth-error">
            <TriangleAlert size={14} />
            起止时刻相同等于全天运行，与「不限」没有区别
          </div>
        )}
        <div className="hint">结束早于开始表示跨天到次日；00:00 表示 24:00；起止相同等于全天。</div>
      </div>

      <div className="field">
        <label>运行日期</label>
        <Segmented value={dayMode} options={MODE_OPTIONS} onChange={setDayMode} />
        {dayMode === 'custom' && (
          <div className="member-days" style={{ marginTop: 6 }}>
            <input
              className="input"
              type="text"
              inputMode="numeric"
              value={startDay}
              placeholder="1"
              onChange={(event) => setStartDay(event.target.value.replace(/[^0-9]/g, '').slice(0, 2))}
              title="起始日"
            />
            <span className="member-time-sep">–</span>
            <input
              className="input"
              type="text"
              inputMode="numeric"
              value={endDay}
              placeholder="31"
              onChange={(event) => setEndDay(event.target.value.replace(/[^0-9]/g, '').slice(0, 2))}
              title="结束日"
            />
            <span>日</span>
          </div>
        )}
        <div className="hint">每月这个日期段内运行（1 号到月末，不跨月）；超过当月天数按月末截断。</div>
      </div>

      {error && (
        <div className="auth-error">
          <TriangleAlert size={14} />
          {error}
        </div>
      )}
    </Modal>
  )
}
