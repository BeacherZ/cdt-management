import { ArrowRight, Clock3, LoaderCircle, Pencil, Plus, RefreshCw, Repeat, Trash2 } from 'lucide-react'
import type { ShiftGroup } from '../types'
import { Badge, Empty, Meter, phaseTone, statusLabel, statusTone } from './ui'

type Props = {
  groups: ShiftGroup[]
  serverCount: number
  nowTs: number
  busyId: number | null
  onAdd: () => void
  onEdit: (group: ShiftGroup) => void
  onDelete: (group: ShiftGroup) => void
  onSwitch: (group: ShiftGroup) => void
}

function countdown(target: number, now: number): string {
  const diff = Math.max(0, target - now)
  const hours = Math.floor(diff / 3600)
  const minutes = Math.floor((diff % 3600) / 60)
  const seconds = diff % 60
  if (hours > 24) return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

export default function ShiftView({
  groups,
  serverCount,
  nowTs,
  busyId,
  onAdd,
  onEdit,
  onDelete,
  onSwitch,
}: Props) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>联合换班</h1>
          <p>
            把多台服务器编成一组，按顺序各自运行设定的时长，同一时刻只保留一台在线，其余节省停机。
            当班流量达标会自动切到下一台没超的成员，全部达标则全局熔断。DDNS 始终指向当班实例。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" onClick={onAdd} disabled={serverCount < 2}>
            <Plus size={14} /> 新建换班组
          </button>
        </div>
      </div>

      {groups.length === 0 ? (
        <div className="card">
          <Empty icon={<Repeat size={20} />} title="还没有换班组">
            至少添加两台服务器后，把它们编成一组并设定各自运行几小时，例如两台各 12 小时。
          </Empty>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {groups.map((group) => {
            const duty = group.members.find((member) => member.is_duty)
            return (
              <div className="card card-pad" key={group.id}>
                <div className="group-head">
                  <Repeat size={16} style={{ color: 'var(--green)' }} />
                  <h3>{group.name}</h3>
                  <Badge tone={phaseTone(group.phase)} pulse={group.phase === 'switching'}>
                    {group.phase_label}
                  </Badge>
                  {!group.enabled && <Badge tone="gray">已停用</Badge>}
                  {!group.failover_enabled && <Badge tone="gray">故障转移关闭</Badge>}
                  <div className="group-actions">
                    <button
                      className="btn ghost icon sm"
                      title="立即换到下一台"
                      disabled={busyId === group.id}
                      onClick={() => onSwitch(group)}
                    >
                      {busyId === group.id ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        <RefreshCw size={14} />
                      )}
                    </button>
                    <button className="btn ghost icon sm" title="编辑" onClick={() => onEdit(group)}>
                      <Pencil size={14} />
                    </button>
                    <button className="btn ghost icon sm" title="删除" onClick={() => onDelete(group)}>
                      <Trash2 size={14} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                {/* 时间轴 */}
                <div className="timeline" style={{ marginTop: 14 }}>
                  {group.members
                    .filter((member) => member.enabled)
                    .map((member) => (
                      <div
                        key={member.id}
                        className={`timeline-seg${member.is_duty ? ' duty' : ''}`}
                        style={{ flexGrow: member.hours }}
                        title={`${member.server?.label ?? '?'} · ${member.hours} 小时`}
                      >
                        {member.server?.label ?? '—'}
                      </div>
                    ))}
                </div>

                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 14,
                    marginTop: 10,
                    fontSize: 11.5,
                    color: 'var(--muted)',
                    flexWrap: 'wrap',
                  }}
                >
                  <span>
                    <Clock3 size={11} style={{ verticalAlign: -1 }} /> 一个周期 {group.cycle_hours} 小时
                  </span>
                  <span>
                    共 {group.members.filter((member) => member.enabled).length} 台参与
                  </span>
                  {group.ddns_record && <span>DDNS：{group.ddns_record}</span>}
                  {group.next_switch_at > 0 && (
                    <span style={{ color: 'var(--ink-soft)' }}>
                      距下次切换 <b>{countdown(group.next_switch_at, nowTs)}</b>
                    </span>
                  )}
                  <span style={{ marginLeft: 'auto' }}>
                    当班 <b style={{ color: 'var(--green)' }}>{duty?.server?.label ?? '—'}</b>
                  </span>
                </div>

                {group.note && (
                  <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--amber)' }}>
                    {group.note}
                  </div>
                )}

                {/* 成员列表 */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 14 }}>
                  {group.members.map((member) => {
                    const server = member.server
                    if (!server) {
                      return (
                        <div key={member.id} className="slot" style={{ opacity: 0.6 }}>
                          <div className="slot-name">服务器已被删除</div>
                        </div>
                      )
                    }
                    return (
                      <div
                        key={member.id}
                        className={`slot${member.is_duty ? ' is-duty' : ''}`}
                        style={{ display: 'grid', gridTemplateColumns: '1fr 150px', gap: 14 }}
                      >
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 7, minWidth: 0 }}>
                          <div className="slot-top">
                            <span>第 {member.position + 1} 位 · {member.hours} 小时</span>
                            {member.is_duty && <span className="duty-tag">当班</span>}
                            {!member.enabled && <Badge tone="gray">已禁用</Badge>}
                          </div>
                          <div className="slot-name">{server.label}</div>
                          <div className="slot-line">
                            <Badge tone={statusTone(server.instance_status)}>
                              {statusLabel(server.instance_status)}
                            </Badge>
                            <code>{server.instance_id || '未填实例'}</code>
                          </div>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, justifyContent: 'center' }}>
                          <div style={{ fontSize: 11, color: 'var(--muted)', textAlign: 'right' }}>
                            {server.public_ip || '无公网 IP'}
                          </div>
                          <Meter
                            used={server.traffic_used}
                            quota={server.quota_gb}
                            threshold={server.threshold_gb}
                          />
                        </div>
                      </div>
                    )
                  })}
                </div>

                {group.members.filter((member) => member.enabled).length > 1 && (
                  <div
                    style={{
                      marginTop: 10,
                      fontSize: 11,
                      color: 'var(--muted-soft)',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 5,
                    }}
                  >
                    <ArrowRight size={11} />
                    轮换顺序：{' '}
                    {group.members
                      .filter((member) => member.enabled)
                      .map((member) => `${member.server?.label ?? '?'}(${member.hours}h)`)
                      .join(' → ')}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
