import { HeartPulse, LoaderCircle, Plus, Power, RefreshCw, Trash2 } from 'lucide-react'
import type { Keepalive, Server } from '../types'
import { Badge, Empty, Meter, statusLabel, statusTone } from './ui'

type Props = {
  keepalives: Keepalive[]
  servers: Server[]
  groupedIds: Set<number>
  busyId: number | null
  onAdd: () => void
  onToggle: (item: Keepalive, enabled: boolean) => void
  onRemove: (item: Keepalive) => void
  onAction: (serverId: number, action: 'start' | 'stop') => void
  onRefresh: (serverId: number) => void
}

export default function KeepaliveView({
  keepalives,
  servers,
  groupedIds,
  busyId,
  onAdd,
  onToggle,
  onRemove,
  onAction,
  onRefresh,
}: Props) {
  const available = servers.length - keepalives.length

  return (
    <>
      <div className="page-head">
        <div>
          <h1>单机保活</h1>
          <p>
            只做一件事：实例意外停机（或抢占被回收）就自动拉起来，不参与换班、不停机。
            同一台服务器如果已经加入换班组，这里会自动让位，避免两边互相拉扯。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" onClick={onAdd} disabled={available <= 0}>
            <Plus size={14} /> 添加保活
          </button>
        </div>
      </div>

      {keepalives.length === 0 ? (
        <div className="card">
          <Empty icon={<HeartPulse size={20} />} title="还没有保活任务">
            从「服务器」里挑一台加进来，它掉线后会被自动拉起。多台机器轮流跑请用「联合换班」。
          </Empty>
        </div>
      ) : (
        <div className="server-grid">
          {keepalives.map((item) => {
            const running = item.instance_status === 'Running'
            return (
              <div className="card server-card" key={item.id}>
                <div className="head">
                  <Badge tone={statusTone(item.instance_status)} pulse={running && item.enabled}>
                    {statusLabel(item.instance_status)}
                  </Badge>
                  <strong title={item.label}>{item.label}</strong>
                  <div className="actions">
                    <button
                      className="btn ghost icon sm"
                      title="刷新"
                      disabled={busyId === item.server_id}
                      onClick={() => onRefresh(item.server_id)}
                    >
                      {busyId === item.server_id ? (
                        <LoaderCircle size={13} className="spin" />
                      ) : (
                        <RefreshCw size={13} />
                      )}
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title={running ? '节省停机' : '开机'}
                      onClick={() => onAction(item.server_id, running ? 'stop' : 'start')}
                    >
                      <Power size={13} />
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title="移出保活"
                      onClick={() => onRemove(item)}
                    >
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                <div className="meta-line">
                  <code>{item.instance_id || '未填实例 ID'}</code>
                  <span className="grow">{item.region_id}</span>
                </div>
                <div className="meta-line">
                  <span style={{ fontFamily: 'ui-monospace, Consolas, monospace' }}>
                    {item.public_ip || '无公网 IP'}
                  </span>
                </div>

                <Meter
                  used={item.traffic_used}
                  quota={item.quota_gb}
                  threshold={item.threshold_gb}
                />

                <div className="toggle-row" style={{ paddingTop: 4, paddingBottom: 0, borderBottom: 0 }}>
                  <div className="toggle-text">
                    {groupedIds.has(item.server_id) ? (
                      <>
                        <strong style={{ fontSize: 12, color: 'var(--amber)' }}>已让位给换班组</strong>
                        <span>这台机器在换班组里，启停由班次决定，保活不会插手</span>
                      </>
                    ) : (
                      <>
                        <strong style={{ fontSize: 12 }}>
                          {item.enabled ? '保活运行中' : '已暂停'}
                        </strong>
                        <span>暂停后不再自动拉起，实例状态保持不变</span>
                      </>
                    )}
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={item.enabled}
                    aria-label="保活开关"
                    className={`switch${item.enabled ? ' on' : ''}`}
                    disabled={groupedIds.has(item.server_id)}
                    onClick={() => onToggle(item, !item.enabled)}
                  />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
