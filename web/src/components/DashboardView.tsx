import {
  Activity,
  AlertTriangle,
  BellRing,
  CheckCircle2,
  ChevronDown,
  Cloud,
  CloudOff,
  Gauge,
  HeartPulse,
  Info,
  Loader2,
  MapPin,
  Repeat,
  Server as ServerIcon,
  ShieldAlert,
  UserMinus,
  UserPlus,
  XCircle,
  Zap,
} from 'lucide-react'
import { useState } from 'react'
import type { EventItem, Overview } from '../types'
import { clockText, timeAgo } from '../time'
import { regionLabel } from '../regions'
import { ownerBase, ownerLabel } from '../owners'
import { Badge, Empty, Meter, Ring, money, statusLabel, statusTone } from './ui'

const EVENT_ICON: Record<string, typeof Info> = {
  shift_done: Zap,
  shift_failover: AlertTriangle,
  manual: Repeat,
  group_new: UserPlus,
  group_dissolve: UserMinus,
  breaker: ShieldAlert,
  limit_stop: ShieldAlert,
  recovery: Activity,
  keepalive: HeartPulse,
  error: AlertTriangle,
  daily_report: Gauge,
  stop: CloudOff,
  start: Cloud,
  // 单机保活的窗口相关事件：暂停 / 恢复 / 计划内拉起，图标要与「抢占恢复」区分开
  keepalive_pause: CloudOff,
  keepalive_resume: Cloud,
  keepalive_quiet: Cloud,
  keepalive_rest_error: AlertTriangle,
}

/** 事件列表默认露这么多条，点一次「显示更多事件」再加这么多（后端最多给 20 条） */
const RECENT_EVENT_PAGE = 8

export default function DashboardView({
  overview,
  onNavigate,
}: {
  overview: Overview
  onNavigate: (view: 'servers' | 'keepalive' | 'shift') => void
}) {
  // 后端一次给最近 20 条，先露 8 条，剩下的点「显示更多事件」再展开
  const [eventCount, setEventCount] = useState(RECENT_EVENT_PAGE)
  const running = overview.servers.filter((server) => server.instance_status === 'Running').length
  const totalTraffic = overview.servers.reduce((sum, server) => sum + (server.traffic_used || 0), 0)
  // 百分比的分母永远是「总配额」（每台默认 200GB，N 台就是 200×N），和后端通知、
  // 实例列表里的流量条同一个口径。阈值只决定什么时候换班，不参与百分比计算——
  // 阈值 188/200 会让同一份用量在概览显示 100%、在通知里显示 94%，对不上。
  const totalQuota = overview.servers.reduce((sum, server) => sum + (server.quota_gb || 200), 0)
  const trafficPercent = totalQuota > 0 ? (totalTraffic / totalQuota) * 100 : 0
  const alarms = overview.servers.filter(
    // 和后端 shift._over_threshold 同一口径：阈值没配（0）的机器不算超限。
    // 少了 threshold_gb > 0，0 >= 0 永远成立，概览会报「N 台超阈值」而引擎根本不动作。
    (server) => server.threshold_gb > 0 && (server.traffic_used || 0) >= server.threshold_gb,
  ).length
  const recentEvents = overview.events.slice(0, eventCount)
  const hiddenEvents = overview.events.length - recentEvents.length

  return (
    <>
      <div className="page-head">
        <div>
          <h1>概览</h1>
          <p>
            当前 {overview.timezone} · {overview.now_label}
          </p>
        </div>
        <div className="page-actions">
          {overview.dns.enabled ? (
            <Badge
              tone={overview.dns.records.some((item) => item.state === 'error') ? 'amber' : 'green'}
              dot
            >
              DNS {overview.dns.records.length} 条记录
            </Badge>
          ) : (
            <Badge tone="gray" dot>
              DNS 关闭
            </Badge>
          )}
          {overview.notifications.channels.length > 0 ? (
            <Badge tone="green" dot>
              {overview.notifications.channels.join(' / ')}
            </Badge>
          ) : (
            <Badge tone="gray" dot>
              未配置通知
            </Badge>
          )}
        </div>
      </div>

      <div className="stat-grid">
        <button
          type="button"
          className="stat stat-link"
          title="查看实例列表"
          onClick={() => onNavigate('servers')}
        >
          <div className="stat-main">
            <span className="label">
              <ServerIcon size={11} /> 实例
            </span>
            <span className="value">{overview.servers.length}</span>
            <span className="sub">{running} 台运行中</span>
          </div>
          <Ring
            percent={overview.servers.length ? (running / overview.servers.length) * 100 : 0}
            tone="accent"
          />
        </button>
        <button
          type="button"
          className="stat stat-link"
          title="查看单机保活"
          onClick={() => onNavigate('keepalive')}
        >
          <div className="stat-main">
            <span className="label">
              <HeartPulse size={11} /> 单机保活
            </span>
            <span className="value">{overview.keepalives.length}</span>
            <span className="sub">
              {overview.keepalives.filter((item) => item.enabled).length} 个运行中
            </span>
          </div>
          <Ring
            percent={
              overview.keepalives.length
                ? (overview.keepalives.filter((item) => item.enabled).length /
                    overview.keepalives.length) *
                  100
                : 0
            }
            tone="accent"
          />
        </button>
        <button
          type="button"
          className="stat stat-link"
          title="查看联合换班"
          onClick={() => onNavigate('shift')}
        >
          <div className="stat-main">
            <span className="label">
              <Repeat size={11} /> 联合换班
            </span>
            <span className="value">{overview.groups.length}</span>
            <span className="sub">
              {overview.groups.filter((group) => group.enabled).length} 个启用
            </span>
          </div>
          <Ring
            percent={
              overview.groups.length
                ? (overview.groups.filter((group) => group.enabled).length / overview.groups.length) *
                  100
                : 0
            }
            tone="blue"
          />
        </button>
        <div className="stat">
          <div className="stat-main">
            <span className="label">
              <Gauge size={11} /> 月累计流量
            </span>
            <span className="value">
              {totalTraffic.toFixed(1)}
              <em>GB</em>
            </span>
            <span className="sub">{alarms > 0 ? `${alarms} 台超阈值` : '全部低于阈值'}</span>
          </div>
          <Ring
            percent={trafficPercent}
            tone={alarms > 0 ? 'red' : 'accent'}
            size={46}
            title={`已用 ${totalTraffic.toFixed(1)} GB / 总配额 ${totalQuota} GB`}
          >
            {`${Math.round(trafficPercent)}%`}
          </Ring>
        </div>
      </div>

      {/* 实例状态：一台实例一行（列表形态），横向把 状态/名称/地域/IP/归属/费用/流量
          全部摊在一条基线上，比卡片网格更好横向对比。配色沿用概览的绿调：
          运行中的行左侧铺一层浅绿渐层 + 绿描边徽章，停机行保持中性。 */}
      <div className="section">
        <div className="section-head">
          <h2>实例状态</h2>
          <span>全部实例的在线状态、流量与归属</span>
          <div className="section-actions">
            <Badge tone={alarms > 0 ? 'amber' : 'gray'}>
              {overview.servers.length} 台 · {running} 台运行中
            </Badge>
          </div>
        </div>
        {overview.servers.length === 0 ? (
          <div className="card">
            <Empty icon={<ServerIcon size={20} />} title="还没有实例">
              去「实例管理」添加阿里云账号与 ECS 实例后，这里会展示每台的状态。
            </Empty>
          </div>
        ) : (
          <div className="card inst-list">
            <table className="inst-table">
              <colgroup>
                <col style={{ width: '12%' }} />
                <col style={{ width: '21%' }} />
                <col style={{ width: '13%' }} />
                <col style={{ width: '15%' }} />
                <col style={{ width: '13%' }} />
                <col style={{ width: '12%' }} />
                <col style={{ width: '14%' }} />
              </colgroup>
              <thead>
                <tr>
                  <th>状态</th>
                  <th>实例</th>
                  <th>地域</th>
                  <th>IP</th>
                  <th>归属</th>
                  <th>费用</th>
                  <th>流量</th>
                </tr>
              </thead>
              <tbody>
                {overview.servers.map((server) => {
                  const running = server.instance_status === 'Running'
                  const owner = server.managed ?? null
                  // 换班组里此刻当班的那台，保留一个「当班」标记 —— 这是老卡片里唯一有信息量的部分
                  const dutyOf = overview.groups.find(
                    (group) => group.duty_server_id === server.id,
                  )
                  return (
                    <tr className={running ? 'is-running' : undefined} key={server.id}>
                      <td data-label="状态">
                        <Badge tone={statusTone(server.instance_status)} pulse={running}>
                          {statusLabel(server.instance_status)}
                        </Badge>
                      </td>
                      {/* 别名下面是实例 ID 胶囊：改名之后还能一眼对上是哪台机器，
                          ID 比别名长，交给省略号收尾，完整值放 title */}
                      <td data-label="实例">
                        <div className="inst-main">
                          <div className="inst-name-line">
                            <span className="inst-name" title={server.label}>
                              {server.label}
                            </span>
                            {dutyOf && <span className="duty-tag">当班</span>}
                          </div>
                          <span className="inst-id" title={`实例 ID：${server.instance_id}`}>
                            {server.instance_id}
                          </span>
                        </div>
                      </td>
                      {/* 这一格只留地域：站点（中国站 / 国际站）在「实例管理」里看得到，
                          概览要的是「这台在哪个机房」——少一个胶囊，这一列干净很多 */}
                      <td data-label="地域">
                        <span className="inst-region" title={server.region_id}>
                          <MapPin size={11} />
                          <span className="inst-region-text">{regionLabel(server.region_id)}</span>
                        </span>
                      </td>
                      <td data-label="IP">
                        <span className="addr-chip">{server.public_ip || '无公网 IP'}</span>
                      </td>
                      <td data-label="归属">
                        <span
                          className={`inst-owner${owner ? '' : ' is-idle'}`}
                          title={ownerLabel(owner)}
                        >
                          {owner ? ownerBase(owner) : ownerLabel(null)}
                          {owner && !owner.active && <i> · 已暂停</i>}
                        </span>
                      </td>
                      <td data-label="费用">
                        <div className="inst-cost">
                          <span title="本月消费">
                            <em>本月</em>
                            {money(server.billing.monthly_cost, server.billing.currency)}
                          </span>
                          <span title="账户余额">
                            <em>余额</em>
                            {money(server.billing.balance, server.billing.currency)}
                          </span>
                        </div>
                      </td>
                      <td data-label="流量">
                        <div className="inst-flow">
                          <Meter
                            used={server.traffic_used}
                            quota={server.quota_gb}
                            threshold={server.threshold_gb}
                            compact
                          />
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <div className="section-head">
          <h2>最近事件</h2>
          <span>换班、故障转移、熔断、保活与手动操作</span>
          <div className="section-actions">
            <Badge tone="gray">
              <BellRing size={11} style={{ marginRight: 2 }} />
              {overview.events.length}
            </Badge>
          </div>
        </div>
        <div className="card card-pad">
          {recentEvents.length === 0 ? (
            <div className="empty">
              <div className="empty-icon">
                <Info size={20} />
              </div>
              <strong>暂无事件</strong>
              <p>第一轮同步完成后，这里会出现换班与状态变化的记录。</p>
            </div>
          ) : (
            <>
              <div className="events">
                {recentEvents.map((event) => (
                  <EventRow key={event.id} event={event} />
                ))}
              </div>
              {hiddenEvents > 0 && (
                <button
                  type="button"
                  className="btn ghost events-more"
                  onClick={() => setEventCount((current) => current + RECENT_EVENT_PAGE)}
                >
                  <ChevronDown size={14} />
                  显示更多事件（还有 {hiddenEvents} 条）
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </>
  )
}

// 通知永远是流程的最后一步：事件都是「确认状态到位 → 同步 DDNS → 推送通知」。
// 旧版本的事件把「已推送…通知」记在了确认之前，这里按显示顺序沉到最后，
// 让历史事件和新事件读起来一致（各自保持原有的先后关系）。
// 文案有三代：「已推送 xxx 通知」（更早）、「报告已发送 / 报告发送失败：xxx」（1.3.0 及以前）
// 和现在的「通知已发送 / 通知发送失败：xxx」。三个都认，历史事件才不会错位。
function isNotifyStep(text: string) {
  return text.includes('通知') || text.includes('报告')
}

function orderSteps(steps: { ts: number; text: string }[]) {
  if (steps.length < 2) return steps
  const notify: typeof steps = []
  const rest: typeof steps = []
  for (const step of steps) {
    if (isNotifyStep(step.text)) notify.push(step)
    else rest.push(step)
  }
  return notify.length ? [...rest, ...notify] : steps
}

function EventRow({ event }: { event: EventItem }) {
  const [open, setOpen] = useState(false)
  const Icon = EVENT_ICON[event.kind] ?? Info
  const level = event.level === 'error' ? 'error' : event.level === 'warning' ? 'warning' : 'info'
  const running = event.status === 'running'
  const failed = event.status === 'error'
  const steps = orderSteps(event.progress ?? [])
  const hasProgress = steps.length > 0
  const finishedAt = event.finished_at || 0

  return (
    <div className={`event ${open ? 'is-open' : ''}`}>
      <button type="button" className="event-main" onClick={() => setOpen((v) => !v)}>
        <div className={`event-icon ${level} ${running ? 'spinning' : ''}`}>
          {running ? (
            <Loader2 size={13} className="spin" />
          ) : failed ? (
            <XCircle size={13} />
          ) : (
            <Icon size={13} />
          )}
        </div>
        <div className="event-body">
          <div className="event-title-line">
            <strong>{event.title}</strong>
            {running ? (
              <span className="event-status running">
                <Loader2 size={10} className="spin" /> 进行中
              </span>
            ) : failed ? (
              <span className="event-status failed">
                <XCircle size={10} /> 失败
              </span>
            ) : (
              <span className="event-status done">
                <CheckCircle2 size={10} /> 完成
              </span>
            )}
          </div>
          {event.detail && !open && <p>{event.detail}</p>}
        </div>
        <span className="event-time">{timeAgo(event.ts)}</span>
        <ChevronDown size={14} className={`event-chevron ${open ? 'open' : ''}`} />
      </button>

      {open && (
        <div className="event-detail">
          {event.detail && <p className="event-detail-summary">{event.detail}</p>}

          {hasProgress && (
            <ol className="event-progress">
              {steps.map((step, index) => (
                <li key={index}>
                  <span className="event-progress-time">{clockText(step.ts)}</span>
                  <span className="event-progress-dot" />
                  <span className="event-progress-text">{step.text}</span>
                </li>
              ))}
            </ol>
          )}

          {!running && finishedAt > 0 && (
            <div className="event-finished">
              {failed ? '失败于' : '完成于'} {clockText(finishedAt)} · 全程{' '}
              {Math.max(0, finishedAt - event.ts) >= 60
                ? `${Math.round((finishedAt - event.ts) / 60)} 分钟`
                : `${Math.max(0, finishedAt - event.ts)} 秒`}
            </div>
          )}

          {running && (
            <div className="event-finished running-hint">
              <Loader2 size={11} className="spin" /> 等待确认状态中，完成后自动更新
            </div>
          )}
        </div>
      )}
    </div>
  )
}
