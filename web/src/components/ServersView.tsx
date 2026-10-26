import {
  AlertTriangle,
  HeartPulse,
  LoaderCircle,
  Pencil,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  Repeat,
  Server as ServerIcon,
  Trash2,
} from 'lucide-react'
import type { Billing, Server } from '../types'
import { Badge, Empty, Meter, statusLabel, statusTone } from './ui'

type Props = {
  servers: Server[]
  busyId: number | null
  billingId: number | null
  onAdd: () => void
  onEdit: (server: Server) => void
  onDelete: (server: Server) => void
  onRefresh: (server: Server) => void
  onAction: (server: Server, action: 'start' | 'stop') => void
  onAddToKeepalive: (server: Server) => void
  onBilling: (server: Server) => void
}

export default function ServersView({
  servers,
  busyId,
  billingId,
  onAdd,
  onEdit,
  onDelete,
  onRefresh,
  onAction,
  onAddToKeepalive,
  onBilling,
}: Props) {
  const idleCount = servers.filter((server) => !server.managed).length

  return (
    <>
      <div className="page-head">
        <div>
          <h1>服务器</h1>
          <p>
            这里登记的是「阿里云账号 + 一台 ECS 实例」。AccessKey Secret 用本机主密钥加密后存
            SQLite，界面只显示打码值。
            {idleCount > 0 && ` 其中 ${idleCount} 台是闲散机器，可以直接手动开关机。`}
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
            const owner = server.managed ?? null
            const busy = busyId === server.id
            // 正在被自动化流程接管的机器不给手动启停：手动关机会被下一轮对账立刻拉起来，
            // 还会额外触发一条假的「抢占恢复」通知
            const locked = Boolean(owner?.active)
            const ownerText =
              owner?.kind === 'group'
                ? `换班组「${owner.name}」`
                : owner?.kind === 'keepalive'
                  ? '单机保活'
                  : ''
            const lockHint = locked
              ? `${ownerText}托管中，先暂停或移出后再手动启停`
              : running
                ? '按节省停机模式关机'
                : '开机'

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
                      title="刷新状态"
                      disabled={busy}
                      onClick={() => onRefresh(server)}
                    >
                      {busy ? (
                        <LoaderCircle size={13} className="spin" />
                      ) : (
                        <RefreshCw size={13} />
                      )}
                    </button>
                    <button className="btn ghost icon sm" title="编辑" onClick={() => onEdit(server)}>
                      <Pencil size={13} />
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title="删除"
                      onClick={() => onDelete(server)}
                    >
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                <div className="meta-line">
                  <code>{server.instance_id || '未填实例 ID'}</code>
                  <span className="grow">{server.region_id}</span>
                  <span className={`site-tag ${server.site === 'international' ? 'intl' : ''}`}>
                    {server.site === 'international' ? '国际站' : '中国站'}
                  </span>
                </div>
                <div className="meta-line">
                  <span style={{ fontFamily: 'ui-monospace, Consolas, monospace' }}>
                    {server.public_ip || '无公网 IP'}
                  </span>
                  <span style={{ marginLeft: 'auto' }}>{server.access_key_masked}</span>
                </div>

                <Meter
                  used={server.traffic_used}
                  quota={server.quota_gb}
                  threshold={server.threshold_gb}
                />

                <CostRow
                  billing={server.billing}
                  busy={billingId === server.id}
                  onRefresh={() => onBilling(server)}
                />

                {server.stopped_mode === 'StopCharging' && server.instance_status === 'Stopped' && (
                  <div style={{ fontSize: 11, color: 'var(--muted)' }}>已按节省停机模式停机</div>
                )}
                {server.remark && (
                  <div style={{ fontSize: 11.5, color: 'var(--muted-soft)' }}>{server.remark}</div>
                )}

                <div className="card-foot">
                  <span className={`owner-tag${owner ? (locked ? ' locked' : ' paused') : ' free'}`}>
                    {owner ? (
                      <>
                        {owner.kind === 'group' ? <Repeat size={10} /> : <HeartPulse size={10} />}
                        {ownerText}
                        {locked ? '托管' : '已暂停'}
                      </>
                    ) : (
                      <>闲散 · 手动</>
                    )}
                  </span>

                  <div className="foot-actions">
                    {!owner && (
                      <button className="btn ghost sm" onClick={() => onAddToKeepalive(server)}>
                        <Plus size={12} /> 加入保活
                      </button>
                    )}
                    <button
                      className={`btn sm ${running ? 'danger' : 'primary'}`}
                      title={lockHint}
                      disabled={locked || busy}
                      onClick={() => onAction(server, running ? 'stop' : 'start')}
                    >
                      {busy ? (
                        <LoaderCircle size={12} className="spin" />
                      ) : running ? (
                        <PowerOff size={12} />
                      ) : (
                        <Power size={12} />
                      )}
                      {running ? '关机' : '开机'}
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}

const CURRENCY_SIGN: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  HKD: 'HK$',
  JPY: 'JP¥',
  EUR: '€',
  GBP: '£',
}

function money(value: number | null, currency: string): string {
  if (value === null || Number.isNaN(value)) return '—'
  const sign = CURRENCY_SIGN[currency] ?? `${currency} `
  return `${sign}${value.toFixed(2)}`
}

/** 本月账单 + 账户余额。数据来自 BSS，后台每 30 分钟刷一次，这里只负责展示。 */
function CostRow({
  billing,
  busy,
  onRefresh,
}: {
  billing: Billing
  busy: boolean
  onRefresh: () => void
}) {
  const failed = Boolean(billing.error)
  const hint = failed
    ? `费用查询失败：${billing.error}`
    : billing.updated_at
      ? `更新于 ${new Date(billing.updated_at * 1000).toLocaleTimeString('zh-CN', {
          hour: '2-digit',
          minute: '2-digit',
        })}`
      : '还没有查询过'

  return (
    <div className="cost-row">
      <span className="cost-item" title={hint}>
        <span className="cost-key">本月</span>
        <b className={billing.monthly_cost === null ? 'dim' : ''}>
          {money(billing.monthly_cost, billing.currency)}
        </b>
        {billing.stale && <i className="cost-stale">上月</i>}
      </span>
      <span className="cost-item" title={hint}>
        <span className="cost-key">余额</span>
        <b className={billing.balance === null ? 'dim' : ''}>
          {money(billing.balance, billing.currency)}
        </b>
      </span>
      {failed && (
        <AlertTriangle
          size={11}
          style={{ color: 'var(--amber, #b45309)', flexShrink: 0 }}
          aria-label="费用查询失败"
        />
      )}
      <button
        type="button"
        className="btn ghost icon sm cost-refresh"
        title="重新查询费用"
        disabled={busy}
        onClick={onRefresh}
      >
        {busy ? <LoaderCircle size={12} className="spin" /> : <RefreshCw size={11} />}
      </button>
    </div>
  )
}
