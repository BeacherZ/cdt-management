import {
  Activity,
  AlertTriangle,
  BellRing,
  Cloud,
  CloudOff,
  Gauge,
  HeartPulse,
  Info,
  Repeat,
  Server as ServerIcon,
  ShieldAlert,
  Zap,
} from 'lucide-react'
import type { EventItem, Overview } from '../types'
import { Badge, Meter, Ring, phaseTone, statusLabel, statusTone } from './ui'

const EVENT_ICON: Record<string, typeof Info> = {
  shift_done: Zap,
  shift_failover: AlertTriangle,
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

export default function DashboardView({ overview }: { overview: Overview }) {
  const running = overview.servers.filter((server) => server.instance_status === 'Running').length
  const totalTraffic = overview.servers.reduce((sum, server) => sum + (server.traffic_used || 0), 0)
  const alarms = overview.servers.filter(
    (server) => (server.traffic_used || 0) >= server.threshold_gb,
  ).length

  return (
    <>
      <div className="page-head">
        <div>
          <h1>总览</h1>
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
          {overview.ddns.enabled ? (
            <Badge tone={overview.ddns.last_status.startsWith('✅') ? 'green' : 'amber'} dot>
              DDNS {overview.ddns.record || '已开启'}
            </Badge>
          ) : (
            <Badge tone="gray" dot>
              DDNS 关闭
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
        <div className="stat">
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
        </div>
        <div className="stat">
          <div className="stat-main">
            <span className="label">
              <Repeat size={11} /> 换班组
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
        </div>
        <div className="stat">
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
        </div>
        <div className="stat">
          <div className="stat-main">
            <span className="label">
              <Gauge size={11} /> 累计流量
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
                          {statusLabel(duty.server.instance_status)}
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
              <p>第一轮对账完成后，这里会出现换班与状态变化的记录。</p>
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

function EventRow({ event }: { event: EventItem }) {
  const Icon = EVENT_ICON[event.kind] ?? Info
  const level = event.level === 'error' ? 'error' : event.level === 'warning' ? 'warning' : 'info'
  return (
    <div className="event">
      <div className={`event-icon ${level}`}>
        <Icon size={13} />
      </div>
      <div className="event-body">
        <strong>{event.title}</strong>
        {event.detail && <p>{event.detail}</p>}
      </div>
      <span className="event-time">{timeAgo(event.ts)}</span>
    </div>
  )
}
