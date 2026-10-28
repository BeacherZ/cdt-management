import { useEffect, useState } from 'react'
import {
  BellRing,
  Cloud,
  Download,
  Github,
  Globe2,
  KeyRound,
  LoaderCircle,
  Save,
  Send,
  SlidersHorizontal,
  TriangleAlert,
  UserRound,
  X,
} from 'lucide-react'
import { SLOW_TIMEOUT_MS, fetchLatestRelease, post } from '../api'
import type { Settings } from '../types'
import { Segmented, Switch } from './ui'

type Props = {
  settings: Settings
  onClose: () => void
  onSaved: (message: string) => void
}

type Tab = 'general' | 'ddns' | 'notify' | 'about'
type NotifyTab = 'channel' | 'category'

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
  const [notifyTab, setNotifyTab] = useState<NotifyTab>('channel')
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
  const [newUsername, setNewUsername] = useState(settings.admin_username ?? '')
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
        billing_interval: Number(form.billing_interval),
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
        new_username: newUsername || undefined,
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
      // 把当前表单草稿一起发过去，测的是「此刻填的值」而不是已保存的旧配置；
      // 留空的敏感字段（token / 密码）后端会回退到已保存值。
      const result = await post<{ ok: boolean; detail: string; status?: string }>(
        `/api/v1/settings/test/${channel}`,
        form,
        // 测试连接要真连 Cloudflare / Telegram / SMTP，DNS 那边还会逐条查记录
        SLOW_TIMEOUT_MS,
      )
      // DDNS 后端返回的 status 已带 emoji（如「✅ 连接正常」「❌ DDNS 异常」），
      // 真正的原因在 detail 里，得一起露出来；其它渠道只有不带 emoji 的 detail。
      const status = result.status || ''
      const detail = result.detail && result.detail !== status ? `：${result.detail}` : ''
      const text = status
        ? `${status}${detail}`
        : `${result.ok ? '✅' : '❌'} ${result.detail || '测试完成'}`
      setTestResult(text)
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
              <div className="section-head">
                <h2>
                  <KeyRound size={14} /> 账号
                </h2>
                <span className="section-sub">登录面板用的账号与密码</span>
              </div>
              <div className="account-card">
                <div className="grid-2">
                  <div className="field">
                    <label htmlFor="newuser">
                      <UserRound size={12} /> 用户名
                    </label>
                    <input
                      id="newuser"
                      className="input"
                      value={newUsername}
                      onChange={(event) => setNewUsername(event.target.value)}
                      placeholder="至少 3 个字符"
                      autoComplete="username"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="newpw">登录密码</label>
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
                </div>
                <div className="hint">
                  改用户名或密码后，其它已登录的浏览器会立即退出，需要重新登录。
                </div>
              </div>

              <div className="section-head">
                <h2>
                  <SlidersHorizontal size={14} /> 运行参数
                </h2>
              </div>
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
                  <label htmlFor="interval">巡检间隔（秒）</label>
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
                  <div className="hint">流量 / 状态 / 保活的查询间隔，默认 60（1 分钟）。</div>
                </div>
                <div className="field">
                  <label htmlFor="billingInterval">费用查询间隔（秒）</label>
                  <input
                    id="billingInterval"
                    className="input"
                    type="number"
                    min={60}
                    max={86400}
                    value={form.billing_interval}
                    onChange={(event) =>
                      patch((draft) => (draft.billing_interval = event.target.value))
                    }
                  />
                  <div className="hint">账单 / 余额是慢数据，默认 600（10 分钟）。</div>
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
                  hint="按上方间隔查本月账单和账户余额，显示在服务器卡片上；需要 RAM 具备 BSS 只读权限"
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
                <label>域名记录</label>
                <div className="hint" style={{ lineHeight: 1.7 }}>
                  记录名和绑定都挪到了左侧「DNS」页：那里能加删域名，并把每条域名指派给
                  单机保活实例、闲散实例或某个换班组。这里只负责 Cloudflare 的认证与 TTL。
                  {form.ddns.records.length > 0 && (
                    <div style={{ marginTop: 6, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {form.ddns.records.map((name) => (
                        <code key={name}>{name}</code>
                      ))}
                    </div>
                  )}
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
              <div className="sub-tabs">
                <button
                  className={notifyTab === 'channel' ? 'on' : ''}
                  onClick={() => setNotifyTab('channel')}
                >
                  通知渠道
                </button>
                <button
                  className={notifyTab === 'category' ? 'on' : ''}
                  onClick={() => setNotifyTab('category')}
                >
                  通知类别
                </button>
              </div>

              {notifyTab === 'channel' && (
                <>
                  <div className="section-head" style={{ marginBottom: 2 }}>
                    <h2>
                      <BellRing size={13} /> Telegram
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
                      onChange={(event) =>
                        patch((draft) => (draft.telegram.proxy = event.target.value))
                      }
                      placeholder="http://127.0.0.1:7890 或 socks5h://127.0.0.1:1080"
                    />
                  </div>
                  <div className="test-row">
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
                    <h2>邮件（SMTP）</h2>
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
                        onChange={(event) =>
                          patch((draft) => (draft.email.host = event.target.value))
                        }
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
                        onChange={(event) =>
                          patch((draft) => (draft.email.port = event.target.value))
                        }
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
                        onChange={(event) =>
                          patch((draft) => (draft.email.to = event.target.value))
                        }
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
                  <div className="test-row">
                    <button className="btn" onClick={() => test('email')} disabled={testing !== ''}>
                      {testing === 'email' ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        <Send size={14} />
                      )}
                      发送测试
                    </button>
                  </div>

                  <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '6px 0' }} />

                  <div className="section-head" style={{ marginBottom: 2 }}>
                    <h2>Webhook</h2>
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
                        onChange={(event) =>
                          patch((draft) => (draft.webhook.url = event.target.value))
                        }
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
                      onChange={(event) =>
                        patch((draft) => (draft.webhook.body = event.target.value))
                      }
                      placeholder={'{"text":"{title}\\n{text}"}'}
                    />
                    <div className="hint">可用占位符：{'{title}'} {'{text}'} {'{kind}'}</div>
                  </div>
                  <button
                    className="btn"
                    onClick={() => test('webhook')}
                    disabled={testing !== ''}
                  >
                    {testing === 'webhook' ? (
                      <LoaderCircle size={14} className="spin" />
                    ) : (
                      <Send size={14} />
                    )}
                    发送测试
                  </button>
                </>
              )}

              {notifyTab === 'category' && (
                <>
                  <div className="section-head" style={{ marginBottom: 6 }}>
                    <h2>
                      <BellRing size={13} /> 推送哪些事件
                    </h2>
                    <span className="section-sub">勾选后，对应事件会通过已启用的渠道推送</span>
                  </div>
                  <Switch
                    label="换班完成"
                    checked={form.notify_on_shift}
                    onChange={(value) => patch((draft) => (draft.notify_on_shift = value))}
                  />
                  <Switch
                    label="故障转移"
                    hint="当班实例流量超限，启用备用实例接替时提醒"
                    checked={form.notify_on_failover}
                    onChange={(value) => patch((draft) => (draft.notify_on_failover = value))}
                  />
                  <Switch
                    label="实例恢复运行"
                    hint="抢占恢复与单机保活恢复共用：实例被停掉后自动拉起成功时提醒"
                    checked={form.notify_on_recovery || form.notify_on_keepalive}
                    onChange={(value) =>
                      patch((draft) => {
                        draft.notify_on_recovery = value
                        draft.notify_on_keepalive = value
                      })
                    }
                  />
                  <Switch
                    label="全局熔断"
                    hint="流量达到阈值被迫停机：当班无处可换只停一台，全部成员超限则全部停机"
                    checked={form.notify_on_limit_stop || form.notify_on_breaker}
                    onChange={(value) =>
                      patch((draft) => {
                        draft.notify_on_limit_stop = value
                        draft.notify_on_breaker = value
                      })
                    }
                  />
                  <Switch
                    label="换班异常"
                    hint="准点换班超过 10 分钟仍未就绪时提醒"
                    checked={form.notify_on_error}
                    onChange={(value) => patch((draft) => (draft.notify_on_error = value))}
                  />
                </>
              )}
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
            <span className="foot-note">
              {newUsername !== (settings.admin_username ?? '') || newPassword
                ? '账号有改动，保存后生效'
                : '修改会即时生效'}
            </span>
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

  // 打开「关于」时自动查一次最新版本，无需手动点
  useEffect(() => {
    if (repo) void check()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo])

  const newer = Boolean(latest) && latest.replace(/^v/, '') !== version
  const repoUrl = repo ? `https://github.com/${repo}` : ''
  const latestText = latest ? latest.replace(/^v/, 'v') : ''

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h3 className="about-title">关于本项目</h3>
      </div>

      {repoUrl && (
        <a
          className="about-repo"
          href={repoUrl}
          target="_blank"
          rel="noopener noreferrer"
          title="打开 GitHub 仓库"
        >
          <Github size={16} />
          <span>{repo}</span>
        </a>
      )}

      <div className="about-versions">
        <div className="about-version-row">
          <span className="about-key">当前版本</span>
          <b>v{version || '未知'}</b>
        </div>
        <div className="about-version-row">
          <span className="about-key">最新版本</span>
          {busy ? (
            <span className="about-latest checking">
              <LoaderCircle size={12} className="spin" /> 检查中…
            </span>
          ) : error ? (
            <span className="about-latest error" title={error}>
              获取失败
            </span>
          ) : latest ? (
            <span className={`about-latest ${newer ? 'newer' : 'latest'}`}>
              <span className="about-dot" />
              {latestText}
              {newer ? '（可更新）' : '（已是最新）'}
            </span>
          ) : (
            <span className="about-latest dim">未检查</span>
          )}
        </div>
      </div>

      <div>
        <button className="btn" onClick={check} disabled={busy || !repo}>
          {busy ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}
          检查更新
        </button>
      </div>
    </div>
  )
}
