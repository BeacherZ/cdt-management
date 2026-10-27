import { useState } from 'react'
import {
  AlertTriangle,
  GripVertical,
  Globe2,
  HeartPulse,
  LayoutGrid,
  List,
  LoaderCircle,
  MapPin,
  Pencil,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  Repeat,
  Server as ServerIcon,
  Trash2,
} from 'lucide-react'
import { regionLabel } from '../regions'
import { ownerBase, ownerLabel } from '../owners'
import type { Billing, Server } from '../types'
import { Badge, Empty, Meter, statusLabel, statusTone, stoppedModeLabel } from './ui'

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
  onReorder: (orderedIds: number[]) => void
}

type ViewMode = 'card' | 'list'

/** 后端把多台记录用顿号连成一串（一台可以绑多条），这里拆回一个个小框 */
function dnsRecords(value?: string): string[] {
  return (value || '')
    .split('、')
    .map((item) => item.trim())
    .filter(Boolean)
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
  onReorder,
}: Props) {
  const idleCount = servers.filter((server) => !server.managed).length
  const [viewMode, setViewMode] = useState<ViewMode>('card')
  // 拖拽排序：记录当前被拖的 id 与悬停目标 id，用于视觉反馈与松手换位
  const [dragId, setDragId] = useState<number | null>(null)
  const [overId, setOverId] = useState<number | null>(null)

  function handleDrop(targetId: number) {
    if (dragId === null || dragId === targetId) {
      setDragId(null)
      setOverId(null)
      return
    }
    const next = [...servers]
    const from = next.findIndex((server) => server.id === dragId)
    const to = next.findIndex((server) => server.id === targetId)
    if (from < 0 || to < 0) {
      setDragId(null)
      setOverId(null)
      return
    }
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    onReorder(next.map((server) => server.id))
    setDragId(null)
    setOverId(null)
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>服务器</h1>
          <p>
            这里登记的是「阿里云账号 + 一台 ECS 实例」。AccessKey Secret 用本机主密钥加密后存
            SQLite，界面上不显示，只在编辑弹窗里能换。
            {idleCount > 0 && ` 其中 ${idleCount} 台是闲散机器，可以直接手动开关机。`}
          </p>
        </div>
        <div className="page-actions">
          {servers.length > 1 && (
            <div className="view-toggle" role="group" aria-label="视图切换">
              <button
                type="button"
                className={`btn sm${viewMode === 'card' ? ' primary' : ''}`}
                title="卡片视图"
                onClick={() => setViewMode('card')}
              >
                <LayoutGrid size={13} /> 卡片
              </button>
              <button
                type="button"
                className={`btn sm${viewMode === 'list' ? ' primary' : ''}`}
                title="列表视图"
                onClick={() => setViewMode('list')}
              >
                <List size={13} /> 列表
              </button>
            </div>
          )}
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
      ) : viewMode === 'list' ? (
        <ServerTable
          servers={servers}
          busyId={busyId}
          billingId={billingId}
          onEdit={onEdit}
          onDelete={onDelete}
          onRefresh={onRefresh}
          onAction={onAction}
          onAddToKeepalive={onAddToKeepalive}
          onBilling={onBilling}
        />
      ) : (
        <div className="server-grid">
          {servers.map((server) => {
            const running = server.instance_status === 'Running'
            const owner = server.managed ?? null
            const busy = busyId === server.id
            const locked = Boolean(owner?.active)
            const ownerText = owner ? ownerLabel(owner) : ''
            const lockHint =
              owner && locked
                ? `${ownerBase(owner)}托管中，先暂停或移出后再手动启停`
                : running
                  ? '关机（优先节省停机，不支持时转普通停机）'
                  : '开机'

            return (
              <div
                className={`card server-card${dragId === server.id ? ' dragging' : ''}${
                  overId === server.id ? ' drag-over' : ''
                }`}
                key={server.id}
                draggable
                onDragStart={(event) => {
                  setDragId(server.id)
                  event.dataTransfer.effectAllowed = 'move'
                  // Firefox 需要设置 data 才会真正启动拖拽
                  event.dataTransfer.setData('text/plain', String(server.id))
                }}
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'move'
                  if (overId !== server.id) setOverId(server.id)
                }}
                onDragLeave={() => {
                  if (overId === server.id) setOverId(null)
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  handleDrop(server.id)
                }}
                onDragEnd={() => {
                  setDragId(null)
                  setOverId(null)
                }}
              >
                <div className="head">
                  <GripVertical size={13} className="drag-handle" aria-hidden />
                  <Badge tone={statusTone(server.instance_status)} pulse={running}>
                    {statusLabel(server.instance_status, server.stopped_mode)}
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
                  <code style={{ fontSize: 11 }}>{server.instance_id || '未填实例 ID'}</code>
                </div>
                <div className="meta-line">
                  <span className="card-region" title={server.region_id}>
                    <MapPin size={11} />
                    {regionLabel(server.region_id)}
                  </span>
                  <span className={`site-tag ${server.site === 'international' ? 'intl' : ''}`}>
                    {server.site === 'international' ? '国际站' : '中国站'}
                  </span>
                  <span className="card-ip" title="公网 IP">
                    {server.public_ip || '无公网 IP'}
                  </span>
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

                {server.instance_status === 'Stopped' &&
                  stoppedModeLabel(server.stopped_mode) === '普通停机' && (
                    <div style={{ fontSize: 11, color: 'var(--amber)' }}>
                      普通停机（KeepCharging）仍计实例规格费
                    </div>
                  )}
                {server.remark && (
                  <div style={{ fontSize: 11.5, color: 'var(--muted-soft)' }}>{server.remark}</div>
                )}

                <div className="card-foot">
                  <span className={`owner-tag${owner ? (locked ? ' locked' : ' paused') : ' free'}`}>
                    {owner ? (
                      <>
                        {owner.kind === 'group' ? <Repeat size={10} /> : <HeartPulse size={10} />}
                        <span className="owner-tag-text">{ownerText}</span>
                      </>
                    ) : (
                      <>闲散 · 手动</>
                    )}
                  </span>
                  {dnsRecords(server.dns_record).map((record) => (
                    <span className="dns-chip" key={record} title={record}>
                      <Globe2 size={10} />
                      <span className="dns-chip-text">{record}</span>
                    </span>
                  ))}

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

/** 表格视图：一屏看全所有服务器的核心字段，适合机器多的时候快速对比 */
function ServerTable({
  servers,
  busyId,
  billingId,
  onEdit,
  onDelete,
  onRefresh,
  onAction,
  onAddToKeepalive,
  onBilling,
}: {
  servers: Server[]
  busyId: number | null
  billingId: number | null
  onEdit: (server: Server) => void
  onDelete: (server: Server) => void
  onRefresh: (server: Server) => void
  onAction: (server: Server, action: 'start' | 'stop') => void
  onAddToKeepalive: (server: Server) => void
  onBilling: (server: Server) => void
}) {
  return (
    <div className="card server-table-card">
      <table className="server-table">
        <colgroup>
          <col style={{ width: '11%' }} />
          <col style={{ width: '10%' }} />
          <col style={{ width: '8%' }} />
          <col style={{ width: '9%' }} />
          <col style={{ width: '8%' }} />
          <col style={{ width: '10%' }} />
          {/* 归属列要比别的列宽一点：卡片的文案是「换班组「组名」托管」，窄了会被省略号截掉 */}
          <col style={{ width: '18%' }} />
          <col style={{ width: '15%' }} />
          <col style={{ width: '11%' }} />
        </colgroup>
        <thead>
          <tr>
            <th>服务器 / 实例 ID</th>
            <th>状态</th>
            <th>地域 / 站点</th>
            <th>公网 IP</th>
            <th>流量 / 配额</th>
            <th>本月 / 余额</th>
            <th>归属</th>
            <th>域名</th>
            <th style={{ textAlign: 'right' }}>操作</th>
          </tr>
        </thead>
        <tbody>
          {servers.map((server) => {
            const running = server.instance_status === 'Running'
            const owner = server.managed ?? null
            const busy = busyId === server.id
            const locked = Boolean(owner?.active)
            const ownerText = ownerLabel(owner)
            return (
              <tr key={server.id}>
                <td data-label="服务器">
                  <div className="server-table-title" title={server.label}>
                    {server.label}
                  </div>
                  <code className="server-table-sub" title={server.instance_id}>
                    {server.instance_id || '未填实例 ID'}
                  </code>
                </td>
                <td data-label="状态">
                  <Badge tone={statusTone(server.instance_status)} pulse={running}>
                    {statusLabel(server.instance_status, server.stopped_mode)}
                  </Badge>
                </td>
                <td data-label="地域">
                  <div className="server-table-region">
                    <span className="server-table-cell">
                      <MapPin size={11} />
                      {regionLabel(server.region_id)}
                    </span>
                    <span className={`site-tag ${server.site === 'international' ? 'intl' : ''}`}>
                      {server.site === 'international' ? '国际' : '中国'}
                    </span>
                  </div>
                </td>
                <td data-label="公网 IP">
                  <span className="server-table-mono">{server.public_ip || '—'}</span>
                </td>
                <td data-label="流量 / 配额">
                  <div className="server-table-meter">
                    <span className="server-table-mono">
                      {(server.traffic_used ?? 0).toFixed(1)}
                    </span>
                    <span className="server-table-muted"> / {server.quota_gb} GB</span>
                    {(server.traffic_used ?? 0) >= server.threshold_gb && (
                      <AlertTriangle size={11} style={{ color: 'var(--red)' }} />
                    )}
                  </div>
                </td>
                <td className="server-table-cost" data-label="费用">
                  <CostRow
                    billing={server.billing}
                    busy={billingId === server.id}
                    onRefresh={() => onBilling(server)}
                    compact
                  />
                </td>
                <td data-label="归属">
                  <span
                    className={`owner-tag${owner ? (locked ? ' locked' : ' paused') : ' free'}`}
                    title={ownerText}
                  >
                    {owner ? (
                      <>
                        {owner.kind === 'group' ? <Repeat size={10} /> : <HeartPulse size={10} />}
                        <span className="owner-tag-text">{ownerText}</span>
                      </>
                    ) : (
                      <>闲散 · 手动</>
                    )}
                  </span>
                </td>
                <td data-label="域名">
                  {/* 与卡片同一套数据：换班组绑定的域名只出现在当班那台 */}
                  <div className="server-table-dns">
                    {dnsRecords(server.dns_record).map((record) => (
                      <span className="dns-chip" key={record} title={record}>
                        <Globe2 size={10} />
                        <span className="dns-chip-text">{record}</span>
                      </span>
                    ))}
                    {!server.dns_record && <span className="server-table-muted">—</span>}
                  </div>
                </td>
                <td data-label="操作">
                  <div className="server-table-actions">
                    <button
                      className="btn ghost icon sm"
                      title="刷新状态"
                      disabled={busy}
                      onClick={() => onRefresh(server)}
                    >
                      {busy ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
                    </button>
                    <button className="btn ghost icon sm" title="编辑" onClick={() => onEdit(server)}>
                      <Pencil size={13} />
                    </button>
                    {!owner && (
                      <button
                        className="btn ghost sm"
                        title="加入保活"
                        onClick={() => onAddToKeepalive(server)}
                      >
                        <HeartPulse size={13} />
                      </button>
                    )}
                    <button
                      className={`btn sm ${running ? 'danger' : 'primary'}`}
                      title={locked ? '托管中，先移出再启停' : running ? '关机' : '开机'}
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
                    <button
                      className="btn ghost icon sm"
                      title="删除"
                      onClick={() => onDelete(server)}
                    >
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
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
  compact = false,
}: {
  billing: Billing
  busy: boolean
  onRefresh: () => void
  compact?: boolean
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

  if (compact) {
    return (
      <div className="cost-row compact" title={hint}>
        <span className="cost-item">
          <span className="cost-key">本月</span>
          <b className={billing.monthly_cost === null ? 'dim' : ''}>
            {money(billing.monthly_cost, billing.currency)}
          </b>
        </span>
        <span className="cost-item">
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
          {busy ? <LoaderCircle size={11} className="spin" /> : <RefreshCw size={11} />}
        </button>
      </div>
    )
  }

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
