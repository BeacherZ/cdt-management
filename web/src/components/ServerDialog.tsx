import { useState } from 'react'
import { Globe2, KeyRound, MapPin, Server, TriangleAlert } from 'lucide-react'
import { post } from '../api'
import type { Server as ServerType, SiteType } from '../types'
import { Modal, Segmented } from './ui'

/**
 * 地域候选。group 只用来在 <select> 里分组显示。
 *
 * sites 决定这个地域在哪个站点下可选：中国大陆地域只有中国站账号能开，
 * 国际站（alibabacloud.com 注册的账号）选不到。这是两个站点最实际的差别之一。
 */
const REGIONS: { value: string; label: string; group: string; sites: SiteType[] }[] = [
  { value: 'cn-hangzhou', label: '华东 1（杭州）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-shanghai', label: '华东 2（上海）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-beijing', label: '华北 2（北京）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-zhangjiakou', label: '华北 3（张家口）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-qingdao', label: '华北 1（青岛）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-huhehaote', label: '华北 5（呼和浩特）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-shenzhen', label: '华南 1（深圳）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-guangzhou', label: '华南 3（广州）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-chengdu', label: '西南 1（成都）', group: '中国大陆', sites: ['china'] },
  { value: 'cn-hongkong', label: '中国香港', group: '港澳与海外', sites: ['china', 'international'] },
  { value: 'ap-southeast-1', label: '新加坡', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-2', label: '澳大利亚（悉尼）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-3', label: '马来西亚（吉隆坡）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-5', label: '印度尼西亚（雅加达）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-6', label: '菲律宾（马尼拉）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-southeast-7', label: '泰国（曼谷）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-northeast-1', label: '日本（东京）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-northeast-2', label: '韩国（首尔）', group: '亚太', sites: ['china', 'international'] },
  { value: 'ap-south-1', label: '印度（孟买）', group: '亚太', sites: ['china', 'international'] },
  { value: 'us-west-1', label: '美国（硅谷）', group: '北美', sites: ['china', 'international'] },
  { value: 'us-east-1', label: '美国（弗吉尼亚）', group: '北美', sites: ['china', 'international'] },
  { value: 'eu-west-1', label: '英国（伦敦）', group: '欧洲', sites: ['china', 'international'] },
  { value: 'eu-central-1', label: '德国（法兰克福）', group: '欧洲', sites: ['china', 'international'] },
  { value: 'me-east-1', label: '阿联酋（迪拜）', group: '中东', sites: ['china', 'international'] },
]

const SITE_OPTIONS: { value: SiteType; label: string }[] = [
  { value: 'china', label: '中国站' },
  { value: 'international', label: '国际站' },
]

const GROUP_ORDER = ['中国大陆', '港澳与海外', '亚太', '北美', '欧洲', '中东']

const DEFAULT_REGION = 'cn-hongkong'

function regionsFor(site: SiteType) {
  return REGIONS.filter((region) => region.sites.includes(site))
}

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
  defaultThresholdPercent = 94,
  onClose,
  onSaved,
}: Props) {
  const editing = Boolean(server && server.id)
  const [form, setForm] = useState(() => ({
    id: server?.id ?? 0,
    label: server?.label ?? '',
    access_key_id: '',
    access_key_secret: '',
    site: (server?.site ?? 'china') as SiteType,
    region_id: server?.region_id ?? DEFAULT_REGION,
    instance_id: server?.instance_id ?? '',
    quota_gb: server?.quota_gb ?? 200,
    threshold_gb: server?.threshold_gb ?? Math.round((200 * defaultThresholdPercent) / 100),
    remark: server?.remark ?? '',
  }))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((current) => ({ ...current, [key]: value }))
  }

  function changeSite(site: SiteType) {
    setForm((current) => {
      const stillValid = regionsFor(site).some((region) => region.value === current.region_id)
      return { ...current, site, region_id: stillValid ? current.region_id : DEFAULT_REGION }
    })
  }

  // 存量数据里地域可能与站点不匹配（比如早期都是中国站），这里补一个选项免得下拉框显示空白
  const regionKnown = regionsFor(form.site).some((region) => region.value === form.region_id)

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
          <Globe2 size={12} /> 站点类型
        </label>
        <Segmented value={form.site} options={SITE_OPTIONS} onChange={changeSite} />
        <div className="hint">
          中国站对应 aliyun.com 账号，国际站对应 alibabacloud.com 账号。ECS 与 CDT
          接口两边相同，仅费用类接口地址不同；国际站不提供中国大陆地域。
        </div>
      </div>

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
          <label htmlFor="region">
            <MapPin size={12} /> 地域
          </label>
          <select
            id="region"
            className="select"
            value={form.region_id}
            onChange={(event) => update('region_id', event.target.value)}
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
          <KeyRound size={12} /> AccessKey ID {!editing && <span className="req">*</span>}
        </label>
        <input
          id="ak"
          className="input mono"
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
          className="input mono"
          type="password"
          value={form.access_key_secret}
          onChange={(event) => update('access_key_secret', event.target.value)}
          placeholder={editing ? '留空不修改' : '填写 AccessKey Secret'}
          autoComplete="new-password"
        />
        <div className="hint">建议用 RAM 子账号，只授予 ECS 启停/查询与 CDT 只读权限。</div>
      </div>

      <div className="field">
        <label htmlFor="instance">实例 ID</label>
        <input
          id="instance"
          className="input mono"
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
