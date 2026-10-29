import { useEffect, useState } from 'react'
import { ArrowRight, Clock3, LoaderCircle, Pencil, Plus, Repeat, Trash2, Zap } from 'lucide-react'
import type { ShiftGroup } from '../types'
import { currentTs } from '../time'
import { Badge, Empty, Meter, groupStatePulse, groupStateTone, statusLabel, statusTone } from './ui'

type Props = {
  groups: ShiftGroup[]
  serverCount: number
  /** 今天所在月份的天数，由后端按排班时区算好下发（一月一轮的平摊、本月 N 天都读它） */
  monthDays: number
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

// 班次时间段：白班（00:00–12:00）；结束 00:00 按「当天到午夜」显示成 24:00；没填时刻就返回空串
function slotPeriod(name: string, start: string, end: string): string {
  const s = (start || '').trim()
  const e = (end || '').trim()
  if (!s && !e) return ''
  // 时刻表按一天 0–24 点切分，0 点结束只能是午夜（12:00–00:00 读着像第二天凌晨）
  const tail = e === '00:00' && s !== '00:00' ? '24:00' : e
  const span = e ? `${s}–${tail}` : s
  return name.trim() ? `${name.trim()}（${span}）` : span
}

export default function ShiftView({
  groups,
  serverCount,
  monthDays,
  busyId,
  onAdd,
  onEdit,
  onDelete,
  onSwitch,
}: Props) {
  // 秒级倒计时只需要在这一页重渲染。挂在 App 上会让整棵树每秒重置一次，
  // 弹窗里的 <select>、输入框都会被反复打断。
  // 时间基准用后端时钟（currentTs）：同步点 + 浏览器流逝的时间，
  // 用户把本机时间调错、人在别的时区，倒计时都还是对的。
  const [, setTick] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const nowTs = currentTs()

  return (
    <>
      <div className="page-head">
        <div>
          <h1>联合换班</h1>
          <p>
            把多台服务器编成一组，按排班周期轮流当班，同一时刻只保留一台在线，其余节省停机。
            一天一轮按小时（也可以填固定时刻表）换，一月一轮按天换；
            当班流量达标会自动切到下一台没超的成员；找不到可接班的成员（全部达标或状态读不到）
            就全局熔断停机。DDNS 始终指向当班实例。
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
            const monthCycle = group.cycle === 'month'
            return (
              <div className="card card-pad" key={group.id}>
                <div className="group-head">
                  <Repeat size={16} style={{ color: 'var(--green)' }} />
                  <h3>{group.name}</h3>
                  <Badge tone={groupStateTone(group.state)} pulse={groupStatePulse(group.state)}>
                    {group.state_label}
                  </Badge>
                  {!group.failover_enabled && <Badge tone="gray">故障转移关闭</Badge>}
                  <div className="group-actions">
                    <button
                      className="btn sm switch-btn"
                      title="立即换到下一台：拉起下一台、停掉当前当班、同步 DDNS"
                      disabled={busyId === group.id}
                      onClick={() => onSwitch(group)}
                    >
                      {busyId === group.id ? (
                        <LoaderCircle size={13} className="spin" />
                      ) : (
                        <Zap size={13} />
                      )}
                      立即换班
                    </button>
                    <button className="btn ghost icon sm" title="编辑" onClick={() => onEdit(group)}>
                      <Pencil size={14} />
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title="解散换班组"
                      onClick={() => onDelete(group)}
                    >
                      <Trash2 size={14} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                {/* 时间轴：一天一轮按工时占宽，一月一轮按天数占宽 */}
                <div className="timeline" style={{ marginTop: 14 }}>
                  {group.members
                    .filter((member) => member.enabled)
                    .map((member) => (
                      <div
                        key={member.id}
                        className={`timeline-seg${member.is_duty ? ' duty' : ''}`}
                        style={{ flexGrow: monthCycle ? member.days || 1 : member.hours }}
                        title={`${member.server?.label ?? '?'} · ${
                          monthCycle
                            ? member.span_label || `${member.days} 天`
                            : `${member.hours} 小时`
                        }`}
                      />
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
                    <Clock3 size={11} style={{ verticalAlign: -1 }} />{' '}
                    {monthCycle ? `本月 ${monthDays} 天，按天轮换` : `一个周期 ${group.cycle_hours} 小时`}
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
                        className={`slot slot-body${member.is_duty ? ' is-duty' : ''}`}
                      >
                        <div className="slot-main">
                          <div className="slot-top">
                            <span>
                              第 {member.position + 1} 位
                              {monthCycle
                                ? ` · ${member.span_label || '本月未排班'}`
                                : slotPeriod(member.slot_name, member.start_time, member.end_time)
                                  ? ` · ${slotPeriod(member.slot_name, member.start_time, member.end_time)}`
                                  : ` · ${member.hours} 小时`}
                            </span>
                            {!member.enabled && <Badge tone="gray">已禁用</Badge>}
                          </div>
                          <div className="slot-name">{server.label}</div>
                          <div className="slot-line">
                            <Badge tone={statusTone(server.instance_status)}>
                              {statusLabel(server.instance_status, server.stopped_mode)}
                            </Badge>
                            <code>{server.instance_id || '未填实例'}</code>
                          </div>
                        </div>
                        <div className="slot-side">
                          {member.is_duty && <span className="duty-tag">当班</span>}
                          <div className="slot-ip">{server.public_ip || '无公网 IP'}</div>
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
                    className="rotate-order"
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
                      .map(
                        (member) =>
                          `${member.server?.label ?? '?'}(${
                            monthCycle ? member.span_label || `${member.days} 天` : `${member.hours}h`
                          })`,
                      )
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
