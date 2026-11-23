import { useState } from 'react'
import {
  AlertTriangle,
  GripVertical,
  Globe2,
  Hand,
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
  Scale,
  Server as ServerIcon,
  Trash2,
} from 'lucide-react'
import { regionLabel } from '../regions'
import {
  ownerActionHint,
  ownerBase,
  ownerDutyText,
  ownerIcon,
  ownerLabel,
  ownerManualFree,
  ownerTone,
} from '../owners'
import { clockText } from '../time'
import type { Billing, Managed, Server } from '../types'
import {
  Badge,
  Empty,
  Meter,
  OwnerIcon,
  money,
  statusLabel,
  statusTone,
  stoppedModeLabel,
} from './ui'

/**
 * 归属胶囊：卡片和列表共用这一份。
 *
 * 图标 + 组名 + 「· 当班 / · 非当班」，换班组和均衡组写法完全一样 —— 以前只有
 * 均衡组带后半句，同一页两种胶囊不一样长，换班组非当班成员的按钮锁着也没理由。
 *
 * 当班那截单独成 span、不参与收缩：组名太长时只截组名，当班/非当班必须留着。
 */
function OwnerTag({ owner }: { owner: Managed | null }) {
  if (!owner) {
    // 闲散手动也有自己的图标（手掌），颜色跟着胶囊文字走，不再是一颗光秃秃的文字胶囊
    return (
      <span className="owner-tag free" title="闲散手动">
        <OwnerIcon kind="free" />
        <span className="owner-tag-text">闲散手动</span>
      </span>
    )
  }
  const duty = ownerDutyText(owner)
  return (
    <span
      className={`owner-tag ${ownerTone(owner)}${owner.active ? '' : ' paused'}`}
      title={ownerLabel(owner)}
    >
      <OwnerIcon kind={ownerIcon(owner)} />
      <span className="owner-tag-text">{ownerBase(owner)}</span>
      {duty && <span className="owner-duty">· {duty}</span>}
    </span>
  )
}

type Props = {
  servers: Server[]
  /** 全量归属表（含已停用的组员 / 保活）：筛选与归属标签都以它为准 */
  owners: Map<number, Managed>
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

/** 归属筛选的一颗胶囊。按**类型**分档，不按组名：
    联合换班、流量均衡下面可能各挂好几个组，列成组名一排排不下，找的也不是「哪个组」。 */
type OwnerChip = {
  key: string
  label: string
  tone: string
  icon: typeof Hand
  count: number
}

/** 分档顺序固定：闲散手动 → 单机保活 → 联合换班 → 流量均衡，跟侧栏口径一致 */
const OWNER_TABS: { key: string; label: string; tone: string; icon: typeof Hand }[] = [
  { key: 'free', label: '闲散手动', tone: 'tone-free', icon: Hand },
  { key: 'keepalive', label: '单机保活', tone: 'tone-keepalive', icon: HeartPulse },
  { key: 'shift', label: '联合换班', tone: 'tone-shift', icon: Repeat },
  { key: 'balance', label: '流量均衡', tone: 'tone-balance', icon: Scale },
]

/** 一台机器属于哪一档：同一个类型下的多个组并进同一档，计数相加 */
function ownerKeyOf(owner: Managed | null): string {
  if (!owner) return 'free'
  return owner.kind === 'group' ? 'shift' : owner.kind
}

/** 后端把多台记录用顿号连成一串（一台可以绑多条），这里拆回一个个小框 */
function dnsRecords(value?: string): string[] {
  return (value || '')
    .split('、')
    .map((item) => item.trim())
    .filter(Boolean)
}

export default function ServersView({
  servers,
  owners,
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
  const [viewMode, setViewMode] = useState<ViewMode>('card')
  // 归属筛选：'all' = 不过滤，其余见 ownerKeyOf
  const [ownerKey, setOwnerKey] = useState<string>('all')
  // 拖拽排序：记录当前被拖的 id 与悬停目标 id，用于视觉反馈与松手换位
  const [dragId, setDragId] = useState<number | null>(null)
  const [overId, setOverId] = useState<number | null>(null)

  const ownerOf = (server: Server): Managed | null => owners.get(server.id) ?? server.managed ?? null

  // 按类型计数：同一个类型下的多个组并成一档
  const counted = new Map<string, number>()
  for (const server of servers) {
    const key = ownerKeyOf(ownerOf(server))
    counted.set(key, (counted.get(key) ?? 0) + 1)
  }
  const chips: OwnerChip[] = OWNER_TABS.map((tab) => ({ ...tab, count: counted.get(tab.key) ?? 0 })).filter(
    (chip) => chip.count > 0,
  )

  // 选中的这一档没了（最后一台机器移出该流程）就退回「全部」，别停在空列表上
  const activeKey = ownerKey === 'all' || counted.has(ownerKey) ? ownerKey : 'all'
  const visible =
    activeKey === 'all'
      ? servers
      : servers.filter((server) => ownerKeyOf(ownerOf(server)) === activeKey)
  const idleCount = counted.get('free') ?? 0

  function handleDrop(targetId: number) {
    if (dragId === null || dragId === targetId) {
      setDragId(null)
      setOverId(null)
      return
    }
    const visibleIds = visible.map((server) => server.id)
    const from = visibleIds.indexOf(dragId)
    const to = visibleIds.indexOf(targetId)
    if (from < 0 || to < 0) {
      setDragId(null)
      setOverId(null)
      return
    }
    const nextVisible = [...visibleIds]
    const [moved] = nextVisible.splice(from, 1)
    // 向下拖时 to 是删除前算的：先 splice 掉 from，目标整体左移一位，
    // 直接插到 to 会落在目标后面（差一格）。向上拖不受影响。
    nextVisible.splice(from < to ? to - 1 : to, 0, moved)
    // 筛选状态下只重排「看得见的那些位置」，被筛掉的机器原地不动
    const slots = new Set(visibleIds)
    let cursor = 0
    onReorder(servers.map((server) => (slots.has(server.id) ? nextVisible[cursor++] : server.id)))
    setDragId(null)
    setOverId(null)
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>实例管理</h1>
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
            <Plus size={14} /> 添加实例
          </button>
        </div>
      </div>

      {/* 归属选择：机器一多，卡片墙一眼扫不到「那台保活的」在哪。
          按归属分档后点一下只剩这一档，档内还是原来的顺序，开关机 / 拖拽都不受影响。
          只有一档归属（全都是闲散、或者只有一台）时不显示，免得白占一行。 */}
      {chips.length > 1 && (
        <div className="owner-filter" role="group" aria-label="按归属筛选实例">
          <button
            type="button"
            className={`owner-chip${activeKey === 'all' ? ' on' : ''}`}
            aria-pressed={activeKey === 'all'}
            onClick={() => setOwnerKey('all')}
          >
            全部
            <span className="owner-chip-count">{servers.length}</span>
          </button>
          {chips.map((chip) => {
            const Icon = chip.icon
            return (
              <button
                key={chip.key}
                type="button"
                className={`owner-chip ${chip.tone}${activeKey === chip.key ? ' on' : ''}`}
                aria-pressed={activeKey === chip.key}
                title={`只看${chip.label}`}
                onClick={() => setOwnerKey(chip.key)}
              >
                <Icon size={12} />
                {chip.label}
                <span className="owner-chip-count">{chip.count}</span>
              </button>
            )
          })}
        </div>
      )}

      {servers.length === 0 ? (
        <div className="card">
          <Empty icon={<ServerIcon size={20} />} title="还没有实例">
            添加后可以把它放进「单机保活」或「联合换班」。建议使用只授予 ECS 启停/查询与 CDT
            只读权限的 RAM 子账号。
          </Empty>
        </div>
      ) : viewMode === 'list' ? (
        <ServerTable
          servers={visible}
          owners={owners}
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
          {visible.map((server) => {
            const running = server.instance_status === 'Running'
            const owner = ownerOf(server)
            const busy = busyId === server.id
            const locked = !ownerManualFree(owner)
            const lockHint = ownerActionHint(owner, running)

            return (
              <div
                className={`card server-card${running ? ' is-live' : ''}${
                  dragId === server.id ? ' dragging' : ''
                }${overId === server.id ? ' drag-over' : ''}`}
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

                {/* 身份行①：实例 ID 靠左，站点类型靠右 */}
                <div className="meta-line">
                  <code style={{ fontSize: 12 }} title={server.instance_id}>
                    {server.instance_id || '未填实例 ID'}
                  </code>
                  <span
                    className={`site-tag meta-right ${server.site === 'international' ? 'intl' : ''}`}
                  >
                    {server.site === 'international' ? '国际站' : '中国站'}
                  </span>
                </div>
                {/* 身份行②：公网 IP 靠左，地域靠右 */}
                <div className="meta-line">
                  <span className="addr-chip" title="公网 IP">
                    {server.public_ip || '无公网 IP'}
                  </span>
                  <span className="card-region meta-right" title={server.region_id}>
                    <MapPin size={11} />
                    {regionLabel(server.region_id)}
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
                    <div style={{ fontSize: 12, color: 'var(--amber)' }}>
                      普通停机（KeepCharging）仍计实例规格费
                    </div>
                  )}
                {server.remark && (
                  <div style={{ fontSize: 12, color: 'var(--muted-soft)' }}>{server.remark}</div>
                )}

                {dnsRecords(server.dns_record).length > 0 && (
                  <div className="card-dns-row">
                    {dnsRecords(server.dns_record).map((record) => (
                      <span className="dns-chip" key={record} title={record}>
                        <Globe2 size={10} />
                        <span className="dns-chip-text">{record}</span>
                      </span>
                    ))}
                  </div>
                )}

                <div className="card-foot">
                  <OwnerTag owner={owner} />

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

/** 表格视图：一屏看全所有实例的核心字段，适合机器多的时候快速对比 */
function ServerTable({
  servers,
  owners,
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
  owners: Map<number, Managed>
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
        {/* 8 列：公网 IP 与域名并成一列（都是「怎么连上这台机器」的信息，上下两行比并排省一整列）。
            列宽不再写死百分比 —— 定宽布局下 9 列挤在 1100 上下时每一列都会被省略号切掉，
            改成按内容自然分配（见 styles.css 里 .server-table 的说明） */}
        <thead>
          <tr>
            {/* 状态在实例名前面：一眼先看「跑没跑」，再认是哪台。
                手机上这张表会按 data-label 重排，「实例」那一格永远排第一行（见 styles.css） */}
            <th>状态</th>
            <th>实例 / 实例 ID</th>
            <th>站点 / 地域</th>
            <th>IP / 域名</th>
            <th>流量 / 配额</th>
            <th>本月 / 余额</th>
            <th>归属</th>
            <th style={{ textAlign: 'right' }}>操作</th>
          </tr>
        </thead>
        <tbody>
          {servers.map((server) => {
            const running = server.instance_status === 'Running'
            const owner = owners.get(server.id) ?? server.managed ?? null
            const busy = busyId === server.id
            const locked = !ownerManualFree(owner)
            return (
              <tr key={server.id}>
                <td data-label="状态">
                  {/* 状态与停机模式拆成两行：并排塞进一枚胶囊时「已停机 · 节省停机」
                      比整列还宽，会溢出压到右边的站点/地域列上 */}
                  <div className="server-table-status">
                    <Badge tone={statusTone(server.instance_status)} pulse={running}>
                      {statusLabel(server.instance_status)}
                    </Badge>
                    {server.instance_status === 'Stopped' && stoppedModeLabel(server.stopped_mode) && (
                      <span className="server-table-status-sub">
                        {stoppedModeLabel(server.stopped_mode)}
                      </span>
                    )}
                  </div>
                </td>
                <td data-label="实例">
                  <div className="server-table-title" title={server.label}>
                    {server.label}
                  </div>
                  <code className="server-table-sub" title={server.instance_id}>
                    {server.instance_id || '未填实例 ID'}
                  </code>
                </td>
                <td data-label="站点 / 地域">
                  <div className="server-table-region">
                    <span className={`site-tag ${server.site === 'international' ? 'intl' : ''}`}>
                      {server.site === 'international' ? '国际' : '中国'}
                    </span>
                    <span className="server-table-cell">
                      <MapPin size={11} />
                      {/* 地域名长度不一（中国香港 / 日本（东京）），列一窄就会顶到右边的公网 IP 上，
                          这里交给省略号收尾，完整名字放 title */}
                      <span className="server-table-cell-text" title={regionLabel(server.region_id)}>
                        {regionLabel(server.region_id)}
                      </span>
                    </span>
                  </div>
                </td>
                <td data-label="IP / 域名">
                  {/* 两行：上面公网 IP（这一行永远是重点），下面域名（没绑就是一条横杠）。
                      并成一列后横向省下约 120px，正好让给左边的实例名。
                      data-label 比表头的「IP / 域名」短一截 —— 手机上标签只有 68px，长了会压到数值 */}
                  <div className="server-table-net">
                    <span className="addr-chip">{server.public_ip || '—'}</span>
                    <div className="server-table-dns">
                      {/* 与卡片同一套数据：换班组绑定的域名只出现在当班那台 */}
                      {dnsRecords(server.dns_record).map((record) => (
                        <span className="dns-chip" key={record} title={record}>
                          <Globe2 size={10} />
                          <span className="dns-chip-text">{record}</span>
                        </span>
                      ))}
                      {!server.dns_record && <span className="server-table-muted">—</span>}
                    </div>
                  </div>
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
                  <OwnerTag owner={owner} />
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
                      className="btn ghost icon sm"
                      title="删除"
                      onClick={() => onDelete(server)}
                    >
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                    <button
                      className={`btn sm ${running ? 'danger' : 'primary'}`}
                      title={ownerActionHint(owner, running)}
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
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 余额直接显示 BSS 返回的 AvailableAmount（国际站后付费恒 0，代理商预付费有真实余额）。 */
function balanceText(billing: Billing): string {
  return money(billing.balance, billing.currency)
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
      ? `更新于 ${clockText(billing.updated_at)}`
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
            {balanceText(billing)}
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
          {balanceText(billing)}
        </b>
      </span>
      {/* 尾部：告警图标和刷新按钮合成一格，贴着右边缘，余额才好落在正中间 */}
      <span className="cost-tail">
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
      </span>
    </div>
  )
}
