import { useEffect, useState } from 'react'
import {
  ArrowRight,
  Clock3,
  Globe2,
  LoaderCircle,
  Pencil,
  Plus,
  Scale,
  Trash2,
  Zap,
} from 'lucide-react'
import type { BalanceGroup } from '../types'
import { currentTs } from '../time'
import { Badge, Empty, Meter, statusLabel, statusTone } from './ui'

type Props = {
  groups: BalanceGroup[]
  serverCount: number
  busyId: number | null
  onAdd: () => void
  onEdit: (group: BalanceGroup) => void
  onDelete: (group: BalanceGroup) => void
  onSwitch: (group: BalanceGroup) => void
}

function countdown(target: number, now: number): string {
  const diff = Math.max(0, target - now)
  const hours = Math.floor(diff / 3600)
  const minutes = Math.floor((diff % 3600) / 60)
  const seconds = diff % 60
  if (hours > 24) return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function intervalLabel(minutes: number): string {
  if (minutes >= 1440) return `${Math.round(minutes / 1440)} 天`
  return `${Math.round(minutes / 60)} 小时`
}

/** 组状态：只看自己的 phase，"当班是否在跑"由成员行自己表达 */
function phaseBadge(group: BalanceGroup): { text: string; tone: 'blue' | 'green' | 'red' | 'gray'; pulse: boolean } {
  if (!group.enabled) return { text: '已停用', tone: 'gray', pulse: false }
  if (group.phase === 'starting' || group.phase === 'switching')
    return { text: '均衡中', tone: 'blue', pulse: true }
  if (group.phase === 'exhausted')
    // 熔断有两种来路：全员到阈值（停了整组），或关着故障转移时只停了当班。
    // 一律写「全员到阈值」会和下面那行原因自相矛盾，按开关分开说。
    return group.failover_enabled
      ? { text: '全员到阈值', tone: 'red', pulse: false }
      : { text: '当班超限已停机', tone: 'red', pulse: false }
  if (group.phase === 'error') return { text: '切换异常', tone: 'red', pulse: false }
  if (!group.duty_server_id) return { text: '待指派', tone: 'gray', pulse: false }
  return { text: '平稳运行', tone: 'green', pulse: false }
}

export default function BalanceView({
  groups,
  serverCount,
  busyId,
  onAdd,
  onEdit,
  onDelete,
  onSwitch,
}: Props) {
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
          <h1>流量均衡</h1>
          <p>
            把多台实例编成一组，域名始终指向累计用得最少的那台。每到整点的评估点比较一次，
            当班比最少的那台多出设定差值才切换，避免域名频繁抖动。
            适合包年包月、不缺机器只缺流量的场景 —— 成员通常保持在线，切换只动 DDNS，不落停机。
            当班流量到阈值或被多次拉起失败，会自动交给当前用得最少的成员。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" onClick={onAdd} disabled={serverCount < 2}>
            <Plus size={14} /> 新建均衡组
          </button>
        </div>
      </div>

      {groups.length === 0 ? (
        <div className="card">
          <Empty icon={<Scale size={20} />} title="还没有均衡组">
            至少添加两台实例后，把它们编成一组。建议一个账号只放一台 ——
            同一个账号下的实例共享同一份 CDT 额度，放两台会读到同一个数字。
          </Empty>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {groups.map((group) => {
            const duty = group.members.find((member) => member.is_duty)
            const badge = phaseBadge(group)
            const countdownOf = (place: 'head' | 'meta') =>
              group.next_eval_at > 0 && group.enabled ? (
                <span
                  className={`group-countdown in-${place}`}
                  title={`距下次评估还有 ${countdown(group.next_eval_at, nowTs)}`}
                >
                  <Clock3 size={11} />
                  <span className="group-countdown-label">距下次评估</span>
                  <b>{countdown(group.next_eval_at, nowTs)}</b>
                </span>
              ) : null
            return (
              <div className="card card-pad" key={group.id}>
                <div className="group-head">
                  <Scale size={16} style={{ color: 'var(--blue)' }} />
                  <h3>{group.name}</h3>
                  <Badge tone={badge.tone} pulse={badge.pulse}>
                    {badge.text}
                  </Badge>
                  {countdownOf('head')}
                  {!group.failover_enabled && <Badge tone="gray">故障转移关闭</Badge>}
                  <div className="group-actions">
                    <button
                      className="btn sm switch-btn"
                      title="立即交给当前累计用得最少的成员，不等评估点"
                      disabled={busyId === group.id}
                      onClick={() => onSwitch(group)}
                    >
                      {busyId === group.id ? (
                        <LoaderCircle size={13} className="spin" />
                      ) : (
                        <Zap size={13} />
                      )}
                      立即均衡
                    </button>
                    <button className="btn ghost icon sm" title="编辑" onClick={() => onEdit(group)}>
                      <Pencil size={14} />
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title="解散均衡组"
                      onClick={() => onDelete(group)}
                    >
                      <Trash2 size={14} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 14,
                    marginTop: 10,
                    fontSize: 12,
                    color: 'var(--muted)',
                    flexWrap: 'wrap',
                  }}
                >
                  <span>
                    <Clock3 size={11} style={{ verticalAlign: -1 }} /> 每 {intervalLabel(group.interval_minutes)} 整点评估一次，差值 {group.diff_gb} GB 才切
                  </span>
                  <span>共 {group.members.filter((member) => member.enabled).length} 台参与</span>
                  {group.ddns_record && (
                    <span className="ddns-item" title="DDNS 记录始终指向当班实例">
                      DDNS
                      <span className="dns-chip">
                        <Globe2 size={10} />
                        <span className="dns-chip-text">{group.ddns_record}</span>
                      </span>
                    </span>
                  )}
                  {countdownOf('meta')}
                  <span style={{ marginLeft: 'auto' }}>
                    当班 <b style={{ color: 'var(--blue)' }}>{duty?.label ?? '—'}</b>
                  </span>
                </div>

                {group.note && (
                  <div style={{ marginTop: 8, fontSize: 12, color: 'var(--amber)' }}>{group.note}</div>
                )}

                <div className="slot-grid">
                  {group.members.map((member) => (
                    <div
                      key={member.id}
                      className={`slot slot-body${member.is_duty ? ' is-duty' : ''}${member.enabled ? '' : ' is-off'}`}
                    >
                      <div className="slot-main">
                        <div className="slot-top">
                          <span>第 {member.position + 1} 位</span>
                          {!member.enabled && <Badge tone="gray">已禁用</Badge>}
                          {member.over_limit && <Badge tone="amber">到阈值</Badge>}
                        </div>
                        <div className="slot-name">{member.label || `#${member.server_id}`}</div>
                        <div className="slot-line">
                          <Badge tone={statusTone(member.instance_status)}>
                            {statusLabel(member.instance_status, '')}
                          </Badge>
                          <code>{member.instance_id || '未填实例'}</code>
                        </div>
                      </div>
                      <div className="slot-side">
                        {member.is_duty && <span className="duty-tag duty-tag-balance">当班</span>}
                        <div className="slot-ip">
                          <span className="addr-chip" title="公网 IP">
                            {member.public_ip || '无公网 IP'}
                          </span>
                        </div>
                        <Meter
                          used={member.traffic_used}
                          quota={member.quota_gb}
                          threshold={member.threshold_gb}
                        />
                      </div>
                    </div>
                  ))}
                </div>

                <div
                  className="rotate-order"
                  style={{
                    marginTop: 10,
                    fontSize: 12,
                    color: 'var(--muted-soft)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 5,
                  }}
                >
                  <ArrowRight size={11} />
                  下班动作：{' '}
                  {group.members
                    .filter((member) => member.enabled)
                    .map(
                      (member) =>
                        `${member.label || '?'}(${member.off_action === 'stop' ? '停机' : '保持运行'})`,
                    )
                    .join('、')}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
