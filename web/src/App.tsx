import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Gauge,
  Globe2,
  HeartPulse,
  LogOut,
  Plus,
  RefreshCw,
  Repeat,
  Server as ServerIcon,
  Settings as SettingsIcon,
  Zap,
} from 'lucide-react'
import { APIError, api, del, post } from './api'
import type { InitStatus, Keepalive, Overview, Server, Settings, ShiftGroup } from './types'
import Auth from './components/Auth'
import DashboardView from './components/DashboardView'
import KeepaliveView from './components/KeepaliveView'
import ServersView from './components/ServersView'
import ShiftView from './components/ShiftView'
import ServerDialog from './components/ServerDialog'
import GroupDialog from './components/GroupDialog'
import SettingsDrawer from './components/SettingsDrawer'
import ConfirmDialog from './components/ConfirmDialog'
import DNSView from './components/DNSView'
import { Badge, Empty, Modal, statusLabel, statusTone } from './components/ui'

type Phase = 'loading' | 'setup' | 'login' | 'ready' | 'fatal'
type View = 'dashboard' | 'keepalive' | 'shift' | 'servers' | 'dns'
type Toast = { id: number; tone: 'success' | 'error' | 'info'; message: string }

/** 删除这类不可撤销的操作，统一走应用内二次确认（window.confirm 在桌面壳里可能被吞掉） */
type ConfirmRequest = {
  title: string
  message: string
  hint?: string
  confirmText?: string
  failText?: string
  run: () => Promise<void>
}

const REFRESH_MS = 10_000

type NavLeaf = { key: View; label: string; icon: typeof Gauge }

/**
 * 侧栏三个总菜单：概览 / 保活 / DNS。
 * 带 key 的总菜单本身就是页面（概览、DNS 可点），不带的只作分组标签（保活）。
 * 子项统一缩进挂在总菜单下面。
 */
type NavSection = {
  title: string
  key?: View
  icon?: typeof Gauge
  children: NavLeaf[]
}

const NAV_SECTIONS: NavSection[] = [
  {
    title: '概览',
    key: 'dashboard',
    icon: Gauge,
    children: [{ key: 'servers', label: '服务器', icon: ServerIcon }],
  },
  {
    title: '保活',
    icon: Activity,
    children: [
      { key: 'keepalive', label: '单机保活', icon: HeartPulse },
      { key: 'shift', label: '联合换班', icon: Repeat },
    ],
  },
  {
    title: 'DNS',
    key: 'dns',
    icon: Globe2,
    children: [],
  },
]

function navCount(key: View, overview: Overview): number {
  if (key === 'servers') return overview.servers.length
  if (key === 'keepalive') return overview.keepalives.length
  if (key === 'shift') return overview.groups.length
  return 0
}

/** 总菜单那一行右侧的数字（DNS 用它显示记录条数） */
function sectionCount(section: NavSection, overview: Overview): number {
  if (section.key === 'dns') return overview.dns?.records.length ?? 0
  return 0
}

export default function App() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [view, setView] = useState<View>('dashboard')
  const [meta, setMeta] = useState<InitStatus>({
    initialized: false,
    brand: 'CDT-Management',
    version: '',
    repo: '',
  })
  const [overview, setOverview] = useState<Overview | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [fatal, setFatal] = useState('')
  const [toasts, setToasts] = useState<Toast[]>([])
  const [busyServer, setBusyServer] = useState<number | null>(null)
  const [billingServer, setBillingServer] = useState<number | null>(null)
  const [busyGroup, setBusyGroup] = useState<number | null>(null)
  const [serverDialog, setServerDialog] = useState<{ open: boolean; server: Server | null }>({
    open: false,
    server: null,
  })
  const [groupDialog, setGroupDialog] = useState<{ open: boolean; group: ShiftGroup | null }>({
    open: false,
    group: null,
  })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [keepalivePicker, setKeepalivePicker] = useState(false)
  const [confirmBox, setConfirmBox] = useState<ConfirmRequest | null>(null)
  const [confirmBusy, setConfirmBusy] = useState(false)
  const [busyDns, setBusyDns] = useState<string | null>(null)
  const [dnsSyncingAll, setDnsSyncingAll] = useState(false)
  const polling = useRef<number | null>(null)

  const toast = useCallback((message: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { id, message, tone }])
    window.setTimeout(() => setToasts((current) => current.filter((item) => item.id !== id)), 4200)
  }, [])

  const loadAll = useCallback(async () => {
    const [nextOverview, nextSettings] = await Promise.all([
      api<Overview>('/api/v1/overview'),
      api<Settings>('/api/v1/settings'),
    ])
    setOverview(nextOverview)
    setSettings(nextSettings)
    setPhase('ready')
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        const init = await api<InitStatus>('/api/v1/system/init-status')
        setMeta(init)
        if (!init.initialized) {
          setPhase('setup')
          return
        }
        await loadAll()
      } catch (error) {
        if (error instanceof APIError && error.status === 401) {
          setPhase('login')
          return
        }
        setFatal(error instanceof Error ? error.message : '无法连接后端')
        setPhase('fatal')
      }
    })()
  }, [loadAll])

  useEffect(() => {
    if (phase !== 'ready') {
      if (polling.current) window.clearInterval(polling.current)
      return
    }
    polling.current = window.setInterval(() => {
      void api<Overview>('/api/v1/overview')
        .then(setOverview)
        .catch((error) => {
          if (error instanceof APIError && error.status === 401) setPhase('login')
        })
    }, REFRESH_MS)
    return () => {
      if (polling.current) window.clearInterval(polling.current)
    }
  }, [phase])

  async function reconcile() {
    try {
      await post('/api/v1/reconcile')
      await loadAll()
      toast('已触发一轮同步', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '同步失败', 'error')
    }
  }

  async function serverAction(serverId: number, action: 'start' | 'stop') {
    setBusyServer(serverId)
    try {
      const result = await post<{
        status?: string
        target_status?: string
        stopped_mode?: string
        managed_note?: string
      }>(`/api/v1/servers/${serverId}/actions/${action}`)
      await loadAll()
      // 手动开关机是异步的：指令已下发，实例状态由后台轮询确认到位后打完成标志。
      // 概览「最近事件」里能看到这条事件的转圈过程。
      toast(
        action === 'start' ? '已下发开机指令，正在确认状态…' : '已下发停机指令，正在确认状态…',
        'success',
      )
      if (result?.managed_note) {
        toast(`${result.managed_note}托管中，下一轮同步会按流程纠正`, 'info')
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : '操作失败', 'error')
    } finally {
      setBusyServer(null)
    }
  }

  /** 服务器手动排序：拖拽结束后把完整有序 id 列表持久化 */
  async function reorderServers(orderedIds: number[]) {
    try {
      await post('/api/v1/servers/reorder', { ids: orderedIds })
      await loadAll()
    } catch (error) {
      toast(error instanceof Error ? error.message : '排序保存失败', 'error')
    }
  }

  async function refreshServer(serverId: number) {
    setBusyServer(serverId)
    try {
      await post(`/api/v1/servers/${serverId}/refresh`)
      await loadAll()
      toast('已刷新', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '刷新失败', 'error')
    } finally {
      setBusyServer(null)
    }
  }

  /** 手动重查费用：BSS 接口慢，单独一个 loading，不跟状态刷新抢同一个 busy */
  async function serverBilling(server: Server) {
    setBillingServer(server.id)
    try {
      const result = await post<{ error?: string }>(`/api/v1/servers/${server.id}/billing`)
      await loadAll()
      if (result?.error) {
        toast(`费用已刷新，但${result.error}`, 'info')
      } else {
        toast('费用已更新', 'success')
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : '费用查询失败', 'error')
    } finally {
      setBillingServer(null)
    }
  }

  function deleteServer(server: Server) {
    setConfirmBox({
      title: '删除服务器',
      message: `确定删除「${server.label}」？`,
      hint: '只删除本机保存的账号配置与密钥，阿里云上的实例本身不受影响，也不会被停机。',
      confirmText: '删除服务器',
      failText: '删除失败',
      run: async () => {
        await del(`/api/v1/servers/${server.id}`)
        await loadAll()
        toast('服务器已删除', 'success')
      },
    })
  }

  async function switchGroup(group: ShiftGroup) {
    setBusyGroup(group.id)
    try {
      await post(`/api/v1/groups/${group.id}/switch`)
      await loadAll()
      toast('正在切到下一台，完成后会推送报告', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '换班失败', 'error')
    } finally {
      setBusyGroup(null)
    }
  }

  function deleteGroup(group: ShiftGroup) {
    setConfirmBox({
      title: '解散换班组',
      message: `确定解散「${group.name}」？`,
      hint: `组内 ${group.members.length} 台服务器保持当前状态：跑着的继续跑，停着的继续停着。解散后它们回到闲散手动，不会被单机保活拉起；组绑定的域名会一并解除。`,
      confirmText: '解散换班组',
      failText: '解散失败',
      run: async () => {
        await del(`/api/v1/groups/${group.id}`)
        await loadAll()
        toast('换班组已解散，组员已回到闲散手动', 'success')
      },
    })
  }

  async function addKeepalive(serverId: number) {
    try {
      await post('/api/v1/keepalives', { server_id: serverId })
      setKeepalivePicker(false)
      await loadAll()
      toast('已加入单机保活', 'success')
    } catch (error) {
      toast(error instanceof Error ? error.message : '添加失败', 'error')
    }
  }

  async function toggleKeepalive(item: Keepalive, enabled: boolean) {
    try {
      await post(`/api/v1/keepalives/${item.id}/toggle`, { enabled })
      await loadAll()
    } catch (error) {
      toast(error instanceof Error ? error.message : '操作失败', 'error')
    }
  }

  function removeKeepalive(item: Keepalive) {
    setConfirmBox({
      title: '移出单机保活',
      message: `确定把「${item.label}」移出单机保活？`,
      hint: '移出后这台机器不再自动拉起，服务器记录本身保留，可以随时重新加入。',
      confirmText: '移出保活',
      failText: '操作失败',
      run: async () => {
        await del(`/api/v1/keepalives/${item.id}`)
        await loadAll()
        toast('已移出保活', 'success')
      },
    })
  }

  // -- DNS -----------------------------------------------------------
  /** DNS 记录手动排序：拖拽结束后把完整有序记录名列表持久化 */
  async function reorderDns(ordered: string[]) {
    try {
      await post('/api/v1/dns/reorder', { records: ordered })
      await loadAll()
    } catch (error) {
      toast(error instanceof Error ? error.message : '排序保存失败', 'error')
    }
  }

  async function bindDns(record: string, kind: string, id: number) {
    setBusyDns(record)
    try {
      const result = await post<{ records?: string[] }>(
        `/api/v1/dns/records/${encodeURIComponent(record)}/bind`,
        { kind, id },
      )
      await loadAll()
      if (!kind) {
        toast('已解绑，这条记录不再被动', 'success')
      } else if ((result?.records?.length ?? 0) > 1) {
        // 一个目标可以挂多条域名，链第二条时提醒一下总量，免得以为是重复绑定
        toast(`已绑定，该目标现在有 ${result?.records?.length} 条域名`, 'success')
      } else {
        toast('已绑定', 'success')
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : '绑定失败', 'error')
    } finally {
      setBusyDns(null)
    }
  }

  async function syncDns(record: string) {
    setBusyDns(record)
    try {
      const result = await post<{ status?: string; detail?: string }>(
        `/api/v1/dns/records/${encodeURIComponent(record)}/sync`,
      )
      await loadAll()
      const ok = (result?.status ?? '').startsWith('✅')
      toast(`${record}：${result?.status ?? '已处理'}${result?.detail && !ok ? `（${result.detail}）` : ''}`,
        ok ? 'success' : 'info')
    } catch (error) {
      toast(error instanceof Error ? error.message : '同步失败', 'error')
    } finally {
      setBusyDns(null)
    }
  }

  async function syncAllDns() {
    setDnsSyncingAll(true)
    try {
      const result = await post<{ items?: { record: string; status: string }[] }>('/api/v1/dns/sync')
      await loadAll()
      const items = result?.items ?? []
      if (items.length === 0) {
        toast('没有需要同步的记录（目标都没在跑，或解析已经是最新的）', 'info')
      } else {
        const failed = items.filter((item) => !item.status.startsWith('✅'))
        toast(
          failed.length === 0
            ? `${items.length} 条记录已同步`
            : `${items.length - failed.length} 条成功，${failed.length} 条失败：${failed
                .map((item) => item.record)
                .join('、')}`,
          failed.length === 0 ? 'success' : 'error',
        )
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : '同步失败', 'error')
    } finally {
      setDnsSyncingAll(false)
    }
  }

  /** 返回 true 表示加成功了，DNS 页据此收起输入框 */
  async function addDnsRecord(name: string): Promise<boolean> {
    try {
      await post('/api/v1/dns/records', { record: name })
      await loadAll()
      toast(`已添加 ${name}`, 'success')
      return true
    } catch (error) {
      toast(error instanceof Error ? error.message : '添加失败', 'error')
      return false
    }
  }

  function removeDnsRecord(record: string) {
    setConfirmBox({
      title: '删除记录名',
      message: `确定删除「${record}」？`,
      hint: '只从本机列表里移除，Cloudflare 上的 DNS 记录不会被删；如果它绑了机器，绑定会一并解除。',
      confirmText: '删除记录名',
      failText: '删除失败',
      run: async () => {
        await del(`/api/v1/dns/records/${encodeURIComponent(record)}`)
        await loadAll()
        toast('记录名已删除', 'success')
      },
    })
  }

  async function runConfirm() {
    if (!confirmBox) return
    setConfirmBusy(true)
    try {
      await confirmBox.run()
    } catch (error) {
      toast(error instanceof Error ? error.message : (confirmBox.failText ?? '操作失败'), 'error')
    } finally {
      setConfirmBusy(false)
      setConfirmBox(null)
    }
  }

  async function logout() {
    try {
      await post('/api/v1/auth/logout')
    } finally {
      setPhase('login')
      setOverview(null)
    }
  }

  if (phase === 'loading') {
    return (
      <div className="boot">
        <div className="spinner" />
        正在连接…
      </div>
    )
  }

  if (phase === 'fatal') {
    return (
      <div className="boot">
        <AlertTriangle size={22} style={{ color: 'var(--red)' }} />
        <div>{fatal}</div>
        <button className="btn" onClick={() => window.location.reload()}>
          重试
        </button>
      </div>
    )
  }

  if (phase === 'setup' || phase === 'login') {
    return (
      <Auth
        mode={phase}
        brand={meta.brand || 'CDT-Management'}
        onDone={() => {
          setPhase('loading')
          void loadAll().catch(() => setPhase('login'))
        }}
      />
    )
  }

  if (!overview || !settings) {
    return (
      <div className="boot">
        <div className="spinner" />
      </div>
    )
  }

  const keepaliveIds = new Set(overview.keepalives.map((item) => item.server_id))
  // 已编进换班组的机器按班次自动启停，单机保活会自动让位，界面上不再给"加入"入口
  const groupedIds = new Set(
    overview.groups.flatMap((group) => group.members.map((member) => member.server_id)),
  )
  const available = overview.servers.filter(
    (server) => !keepaliveIds.has(server.id) && !groupedIds.has(server.id),
  )

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button
          type="button"
          className="brand"
          title="回到概览"
          aria-label="回到概览"
          onClick={() => setView('dashboard')}
        >
          <div className="brand-mark">
            <Repeat size={18} />
          </div>
          <div className="brand-text">
            <strong>{overview.brand}</strong>
          </div>
        </button>

        <nav className="nav-list" aria-label="主视图">
          {NAV_SECTIONS.map((section) => {
            const parentKey = section.key
            const ParentIcon = section.icon
            const sectionActive =
              parentKey === view || section.children.some((leaf) => leaf.key === view)
            return (
              <div
                className={`nav-group${sectionActive ? ' is-active' : ''}`}
                key={section.title}
              >
                {parentKey && ParentIcon ? (
                  <button
                    className={`nav-parent${view === parentKey ? ' on' : ''}`}
                    aria-current={view === parentKey ? 'page' : undefined}
                    onClick={() => setView(parentKey)}
                  >
                    <ParentIcon size={15} />
                    <span className="nav-text">{section.title}</span>
                    {sectionCount(section, overview) > 0 && (
                      <span className="nav-count">{sectionCount(section, overview)}</span>
                    )}
                  </button>
                ) : (
                  <div className="nav-parent label">{section.title}</div>
                )}

                {section.children.length > 0 && (
                  <div className="nav-sub">
                    {section.children.map((item) => {
                      const Icon = item.icon
                      const count = navCount(item.key, overview)
                      return (
                        <button
                          key={item.key}
                          className={`nav-item${view === item.key ? ' on' : ''}`}
                          aria-current={view === item.key ? 'page' : undefined}
                          onClick={() => setView(item.key)}
                        >
                          <Icon size={14} />
                          <span className="nav-text">{item.label}</span>
                          {count > 0 && <span className="nav-count">{count}</span>}
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </nav>

        <div className="sidebar-foot">
          <button className="nav-item" onClick={() => setSettingsOpen(true)}>
            <SettingsIcon size={15} />
            <span className="nav-text">设置</span>
          </button>
          <button className="nav-item" onClick={logout}>
            <LogOut size={15} />
            <span className="nav-text">退出登录</span>
          </button>
          <div className="meta" style={{ paddingLeft: 11, marginTop: 4 }}>
            <Zap size={10} /> v{overview.version}
          </div>
        </div>
      </aside>

      <main className="main-area">
        {view === 'dashboard' && (
          <DashboardView
            overview={overview}
            onNavigate={(target) => setView(target)}
          />
        )}

        {view === 'keepalive' && (
          <KeepaliveView
            keepalives={overview.keepalives}
            servers={overview.servers}
            groupedIds={groupedIds}
            busyId={busyServer}
            onAdd={() => setKeepalivePicker(true)}
            onToggle={toggleKeepalive}
            onRemove={removeKeepalive}
            onAction={serverAction}
            onRefresh={refreshServer}
          />
        )}

        {view === 'shift' && (
          <ShiftView
            groups={overview.groups}
            serverCount={overview.servers.length}
            busyId={busyGroup}
            onAdd={() => setGroupDialog({ open: true, group: null })}
            onEdit={(group) => setGroupDialog({ open: true, group })}
            onDelete={deleteGroup}
            onSwitch={switchGroup}
          />
        )}

        {view === 'servers' && (
          <ServersView
            servers={overview.servers}
            busyId={busyServer}
            billingId={billingServer}
            onAdd={() => setServerDialog({ open: true, server: null })}
            onEdit={(server) => setServerDialog({ open: true, server })}
            onDelete={deleteServer}
            onRefresh={(server) => refreshServer(server.id)}
            onAction={(server, action) => serverAction(server.id, action)}
            onBilling={serverBilling}
            onReorder={reorderServers}
            onAddToKeepalive={async (server) => {
              if (keepaliveIds.has(server.id)) {
                toast('这台已经在单机保活里了', 'info')
                return
              }
              await addKeepalive(server.id)
            }}
          />
        )}

        {view === 'dns' && (
          <DNSView
            dns={overview.dns}
            servers={overview.servers}
            keepalives={overview.keepalives}
            groups={overview.groups}
            busy={busyDns}
            syncingAll={dnsSyncingAll}
            onBind={bindDns}
            onSync={syncDns}
            onSyncAll={syncAllDns}
            onAddRecord={addDnsRecord}
            onRemoveRecord={removeDnsRecord}
            onReorder={reorderDns}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        )}

        <div
          style={{
            marginTop: 28,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            fontSize: 11.5,
            color: 'var(--muted-soft)',
          }}
        >
          <button className="btn ghost sm" onClick={reconcile}>
            <RefreshCw size={12} /> 立即同步
          </button>
          <span>
            同步间隔 {settings.reconcile_interval} 秒 · {overview.timezone}
          </span>
          {!settings.sdk_ready && <Badge tone="amber">阿里云 SDK 未就绪</Badge>}
          {settings.dry_run && <Badge tone="amber">DRY RUN</Badge>}
        </div>
      </main>

      {serverDialog.open && (
        <ServerDialog
          server={serverDialog.server}
          defaultThresholdPercent={Number(settings.traffic_threshold_pct) || 90}
          onClose={() => setServerDialog({ open: false, server: null })}
          onSaved={async (message) => {
            setServerDialog({ open: false, server: null })
            await loadAll()
            toast(message, 'success')
          }}
        />
      )}

      {groupDialog.open && (
        <GroupDialog
          group={groupDialog.group}
          servers={overview.servers}
          onClose={() => setGroupDialog({ open: false, group: null })}
          onKeepaliveCleared={() => void loadAll()}
          onSaved={async (message) => {
            setGroupDialog({ open: false, group: null })
            await loadAll()
            toast(message, 'success')
          }}
        />
      )}

      {keepalivePicker && (
        <Modal
          title="加入单机保活"
          icon={<HeartPulse size={16} style={{ color: 'var(--green)' }} />}
          onClose={() => setKeepalivePicker(false)}
        >
          {available.length === 0 ? (
            <Empty icon={<Plus size={18} />} title="没有可添加的服务器">
              所有服务器都已在单机保活或换班组里了。编进换班组的机器按班次自动启停，不会再进单机保活。
            </Empty>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {available.map((server) => (
                <button
                  key={server.id}
                  className="slot"
                  style={{ textAlign: 'left', cursor: 'pointer' }}
                  onClick={() => addKeepalive(server.id)}
                >
                  <div className="slot-top">
                    <Badge tone={statusTone(server.instance_status)}>
                      {statusLabel(server.instance_status, server.stopped_mode)}
                    </Badge>
                  </div>
                  <div className="slot-name">{server.label}</div>
                  <div className="slot-line">
                    <code>{server.instance_id || '未填实例'}</code>
                    <span>{server.public_ip || '无公网 IP'}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </Modal>
      )}

      {settingsOpen && (
        <SettingsDrawer
          settings={settings}
          onClose={() => setSettingsOpen(false)}
          onSaved={async (message) => {
            setSettingsOpen(false)
            await loadAll()
            toast(message, 'success')
          }}
        />
      )}

      {confirmBox && (
        <ConfirmDialog
          title={confirmBox.title}
          message={confirmBox.message}
          hint={confirmBox.hint}
          confirmText={confirmBox.confirmText}
          busy={confirmBusy}
          onCancel={() => setConfirmBox(null)}
          onConfirm={() => void runConfirm()}
        />
      )}

      <div className="toasts">
        {toasts.map((item) => (
          <div className={`toast ${item.tone}`} key={item.id}>
            {item.message}
          </div>
        ))}
      </div>
    </div>
  )
}
