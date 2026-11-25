import { useState } from 'react'
import { Globe2, KeyRound, MapPin, Server, TriangleAlert } from 'lucide-react'
import { post } from '../api'
import { DEFAULT_REGION, GROUP_ORDER, poolOf, regionsFor } from '../regions'
import type { Server as ServerType, SiteType, StopMode } from '../types'
import { Modal, Segmented } from './ui'
import ConfirmDialog from './ConfirmDialog'

const SITE_OPTIONS: { value: SiteType; label: string }[] = [
  { value: 'china', label: '中国站' },
  { value: 'international', label: '国际站' },
]

const STOP_MODE_OPTIONS: { value: StopMode; label: string }[] = [
  { value: 'StopCharging', label: '节省停机' },
  { value: 'Stop', label: '普通停机' },
]

function groupedRegions(site: SiteType) {
  const allowed = regionsFor(site)
  return GROUP_ORDER.map((group) => ({
    group,
    items: allowed.filter((region) => region.group === group),
  })).filter((entry) => entry.items.length > 0)
}

type Props = {
  server: ServerType | null
  defaultThresholdPercent?: number
  onClose: () => void
  onSaved: (message: string) => void
}

export default function ServerDialog({
  server,
  defaultThresholdPercent = 90,
  onClose,
  onSaved,
}: Props) {
  const editing = Boolean(server && server.id)
  // 配额默认按地域的流量包额度给（中国内地 20 GB、非中国内地 200 GB），但可以手改：
  // 阿里以后调免费额度、或者账号单独谈过配额，都靠这一格。
  // 编辑时用库里存的值，不会被地域默认值悄悄盖掉。
  const initialRegion = server?.region_id ?? DEFAULT_REGION
  const initialQuota = server?.quota_gb ?? poolOf(initialRegion).quota
  const [form, setForm] = useState(() => ({
    id: server?.id ?? 0,
    label: server?.label ?? '',
    access_key_id: '',
    access_key_secret: '',
    site: (server?.site ?? 'china') as SiteType,
    region_id: initialRegion,
    instance_id: server?.instance_id ?? '',
    quota_gb: initialQuota,
    // 阈值自由设置，但不能超过配额；老数据超过时先压到配额上限，保存时不会被卡住
    threshold_gb: Math.min(
      server?.threshold_gb ?? Math.round((initialQuota * defaultThresholdPercent) / 100),
      initialQuota,
    ),
    stop_mode: (server?.stop_mode ?? 'StopCharging') as StopMode,
    remark: server?.remark ?? '',
  }))
  const [error, setError] = useState('')
  // 出错的那一格（input 的 id），用来描红 + 聚焦；一开始填就清掉
  const [badField, setBadField] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmStop, setConfirmStop] = useState(false)

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((current) => ({ ...current, [key]: value }))
    if (badField) setBadField('')
    if (error) setError('')
  }

  // 切地域（含切站点导致地域回退到默认值）时：配额还是老地域的默认额度（或本来就没填）
  // 就换成新地域的默认额度（中国内地 20 GB、非中国内地 200 GB），阈值跟着按「新建实例
  // 默认阈值（% 配额）」重算；手改过配额则只换地域，配额和阈值都保留。
  // 判据与后端 upsert_server / update_server_runtime 一致：值 == 老地域默认值即视为没改过。
  function withPoolQuota<
    T extends { region_id: string; quota_gb: number; threshold_gb: number },
  >(current: T, regionId: string): T {
    const customized = current.quota_gb > 0 && current.quota_gb !== poolOf(current.region_id).quota
    const quota = poolOf(regionId).quota
    if (customized || quota === current.quota_gb) return { ...current, region_id: regionId }
    return {
      ...current,
      region_id: regionId,
      quota_gb: quota,
      threshold_gb: Math.round((quota * defaultThresholdPercent) / 100),
    }
  }

  // 手改配额：把阈值改到配额以下时按默认百分比压回，不然保存时才报错更难受
  function changeQuota(value: number) {
    setForm((current) => ({
      ...current,
      quota_gb: value,
      threshold_gb:
        value > 0 && current.threshold_gb > value
          ? Math.round((value * defaultThresholdPercent) / 100)
          : current.threshold_gb,
    }))
    if (badField) setBadField('')
    if (error) setError('')
  }

  function changeSite(site: SiteType) {
    setForm((current) => {
      const stillValid = regionsFor(site).some((region) => region.value === current.region_id)
      return withPoolQuota({ ...current, site }, stillValid ? current.region_id : DEFAULT_REGION)
    })
  }

  // 存量数据里地域可能与站点不匹配（比如早期都是中国站），这里补一个选项免得下拉框显示空白
  const regionKnown = regionsFor(form.site).some((region) => region.value === form.region_id)

  // 提示条永远贴在正文底部，这里再把出事的那一格滚进视野并聚焦，
  // 让「点了保存没反应」变成「红框 + 光标停在该填的格子里」
  function fail(id: string, message: string) {
    setError(message)
    setBadField(id)
    window.requestAnimationFrame(() => {
      const el = document.getElementById(id)
      if (!el) return
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      el.focus({ preventScroll: true })
    })
  }

  // 必填校验按表单自上而下的顺序走，报出来的第一条就是最靠上那处没填的。
  // 编辑态的 AccessKey / Secret 留空表示沿用已存的密钥（后端也是这么处理的），
  // 只有新建时必须填 —— 星号两处都打，弹不弹提示按这个区分。
  function firstMissing(): { id: string; message: string } | null {
    if (!form.label.trim()) return { id: 'label', message: '请填写别名' }
    if (!editing && !form.access_key_id.trim()) {
      return { id: 'ak', message: '请填写 AccessKey ID' }
    }
    if (!editing && !form.access_key_secret.trim()) {
      return { id: 'sk', message: '请填写 AccessKey Secret' }
    }
    // 存量数据可能本来就没有实例 ID（列表里显示「未填实例 ID」）：
    // 编辑这种老记录时不拦，否则连配额都改不了；但把已填的 ID 删空要拦住。
    const legacyWithoutId = editing && !(server?.instance_id ?? '').trim()
    if (!form.instance_id.trim() && !legacyWithoutId) {
      return { id: 'instance', message: '请填写实例 ID' }
    }
    return null
  }

  async function save() {
    setError('')
    setBadField('')
    const missing = firstMissing()
    if (missing) {
      fail(missing.id, missing.message)
      return
    }
    // 数值框 min 属性对「onClick 保存」不生效，清空会变成 Number('')=0 静默提交，
    // 必须在提交前拦住，别把配额/阈值写成 0 污染换班引擎判定。
    const quota = Number(form.quota_gb)
    const threshold = Number(form.threshold_gb)
    if (!Number.isFinite(quota) || quota <= 0) {
      fail('quota', '总配额必须是大于 0 的数字')
      return
    }
    if (!Number.isFinite(threshold) || threshold <= 0) {
      fail('threshold', '限流阈值必须是大于 0 的数字')
      return
    }
    if (threshold > quota) {
      fail('threshold', '限流阈值不能超过总配额')
      return
    }
    // 从节省停机改成普通停机要二次确认：这一改动会让每次关机都开始计规格费，
    // 用户多半只是想看看有什么区别，一句提示比事后看账单便宜。
    if (form.stop_mode === 'Stop' && (server?.stop_mode ?? 'StopCharging') !== 'Stop') {
      setConfirmStop(true)
      return
    }
    await submit()
  }

  async function submit() {
    setBusy(true)
    try {
      await post('/api/v1/servers', { ...form, access_key_id: form.access_key_id.trim() })
      onSaved(editing ? '实例已更新' : '实例已添加')
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={editing ? '编辑实例' : '添加实例'}
      icon={<Server size={16} style={{ color: 'var(--accent)' }} />}
      onClose={onClose}
      footer={
        <>
          <span className="foot-note">Secret 用本机主密钥加密后存入 SQLite</span>
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
      <div className="field">
        <label>
          <Globe2 size={12} /> 站点类型<span className="req" aria-hidden="true">*</span>
        </label>
        <Segmented value={form.site} options={SITE_OPTIONS} onChange={changeSite} />
        <div className="hint">
          中国站对应 aliyun.com 账号，国际站对应 alibabacloud.com 账号。ECS 与 CDT
          接口两边相同，仅费用类接口地址不同；国际站不提供中国大陆地域。
        </div>
      </div>

      <div className="grid-2">
        <div className="field">
          <label htmlFor="label">
            别名<span className="req" aria-hidden="true">*</span>
          </label>
          <input
            id="label"
            aria-invalid={badField === 'label'}
            className="input"
            value={form.label}
            onChange={(event) => update('label', event.target.value)}
            placeholder="例如 香港-1号"
          />
        </div>
        <div className="field">
          <label htmlFor="region">
            <MapPin size={12} /> 地域<span className="req" aria-hidden="true">*</span>
          </label>
          <select
            id="region"
            className="select"
            value={form.region_id}
            onChange={(event) =>
              setForm((current) => withPoolQuota(current, event.target.value))
            }
          >
            {groupedRegions(form.site).map((entry) => (
              <optgroup key={entry.group} label={entry.group}>
                {entry.items.map((region) => (
                  <option key={region.value} value={region.value}>
                    {region.label}（{region.value}）
                  </option>
                ))}
              </optgroup>
            ))}
            {!regionKnown && <option value={form.region_id}>{form.region_id}（不在当前站点列表）</option>}
          </select>
        </div>
      </div>

      <div className="field">
        <label htmlFor="ak">
          <KeyRound size={12} /> AccessKey ID<span className="req" aria-hidden="true">*</span>
        </label>
        <input
          id="ak"
          aria-invalid={badField === 'ak'}
          className="input mono"
          value={form.access_key_id}
          onChange={(event) => update('access_key_id', event.target.value)}
          placeholder={editing ? server?.access_key_masked || '留空不修改' : 'LTAI5t…'}
          autoComplete="off"
        />
      </div>

      <div className="field">
        <label htmlFor="sk">
          <KeyRound size={12} /> AccessKey Secret<span className="req" aria-hidden="true">*</span>
        </label>
        <input
          id="sk"
          aria-invalid={badField === 'sk'}
          className="input mono"
          type="password"
          value={form.access_key_secret}
          onChange={(event) => update('access_key_secret', event.target.value)}
          placeholder={editing ? '留空不修改' : '填写 AccessKey Secret'}
          autoComplete="new-password"
        />
      </div>

      <div className="field">
        <label htmlFor="instance">
          实例 ID<span className="req" aria-hidden="true">*</span>
        </label>
        <input
          id="instance"
          aria-invalid={badField === 'instance'}
          className="input mono"
          value={form.instance_id}
          onChange={(event) => update('instance_id', event.target.value)}
          placeholder="i-j6cxxxxxxxxxxxxxxxx"
        />
      </div>

      <div className="grid-2">
        <div className="field">
          <label htmlFor="quota">
            总配额（GB）<span className="req" aria-hidden="true">*</span>
          </label>
          <input
            id="quota"
            aria-invalid={badField === 'quota'}
            className="input"
            type="number"
            min={1}
            value={form.quota_gb}
            onChange={(event) => changeQuota(Number(event.target.value))}
          />
        </div>
        <div className="field">
          <label htmlFor="threshold">
            限流阈值（GB）<span className="req" aria-hidden="true">*</span>
          </label>
          <input
            id="threshold"
            aria-invalid={badField === 'threshold'}
            className="input"
            type="number"
            min={1}
            max={form.quota_gb}
            value={form.threshold_gb}
            onChange={(event) => update('threshold_gb', Number(event.target.value))}
          />
        </div>
      </div>

      <div className="field">
        <label>
          停机模式<span className="req" aria-hidden="true">*</span>
        </label>
        <Segmented value={form.stop_mode} options={STOP_MODE_OPTIONS} onChange={(value) => update('stop_mode', value)} />
        <div className="hint">
          保活、换班休班、超限与手动关机的所有停机动作都按这里执行。普通停机仍按规格收费；
          本地盘 / 包年包月 / GPU 等规格不支持节省停机，会自动退化为普通停机。
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

      {confirmStop && (
        <ConfirmDialog
          title="改成普通停机？"
          message={`「${form.label || '这台实例'}」之后每次关机都会按实例规格计费。`}
          hint="只有「到点必须开得起来」或「IP 不能变、又不走 DDNS」时才建议这样；只想让它停机省钱就用节省停机。"
          confirmText="仍要改成普通停机"
          onCancel={() => setConfirmStop(false)}
          onConfirm={() => {
            setConfirmStop(false)
            void submit()
          }}
        />
      )}
    </Modal>
  )
}
