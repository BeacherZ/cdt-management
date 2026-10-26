import { LoaderCircle, Pencil, Plus, RefreshCw, Server as ServerIcon, Trash2 } from 'lucide-react'
import type { Server } from '../types'
import { Badge, Empty, Meter, statusLabel, statusTone } from './ui'

type Props = {
  servers: Server[]
  keepaliveIds: Set<number>
  groupedIds: Set<number>
  busyId: number | null
  onAdd: () => void
  onEdit: (server: Server) => void
  onDelete: (server: Server) => void
  onRefresh: (server: Server) => void
  onAction: (server: Server, action: 'start' | 'stop') => void
  onAddToKeepalive: (server: Server) => void
}

export default function ServersView({
  servers,
  keepaliveIds,
  groupedIds,
  busyId,
  onAdd,
  onEdit,
  onDelete,
  onRefresh,
  onAction,
  onAddToKeepalive,
}: Props) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>服务器</h1>
          <p>
            这里登记的是「阿里云账号 + 一台 ECS 实例」。AccessKey Secret 用本机主密钥加密后存
            SQLite，界面只显示打码值。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" onClick={onAdd}>
            <Plus size={14} /> 添加服务器
          </button>
        </div>
      </div>

      {servers.length === 0 ? (
        <div className="card">
          <Empty icon={<ServerIcon size={20} />} title="还没有服务器">
            添加后可以把它放进「单机保活」或「联合换班」。建议使用只授予 ECS 启停/查询与 CDT
            只读权限的 RAM 子账号。
          </Empty>
        </div>
      ) : (
        <div className="server-grid">
          {servers.map((server) => {
            const running = server.instance_status === 'Running'
            return (
              <div className="card server-card" key={server.id}>
                <div className="head">
                  <Badge tone={statusTone(server.instance_status)} pulse={running}>
                    {statusLabel(server.instance_status)}
                  </Badge>
                  <strong title={server.label}>{server.label}</strong>
                  <div className="actions">
                    <button
                      className="btn ghost icon sm"
                      title="刷新"
                      disabled={busyId === server.id}
                      onClick={() => onRefresh(server)}
                    >
                      {busyId === server.id ? (
                        <LoaderCircle size={13} className="spin" />
                      ) : (
                        <RefreshCw size={13} />
                      )}
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title={running ? '节省停机' : '开机'}
                      onClick={() => onAction(server, running ? 'stop' : 'start')}
                    >
                      <ServerIcon size={13} />
                    </button>
                    <button className="btn ghost icon sm" title="编辑" onClick={() => onEdit(server)}>
                      <Pencil size={13} />
                    </button>
                    <button className="btn ghost icon sm" title="删除" onClick={() => onDelete(server)}>
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                <div className="meta-line">
                  <code>{server.instance_id || '未填实例 ID'}</code>
                  <span className="grow">{server.region_id}</span>
                </div>
                <div className="meta-line">
                  <span style={{ fontFamily: 'ui-monospace, Consolas, monospace' }}>
                    {server.public_ip || '无公网 IP'}
                  </span>
                  <span style={{ marginLeft: 'auto' }}>{server.access_key_masked}</span>
                </div>

                <Meter used={server.traffic_used} quota={server.quota_gb} threshold={server.threshold_gb} />

                {server.stopped_mode === 'StopCharging' && server.instance_status === 'Stopped' && (
                  <div style={{ fontSize: 11, color: 'var(--muted)' }}>已按节省停机模式停机</div>
                )}
                {server.remark && (
                  <div style={{ fontSize: 11.5, color: 'var(--muted-soft)' }}>{server.remark}</div>
                )}

                {groupedIds.has(server.id) ? (
                  <Badge tone="blue">已在换班组 · 按班次自动启停</Badge>
                ) : keepaliveIds.has(server.id) ? (
                  <Badge tone="green">已在单机保活</Badge>
                ) : (
                  <button
                    className="btn sm"
                    style={{ alignSelf: 'flex-start' }}
                    onClick={() => onAddToKeepalive(server)}
                  >
                    <Plus size={12} /> 加入单机保活
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
