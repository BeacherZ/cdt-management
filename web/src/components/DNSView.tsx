import { useState } from 'react'
import {
  ArrowRight,
  CircleSlash,
  Globe2,
  GripVertical,
  HeartPulse,
  LoaderCircle,
  Plus,
  RefreshCw,
  Repeat,
  Server as ServerIcon,
  Trash2,
} from 'lucide-react'
import type { DnsRecord, DnsState, Keepalive, Server, ShiftGroup } from '../types'
import { timeAgo } from '../time'
import { Badge, Empty, type Tone } from './ui'

type Props = {
  dns: DnsState
  servers: Server[]
  keepalives: Keepalive[]
  groups: ShiftGroup[]
  busy: string | null
  syncingAll: boolean
  onBind: (record: string, kind: string, id: number) => void
  onSync: (record: string) => void
  onSyncAll: () => void
  onAddRecord: (name: string) => Promise<boolean>
  onRemoveRecord: (record: string) => void
  onReorder: (ordered: string[]) => void
  onOpenSettings: () => void
}

const STATE_META: Record<DnsRecord['state'], { tone: Tone; text: string }> = {
  unbound: { tone: 'gray', text: '未绑定' },
  idle: { tone: 'amber', text: '目标未运行' },
  pending: { tone: 'blue', text: '待同步' },
  synced: { tone: 'green', text: '已同步' },
  error: { tone: 'red', text: '同步失败' },
}

/** select 的值编码成 kind:id，空值表示解绑 */
function encode(record: DnsRecord): string {
  if (!record.target) return ''
  return `${record.target.kind}:${record.target.id}`
}

function decode(value: string): { kind: string; id: number } {
  if (!value) return { kind: '', id: 0 }
  const [kind, id] = value.split(':')
  return { kind, id: Number(id) }
}

/** 上次同步时间：文案「还没有同步过」是本页特有的，时间基准仍走后端时钟 */
function lastSyncText(ts: number): string {
  if (!ts) return '还没有同步过'
  return timeAgo(ts)
}

export default function DNSView({
  dns,
  servers,
  keepalives,
  groups,
  busy,
  syncingAll,
  onBind,
  onSync,
  onSyncAll,
  onAddRecord,
  onRemoveRecord,
  onReorder,
  onOpenSettings,
}: Props) {
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  // 拖拽排序：记录当前被拖的记录名与悬停目标，用于视觉反馈与松手换位
  const [dragRecord, setDragRecord] = useState<string | null>(null)
  const [overRecord, setOverRecord] = useState<string | null>(null)

  function handleDrop(target: string) {
    if (!dragRecord || dragRecord === target) {
      setDragRecord(null)
      setOverRecord(null)
      return
    }
    const next = dns.records.map((item) => item.record)
    const from = next.indexOf(dragRecord)
    const to = next.indexOf(target)
    if (from < 0 || to < 0) {
      setDragRecord(null)
      setOverRecord(null)
      return
    }
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    setDragRecord(null)
    setOverRecord(null)
    onReorder(next)
  }

  const keepaliveIds = new Set(keepalives.map((item) => item.server_id))
  // 第一项是「所有服务器」全量清单，下面两个大项只是同一批机器的不同视角（快捷入口）：
  // 换班组会接管机器，所以这些机器不再重复出现在「单机保活」里。
  const groupedIds = new Set(
    groups.flatMap((group) => group.members.map((member) => member.server_id)),
  )
  const keepaliveServers = servers.filter(
    (server) => keepaliveIds.has(server.id) && !groupedIds.has(server.id),
  )

  async function submit() {
    const name = draft.trim()
    if (!name) return
    setSaving(true)
    const ok = await onAddRecord(name)
    setSaving(false)
    if (ok) {
      setDraft('')
      setAdding(false)
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>DNS</h1>
          <p>
            一条域名只绑一台机器（或一个换班组），但一台机器、一个换班组都可以挂多条域名。
            没绑的记录一律不动；机器一开机，指向它的所有记录一起更新 —— 不会再出现几台机器抢同一个域名。
            绑到单台机器就固定指它，不跟随换班组的轮换走。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn"
            onClick={onSyncAll}
            disabled={!dns.enabled || syncingAll || dns.records.length === 0}
          >
            {syncingAll ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
            全部同步
          </button>
          <button className="btn primary" onClick={() => setAdding((value) => !value)}>
            <Plus size={14} /> 添加记录名
          </button>
        </div>
      </div>

      {!dns.enabled && (
        <div className="notice amber">
          <CircleSlash size={14} />
          <span>DDNS 已关闭，这里的绑定不会生效。</span>
          <button className="btn ghost sm" onClick={onOpenSettings}>
            去设置里打开
          </button>
        </div>
      )}

      {dns.enabled && !dns.configured && (
        <div className="notice amber">
          <CircleSlash size={14} />
          <span>还没填 Cloudflare Token / Key，同步会直接失败。</span>
          <button className="btn ghost sm" onClick={onOpenSettings}>
            去设置
          </button>
        </div>
      )}

      {adding && (
        <div className="card card-pad dns-add">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="newRecord">新记录名</label>
            <input
              id="newRecord"
              className="input"
              value={draft}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submit()
                if (event.key === 'Escape') setAdding(false)
              }}
              placeholder="cdt-b.example.com"
            />
            <div className="hint">
              必须是在 Cloudflare 里已经存在的 A 记录；Zone ID 留空时会按根域自动反查。
            </div>
          </div>
          <div className="dns-add-actions">
            <button className="btn" onClick={() => setAdding(false)} disabled={saving}>
              取消
            </button>
            <button className="btn primary" onClick={() => void submit()} disabled={saving || !draft.trim()}>
              {saving ? <LoaderCircle size={13} className="spin" /> : null} 添加
            </button>
          </div>
        </div>
      )}

      {dns.records.length === 0 ? (
        <div className="card">
          <Empty icon={<Globe2 size={20} />} title="还没有域名记录">
            先添加一条记录名，再把它绑到某台机器或某个换班组上。第一次用需要先在设置里填 Cloudflare 的
            Token 与 TTL。
          </Empty>
        </div>
      ) : (
        <div className="dns-list">
          {dns.records.map((record) => {
            const meta = STATE_META[record.state]
            const working = busy === record.record
            const target = record.target
            return (
              <div
                className={`card dns-row${dragRecord === record.record ? ' dragging' : ''}${
                  overRecord === record.record ? ' drag-over' : ''
                }`}
                key={record.record}
                draggable
                onDragStart={(event) => {
                  setDragRecord(record.record)
                  event.dataTransfer.effectAllowed = 'move'
                  // Firefox 需要设置 data 才会真正启动拖拽
                  event.dataTransfer.setData('text/plain', record.record)
                }}
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'move'
                  if (overRecord !== record.record) setOverRecord(record.record)
                }}
                onDragLeave={() => {
                  if (overRecord === record.record) setOverRecord(null)
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  handleDrop(record.record)
                }}
                onDragEnd={() => {
                  setDragRecord(null)
                  setOverRecord(null)
                }}
              >
                <div className="dns-row-head">
                  <GripVertical size={13} className="drag-handle" aria-hidden />
                  <Globe2 size={15} style={{ color: 'var(--accent)', flex: 'none' }} />
                  <code className="dns-name">{record.record}</code>
                  <Badge tone={meta.tone} dot>
                    {meta.text}
                  </Badge>
                  {dns.proxied && <span className="dns-tag">CDN 代理</span>}
                  <div className="dns-row-actions">
                    <button
                      className="btn ghost icon sm"
                      title="立即同步这条记录"
                      disabled={!dns.enabled || working || !target}
                      onClick={() => onSync(record.record)}
                    >
                      {working ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title="删除记录名（不会删 Cloudflare 上的记录）"
                      onClick={() => onRemoveRecord(record.record)}
                    >
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                <div className="dns-bind">
                  <label htmlFor={`bind-${record.record}`}>绑定到</label>
                  <select
                    id={`bind-${record.record}`}
                    className="select"
                    value={encode(record)}
                    disabled={working}
                    onChange={(event) => {
                      const { kind, id } = decode(event.target.value)
                      onBind(record.record, kind, id)
                    }}
                  >
                    <option value="">不绑定（这条记录不会被动）</option>
                    {/* 第一项：所有服务器，全量铺开（顺序沿用「服务器」页里的手动排序）。
                        记录名不稀缺，随便哪台机器都能挂一条域名，不必先编进保活或换班组。 */}
                    {servers.length > 0 && (
                      <optgroup label="所有服务器">
                        {servers.map((server) => (
                          <option key={`all-${server.id}`} value={`server:${server.id}`}>
                            {server.label}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {keepaliveServers.length > 0 && (
                      <optgroup label="单机保活">
                        {keepaliveServers.map((server) => (
                          <option key={`ka-${server.id}`} value={`server:${server.id}`}>
                            {server.label}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {groups.length > 0 && (
                      <optgroup label="换班组">
                        {groups.map((group) => (
                          <option key={`g-${group.id}`} value={`group:${group.id}`}>
                            {group.name}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                </div>

                <div className="dns-meta">
                  {target ? (
                    <span className="dns-target">
                      {target.kind === 'group' ? (
                        <Repeat size={11} />
                      ) : keepaliveIds.has(target.id) ? (
                        <HeartPulse size={11} />
                      ) : (
                        <ServerIcon size={11} />
                      )}
                      {target.kind === 'group' ? `换班组「${target.label}」` : target.label}
                      {target.kind === 'group' && target.duty ? ` · 当班 ${target.duty}` : ''}
                      <span className={target.running ? 'dns-run on' : 'dns-run'}>
                        {target.running ? '运行中' : '未运行'}
                      </span>
                    </span>
                  ) : (
                    <span className="dns-target muted">没绑任何机器，同步时直接跳过</span>
                  )}

                  <span className="dns-ip">
                    解析到
                    <code>{record.last_ip || '—'}</code>
                    {record.desired_ip && record.desired_ip !== record.last_ip && (
                      <>
                        <ArrowRight size={11} />
                        <code className="want">{record.desired_ip}</code>
                      </>
                    )}
                  </span>
                  <span className="dns-time">{lastSyncText(record.last_at)}</span>
                  {record.last_status && record.state === 'error' && (
                    <span className="dns-error">{record.last_status}</span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
