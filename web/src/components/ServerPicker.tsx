import { useState } from 'react'
import { Check, HeartPulse, Repeat, TriangleAlert } from 'lucide-react'
import { del } from '../api'
import { ownerLabel } from '../owners'
import type { Server } from '../types'
import { Modal } from './ui'

type Props = {
  servers: Server[]
  /** 正在编辑的组 id：new 时为 undefined，用于区分「本组」和「别的组」 */
  currentGroupId?: number
  /** 本组其它行已经占用的服务器 id，避免同一台被选两次 */
  taken: Set<number>
  onClose: () => void
  onPick: (server: Server) => void
  /** 取消保活成功后通知父级刷新服务器归属 */
  onKeepaliveCleared?: () => void
}

/**
 * 选服务器：一次列出全部机器，右侧标出它现在的归属。
 *
 * - 闲散手动：直接选中
 * - 单机保活：先问一句「取消保活并加入」，因为保活记录不删的话两边会抢同一台机器
 * - 已加入别的换班组：灰掉，不给选（一台机器只能在一个组里）
 */
export default function ServerPicker({
  servers,
  currentGroupId,
  taken,
  onClose,
  onPick,
  onKeepaliveCleared,
}: Props) {
  const [confirming, setConfirming] = useState<Server | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  function pick(server: Server) {
    setError('')
    if (server.managed?.kind === 'keepalive') {
      setConfirming(server)
      return
    }
    onPick(server)
  }

  async function dropKeepalive(server: Server) {
    const keepaliveId = server.managed?.keepalive_id
    if (!keepaliveId) {
      setError('这条保活记录没有 ID，请到「单机保活」页手动移出后再选')
      return
    }
    setBusy(true)
    setError('')
    try {
      await del(`/api/v1/keepalives/${keepaliveId}`)
      onKeepaliveCleared?.()
      onPick(server)
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消保活失败')
    } finally {
      setBusy(false)
    }
  }

  if (confirming) {
    const owner = confirming.managed
    return (
      <Modal
        title="取消单机保活并加入？"
        icon={<HeartPulse size={16} style={{ color: 'var(--amber)' }} />}
        onClose={() => !busy && setConfirming(null)}
        footer={
          <>
            <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
              {owner ? ownerLabel(owner) : ''}
            </span>
            <span className="spacer" />
            <button className="btn" onClick={() => setConfirming(null)} disabled={busy}>
              取消
            </button>
            <button className="btn primary" onClick={() => void dropKeepalive(confirming)} disabled={busy}>
              {busy ? '处理中…' : '取消保活并加入'}
            </button>
          </>
        }
      >
        <div className="confirm-body">
          <p className="confirm-text">
            {owner?.active
              ? `「${confirming.label}」现在归「单机保活」托管。`
              : `「${confirming.label}」有一条停用中的单机保活记录。`}
          </p>
          <p className="confirm-hint">
            加入本换班组前会先删掉这台机器的保活配置（只删配置，机器不启停）；
            之后它只听排班表的。想反悔得重新加回单机保活。
          </p>
        </div>
        {error && (
          <div className="auth-error">
            <TriangleAlert size={14} />
            {error}
          </div>
        )}
      </Modal>
    )
  }

  return (
    <Modal
      title="选择服务器"
      icon={<Check size={16} style={{ color: 'var(--green)' }} />}
      onClose={onClose}
      footer={
        <>
          <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
            已加入换班组的机器是灰的，一台只能进一个组
          </span>
          <span className="spacer" />
          <button className="btn" onClick={onClose}>
            关闭
          </button>
        </>
      }
    >
      {servers.length === 0 ? (
        <div className="hint">还没有服务器，先去「服务器」页添加</div>
      ) : (
        <div className="server-pick-list">
          {servers.map((server) => {
            const owner = server.managed ?? null
            const sameGroup =
              owner?.kind === 'group' && currentGroupId != null && owner.group_id === currentGroupId
            const otherGroup = owner?.kind === 'group' && !sameGroup
            const used = taken.has(server.id)
            const blocked = otherGroup || used
            const reason = used
              ? '本组别的行已经选了这台'
              : otherGroup
                ? '已加入别的换班组，一台只能进一个组'
                : ''
            return (
              <button
                type="button"
                key={server.id}
                className={`server-pick-row${blocked ? ' is-disabled' : ''}`}
                disabled={blocked}
                title={reason || `选择「${server.label}」`}
                onClick={() => pick(server)}
              >
                <span className="server-pick-main">
                  <span className="server-pick-name">{server.label}</span>
                  <code className="server-pick-id">{server.instance_id || '未填实例 ID'}</code>
                </span>
                <span className={`owner-tag${owner ? (owner.active ? ' locked' : ' paused') : ' free'}`}>
                  {owner?.kind === 'group' ? <Repeat size={10} /> : null}
                  {owner?.kind === 'keepalive' ? <HeartPulse size={10} /> : null}
                  <span className="owner-tag-text">{ownerLabel(owner)}</span>
                </span>
                {used && <span className="server-pick-note">已在其他行</span>}
              </button>
            )
          })}
        </div>
      )}
    </Modal>
  )
}
