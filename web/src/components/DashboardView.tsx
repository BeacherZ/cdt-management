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
import { clockText, isBackendToday, monthDayTimeText, timeAgo } from '../time'
import { Badge, Meter, Ring, groupStatePulse, groupStateTone, statusLabel, statusTone } from './ui'

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
}

/** 事件列表默认露这么多条，点一次「显示更多事件」再加这么多（后端最多给 20 条） */
const RECENT_EVENT_PAGE = 8

/** 下次换班：今天之内只报时刻，跨天（一月一轮常见）补上日期，免得「下次 00:00」被读成今晚。
 *  「今天」按后端时区算（time.isBackendToday），不看浏览器本地时区。 */
function nextSwitch(ts: number): string {
  if (!ts) return '—'
  return isBackendToday(ts) ? clockText(ts) : monthDayTimeText(ts)
}

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
          title="查看服务器列表"
          onClick={() => onNavigate('servers')}
        >
          <div className="stat-main">
            <span className="label">
              <ServerIcon size={11} /> 服务器
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
            percent={alarms > 0 ? Math.min(100, (alarms / Math.max(1, overview.servers.length)) * 100) : 0}
            tone={alarms > 0 ? 'red' : 'accent'}
            size={46}
          >
            {alarms > 0 ? `${alarms}` : '稳'}
          </Ring>
        </div>
      </div>

      {overview.groups.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>当前当班</h2>
            <span>联合换班组实时状态</span>
          </div>
          <div className="group-grid">
            {overview.groups.map((group) => {
              const duty = group.members.find((member) => member.is_duty)
              const standby = group.members.filter(
                (member) => member.enabled && !member.is_duty,
              )
              return (
                <div className="card group-card" key={group.id}>
                  <div className="group-head">
                    <Repeat size={15} style={{ color: 'var(--green)' }} />
                    <h3>{group.name}</h3>
                    <Badge tone={groupStateTone(group.state)} pulse={groupStatePulse(group.state)}>
                      {group.state_label}
                    </Badge>
                  </div>
                  {duty?.server ? (
                    <div className="slot is-duty">
                      <div className="slot-top">
                        <span>
                          {group.cycle === 'month'
                            ? duty.span_label || `${duty.days} 天班`
                            : duty.span_label || `${duty.hours} 小时班`}
                        </span>
                        <span className="duty-tag">当班</span>
                      </div>
                      <div className="slot-name">{duty.server.label}</div>
                      <div className="slot-line">
                        <Badge tone={statusTone(duty.server.instance_status)}>
                          {statusLabel(duty.server.instance_status, duty.server.stopped_mode)}
                        </Badge>
                        <code>{duty.server.public_ip || '—'}</code>
                      </div>
                      <Meter
                        used={duty.server.traffic_used}
                        quota={duty.server.quota_gb}
                        threshold={duty.server.threshold_gb}
                      />
                    </div>
                  ) : (
                    <div className="slot">
                      <div className="slot-name">未计算出当班</div>
                    </div>
                  )}
                  <div className="group-foot">
                    <span>
                      待机 {standby.length} 台 ·{' '}
                      {standby.map((member) => member.server?.label ?? '?').join('、') || '无'}
                    </span>
                    {group.next_switch_at > 0 && (
                      <span style={{ marginLeft: 'auto' }}>
                        下次 {nextSwitch(group.next_switch_at)}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

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
// 文案有两代：「已推送 xxx 通知」（旧）和「报告已发送 / 报告发送失败：xxx」（新），
// 两个都认；只认「通知」会让新事件里这一步失去排序保护。
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
