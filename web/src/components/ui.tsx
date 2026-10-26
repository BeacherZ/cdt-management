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

export function statusLabel(status: string) {
  return STATUS_LABEL[status] ?? status ?? '未知'
}

export function phaseTone(phase: string): Tone {
  if (phase === 'settled') return 'green'
  if (phase === 'failover') return 'amber'
  if (phase === 'breaker') return 'red'
  if (phase === 'error') return 'red'
  if (phase === 'switching') return 'blue'
  return 'gray'
}

/* -------------------------------------------------------------- 流量条 */

export function Meter({ used, quota, threshold }: { used: number | null; quota: number; threshold: number }) {
  const percent = used === null ? 0 : Math.min(100, (used / (quota || 1)) * 100)
  const thresholdPercent = Math.min(100, (threshold / (quota || 1)) * 100)
  const tone = percent >= thresholdPercent ? 'danger' : percent >= thresholdPercent * 0.9 ? 'warn' : ''
  return (
    <div className="meter">
      <div className="meter-track" title={`阈值线 ${thresholdPercent.toFixed(0)}%`}>
        <div className={`meter-fill ${tone}`} style={{ width: `${percent}%` }} />
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

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    ref.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [onClose])

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
