import { useEffect, useState } from 'react'
import {
  ArrowDown,
  ArrowUp,
  GripVertical,
  Plus,
  Scale,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { api, post, SLOW_TIMEOUT_MS } from '../api'
import type { BalanceGroup, Server } from '../types'
import { Modal, Switch } from './ui'
import ServerPicker from './ServerPicker'

type OffAction = 'keep' | 'stop'

type Row = {
  /** 0 = 还没选实例 */
  server_id: number
  off_action: OffAction
  enabled: boolean
}

function emptyRow(): Row {
  return { server_id: 0, off_action: 'keep', enabled: true }
}

const INTERVAL_OPTIONS = [
  { value: 60, label: '1 小时' },
  { value: 180, label: '3 小时' },
  { value: 360, label: '6 小时' },
  { value: 720, label: '12 小时' },
  { value: 1440, label: '24 小时' },
]

const DIFF_OPTIONS = [1, 5, 10, 30, 50]

type Props = {
  group: BalanceGroup | null
  servers: Server[]
  onClose: () => void
  onSaved: (message: string) => void
}

export default function BalanceDialog({ group, servers, onClose, onSaved }: Props) {
  const editing = Boolean(group && group.id)
  const [name, setName] = useState(group?.name ?? '均衡组')
  const [enabled, setEnabled] = useState(group?.enabled ?? true)
  const [failover, setFailover] = useState(group?.failover_enabled ?? true)
  const [interval, setIntervalMinutes] = useState(group?.interval_minutes ?? 60)
  const [diff, setDiff] = useState(group?.diff_gb ?? 10)
  const [record, setRecord] = useState(group?.ddns_record ?? '')
  const [options, setOptions] = useState<string[]>([])
  const [rows, setRows] = useState<Row[]>(() => {
    if (group?.members?.length) {
      return group.members.map((member) => ({
        server_id: member.server_id,
        off_action: member.off_action === 'stop' ? 'stop' : 'keep',
        enabled: member.enabled,
      }))
    }
    return [emptyRow(), emptyRow()]
  })
  const [picking, setPicking] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // 老数据可能是直接调接口写进来的任意值（1–200）：不在预设档里也补一颗，
  // 否则分段控件一个都不亮，看起来像没设过。
  const diffOptions = DIFF_OPTIONS.includes(diff)
    ? DIFF_OPTIONS
    : [...DIFF_OPTIONS, diff].sort((a, b) => a - b)

  // 下拉里只放「还没被别的目标占用」的记录 + 本组自己已绑的那条
  useEffect(() => {
    let alive = true
    api<{ items: string[] }>(`/api/v1/dns/options?kind=balance&target_id=${group?.id ?? 0}`)
      .then((result) => {
        if (alive) setOptions(result.items ?? [])
      })
      .catch(() => {
        if (alive) setOptions(group?.ddns_record ? [group.ddns_record] : [])
      })
    return () => {
      alive = false
    }
  }, [group?.id, group?.ddns_record])

  const labelOf = (serverId: number) =>
    servers.find((item) => item.id === serverId)?.label ?? '（实例已删除）'

  function patchRow(index: number, patch: Partial<Row>): void {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function move(index: number, delta: number): void {
    setRows((current) => {
      const next = [...current]
      const target = index + delta
      if (target < 0 || target >= next.length) return current
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  function add(): void {
    const used = new Set(rows.map((row) => row.server_id))
    if (!servers.some((server) => !used.has(server.id))) {
      setError('所有实例都已经在这个组里了')
      return
    }
    setRows((current) => [...current, emptyRow()])
    setError('')
  }

  function remove(index: number): void {
    setRows((current) => current.filter((_, i) => i !== index))
  }

  async function submit(): Promise<void> {
    setError('')
    const filled = rows.filter((row) => row.server_id)
    if (filled.length < 2) {
      setError('至少选两台实例')
      return
    }
    const ids = filled.map((row) => row.server_id)
    if (new Set(ids).size !== ids.length) {
      setError('同一台实例不能重复添加')
      return
    }
    setBusy(true)
    try {
      const result = await post<{ id: number; bootstrap?: { error?: string } }>(
        '/api/v1/balances',
        {
          id: group?.id ?? 0,
          name,
          enabled,
          failover_enabled: failover,
          interval_minutes: interval,
          diff_gb: diff,
          ddns_record: record,
          members: filled.map((row) => ({
            server_id: row.server_id,
            off_action: row.off_action,
            enabled: row.enabled,
          })),
        },
        SLOW_TIMEOUT_MS,
      )
      if (editing) {
        onSaved('均衡组已更新')
      } else if (result?.bootstrap?.error) {
        onSaved('均衡组已创建，但首次指派失败，详情见最近事件')
      } else {
        onSaved('均衡组已创建，正在指派到流量最少的成员')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const hours = interval / 60

  return (
    <Modal
      title={editing ? '编辑均衡组' : '新建均衡组'}
      icon={<Scale size={16} style={{ color: 'var(--blue)' }} />}
      onClose={onClose}
      wide
      footer={
        <>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            每 {hours} 小时整点评估一次，比最少的那台多出 {diff} GB 才切换
          </span>
          <span className="spacer" />
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={submit} disabled={busy}>
            保存
          </button>
        </>
      }
    >
      {servers.length < 2 && (
        <div className="auth-error">
          <TriangleAlert size={14} />
          先去「实例管理」页添加至少两台实例
        </div>
      )}

      <div className="field">
        <label htmlFor="bname">组名称</label>
        <input
          id="bname"
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="均衡组"
        />
      </div>

      <div className="field">
        <label>评估周期</label>
        <div className="segmented">
          {INTERVAL_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={interval === option.value ? 'on' : ''}
              onClick={() => setIntervalMinutes(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="hint">
          只按整点评估：1 小时是每个整点，3 小时是 00/03/06…，24 小时是每天 0 点。
          没到点不评估，DDNS 也不会动。
        </div>
      </div>

      <div className="field">
        <label>差值阈值</label>
        <div className="segmented">
          {diffOptions.map((value) => (
            <button
              key={value}
              type="button"
              className={diff === value ? 'on' : ''}
              onClick={() => setDiff(value)}
            >
              {value} GB
            </button>
          ))}
        </div>
        <div className="hint">
          当班比最少的那台多出这么多才值得切。设太小会让域名每隔一个周期抖一次。
        </div>
      </div>

      <div className="field">
        <label>成员</label>
        <div className="member-editor">
          {rows.map((row, index) => (
            <div className="member-row" key={`${row.server_id}-${index}`}>
              <GripVertical size={14} style={{ color: 'var(--muted-soft)' }} />
              <span className="member-order">{index + 1}</span>
              <button
                type="button"
                className={`select member-pick${row.server_id ? '' : ' is-empty'}`}
                title="点击选择实例"
                onClick={() => setPicking(index)}
              >
                <span className="member-pick-text">
                  {row.server_id ? labelOf(row.server_id) : '选择实例'}
                </span>
              </button>
              <div className="member-off">
                <span>下班后</span>
                <div className="segmented mini">
                  <button
                    type="button"
                    className={row.off_action === 'keep' ? 'on' : ''}
                    onClick={() => patchRow(index, { off_action: 'keep' })}
                  >
                    保持运行
                  </button>
                  <button
                    type="button"
                    className={row.off_action === 'stop' ? 'on' : ''}
                    onClick={() => patchRow(index, { off_action: 'stop' })}
                  >
                    停机
                  </button>
                </div>
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
                disabled={rows.length <= 2}
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
        </div>
        <div className="hint">
          一个账号建议只放一台：同一账号下的实例共享同一份 CDT
          额度，放两台会读到同一个数字。下班后动作只在均衡切走这台时生效。
        </div>
      </div>

      <div className="field">
        <label htmlFor="bdns">DDNS 记录</label>
        <select
          id="bdns"
          className="select"
          value={record}
          onChange={(event) => setRecord(event.target.value)}
        >
          <option value="">不绑定 —— 切换时不改任何域名</option>
          {options.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        <div className="hint">
          只列出还没被其他组占用的记录；一条域名只归一个组。要先在 DNS 页登记，这里才选得到。
        </div>
      </div>

      <div>
        <Switch label="启用该均衡组" checked={enabled} onChange={setEnabled} />
        <Switch
          label="允许故障转移"
          hint="当班流量到阈值，或被抢占后多次拉起失败，自动交给当前用量最少的成员；全员到阈值则整组停机"
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
          currentKind="balance"
          currentGroupId={group?.id || undefined}
          taken={
            new Set(
              rows
                .filter((row, index) => index !== picking && row.server_id)
                .map((row) => row.server_id),
            )
          }
          onClose={() => setPicking(null)}
          onPick={(server) => {
            const index = picking
            patchRow(index, { server_id: server.id })
            setPicking(null)
            setError('')
          }}
        />
      )}
    </Modal>
  )
}
