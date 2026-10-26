import { useState } from 'react'
import { KeyRound, Server, TriangleAlert } from 'lucide-react'
import { post } from '../api'
import type { Server as ServerType } from '../types'
import { Modal } from './ui'

const REGIONS: { value: string; label: string }[] = [
  { value: 'cn-hongkong', label: '中国香港' },
  { value: 'cn-hangzhou', label: '华东 1（杭州）' },
  { value: 'cn-shanghai', label: '华东 2（上海）' },
  { value: 'cn-beijing', label: '华北 2（北京）' },
  { value: 'cn-zhangjiakou', label: '华北 3（张家口）' },
  { value: 'cn-qingdao', label: '华北 1（青岛）' },
  { value: 'cn-shenzhen', label: '华南 1（深圳）' },
  { value: 'cn-guangzhou', label: '华南 3（广州）' },
  { value: 'cn-chengdu', label: '西南 1（成都）' },
  { value: 'ap-southeast-1', label: '新加坡' },
  { value: 'ap-northeast-1', label: '日本（东京）' },
  { value: 'ap-northeast-2', label: '韩国（首尔）' },
  { value: 'us-west-1', label: '美国（硅谷）' },
  { value: 'us-east-1', label: '美国（弗吉尼亚）' },
]

type Props = {
  server: ServerType | null
  defaultThresholdPercent?: number
  onClose: () => void
  onSaved: (message: string) => void
}

export default function ServerDialog({
  server,
  defaultThresholdPercent = 94,
  onClose,
  onSaved,
}: Props) {
  const editing = Boolean(server && server.id)
  const [form, setForm] = useState({
    id: server?.id ?? 0,
    label: server?.label ?? '',
    access_key_id: '',
    access_key_secret: '',
    region_id: server?.region_id ?? 'cn-hongkong',
    instance_id: server?.instance_id ?? '',
    quota_gb: server?.quota_gb ?? 200,
    threshold_gb: server?.threshold_gb ?? Math.round((200 * defaultThresholdPercent) / 100),
    remark: server?.remark ?? '',
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((current) => ({ ...current, [key]: value }))
  }

  async function save() {
    setError('')
    if (!editing && (!form.access_key_id.trim() || !form.access_key_secret.trim())) {
      setError('新建服务器必须填写 AccessKey ID 与 Secret')
      return
    }
    setBusy(true)
    try {
      await post('/api/v1/servers', { ...form, access_key_id: form.access_key_id.trim() })
      onSaved(editing ? '服务器已更新' : '服务器已添加')
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={editing ? '编辑服务器' : '添加服务器'}
      icon={<Server size={16} style={{ color: 'var(--green)' }} />}
      onClose={onClose}
      footer={
        <>
          <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
            Secret 用本机主密钥加密后存入 SQLite
          </span>
          <span className="spacer" />
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={save} disabled={busy}>
            保存
          </button>
        </>
      }
    >
      <div className="grid-2">
        <div className="field">
          <label htmlFor="label">别名</label>
          <input
            id="label"
            className="input"
            value={form.label}
            onChange={(event) => update('label', event.target.value)}
            placeholder="例如 香港-1号"
          />
        </div>
        <div className="field">
          <label htmlFor="region">地域</label>
          <select
            id="region"
            className="select"
            value={form.region_id}
            onChange={(event) => update('region_id', event.target.value)}
          >
            {REGIONS.map((region) => (
              <option key={region.value} value={region.value}>
                {region.label}（{region.value}）
              </option>
            ))}
            {!REGIONS.some((region) => region.value === form.region_id) && (
              <option value={form.region_id}>{form.region_id}</option>
            )}
          </select>
        </div>
      </div>

      <div className="field">
        <label htmlFor="ak">
          <KeyRound size={12} /> AccessKey ID {!editing && <span className="req">*</span>}
        </label>
        <input
          id="ak"
          className="input"
          value={form.access_key_id}
          onChange={(event) => update('access_key_id', event.target.value)}
          placeholder={editing ? server?.access_key_masked || '留空不修改' : 'LTAI5t…'}
          autoComplete="off"
        />
      </div>

      <div className="field">
        <label htmlFor="sk">
          <KeyRound size={12} /> AccessKey Secret {!editing && <span className="req">*</span>}
        </label>
        <input
          id="sk"
          className="input"
          type="password"
          value={form.access_key_secret}
          onChange={(event) => update('access_key_secret', event.target.value)}
          placeholder={editing ? '留空不修改' : '填写 AccessKey Secret'}
          autoComplete="new-password"
        />
        <div className="hint">
          建议用 RAM 子账号，只授予 ECS 启停/查询与 CDT 只读权限。
        </div>
      </div>

      <div className="field">
        <label htmlFor="instance">实例 ID</label>
        <input
          id="instance"
          className="input"
          value={form.instance_id}
          onChange={(event) => update('instance_id', event.target.value)}
          placeholder="i-j6cxxxxxxxxxxxxxxxx"
        />
      </div>

      <div className="grid-2">
        <div className="field">
          <label htmlFor="quota">总配额（GB）</label>
          <input
            id="quota"
            className="input"
            type="number"
            min={1}
            value={form.quota_gb}
            onChange={(event) => update('quota_gb', Number(event.target.value))}
          />
        </div>
        <div className="field">
          <label htmlFor="threshold">限流阈值（GB）</label>
          <input
            id="threshold"
            className="input"
            type="number"
            min={1}
            value={form.threshold_gb}
            onChange={(event) => update('threshold_gb', Number(event.target.value))}
          />
          <div className="hint">达到该值即触发换班；全部成员都达标则全局熔断。</div>
        </div>
      </div>

      <div className="field">
        <label htmlFor="remark">备注</label>
        <input
          id="remark"
          className="input"
          value={form.remark}
          onChange={(event) => update('remark', event.target.value)}
          placeholder="可选"
        />
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
