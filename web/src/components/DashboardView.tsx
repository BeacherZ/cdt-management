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
  XCircle,
  Zap,
} from 'lucide-react'
import { useState } from 'react'
import type { EventItem, Overview } from '../types'
import { Badge, Meter, Ring, phaseTone, statusLabel, statusTone } from './ui'

const EVENT_ICON: Record<string, typeof Info> = {
  shift_done: Zap,
  shift_failover: AlertTriangle,
  manual: Repeat,
  breaker: ShieldAlert,
  limit_stop: ShieldAlert,
  recovery: Activity,
  keepalive: HeartPulse,
  error: AlertTriangle,
  daily_report: Gauge,
  stop: CloudOff,
  start: Cloud,
}

function timeAgo(ts: number): string {
  if (!ts) return '—'
  const diff = Math.floor(Date.now() / 1000) - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return new Date(ts * 1000).toLocaleDateString('zh-CN')
}

function clock(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function DashboardView({
  overview,
  onNavigate,
}: {
  overview: Overview
  onNavigate: (view: 'servers' | 'keepalive' | 'shift') => void
}) {
  const running = overview.servers.filter((server) => server.instance_status === 'Running').length
  const totalTraffic = overview.servers.reduce((sum, server) => sum + (server.traffic_used || 0), 0)
  const alarms = overview.servers.filter(
    (server) => (server.traffic_used || 0) >= server.threshold_gb,
  ).length

  return (
    <>
      <div className="page-head">
        <div>
          <h1>概览</h1>
          <p>
            当前 {overview.timezone} ·{' '}
            {new Date(overview.now).toLocaleString('zh-CN', {
              year: 'numeric',
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
            })}
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
                    <Badge tone={phaseTone(group.phase)} pulse={group.phase === 'switching'}>
                      {group.phase_label}
                    </Badge>
                  </div>
                  {duty?.server ? (
                    <div className="slot is-duty">
                      <div className="slot-top">
                        <span>{duty.hours} 小时班</span>
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
                        下次 {new Date(group.next_switch_at * 1000).toLocaleTimeString('zh-CN', {
                          hour: '2-digit',
                          minute: '2-digit',
                        })}
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
          {overview.events.length === 0 ? (
            <div className="empty">
              <div className="empty-icon">
                <Info size={20} />
              </div>
              <strong>暂无事件</strong>
              <p>第一轮同步完成后，这里会出现换班与状态变化的记录。</p>
            </div>
          ) : (
            <div className="events">
              {overview.events.map((event) => (
                <EventRow key={event.id} event={event} />
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  )
}

// 通知永远是流程的最后一步：事件都是「确认状态到位 → 同步 DDNS → 推送通知」。
// 旧版本的事件把「已推送…通知」记在了确认之前，这里按显示顺序沉到最后，
// 让历史事件和新事件读起来一致（各自保持原有的先后关系）。
function orderSteps(steps: { ts: number; text: string }[]) {
  if (steps.length < 2) return steps
  const notify: typeof steps = []
  const rest: typeof steps = []
  for (const step of steps) {
    if (step.text.includes('通知')) notify.push(step)
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
                  <span className="event-progress-time">{clock(step.ts)}</span>
                  <span className="event-progress-dot" />
                  <span className="event-progress-text">{step.text}</span>
                </li>
              ))}
            </ol>
          )}

          {!running && finishedAt > 0 && (
            <div className="event-finished">
              {failed ? '失败于' : '完成于'} {clock(finishedAt)} · 全程{' '}
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
