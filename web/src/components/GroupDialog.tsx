import { useState } from 'react'
import { ArrowDown, ArrowUp, GripVertical, Plus, Repeat, Trash2, TriangleAlert } from 'lucide-react'
import { post } from '../api'
import type { Server, ShiftGroup } from '../types'
import { Modal, Switch } from './ui'

type Row = { server_id: number; hours: number; enabled: boolean }

type Props = {
  group: ShiftGroup | null
  servers: Server[]
  ddnsRecords: string[]
  onClose: () => void
  onSaved: (message: string) => void
}

export default function GroupDialog({ group, servers, ddnsRecords, onClose, onSaved }: Props) {
  const editing = Boolean(group && group.id)
  const [name, setName] = useState(group?.name ?? '联合换班')
  const [enabled, setEnabled] = useState(group?.enabled ?? true)
  const [failover, setFailover] = useState(group?.failover_enabled ?? true)
  const [ddnsRecord, setDdnsRecord] = useState(group?.ddns_record ?? '')
  const [rows, setRows] = useState<Row[]>(() => {
    if (group?.members?.length) {
      return group.members.map((member) => ({
        server_id: member.server_id,
        hours: member.hours,
        enabled: member.enabled,
      }))
    }
    const seed = servers.slice(0, 2)
    return seed.map((server) => ({
      server_id: server.id,
      hours: Math.round(24 / Math.max(1, seed.length)),
      enabled: true,
    }))
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const total = rows.filter((row) => row.enabled).reduce((sum, row) => sum + (row.hours || 0), 0)

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
    const candidate = servers.find((server) => !used.has(server.id))
    if (!candidate) {
      setError('所有服务器都已经在这个组里了')
      return
    }
    setRows((current) => [...current, { server_id: candidate.id, hours: 12, enabled: true }])
    setError('')
  }

  async function save() {
    setError('')
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
    setBusy(true)
    try {
      await post('/api/v1/groups', {
        id: group?.id ?? 0,
        name,
        enabled,
        failover_enabled: failover,
        ddns_record: ddnsRecord,
        members: rows.map((row) => ({
          server_id: row.server_id,
          hours: row.hours,
          enabled: row.enabled,
        })),
      })
      onSaved(editing ? '换班组已更新' : '换班组已创建')
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
            一个周期 {total || 0} 小时，按顺序轮换
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
        <label>轮换顺序（每台运行多少小时）</label>
        <div className="member-editor">
          {rows.map((row, index) => (
            <div className="member-row" key={`${row.server_id}-${index}`}>
              <GripVertical size={14} style={{ color: 'var(--muted-soft)' }} />
              <span className="member-order">{index + 1}</span>
              <select
                className="select"
                value={row.server_id}
                onChange={(event) => update(index, { server_id: Number(event.target.value) })}
              >
                {servers.map((server) => {
                  // 已经被别的行占用就别再选一次，省得保存时才报「重复添加」
                  const taken = rows.some(
                    (other, otherIndex) => otherIndex !== index && other.server_id === server.id,
                  )
                  return (
                    <option key={server.id} value={server.id} disabled={taken}>
                      {taken ? `${server.label}（已在本组）` : server.label}
                    </option>
                  )
                })}
              </select>
              <div className="member-hours">
                <input
                  className="input"
                  type="number"
                  min={0.5}
                  step={0.5}
                  value={row.hours}
                  onChange={(event) => update(index, { hours: Number(event.target.value) })}
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
        <button className="btn sm" style={{ alignSelf: 'flex-start' }} onClick={add}>
          <Plus size={13} /> 添加成员
        </button>
      </div>

      <div className="field">
        <label htmlFor="ddnsRecord">本组 DDNS 记录</label>
        <select
          id="ddnsRecord"
          className="select"
          value={ddnsRecord}
          onChange={(event) => setDdnsRecord(event.target.value)}
          disabled={ddnsRecords.length === 0 && !ddnsRecord}
        >
          <option value="">
            {ddnsRecords.length ? `跟随默认（${ddnsRecords[0]}）` : '跟随默认（还没配记录名）'}
          </option>
          {ddnsRecords.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
          {/* 换班组以前填过、但设置里已经删掉的记录名，保留一项免得被静默改掉 */}
          {ddnsRecord && !ddnsRecords.includes(ddnsRecord) && (
            <option value={ddnsRecord}>{ddnsRecord}（已不在记录名列表）</option>
          )}
        </select>
        <div className="hint">
          选项来自「设置 → DDNS」里的记录名，可以先去那里加多条，再回来给每个组挑一条。
        </div>
      </div>

      <div>
        <Switch label="启用该换班组" checked={enabled} onChange={setEnabled} />
        <Switch
          label="允许流量故障转移"
          hint="当班流量达标时自动切到下一台没超的成员；全部达标则全局熔断"
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
    </Modal>
  )
}
