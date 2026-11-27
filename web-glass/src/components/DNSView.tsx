import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  ArrowRight,
  Check,
  ChevronDown,
  CircleSlash,
  Globe2,
  GripVertical,
  LoaderCircle,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import type {
  BalanceGroup,
  DnsRecord,
  DnsState,
  Keepalive,
  Managed,
  Server,
  ShiftGroup,
} from '../types'
import { timeAgo } from '../time'
import { ownerIcon, type OwnerKind } from '../owners'
import { Badge, Empty, OwnerIcon, type Tone } from './ui'

type Props = {
  dns: DnsState
  servers: Server[]
  keepalives: Keepalive[]
  groups: ShiftGroup[]
  balances: BalanceGroup[]
  /** 全量归属表：DNS 卡里那颗图标要跟实例管理说同一件事（闲散的显示手掌） */
  owners: Map<number, Managed>
  busy: string | null
  syncingAll: boolean
  onBind: (record: string, kind: string, id: number) => void
  onSync: (record: string) => void
  onSyncAll: () => void
  onAddRecord: (name: string) => Promise<boolean>
  onRemoveRecord: (record: string) => void
  onReorder: (ordered: string[]) => void
  onOpenSettings: () => void
}

const STATE_META: Record<DnsRecord['state'], { tone: Tone; text: string }> = {
  unbound: { tone: 'gray', text: '未绑定' },
  idle: { tone: 'amber', text: '目标未运行' },
  pending: { tone: 'amber', text: '待同步' },
  synced: { tone: 'green', text: '已同步' },
  error: { tone: 'red', text: '同步失败' },
}

/** select 的值编码成 kind:id，空值表示解绑 */
function encode(record: DnsRecord): string {
  if (!record.target) return ''
  return `${record.target.kind}:${record.target.id}`
}

function decode(value: string): { kind: string; id: number } {
  if (!value) return { kind: '', id: 0 }
  const [kind, id] = value.split(':')
  return { kind, id: Number(id) }
}

/** 上次同步时间：文案「还没有同步过」是本页特有的，时间基准仍走后端时钟 */
function lastSyncText(ts: number): string {
  if (!ts) return '还没有同步过'
  return timeAgo(ts)
}

/* ------------------------------------------------------------ 绑定下拉 */

type BindItem = { value: string; label: string }
type BindSection = { label: string; kind?: OwnerKind; items: BindItem[] }

const UNBOUND: BindItem = { value: '', label: '不绑定（这条记录不会被动）' }

/**
 * 「绑定到」下拉。
 *
 * 为什么不用原生 <select>：分组标题要带语义色小图标（闲散灰手掌 / 保活紫心脉 /
 * 换班绿循环 / 均衡蓝天平），而 <option> 里只能放纯文本，连颜色都只在大体上
 * 生效、各家浏览器还不一致。于是自绘一个：收起状态长得和原来的 .select 一模一样，
 * 点开才多出图标和一列分组标题。
 *
 * 交互按原生下拉的习惯做：点空白处收起、Esc 收起、再点一次触发器也收起；
 * 选项是 button，键盘 Tab / Enter 都能走完。
 */
function BindPicker({
  id,
  value,
  sections,
  disabled,
  onPick,
}: {
  id: string
  value: string
  sections: BindSection[]
  disabled: boolean
  onPick: (value: string) => void
}) {
  const [open, setOpen] = useState(false)
  // 朝上还是朝下：卡片贴着视口底部时菜单必须往上翻，否则最后几项永远在屏外
  const [flip, setFlip] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      if (!boxRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  /**
   * 打开时一次定好三件事，用 useLayoutEffect 是为了在绘制前完成，避免看见跳动：
   *
   * 1. 朝下还是朝上——手机上最后一张卡的菜单有 264px，视口底部根本放不下，
   *    以前会伸到屏外，底部那几项（均衡组）既点不到也滚不出来；
   * 2. 菜单最高多少——按「实际可用空间」和「内容高度」取小，两个方向都不溢出；
   * 3. 把当前选中项滚到可视区中间——分组一多，选中项常在首屏之外。
   */
  useLayoutEffect(() => {
    if (!open) return
    const box = boxRef.current
    const menu = menuRef.current
    if (!box || !menu) return
    const rect = box.getBoundingClientRect()
    const gap = 6
    const below = window.innerHeight - rect.bottom - gap
    const above = rect.top - gap
    // 上限跟 CSS 对齐：手机 264、桌面 400（见 styles.css 的 .bind-menu）
    const cap = window.innerWidth <= 700 ? 264 : 400
    const need = Math.min(menu.scrollHeight, cap)
    const up = below < need && above > below
    setFlip(up)
    menu.style.maxHeight = `${Math.round(Math.max(140, Math.min(need, up ? above : below)))}px`
    const active = activeRef.current
    if (active) {
      menu.scrollTop = Math.max(0, active.offsetTop - menu.clientHeight / 2 + active.offsetHeight / 2)
    }
  }, [open])

  const current =
    sections.flatMap((section) => section.items).find((item) => item.value === value) ?? UNBOUND

  function pick(next: string) {
    setOpen(false)
    if (next !== value) onPick(next)
  }

  return (
    <div className={`bind-picker${open ? ' is-open' : ''}${flip ? ' is-flip' : ''}`} ref={boxRef}>
      <button
        type="button"
        id={id}
        className="select bind-trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className="bind-trigger-text">{current.label}</span>
        <ChevronDown size={14} className="bind-caret" aria-hidden />
      </button>
      {open && (
        <div
          className="bind-menu"
          role="listbox"
          aria-labelledby={id}
          ref={menuRef}
          /* 菜单在 draggable 的卡片里：不拦住的话，按住菜单上下拖会变成拖整张卡。
             preventDefault 掐掉这次拖拽，stopPropagation 挡住卡片那层 onDragStart ——
             否则卡片照样进入「正在排序」状态，而拖拽已被取消、dragend 不会来，
             这张卡会一直挂着 dragging 的高亮。 */
          onDragStart={(event) => {
            event.preventDefault()
            event.stopPropagation()
          }}
        >
          <button
            type="button"
            role="option"
            aria-selected={value === ''}
            ref={value === '' ? activeRef : null}
            className={`bind-option${value === '' ? ' is-active' : ''}`}
            onClick={() => pick('')}
          >
            <span className="bind-option-text">{UNBOUND.label}</span>
            {value === '' && <Check size={13} aria-hidden />}
          </button>
          {sections.map((section) => (
            <div className="bind-section" key={section.label} role="group" aria-label={section.label}>
              <div className="bind-section-label">
                {section.kind && <OwnerIcon kind={section.kind} size={12} />}
                {section.label}
              </div>
              {section.items.map((item) => (
                <button
                  type="button"
                  role="option"
                  aria-selected={item.value === value}
                  key={`${section.label}-${item.value}`}
                  ref={item.value === value ? activeRef : null}
                  className={`bind-option${item.value === value ? ' is-active' : ''}`}
                  onClick={() => pick(item.value)}
                >
                  <span className="bind-option-text">{item.label}</span>
                  {item.value === value && <Check size={13} aria-hidden />}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function DNSView({
  dns,
  servers,
  keepalives,
  groups,
  balances,
  owners,
  busy,
  syncingAll,
  onBind,
  onSync,
  onSyncAll,
  onAddRecord,
  onRemoveRecord,
  onReorder,
  onOpenSettings,
}: Props) {
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  // 拖拽排序：记录当前被拖的记录名与悬停目标，用于视觉反馈与松手换位
  const [dragRecord, setDragRecord] = useState<string | null>(null)
  const [overRecord, setOverRecord] = useState<string | null>(null)

  function handleDrop(target: string) {
    if (!dragRecord || dragRecord === target) {
      setDragRecord(null)
      setOverRecord(null)
      return
    }
    const next = dns.records.map((item) => item.record)
    const from = next.indexOf(dragRecord)
    const to = next.indexOf(target)
    if (from < 0 || to < 0) {
      setDragRecord(null)
      setOverRecord(null)
      return
    }
    const [moved] = next.splice(from, 1)
    // 与实例列表同一处偏移：向下拖要减一，否则会插到目标后面。
    next.splice(from < to ? to - 1 : to, 0, moved)
    setDragRecord(null)
    setOverRecord(null)
    onReorder(next)
  }

  const keepaliveIds = new Set(keepalives.map((item) => item.server_id))
  // 第一项是「所有实例」全量清单，下面几个大项只是同一批机器的不同视角（快捷入口）：
  // 换班组 / 均衡组会接管机器，所以这些机器不再重复出现在「单机保活」里。
  const groupedIds = new Set([
    ...groups.flatMap((group) => group.members.map((member) => member.server_id)),
    ...balances.flatMap((group) => group.members.map((member) => member.server_id)),
  ])
  const keepaliveServers = servers.filter(
    (server) => keepaliveIds.has(server.id) && !groupedIds.has(server.id),
  )
  // 闲散手动：没被任何流程接管的机器。这里查 owners（后端全量口径，含已停用的组），
  // 不看 server.managed —— 后端 managed_map 只标启用中的组员，停用的组员会被漏成闲散。
  const idleServers = servers.filter((server) => !owners.has(server.id))

  // 下拉分组：所有实例 → 闲散手动 → 单机保活 → 换班组 → 均衡组。
  // 后四项是同一批机器的不同视角（快捷入口），有内容才出现，空的整组不占位。
  // 四个归属组的标题都带语义色图标，和实例管理、DNS 卡片下方那行是同一套映射。
  const bindSections: BindSection[] = []
  if (servers.length > 0) {
    bindSections.push({
      label: '所有实例',
      items: servers.map((server) => ({ value: `server:${server.id}`, label: server.label })),
    })
  }
  if (idleServers.length > 0) {
    bindSections.push({
      label: '闲散手动',
      kind: 'free',
      items: idleServers.map((server) => ({ value: `server:${server.id}`, label: server.label })),
    })
  }
  if (keepaliveServers.length > 0) {
    bindSections.push({
      label: '单机保活',
      kind: 'keepalive',
      items: keepaliveServers.map((server) => ({ value: `server:${server.id}`, label: server.label })),
    })
  }
  if (groups.length > 0) {
    bindSections.push({
      label: '换班组',
      kind: 'shift',
      items: groups.map((group) => ({ value: `group:${group.id}`, label: group.name })),
    })
  }
  if (balances.length > 0) {
    bindSections.push({
      label: '均衡组',
      kind: 'balance',
      items: balances.map((group) => ({ value: `balance:${group.id}`, label: group.name })),
    })
  }

  async function submit() {
    const name = draft.trim()
    if (!name) return
    setSaving(true)
    const ok = await onAddRecord(name)
    setSaving(false)
    if (ok) {
      setDraft('')
      setAdding(false)
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>域名记录</h1>
          <p>
            一条域名只绑一台机器（或一个组），但一台机器、一个组都可以挂多条域名。
            没绑的记录一律不动；机器一开机，指向它的所有记录一起更新 —— 不会再出现几台机器抢同一个域名。
            绑到单台机器就固定指它，不跟随换班组 / 均衡组的轮换走。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn"
            onClick={onSyncAll}
            disabled={!dns.enabled || syncingAll || dns.records.length === 0}
          >
            {syncingAll ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
            全部同步
          </button>
          <button className="btn primary" onClick={() => setAdding((value) => !value)}>
            <Plus size={14} /> 添加记录名
          </button>
        </div>
      </div>

      {!dns.enabled && (
        <div className="notice amber">
          <CircleSlash size={14} />
          <span>DDNS 已关闭，这里的绑定不会生效。</span>
          <button className="btn ghost sm" onClick={onOpenSettings}>
            去设置里打开
          </button>
        </div>
      )}

      {dns.enabled && !dns.configured && (
        <div className="notice amber">
          <CircleSlash size={14} />
          <span>还没填 Cloudflare Token / Key，同步会直接失败。</span>
          <button className="btn ghost sm" onClick={onOpenSettings}>
            去设置
          </button>
        </div>
      )}

      {adding && (
        <div className="card card-pad dns-add">
          <div className="field" style={{ flex: 1 }}>
            <label htmlFor="newRecord">新记录名</label>
            <input
              id="newRecord"
              className="input"
              value={draft}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submit()
                if (event.key === 'Escape') setAdding(false)
              }}
              placeholder="cdt-b.example.com"
            />
            <div className="hint">
              必须是在 Cloudflare 里已经存在的 A 记录；Zone ID 留空时会按根域自动反查。
            </div>
          </div>
          <div className="dns-add-actions">
            <button className="btn" onClick={() => setAdding(false)} disabled={saving}>
              取消
            </button>
            <button className="btn primary" onClick={() => void submit()} disabled={saving || !draft.trim()}>
              {saving ? <LoaderCircle size={13} className="spin" /> : null} 添加
            </button>
          </div>
        </div>
      )}

      {dns.records.length === 0 ? (
        <div className="card">
          <Empty icon={<Globe2 size={20} />} title="还没有域名记录">
            先添加一条记录名，再把它绑到某台机器或某个换班组上。第一次用需要先在设置里填 Cloudflare 的
            Token 与 TTL。
          </Empty>
        </div>
      ) : (
        <div className="dns-list">
          {dns.records.map((record) => {
            const meta = STATE_META[record.state]
            const working = busy === record.record
            const target = record.target
            // 只有目标此刻真在跑才铺渐变：绑在已停机实例/组上的记录保持素卡，
            // 否则绿底配着「目标未运行」的黄标，两种信号打架。
            const live = Boolean(target?.running)
            return (
              <div
                className={`card dns-row${live ? ' is-live' : ''}${
                  dragRecord === record.record ? ' dragging' : ''
                }${overRecord === record.record ? ' drag-over' : ''}`}
                key={record.record}
                draggable
                onDragStart={(event) => {
                  setDragRecord(record.record)
                  event.dataTransfer.effectAllowed = 'move'
                  // Firefox 需要设置 data 才会真正启动拖拽
                  event.dataTransfer.setData('text/plain', record.record)
                }}
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'move'
                  if (overRecord !== record.record) setOverRecord(record.record)
                }}
                onDragLeave={() => {
                  if (overRecord === record.record) setOverRecord(null)
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  handleDrop(record.record)
                }}
                onDragEnd={() => {
                  setDragRecord(null)
                  setOverRecord(null)
                }}
              >
                <div className="dns-row-head">
                  <GripVertical size={13} className="drag-handle" aria-hidden />
                  <Globe2 size={15} style={{ color: 'var(--accent)', flex: 'none' }} />
                  <code className="dns-name">{record.record}</code>
                  <Badge tone={meta.tone} dot>
                    {meta.text}
                  </Badge>
                  {dns.proxied && <span className="dns-tag">CDN 代理</span>}
                  <div className="dns-row-actions">
                    <button
                      className="btn ghost icon sm"
                      title="立即同步这条记录"
                      disabled={!dns.enabled || working || !target}
                      onClick={() => onSync(record.record)}
                    >
                      {working ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
                    </button>
                    <button
                      className="btn ghost icon sm"
                      title="删除记录名（不会删 Cloudflare 上的记录）"
                      onClick={() => onRemoveRecord(record.record)}
                    >
                      <Trash2 size={13} style={{ color: 'var(--red)' }} />
                    </button>
                  </div>
                </div>

                <div className="dns-bind">
                  <label htmlFor={`bind-${record.record}`}>绑定到</label>
                  <BindPicker
                    id={`bind-${record.record}`}
                    value={encode(record)}
                    sections={bindSections}
                    disabled={working}
                    onPick={(value) => {
                      const { kind, id } = decode(value)
                      onBind(record.record, kind, id)
                    }}
                  />
                </div>

                <div className="dns-meta">
                  {target ? (
                    <span className="dns-target">
                      {/* 图标和实例管理说同一件事：换班组=品牌绿、均衡组=蓝、保活=紫，
                          没归属的机器（闲散手动）=手掌，颜色跟这行文字一样是中性灰。
                          颜色由全局 .owner-icon-* 那四条给，组件本身只挑图标。 */}
                      <OwnerIcon
                        kind={
                          target.kind === 'group'
                            ? 'shift'
                            : target.kind === 'balance'
                              ? 'balance'
                              : /* 单台机器看它的真实归属：编在组里就是组的颜色，
                                   没归属就是手掌（ownerIcon 对空值返回 'free'） */
                                ownerIcon(owners.get(target.id))
                        }
                        size={11}
                      />
                      {/* 名字单独包一层：这一层负责省略号截断。以前名字是裸文本节点，
                          截断不了，只能整行换行，同排卡片就一高一低。
                          这里不再放「运行中 / 未运行」胶囊：卡片第一行的状态徽标
                          （未绑定 / 目标未运行 / 待同步 / 已同步 / 同步失败）已经说清了
                          目标在不在，同一个信息写两遍反而抢这一行的位置。 */}
                      <span className="dns-target-text">
                        {target.kind === 'group'
                          ? `换班组「${target.label}」`
                          : target.kind === 'balance'
                            ? `均衡组「${target.label}」`
                            : target.label}
                        {(target.kind === 'group' || target.kind === 'balance') && target.duty
                          ? ` · 当班 ${target.duty}`
                          : ''}
                      </span>
                    </span>
                  ) : (
                    <span className="dns-target muted">没绑任何机器，同步时直接跳过</span>
                  )}

                  <span className="dns-ip">
                    解析到
                    <code>{record.last_ip || '—'}</code>
                    {record.desired_ip && record.desired_ip !== record.last_ip && (
                      <>
                        <ArrowRight size={11} />
                        <code className="want">{record.desired_ip}</code>
                      </>
                    )}
                  </span>
                  <span className="dns-time">{lastSyncText(record.last_at)}</span>
                  {record.last_status && record.state === 'error' && (
                    <span className="dns-error">{record.last_status}</span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
