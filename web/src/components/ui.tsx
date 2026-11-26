import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { Hand, HeartPulse, Repeat, Scale, X } from 'lucide-react'
import type { OwnerKind } from '../owners'
import { poolName, poolOf } from '../regions'

/* -------------------------------------------------------------- 徽章 */

export type Tone = 'green' | 'red' | 'amber' | 'blue' | 'gray'

export function Badge({
  tone = 'gray',
  children,
  pulse = false,
  dot = true,
}: {
  tone?: Tone
  children: ReactNode
  pulse?: boolean
  dot?: boolean
}) {
  return (
    <span className={`badge ${tone}${pulse ? ' pulse' : ''}`}>
      {dot && <i className="dot" />}
      {children}
    </span>
  )
}

const STATUS_TONE: Record<string, Tone> = {
  Running: 'green',
  Stopped: 'gray',
  Starting: 'amber',
  Stopping: 'amber',
  Pending: 'amber',
  Unknown: 'gray',
}

export function statusTone(status: string): Tone {
  return STATUS_TONE[status] ?? 'gray'
}

export const STATUS_LABEL: Record<string, string> = {
  Running: '运行中',
  Stopped: '已停机',
  Starting: '启动中',
  Stopping: '停止中',
  Pending: '创建中',
  Unknown: '未知',
}

/** 阿里云 StoppedMode。节省停机不计规格费，普通停机照常计费，必须分清楚 */
export const STOPPED_MODE_LABEL: Record<string, string> = {
  StopCharging: '节省停机',
  KeepCharging: '普通停机',
  Stop: '普通停机',
  'Not-applicable': '普通停机',
}

export function stoppedModeLabel(mode?: string | null): string {
  const key = (mode ?? '').trim()
  if (!key) return ''
  return STOPPED_MODE_LABEL[key] ?? key
}

export function statusLabel(status: string, mode?: string | null) {
  const base = STATUS_LABEL[status] ?? status ?? '未知'
  // 停机时把真实模式一起显示：以前一律当节省停机，普通停机会被误认为不花钱
  const detail = status === 'Stopped' ? stoppedModeLabel(mode) : ''
  return detail ? `${base} · ${detail}` : base
}

/** 换班组徽章配色：正常绿、故障转移黄、手动换班 / 换班中蓝、熔断与异常红、其余灰 */
export function groupStateTone(state: string): Tone {
  if (state === 'scheduled') return 'green'
  if (state === 'failover') return 'amber'
  if (state === 'recovering') return 'amber'
  if (state === 'manual') return 'blue'
  if (state === 'switching') return 'blue'
  if (state === 'breaker') return 'red'
  // 超限停机比熔断轻一档：只停了当班，不是整组都没法接
  if (state === 'limit_stop') return 'amber'
  if (state === 'error') return 'red'
  return 'gray'
}

/** 只在「正在动」的状态上闪，静态状态闪起来像在报警 */
export function groupStatePulse(state: string): boolean {
  return state === 'switching' || state === 'recovering'
}

/* -------------------------------------------------------------- 归属图标 */

/**
 * 归属图标：四种状态各一颗，一次定义，四处共用（实例管理卡片、实例列表、
 * 换班组的成员选择器、DNS 记录卡）。
 *
 * 以前每个界面各写一串三元，结果均衡组在实例列表里漏过一次（只有换班和保活
 * 有图标）；「闲散手动」干脆一颗都没有，和另外三种摆在一起像掉了零件。
 *
 * 颜色不进组件：默认继承父级 `color`，想上语义色就由父容器的 class 决定
 * （见 styles.css 里 .owner-icon-* 那四条）。闲散手动因此天然和文字同色。
 */
export function OwnerIcon({ kind, size = 10 }: { kind: OwnerKind; size?: number }) {
  const Icon =
    kind === 'shift'
      ? Repeat
      : kind === 'balance'
        ? Scale
        : kind === 'keepalive'
          ? HeartPulse
          : Hand
  return <Icon size={size} className={`owner-icon owner-icon-${kind}`} aria-hidden />
}

/* -------------------------------------------------------------- 环形进度 */

export function Ring({
  percent,
  size = 46,
  stroke = 4,
  tone = 'accent',
  children,
  title,
}: {
  percent: number
  size?: number
  stroke?: number
  tone?: 'accent' | 'amber' | 'red' | 'blue' | 'violet'
  children?: ReactNode
  title?: string
}) {
  const safe = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0))
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  return (
    <div className={`ring ${tone}`} style={{ width: size, height: size }} title={title}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          className="ring-track"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={stroke}
          fill="none"
        />
        <circle
          className="ring-bar"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={stroke}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - safe / 100)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <span className="ring-text">{children ?? `${Math.round(safe)}%`}</span>
    </div>
  )
}

/* -------------------------------------------------------------- 流量条 */

export function Meter({
  used,
  quota,
  threshold,
  region,
  compact = false,
}: {
  used: number | null
  quota: number
  threshold: number
  /** 实例所在地域：配额缺失时兜底分母，也决定下方文字的「国内包 / 海外包」 */
  region?: string | null
  /** 紧凑模式：只报「已用 + 百分比」，阈值只留刻度线，给列表行那种窄列用 */
  compact?: boolean
}) {
  // 分母只认配额（后端按地域给默认额度，用户可改），和通知、概览流量圈一个口径；
  // 阈值只用来定颜色和那条刻度线，不参与百分比。
  // 配额缺失时按地域推断，兜底也用 200 会把国内机的百分比算成 1/10。
  const base = quota > 0 ? quota : poolOf(region).quota
  const rawPercent = used === null ? 0 : (used / base) * 100
  // 进度条画到 100% 就顶格，但标签照报真实值（超配额时能读到 103.4%，别糊成 100.0%）
  const percent = Math.min(100, rawPercent)
  const thresholdPercent = Math.min(100, (threshold / base) * 100)
  const tone = rawPercent >= thresholdPercent ? 'danger' : rawPercent >= thresholdPercent * 0.9 ? 'warn' : ''
  const trackRef = useRef<HTMLDivElement>(null)
  const markRef = useRef<HTMLElement>(null)

  // 阈值刻度线必须落在整像素上：百分比定位落在亚像素位置时，浏览器按设备像素
  // 对齐后有的卡片只压住 1 列像素、有的压住 2 列，看起来就是「一边细一边粗」。
  // 轨道宽度差零点几像素就会触发（415px 与 416px 的 90% 落在不同相位），所以
  // 这里量出轨道的绝对位置，把刻度线的**页面坐标**取整，再减去轨道自身的小数偏移
  // ——只按轨道内部取整还不够，轨道本身起点在小数像素上时照样会糊。
  //
  // 用 useLayoutEffect 而不是 useEffect：内联的百分比只是首帧兜底，整像素修正在
  // 这里同步完成，浏览器还没绘制就已经落位。换成 useEffect 会先画一帧百分比位置
  // （实测 315.828px）再改成 317px，配上线体残留的 left 过渡，切页面时能看到
  // 竖条明显滑一下。
  useLayoutEffect(() => {
    const track = trackRef.current
    const mark = markRef.current
    if (!track || !mark) return
    const snap = () => {
      const rect = track.getBoundingClientRect()
      if (!rect.width) return
      const target = rect.left + (thresholdPercent / 100) * rect.width
      const snapped = Math.round(target)
      // CSS 里 margin-left: -1px 让 2px 宽的线以 left 为中心，这里补回那 1px
      mark.style.left = `${snapped - rect.left + 1}px`
    }
    snap()
    const observer = new ResizeObserver(snap)
    observer.observe(track)
    return () => observer.disconnect()
  }, [thresholdPercent])

  // 两处提示分工，五个界面用同一套说法：
  //   条上 —— 阈值线画在哪、后面还剩多少余量（百分比 + 绝对 GB）；
  //   文字那行 —— 这台机器算哪个流量包、总配额多少。
  // 以前两套混在一个 title 里（紧凑模式把「已用 / 配额 / 阈值」和阈值线拼成两行），
  // 同一组数字在不同界面说法不一样，读起来才觉得乱。
  // 提示仍挂在整条 meter 而不是 7px 的轨道上：轨道太细，鼠标偏一点就落空。
  const fmtGb = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1))
  const meterTitle = `阈值线 ${thresholdPercent.toFixed(0)}% · ${fmtGb(threshold)} GB`
  // 文字那行写「总配额」而不是「配额」：这一格是实例设置里填的总量，和阈值/剩余
  // 区分开，避免读成「本包还剩多少」。
  const labelTitle = `${poolName(region)} · 总配额 ${fmtGb(base)} GB`

  return (
    <div className={`meter${compact ? ' is-compact' : ''}`} title={meterTitle}>
      <div className="meter-track" ref={trackRef}>
        <div className={`meter-fill ${tone}`} style={{ width: `${percent}%` }} />
        {/* 内联百分比只是首帧兜底，挂载后由上面那段 effect 改成整像素 */}
        <i className="meter-mark" ref={markRef} style={{ left: `${thresholdPercent}%` }} />
      </div>
      {/* 自己的 title 会盖住外层，所以悬停这行数字给的是包信息，不是阈值信息 */}
      <div className="meter-labels" title={labelTitle}>
        <span>{used === null ? '未读取' : `${used.toFixed(2)} GB`}</span>
        {/* 只留百分比：分母是总配额这件事由悬浮提示说明（「海外包 · 总配额 200 GB」），
            一行里再挤一串配额数字，窄卡上容易折行，信息也重复。 */}
        <span>{rawPercent.toFixed(1)}%</span>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------- 开关 */

export function Switch({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  hint?: string
}) {
  return (
    <div className="toggle-row">
      <div className="toggle-text">
        <strong>{label}</strong>
        {hint && <span>{hint}</span>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        className={`switch${checked ? ' on' : ''}`}
        onClick={() => onChange(!checked)}
      />
    </div>
  )
}

/* -------------------------------------------------------------- 分段 */

export function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}) {
  return (
    <div className="segmented">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={value === option.value ? 'on' : ''}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

/* -------------------------------------------------------------- 模态 */

/** 打开中的弹窗栈（底 → 顶）。Esc 只关最上面那一层。 */
const modalStack: symbol[] = []

export function Modal({
  title,
  icon,
  onClose,
  children,
  footer,
  wide = false,
}: {
  title: string
  icon?: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const modalId = useRef(Symbol('modal'))
  // onClose 每次渲染都是新函数（父组件里是内联箭头），不能进依赖数组：
  // 否则父组件每秒 setTick 重渲染一次，这个 effect 就每秒重跑一次，
  // 每次都 focus() 一下会把 <select> 的原生下拉框顶掉——表现就是「点开闪一下就没了」。
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const id = modalId.current
    modalStack.push(id)
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      // 只让最上面那一层响应。以前每层都独立监听 document，ServerDialog 里再开
      // 停机确认框时，一次 Esc 会把两层一起关掉，AK/SK 和没保存的输入全丢。
      if (modalStack[modalStack.length - 1] !== id) return
      event.stopPropagation()
      closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // 只在挂载时抢一次焦点，之后再不碰
    ref.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      const index = modalStack.lastIndexOf(id)
      if (index >= 0) modalStack.splice(index, 1)
      document.body.style.overflow = previousOverflow
    }
  }, [])

  return (
    <div className="overlay" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`modal${wide ? ' wide' : ''}`} role="dialog" aria-modal="true" tabIndex={-1} ref={ref}>
        <div className="modal-head">
          {icon}
          <h3>{title}</h3>
          <button type="button" className="btn ghost icon sm close" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}

/* -------------------------------------------------------------- 金额 */

const CURRENCY_SIGN: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  HKD: 'HK$',
  JPY: 'JP¥',
  EUR: '€',
  GBP: '£',
}

/** 金额展示统一走这里：实例卡片与概览的实例状态卡读的是同一份 BSS 快照，
 *  各写一份格式化会出现「¥1.42」和「1.42 元」两种写法 */
export function money(value: number | null, currency: string): string {
  if (value === null || Number.isNaN(value)) return '—'
  const sign = CURRENCY_SIGN[currency] ?? `${currency} `
  return `${sign}${value.toFixed(2)}`
}

/* -------------------------------------------------------------- 空状态 */

export function Empty({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-icon">{icon}</div>
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  )
}
