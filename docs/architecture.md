# 架构说明

一份给自己看的实现笔记：为什么这么拆、状态存在哪、换班怎么保证不重复操作。

## 进程模型

单进程，三个线程：

```
uvicorn (asyncio)
 ├── FastAPI 路由          → 同步端点丢到线程池，不阻塞事件循环
 └── scheduler 线程 (daemon)
       ├─ 每 N 秒：engine.reconcile_all()
       ├─ 每分钟：检查日报时间
       └─ 每小时：清理事件与采样
```

没有独立 Worker、没有任务队列。对账是幂等的，重复执行没有副作用，所以不需要租约或分布式锁 —— 一个进程内用 `threading.Lock`（非阻塞）挡住并发即可。

SQLite 用 WAL + 每线程独立连接（`threading.local`）。读写混跑不打架；`busy_timeout=15s` 兜住偶发争用。

## 数据模型

```
servers ──────────── 一台机器 = 一个阿里云账号 + 一个 ECS 实例
   │                  quota_gb / threshold_gb / 运行时快照（流量、状态、IP）
   │
   ├── keepalives ─── 单机保活（server_id 唯一）
   │
   └── shift_members ─┐
                      ├── shift_groups ── 联合换班组
                      │      anchor_at：轮换周期的时间原点
                      └── position / hours：顺序与时长

shift_runs ──────── 每个「班次」的执行状态
                      slot = 该班次的开始时间戳（Unix 秒）
                      phase = pending | switching | settled | failover | breaker | error

flags ───────────── 一次性标记（通知去重、恢复待确认）
events ──────────── 事件流，给人看的
traffic_samples ─── 流量采样，给图表用
```

关键设计：**`slot` 用「班次开始时间戳」而非「日期 + AM/PM」**。

这样无论是 2 台各 12 小时、3 台各 8 小时，还是 10h + 14h，都能天然得到唯一且稳定的 slot key。改成员或改时长时重置 `anchor_at`，slot 自然翻新，不会沿用旧标记。

## 轮换算法

```python
total  = Σ(member.hours) * 3600
elapsed = max(0, now - anchor)
cycle  = elapsed // total          # 第几个周期
offset = elapsed % total           # 周期内偏移
逐成员累加，第一个满足 offset < Σhours 的就是当班
```

O(n) 且无状态。`anchor_at` 是唯一需要持久化的东西 —— 手动换班就是改它。

手动换班为什么是「平移 anchor」而不是「记一个强制下次当班」？因为后者会引入一个额外的状态机分支（要判断什么时候失效、跟自然轮换怎么合并）。平移 anchor 之后，系统仍然只有一个真相来源（时间 + anchor），逻辑不分叉。

平移量 = `now - 下一个成员在本周期内的累计起点`。等价于「把 now 当成下一班的第 0 秒」。

## 对账流程

`reconcile_group` 一次跑完：

```
1. 算当班 / 待机
2. 并行刷新所有成员的流量与实例状态
3. 当班超阈值？
   ├─ 有没超的成员 → 换人（failover=true），原当班转入待机集合
   ├─ 全都超了     → 熔断：全员 StopCharging，确认后推熔断报告
   ├─ 别人查不到   → 保守维持（不能因为 API 抖动就把机器停掉）
   └─ 关闭了故障转移 → 超限停机兜底
4. 当班没 Running → Start（若该班次已结算过，打上「待恢复」标记）
5. 当班 Running   → 其余全部 StopCharging
6. 全部就绪（当班 Running + 其余 Stopped/StopCharging + 有公网 IP）
     → 同步 DDNS → 推「换班完成 / 临时换班」
7. 未就绪 → 检查是否超过 10 分钟 → 推「换班异常」
```

几个刻意的选择：

- **DDNS 必须先于通知**。通知文案里会回显 `☁️ DDNS：✅ 已同步`，先发通知再同步会撒谎。
- **待机没确认停机就不算结算**。只下发了 Stop 指令不算，得看到 `Stopped` 才算 ready，否则会误报「换班完成」。
- **别人的状态查不到时不动手**。宁可这轮什么都不做，也不要基于空数据把实例停掉。
- **超限兜底与熔断分开**。熔断是「都没得选」，超限停机是「换不了但还在跑」；两者的通知文案不同，用户要知道是哪一种。

## 幂等与去重

每次要发通知前，先查 `flags`：

| 标记 | 作用 |
| --- | --- |
| `notify:{group}:{slot}:settled` | 同一个班次只推一次换班报告 |
| `breaker:{group}:{slot}` | 熔断只报一次 |
| `limit_stop:{group}:{slot}` | 超限停机只报一次 |
| `timeout:{group}:{slot}` | 换班异常只报一次 |
| `recovery:{group}:{slot}` | 待确认的抢占恢复 |

标记落库而不是内存，容器重启后不会重复推送。

开关机的去重靠状态判断而不是标记：`status != Running` 才 Start，`status != Stopped` 才 Stop。阿里云那边状态是真相，所以不需要额外记「已经发过启动指令」。

## 单机保活与换班的互斥

同一台机器如果同时挂在两个模块下，会互相拉扯：保活看到 Stopped 就拉起来，换班看到不该它当班就停掉，来回震荡。

处理方式：`reconcile_all` 先算出「所有进组的 server_id」，保活对账时跳过这些机器，并记一条一次性警告。**让位给换班**，因为换班的约束更强（同一时刻只能一台在线）。

## 安全

| 项 | 做法 |
| --- | --- |
| AccessKey Secret / Token / 密码 | AES-GCM，密钥是 `master.key`（32 字节随机，600 权限） |
| 面板密码 | PBKDF2-SHA256，260k 轮，随机盐 |
| 会话 | HMAC-SHA256 签名票据，含过期时间，无服务端状态 |
| CSRF | 双提交 Cookie：`cdt_mgmt_csrf` 必须在 `X-CDT-CSRF` 头回传 |
| 登录限速 | 内存计数，同 IP 5 分钟 8 次 |
| 敏感字段回显 | 只返回 `*_configured: true` 和打码值，永不回传明文 |

`master.key` 丢失等于所有凭据无法解密，所以 README 里反复强调要跟 db 一起备份。

## 为什么是 Python 不是 Go

交付这台机器上没有 Go 工具链，无法编译验证。Python 能本地把「初始化 → 建机器 → 建组 → 对账 → 结算 → 通知文案」整条链路跑通再交付，风险更可控。运行时是 `uvicorn + SQLite`，单容器无外部依赖，镜像基于 `python:3.13-slim`。

`CDT_DRY_RUN=1` 会在内存里模拟实例状态流转（停止 → 启动中 → 运行），配合 `scripts/smoke_test.py` 可以完整验证换班流程而不碰真实阿里云。
