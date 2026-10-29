import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

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
  if (state === 'error') return 'red'
  return 'gray'
}

/** 只在「正在动」的状态上闪，静态状态闪起来像在报警 */
export function groupStatePulse(state: string): boolean {
  return state === 'switching' || state === 'recovering'
}

/* -------------------------------------------------------------- 环形进度 */

export function Ring({
  percent,
  size = 46,
  stroke = 4,
  tone = 'accent',
  children,
}: {
  percent: number
  size?: number
  stroke?: number
  tone?: 'accent' | 'amber' | 'red' | 'blue'
  children?: ReactNode
}) {
  const safe = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0))
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  return (
    <div className={`ring ${tone}`} style={{ width: size, height: size }}>
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

export function Meter({ used, quota, threshold }: { used: number | null; quota: number; threshold: number }) {
  const percent = used === null ? 0 : Math.min(100, (used / (quota || 1)) * 100)
  const thresholdPercent = Math.min(100, (threshold / (quota || 1)) * 100)
  const tone = percent >= thresholdPercent ? 'danger' : percent >= thresholdPercent * 0.9 ? 'warn' : ''
  const trackRef = useRef<HTMLDivElement>(null)
  const markRef = useRef<HTMLElement>(null)

  // 阈值刻度线必须落在整像素上：百分比定位落在亚像素位置时，浏览器按设备像素
  // 对齐后有的卡片只压住 1 列像素、有的压住 2 列，看起来就是「一边细一边粗」。
  // 轨道宽度差零点几像素就会触发（415px 与 416px 的 90% 落在不同相位），所以
  // 这里量出轨道的绝对位置，把刻度线的**页面坐标**取整，再减去轨道自身的小数偏移
  // ——只按轨道内部取整还不够，轨道本身起点在小数像素上时照样会糊。
  useEffect(() => {
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

  return (
    <div className="meter">
      <div className="meter-track" ref={trackRef} title={`阈值线 ${thresholdPercent.toFixed(0)}%`}>
        <div className={`meter-fill ${tone}`} style={{ width: `${percent}%` }} />
        {/* 内联百分比只是首帧兜底，挂载后由上面那段 effect 改成整像素 */}
        <i className="meter-mark" ref={markRef} style={{ left: `${thresholdPercent}%` }} />
      </div>
      <div className="meter-labels">
        <span>{used === null ? '未读取' : `${used.toFixed(2)} GB`}</span>
        <span>
          {percent.toFixed(1)}% / 阈值 {threshold} GB
        </span>
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
  // onClose 每次渲染都是新函数（父组件里是内联箭头），不能进依赖数组：
  // 否则父组件每秒 setTick 重渲染一次，这个 effect 就每秒重跑一次，
  // 每次都 focus() 一下会把 <select> 的原生下拉框顶掉——表现就是「点开闪一下就没了」。
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // 只在挂载时抢一次焦点，之后再不碰
    ref.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
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
