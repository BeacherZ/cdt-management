import { useState } from 'react'
import {
  BellRing,
  Cloud,
  Download,
  Globe2,
  LoaderCircle,
  Plus,
  Save,
  Send,
  SlidersHorizontal,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-react'
import { fetchLatestRelease, post } from '../api'
import type { Settings } from '../types'
import { Segmented, Switch } from './ui'

type Props = {
  settings: Settings
  onClose: () => void
  onSaved: (message: string) => void
}

type Tab = 'general' | 'ddns' | 'notify' | 'about'

const TIMEZONES = [
  'Asia/Shanghai',
  'Asia/Hong_Kong',
  'Asia/Taipei',
  'Asia/Singapore',
  'Asia/Tokyo',
  'UTC',
]

export default function SettingsDrawer({ settings, onClose, onSaved }: Props) {
  const [tab, setTab] = useState<Tab>('general')
  const [form, setForm] = useState(() => {
    const draft = structuredClone(settings)
    // 老版本的响应里只有单条 record，补成数组，后面全按数组处理
    if (!Array.isArray(draft.ddns.records)) draft.ddns.records = []
    if (draft.ddns.records.length === 0 && draft.ddns.record) {
      draft.ddns.records = [draft.ddns.record]
    }
    return draft
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState('')
  const [testResult, setTestResult] = useState('')
  const [newPassword, setNewPassword] = useState('')

  function patch(updater: (draft: Settings) => void) {
    setForm((current) => {
      const draft = structuredClone(current)
      updater(draft)
      return draft
    })
  }

  async function save() {
    setError('')
    setBusy(true)
    try {
      await post('/api/v1/settings', {
        timezone: form.timezone,
        traffic_threshold_pct: Number(form.traffic_threshold_pct),
        reconcile_interval: Number(form.reconcile_interval),
        billing_enabled: form.billing_enabled,
        daily_report_enabled: form.daily_report_enabled,
        daily_report_time: form.daily_report_time,
        notify_on_shift: form.notify_on_shift,
        notify_on_failover: form.notify_on_failover,
        notify_on_recovery: form.notify_on_recovery,
        notify_on_breaker: form.notify_on_breaker,
        notify_on_limit_stop: form.notify_on_limit_stop,
        notify_on_error: form.notify_on_error,
        notify_on_keepalive: form.notify_on_keepalive,
        ddns: {
          enabled: form.ddns.enabled,
          provider: form.ddns.provider,
          cf_auth_mode: form.ddns.cf_auth_mode,
          cf_token: (form.ddns as unknown as { cf_token?: string }).cf_token || '',
          cf_email: form.ddns.cf_email,
          cf_key: (form.ddns as unknown as { cf_key?: string }).cf_key || '',
          zone_id: form.ddns.zone_id,
          records: form.ddns.records.map((name) => name.trim()).filter(Boolean),
          ttl: form.ddns.ttl,
          proxied: form.ddns.proxied,
        },
        telegram: {
          enabled: form.telegram.enabled,
          token: (form.telegram as unknown as { token?: string }).token || '',
          chat_id: form.telegram.chat_id,
          proxy: form.telegram.proxy,
        },
        email: {
          enabled: form.email.enabled,
          host: form.email.host,
          port: form.email.port,
          security: form.email.security,
          username: form.email.username,
          password: (form.email as unknown as { password?: string }).password || '',
          to: form.email.to,
        },
        webhook: {
          enabled: form.webhook.enabled,
          url: form.webhook.url,
          method: form.webhook.method,
          headers: (form.webhook as unknown as { headers?: string }).headers || '',
          body: form.webhook.body,
        },
        new_password: newPassword || undefined,
      })
      onSaved('设置已保存')
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  async function test(channel: string) {
    setTesting(channel)
    setTestResult('')
    try {
      const result = await post<{ ok: boolean; detail: string; status?: string }>(
        `/api/v1/settings/test/${channel}`,
      )
      setTestResult(`${result.ok ? '✅' : '❌'} ${result.status || result.detail}`)
    } catch (err) {
      setTestResult(`❌ ${err instanceof Error ? err.message : '测试失败'}`)
    } finally {
      setTesting('')
    }
  }

  const tgToken = (form.telegram as unknown as { token?: string }).token || ''
  const cfToken = (form.ddns as unknown as { cf_token?: string }).cf_token || ''
  const cfKey = (form.ddns as unknown as { cf_key?: string }).cf_key || ''
  const mailPassword = (form.email as unknown as { password?: string }).password || ''

  return (
    <>
      <div className="overlay" style={{ zIndex: 90 }} onMouseDown={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">
        <div className="modal-head">
          <SlidersHorizontal size={16} style={{ color: 'var(--green)' }} />
          <h3>设置</h3>
          <button className="btn ghost icon sm close" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </div>

        <div className="tabs">
          <button className={tab === 'general' ? 'on' : ''} onClick={() => setTab('general')}>
            常规
          </button>
          <button className={tab === 'ddns' ? 'on' : ''} onClick={() => setTab('ddns')}>
            DDNS
          </button>
          <button className={tab === 'notify' ? 'on' : ''} onClick={() => setTab('notify')}>
            通知
          </button>
          <button className={tab === 'about' ? 'on' : ''} onClick={() => setTab('about')}>
            关于
          </button>
        </div>

        <div className="modal-body" style={{ flex: 1 }}>
          {tab === 'general' && (
            <>
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="tz">
                    <Globe2 size={12} /> 时区
                  </label>
                  <select
                    id="tz"
                    className="select"
                    value={form.timezone}
                    onChange={(event) => patch((draft) => (draft.timezone = event.target.value))}
                  >
                    {TIMEZONES.map((zone) => (
                      <option key={zone} value={zone}>
                        {zone}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="interval">对账间隔（秒）</label>
                  <input
                    id="interval"
                    className="input"
                    type="number"
                    min={30}
                    max={3600}
                    value={form.reconcile_interval}
                    onChange={(event) =>
                      patch((draft) => (draft.reconcile_interval = event.target.value))
                    }
                  />
                  <div className="hint">建议 60；越短越灵敏，但更耗阿里云 API 配额。</div>
                </div>
              </div>

              <div className="field">
                <label htmlFor="pct">新建服务器默认阈值（% 配额）</label>
                <input
                  id="pct"
                  className="input"
                  type="number"
                  min={50}
                  max={100}
                  value={form.traffic_threshold_pct}
                  onChange={(event) =>
                    patch((draft) => (draft.traffic_threshold_pct = event.target.value))
                  }
                />
                <div className="hint">
                  每台服务器的绝对阈值在「服务器」里单独配置；这里只是新增时的预填值。
                </div>
              </div>

              <div>
                <Switch
                  label="费用与余额"
                  hint="每 30 分钟查一次本月账单和账户余额，显示在服务器卡片上；需要 RAM 具备 BSS 只读权限"
                  checked={form.billing_enabled}
                  onChange={(value) => patch((draft) => (draft.billing_enabled = value))}
                />
              </div>

              <div>
                <Switch
                  label="每日流量报告"
                  hint="到点把所有服务器的流量与状态推送一次"
                  checked={form.daily_report_enabled}
                  onChange={(value) => patch((draft) => (draft.daily_report_enabled = value))}
                />
                {form.daily_report_enabled && (
                  <div className="field" style={{ marginTop: 10 }}>
                    <label htmlFor="reportTime">推送时间</label>
                    <input
                      id="reportTime"
                      className="input"
                      type="time"
                      value={form.daily_report_time}
                      onChange={(event) =>
                        patch((draft) => (draft.daily_report_time = event.target.value))
                      }
                    />
                  </div>
                )}
              </div>

              <div>
                <div className="section-head" style={{ marginBottom: 4 }}>
                  <h2 style={{ fontSize: 13 }}>推送哪些事件</h2>
                </div>
                <Switch
                  label="换班完成"
                  checked={form.notify_on_shift}
                  onChange={(value) => patch((draft) => (draft.notify_on_shift = value))}
                />
                <Switch
                  label="临时换班（故障转移）"
                  checked={form.notify_on_failover}
                  onChange={(value) => patch((draft) => (draft.notify_on_failover = value))}
                />
                <Switch
                  label="抢占恢复"
                  checked={form.notify_on_recovery}
                  onChange={(value) => patch((draft) => (draft.notify_on_recovery = value))}
                />
                <Switch
                  label="超限停机"
                  hint="当班流量达到阈值且无处可换时，强制节省停机"
                  checked={form.notify_on_limit_stop}
                  onChange={(value) => patch((draft) => (draft.notify_on_limit_stop = value))}
                />
                <Switch
                  label="全局熔断"
                  hint="所有成员都超限时，全部节省停机"
                  checked={form.notify_on_breaker}
                  onChange={(value) => patch((draft) => (draft.notify_on_breaker = value))}
                />
                <Switch
                  label="换班异常"
                  hint="准点换班超过 10 分钟仍未就绪时提醒"
                  checked={form.notify_on_error}
                  onChange={(value) => patch((draft) => (draft.notify_on_error = value))}
                />
                <Switch
                  label="单机保活恢复"
                  hint="单机保活的实例被拉起成功后提醒"
                  checked={form.notify_on_keepalive}
                  onChange={(value) => patch((draft) => (draft.notify_on_keepalive = value))}
                />
              </div>

              <div className="field">
                <label htmlFor="newpw">修改面板密码</label>
                <input
                  id="newpw"
                  className="input"
                  type="password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  placeholder="留空表示不修改，至少 10 个字符"
                  autoComplete="new-password"
                />
              </div>
            </>
          )}

          {tab === 'ddns' && (
            <>
              <Switch
                label="启用 Cloudflare DDNS"
                hint="当班实例 IP 变化时自动更新 A 记录，先同步再发通知"
                checked={form.ddns.enabled}
                onChange={(value) => patch((draft) => (draft.ddns.enabled = value))}
              />

              <div className="field">
                <label>
                  <Cloud size={12} /> 认证方式
                </label>
                <Segmented
                  value={form.ddns.cf_auth_mode}
                  options={[
                    { value: 'token', label: 'API Token' },
                    { value: 'global_key', label: 'Global Key' },
                  ]}
                  onChange={(value) => patch((draft) => (draft.ddns.cf_auth_mode = value))}
                />
                <div className="hint">
                  {form.ddns.cf_auth_mode === 'token'
                    ? '推荐：Cloudflare 后台创建 Token，权限选 Zone.DNS:Edit，范围限定单个域名。'
                    : 'Global API Key 权限过大，泄露等于整个 Cloudflare 账号失守，能用 Token 就别用这个。'}
                </div>
              </div>

              {form.ddns.cf_auth_mode === 'token' ? (
                <div className="field">
                  <label htmlFor="cftoken">API Token</label>
                  <input
                    id="cftoken"
                    className="input"
                    type="password"
                    value={cfToken}
                    onChange={(event) =>
                      patch(
                        (draft) =>
                          (draft.ddns as unknown as { cf_token: string }).cf_token =
                            event.target.value,
                      )
                    }
                    placeholder={form.ddns.cf_token_configured ? '已配置，留空不修改' : '粘贴 Token'}
                    autoComplete="new-password"
                  />
                </div>
              ) : (
                <div className="grid-2">
                  <div className="field">
                    <label htmlFor="cfemail">注册邮箱</label>
                    <input
                      id="cfemail"
                      className="input"
                      value={form.ddns.cf_email}
                      onChange={(event) =>
                        patch((draft) => (draft.ddns.cf_email = event.target.value))
                      }
                      placeholder="noreply@example.com"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="cfkey">Global API Key</label>
                    <input
                      id="cfkey"
                      className="input"
                      type="password"
                      value={cfKey}
                      onChange={(event) =>
                        patch(
                          (draft) =>
                            (draft.ddns as unknown as { cf_key: string }).cf_key =
                              event.target.value,
                        )
                      }
                      placeholder={form.ddns.cf_key_configured ? '已配置，留空不修改' : ''}
                      autoComplete="new-password"
                    />
                  </div>
                </div>
              )}

              <div className="field">
                <label htmlFor="record">记录名</label>
                <div className="record-list">
                  {(form.ddns.records.length ? form.ddns.records : ['']).map((name, index) => (
                    <div className="record-row" key={index}>
                      <input
                        id={index === 0 ? 'record' : undefined}
                        className="input"
                        value={name}
                        onChange={(event) =>
                          patch((draft) => {
                            const list = draft.ddns.records.length
                              ? draft.ddns.records
                              : ['']
                            list[index] = event.target.value
                            draft.ddns.records = list
                          })
                        }
                        placeholder="cdt.example.com"
                      />
                      {index === 0 ? (
                        <span className="record-tag">默认</span>
                      ) : (
                        <button
                          type="button"
                          className="btn ghost icon sm"
                          title="删除这条记录"
                          onClick={() =>
                            patch((draft) => {
                              draft.ddns.records = draft.ddns.records.filter(
                                (_, i) => i !== index,
                              )
                            })
                          }
                        >
                          <Trash2 size={13} style={{ color: 'var(--red)' }} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  className="btn sm"
                  style={{ alignSelf: 'flex-start' }}
                  onClick={() =>
                    patch((draft) => {
                      if (!draft.ddns.records.length) draft.ddns.records = ['']
                      draft.ddns.records.push('')
                    })
                  }
                >
                  <Plus size={13} /> 添加记录名
                </button>
                <div className="hint">
                  第一条是默认记录：单机保活、以及没单独指定域名的换班组都用它。换班组可以在组里另选一条。
                </div>
              </div>

              <div className="field">
                <label htmlFor="ttl">TTL（秒）</label>
                <input
                  id="ttl"
                  className="input"
                  type="number"
                  min={1}
                  value={form.ddns.ttl}
                  onChange={(event) => patch((draft) => (draft.ddns.ttl = event.target.value))}
                />
              </div>

              <div className="field">
                <label htmlFor="zone">Zone ID（可留空自动识别）</label>
                <input
                  id="zone"
                  className="input"
                  value={form.ddns.zone_id}
                  onChange={(event) => patch((draft) => (draft.ddns.zone_id = event.target.value))}
                  placeholder="留空则按记录名反查"
                />
              </div>

              <Switch
                label="走 Cloudflare 代理（小云朵）"
                hint="开启后源站 IP 不会暴露，但非标端口与部分协议不可用；换班场景通常关闭"
                checked={form.ddns.proxied}
                onChange={(value) => patch((draft) => (draft.ddns.proxied = value))}
              />

              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <button className="btn" onClick={() => test('ddns')} disabled={testing !== ''}>
                  {testing === 'ddns' ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
                  测试连接
                </button>
                {form.ddns.last_status && (
                  <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
                    上次：{form.ddns.last_status} · {form.ddns.last_ip || '—'}
                  </span>
                )}
              </div>
            </>
          )}

          {tab === 'notify' && (
            <>
              <div className="section-head" style={{ marginBottom: 2 }}>
                <h2 style={{ fontSize: 13 }}>
                  <BellRing size={13} style={{ verticalAlign: -2 }} /> Telegram
                </h2>
              </div>
              <Switch
                label="启用 Telegram"
                checked={form.telegram.enabled}
                onChange={(value) => patch((draft) => (draft.telegram.enabled = value))}
              />
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="tgtoken">Bot Token</label>
                  <input
                    id="tgtoken"
                    className="input"
                    type="password"
                    value={tgToken}
                    onChange={(event) =>
                      patch(
                        (draft) =>
                          (draft.telegram as unknown as { token: string }).token =
                            event.target.value,
                      )
                    }
                    placeholder={form.telegram.token_configured ? '已配置，留空不修改' : '123456:AA…'}
                    autoComplete="new-password"
                  />
                </div>
                <div className="field">
                  <label htmlFor="tgchat">Chat ID</label>
                  <input
                    id="tgchat"
                    className="input"
                    value={form.telegram.chat_id}
                    onChange={(event) =>
                      patch((draft) => (draft.telegram.chat_id = event.target.value))
                    }
                    placeholder="123456789"
                  />
                </div>
              </div>
              <div className="field">
                <label htmlFor="tgproxy">代理（可选）</label>
                <input
                  id="tgproxy"
                  className="input"
                  value={form.telegram.proxy}
                  onChange={(event) => patch((draft) => (draft.telegram.proxy = event.target.value))}
                  placeholder="http://127.0.0.1:7890 或 socks5h://127.0.0.1:1080"
                />
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <button
                  className="btn"
                  onClick={() => test('telegram')}
                  disabled={testing !== ''}
                >
                  {testing === 'telegram' ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <Send size={14} />
                  )}
                  发送测试
                </button>
              </div>

              <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '6px 0' }} />

              <div className="section-head" style={{ marginBottom: 2 }}>
                <h2 style={{ fontSize: 13 }}>邮件（SMTP）</h2>
              </div>
              <Switch
                label="启用邮件通知"
                checked={form.email.enabled}
                onChange={(value) => patch((draft) => (draft.email.enabled = value))}
              />
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="mailhost">SMTP 主机</label>
                  <input
                    id="mailhost"
                    className="input"
                    value={form.email.host}
                    onChange={(event) => patch((draft) => (draft.email.host = event.target.value))}
                    placeholder="smtp.example.com"
                  />
                </div>
                <div className="field">
                  <label htmlFor="mailport">端口</label>
                  <input
                    id="mailport"
                    className="input"
                    type="number"
                    value={form.email.port}
                    onChange={(event) => patch((draft) => (draft.email.port = event.target.value))}
                  />
                </div>
              </div>
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="mailsec">加密方式</label>
                  <select
                    id="mailsec"
                    className="select"
                    value={form.email.security}
                    onChange={(event) =>
                      patch((draft) => (draft.email.security = event.target.value))
                    }
                  >
                    <option value="ssl">SSL（465）</option>
                    <option value="starttls">STARTTLS（587）</option>
                    <option value="none">不加密</option>
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="mailto">收件人</label>
                  <input
                    id="mailto"
                    className="input"
                    value={form.email.to}
                    onChange={(event) => patch((draft) => (draft.email.to = event.target.value))}
                    placeholder="noreply@example.com"
                  />
                </div>
              </div>
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="mailuser">用户名</label>
                  <input
                    id="mailuser"
                    className="input"
                    value={form.email.username}
                    onChange={(event) =>
                      patch((draft) => (draft.email.username = event.target.value))
                    }
                    autoComplete="off"
                  />
                </div>
                <div className="field">
                  <label htmlFor="mailpass">密码 / 授权码</label>
                  <input
                    id="mailpass"
                    className="input"
                    type="password"
                    value={mailPassword}
                    onChange={(event) =>
                      patch(
                        (draft) =>
                          (draft.email as unknown as { password: string }).password =
                            event.target.value,
                      )
                    }
                    placeholder={form.email.password_configured ? '已配置，留空不修改' : ''}
                    autoComplete="new-password"
                  />
                </div>
              </div>
              <div style={{ display: 'flex', gap: 10 }}>
                <button className="btn" onClick={() => test('email')} disabled={testing !== ''}>
                  {testing === 'email' ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
                  发送测试
                </button>
              </div>

              <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '6px 0' }} />

              <div className="section-head" style={{ marginBottom: 2 }}>
                <h2 style={{ fontSize: 13 }}>Webhook</h2>
              </div>
              <Switch
                label="启用 Webhook"
                checked={form.webhook.enabled}
                onChange={(value) => patch((draft) => (draft.webhook.enabled = value))}
              />
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="whurl">地址</label>
                  <input
                    id="whurl"
                    className="input"
                    value={form.webhook.url}
                    onChange={(event) => patch((draft) => (draft.webhook.url = event.target.value))}
                    placeholder="https://example.com/hook"
                  />
                </div>
                <div className="field">
                  <label htmlFor="whmethod">方法</label>
                  <select
                    id="whmethod"
                    className="select"
                    value={form.webhook.method}
                    onChange={(event) =>
                      patch((draft) => (draft.webhook.method = event.target.value))
                    }
                  >
                    <option value="POST">POST</option>
                    <option value="GET">GET</option>
                  </select>
                </div>
              </div>
              <div className="field">
                <label htmlFor="whbody">请求体模板（留空用内置 JSON）</label>
                <textarea
                  id="whbody"
                  className="textarea"
                  value={form.webhook.body}
                  onChange={(event) => patch((draft) => (draft.webhook.body = event.target.value))}
                  placeholder={'{"text":"{title}\\n{text}"}'}
                />
                <div className="hint">可用占位符：{'{title}'} {'{text}'} {'{kind}'}</div>
              </div>
              <button className="btn" onClick={() => test('webhook')} disabled={testing !== ''}>
                {testing === 'webhook' ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
                发送测试
              </button>
            </>
          )}

          {tab === 'about' && <AboutPanel repo={form.repo ?? ''} version={form.version ?? ''} />}

          {testResult && (
            <div
              className="auth-error"
              style={{
                background: testResult.startsWith('✅') ? 'var(--green-soft)' : 'var(--red-soft)',
                color: testResult.startsWith('✅') ? 'var(--green)' : 'var(--red)',
              }}
            >
              {testResult}
            </div>
          )}

          {error && (
            <div className="auth-error">
              <TriangleAlert size={14} />
              {error}
            </div>
          )}
        </div>

        {tab !== 'about' && (
          <div className="modal-foot">
            <span className="spacer" />
            <button className="btn" onClick={onClose}>
              取消
            </button>
            <button className="btn primary" onClick={save} disabled={busy}>
              {busy ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
              保存
            </button>
          </div>
        )}
      </aside>
    </>
  )
}

function AboutPanel({ repo, version }: { repo: string; version: string }) {
  const [latest, setLatest] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function check() {
    if (!repo) {
      setError('未配置仓库地址')
      return
    }
    setBusy(true)
    setError('')
    try {
      setLatest(await fetchLatestRelease(repo))
    } catch (err) {
      setError(err instanceof Error ? err.message : '检查失败')
    } finally {
      setBusy(false)
    }
  }

  const newer = latest && latest.replace(/^v/, '') !== version

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="field">
        <label>当前版本</label>
        <div style={{ fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 13 }}>
          v{version || '未知'}
        </div>
      </div>
      <div className="field">
        <label>上游与致谢</label>
        <div className="hint" style={{ lineHeight: 1.75 }}>
          面板与交互参考{' '}
          <a href="https://github.com/wang4386/CDT-Monitor" target="_blank" rel="noreferrer">
            wang4386/CDT-Monitor
          </a>
          （MIT，作者 青柠）；安装脚本形态参考{' '}
          <a href="https://github.com/BeacherZ/dailycheckin-plus" target="_blank" rel="noreferrer">
            BeacherZ/dailycheckin-plus
          </a>
          。本项目的换班、DDNS 与通知逻辑为独立实现。
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <button className="btn" onClick={check} disabled={busy}>
          {busy ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}
          检查更新
        </button>
        {latest && (
          <span style={{ fontSize: 12, color: newer ? 'var(--amber)' : 'var(--muted)' }}>
            {newer ? `发现新版本 ${latest}` : `已是最新（${latest}）`}
          </span>
        )}
        {error && <span style={{ fontSize: 12, color: 'var(--red)' }}>{error}</span>}
      </div>
    </div>
  )
}
