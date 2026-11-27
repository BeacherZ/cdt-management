import { TriangleAlert } from 'lucide-react'
import { Modal } from './ui'

type Props = {
  title: string
  message: string
  /** 第二行补充说明：告诉他「会发生什么 / 不会发生什么」，比一句「确定吗」有用 */
  hint?: string
  confirmText?: string
  busy?: boolean
  onCancel: () => void
  onConfirm: () => void
}

/**
 * 应用内二次确认。
 *
 * 不用 window.confirm：桌面壳 / 部分内嵌浏览器里它可能被静默吞掉，
 * 结果就是「点一下直接删了」，而这几个操作都是不可撤销的。
 */
export default function ConfirmDialog({
  title,
  message,
  hint,
  confirmText = '确认删除',
  busy = false,
  onCancel,
  onConfirm,
}: Props) {
  return (
    <Modal
      title={title}
      icon={<TriangleAlert size={14} style={{ color: 'var(--red)' }} />}
      onClose={() => !busy && onCancel()}
      footer={
        <>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="btn danger" onClick={onConfirm} disabled={busy}>
            {busy ? '处理中…' : confirmText}
          </button>
        </>
      }
    >
      <div className="confirm-body">
        <p className="confirm-text">{message}</p>
        {hint && <p className="confirm-hint">{hint}</p>}
      </div>
    </Modal>
  )
}
