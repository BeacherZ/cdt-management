# CDT-Management

**Docker 搭建的网页管理面板：阿里云 CDT 抢占式实例的单机保活 + 多机联合换班 + 多机流量均衡 + DDNS + 通知。**

四个模块各管一段，互不干扰：

| 模块 | 作用 |
| --- | --- |
| **单机保活** | 一台实例单独托管，掉线就自动拉起，可自由限定运行时段 |
| **联合换班** | 多台编成一组，按工时或天数轮流当班，同一时刻只留一台 |
| **流量均衡** | 多台共用一套服务，域名指向累计用量最少的那台，周期轮换 |
| **DNS** | 一条记录绑定实例、换班组或均衡组，IP 变化时自动同步 |

没编进任何流程的**闲散实例**照旧可以手动开关机。

这几套流程之上压着一层**流量保护**：每台单独设阈值，超了就停；换班组会先尝试故障转移，全超才熔断。

**实例不限抢占式**：流量保护和流量均衡只看用量、不看计费方式，按量付费与包年包月同样适用，只是用不到抢占式专属的「抢占恢复」。

[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![python](https://img.shields.io/badge/python-3.13-blue)
![docker](https://img.shields.io/badge/docker-amd64%20%7C%20arm64-2496ed)


## 🖥️界面

浅色 / 深色（`设置 → 常规设置 → 外观` ，点击查看大图）

<p align="center">
  <a href="docs/screenshots/02-dashboard.png"><img src="docs/screenshots/02-dashboard.png" width="48.5%" alt="运行概览（浅色）"></a>
  <a href="docs/screenshots/03-dashboard-dark.png"><img src="docs/screenshots/03-dashboard-dark.png" width="48.5%" align="top" alt="运行概览（深色）"></a>
</p>

手机端（自适应布局，点击查看大图）

<p align="center">
  <a href="docs/screenshots/04-mobile-dashboard.png"><img src="docs/screenshots/04-mobile-dashboard.png" width="24%" align="top" alt="手机端运行概览（浅色）"></a>
  <a href="docs/screenshots/05-mobile-dashboard-dark.png"><img src="docs/screenshots/05-mobile-dashboard-dark.png" width="24%" align="top" alt="手机端运行概览（深色）"></a>
  <a href="docs/screenshots/06-mobile-instances.png"><img src="docs/screenshots/06-mobile-instances.png" width="24%" align="top" alt="手机端实例管理"></a>
  <a href="docs/screenshots/07-mobile-instances-dark.png"><img src="docs/screenshots/07-mobile-instances-dark.png" width="24%" align="top" alt="手机端实例管理（深色）"></a>
</p>

## ✨主要功能

### 流量保护

**说明**：每台单独配阈值，超限自动停机；换班组会先尝试故障转移，全超才熔断。

<details>
<summary>详细说明</summary>

> 超限停机后不再拉起；流量回落到阈值以下（月初流量包重置，或调高阈值）会清掉超限标记，自动恢复开机并保活。
> 
> 故障转移的两种触发情形：当班流量到阈值，或当班被抢占后尝试3次无法拉起。

</details>

**目的**：不让任何一台实例烧穿流量包。

### 单机保活

**说明**：一台实例自己管自己，掉了就拉起来，不换班、不碰别的实例。

<details>
<summary>详细说明</summary>

> 每轮巡检（默认 60 秒，可自定义间隔与起始秒）停着就再下发一次开机，直到拉起为止；启动中不重复下发。
>
> **运行窗口（可选）**：比如只让它每天 08:00–20:00 跑，或只在这个月的 1–16 号跑，两项同时填按交集算。结束早于开始表示跨天到次日（如 `20:00–06:00`）；结束填 `00:00` 表示到 24:00；起止相同等于全天。窗口外自动停机、窗口内自动保活，窗口两端都**不推通知**。

</details>

**目的**：失联第一时间拉起来，保证在线率；设定运行窗口让实例只在用得上的时间在线，减少费用。

### 联合换班

**说明**：多台实例编成一组，按顺序轮流当班，同一时刻只留一台在线。排班周期两种任选：

<details>
<summary>详细说明</summary>

>  **一天一轮**：填每台的工时，或直接排 0–24 点的固定时刻表，24 小时内换完；
> 
>  **一月一轮**：填每台上几天，1 号 0 点从第 1 位开始，最后一位承接当月剩下的天数。
> 
>  **交接顺序**：上班准时——到点先把当班实例开机；下班不抢跑——等下一台确认运行中，上一台才停机。

</details>

**目的**：通过联合换班，降低多台抢占式实例同时在线的成本，并充分利用每台实例各自的 200G CDT 免费流量包。

### 流量均衡

**说明**：组内几台实例跑同一套服务，域名同一时刻只指向其中一台。每到一个整点，把组内累计用量比一遍：当班那台比用得最少的那台多出差值阈值，就把域名切过去。不看时钟、不排班，只看谁用得少。

<details>
<summary>详细说明</summary>

> **成员前提**：每台都部署同一套服务、同一份配置，域名指过去就能直接接客，不用改客户端。
> 
> **评估周期**：1 / 3 / 6 / 12 / 24 小时，只按整点评估（3 小时就是 00/03/06…，24 小时是每天 0 点）。**差值阈值**：当班比最少的那台多出这么多 GB 才值得切；设太小，域名会每隔一个周期抖一次。
> 
> **切换顺序**：先确认接替的那台在运行 → 再改 DNS → 最后按各成员自己的「下班后」动作处理上一台：保持运行还是停机，添加组员时逐台设定。
> 
> **与换班的区别**：换班看时钟、到点交接停机；均衡看累计流量、不排班，两者不能挂同一台机器。

</details>

**目的**：多台实例各带一个 200G 免费流量包，又不想按时间排班时，按实际用量轮换 —— 谁用得少，域名就指向谁，省流量也省心。

### DNS

**说明**：一条记录绑一台实例、一个换班组或一个均衡组；目标在跑时，公网 IP 一变（开机换 IP、手动换弹性 IP）下一轮巡检自动指过去。

<details>
<summary>详细说明</summary>

> 每轮巡检只拿API返回的IP比对本地记录，不查 Cloudflare；只有手动开机、单机保活拉起、换班结算这三个时点会强制查一次实际解析值。一致就不改记录，DNS 页的「上次同步」也不会跳；不一致才更新。

</details>

**目的**：单实例换了弹性 IP 不用手动改 DNS；换班组绑定的域名 `DDNS` 同步到目前当班运行的实例IP，组员搭建的同种服务共享配置后可无感切换使用该服务。

### 通知

**说明**：支持 Telegram、邮件、Webhook 三类渠道，可同时开启。

<details>
<summary>9 种通知的实际推送效果（点击查看大图）</summary>

<p align="center">
  <a href="docs/screenshots/25-notify-overview.png"><img src="docs/screenshots/25-notify-overview.png" width="96%" alt="9 种通知推送效果：换班完成、故障转移、全局熔断、超限停机（单机保活 / 闲散）、均衡完成、抢占恢复、换班异常、每日报告"></a>
</p>

</details>

**目的**：换班、故障转移、熔断、超限停机、均衡切换、抢占恢复、换班异常、每日报告都主动推给你，不用盯面板。

## 🚀快速开始

三种装法任选一种。默认数据目录 `/opt/docker/cdt-management`，宿主机端口 `8282`，容器端口 `8080`。

装好后浏览器打开 `http://你的地址:8282`，首次会引导设置管理员账号。

### 方式一：一键脚本（最省事）

```bash
curl -fsSL https://raw.githubusercontent.com/BeacherZ/cdt-management/main/install.sh | bash
```

内部走【方式三】的 `docker run`；没装 Docker 会自动装。换端口或换目录自行加参数：

```bash
curl -fsSL https://raw.githubusercontent.com/BeacherZ/cdt-management/main/install.sh | PORT=18282 DIR=/srv/cdt bash
```

**升级**：重跑同一条命令，会先比对版本，已是最新就不动容器；有新版本才删容器重建，数据不动。

### 方式二：docker compose

```yaml
services:
  cdt-management:
    image: ghcr.io/beacherz/cdt-management:latest
    container_name: cdt-management
    restart: unless-stopped
    init: true
    ports:
      - "8282:8080"
    environment:
      TZ: Asia/Shanghai
      CDT_DATA_DIR: /data
    volumes:
      - /opt/docker/cdt-management:/data
```

```bash
mkdir -p /opt/docker/cdt-management
docker compose up -d
docker compose logs -f cdt-management
```

**升级**（两行一起粘，任意目录都能跑）：

```bash
D=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' cdt-management)
cd "$D" && docker compose pull && docker compose up -d
```

### 方式三：docker run

```bash
mkdir -p /opt/docker/cdt-management

docker run -d --name cdt-management --restart unless-stopped --init \
  -p 8282:8080 \
  -e TZ=Asia/Shanghai \
  -e CDT_DATA_DIR=/data \
  -v /opt/docker/cdt-management:/data \
  ghcr.io/beacherz/cdt-management:latest
```

**升级**：

```bash
docker pull ghcr.io/beacherz/cdt-management:latest
docker rm -f cdt-management
# 再把上面那条 docker run 原样粘一次，挂载目录不动，数据不会丢
```

Windows 挂载路径写成 `-v D:/docker/cdt-management:/data`。


### 卸载

```bash
# 只删本项目（容器、镜像和数据目录一起删）
curl -fsSL https://raw.githubusercontent.com/BeacherZ/cdt-management/main/install.sh | UNINSTALL=1 bash

# 连 Docker 一起卸（本机所有容器和镜像都会没）
curl -fsSL https://raw.githubusercontent.com/BeacherZ/cdt-management/main/install.sh | UNINSTALL=docker bash
```

两条都会问一次 `yes`，输对了才动手。

## 🔑阿里云 RAM 权限

完整使用本项目功能需要给当前用户添加以下三种 RAM 权限：  
（RAM 访问控制 - 身份管理 - 用户 - 权限管理）

| RAM 权限 | 用在哪 |
| --- | --- |
| `AliyunECSFullAccess` | 查实例、开机、停机（含节省停机） |
| `AliyunCDTReadOnlyAccess` | 读当月 CDT 公网流量 |
| `AliyunBSSReadOnlyAccess` | 账户余额、实例账单 |

- 少了 CDT 这组，整轮对账都会失败，不是流量空着 —— 必须给
- 少了 BSS 这组，费用和余额显示不出来，启停照常
- 国内站和国际站的 AK 不通用，一台实例一套凭据
- RAM 控制台：[国内站](https://ram.console.aliyun.com/users) · [国际站](https://ram.console.alibabacloud.com/users)

## 🧭配一遍的流程

1. **实例管理** → 添加实例：填别名、Region、AccessKey ID/Secret、实例 ID、配额与限流阈值，按需选停机模式（默认节省停机）
2. 三选一（或都要，但**同一台不要同时挂两边**）：
   - **单机保活** → 添加保活，勾上这台；要限定运行时段/日期就在同一个设置页里填
   - **联合换班** → 新建换班组，选排班周期（一天一轮按工时，一月一轮按天数），按顺序添加成员
   - **流量均衡** → 新建均衡组，选评估周期与差值阈值，按顺序加成员（每台的「下班后」动作单独设）
3. **设置 → DDNS**：填 Cloudflare Token（或 Global API Key）、TTL，开启
4. **DNS**：加域名记录，再把它绑到某台实例、某个换班组或某个均衡组（不绑就谁也不会去动它）
5. **设置 → 通知**：填 Telegram Bot Token + Chat ID，点「发送测试」确认能收到
6. 回「运行概览」点「立即同步」，看第一轮结果

## 💾数据放在哪

| 路径 | 内容 |
| --- | --- |
| `cdt-management.db` | SQLite：实例、保活、换班组、均衡组、换班进度、事件、流量采样 |
| `master.key` | 加密主密钥，**必须与 db 一起备份**，丢了 AccessKey Secret 无法解密 |

备份就把整个目录拷走；换宿主机时两个文件一起搬。

## 🔒公网部署与安全

直接暴露到公网（或通过端口映射）并不安全，**建议套一层 HTTPS 反向代理**。

## ❓常见问题

**换班了但没收到通知？** 先看「设置 → 通知」里渠道是否启用、点测试能不能发出去；再看「运行概览」的「最近事件」有没有对应记录。通知失败只记事件，不会阻塞换班。

**DDNS 显示「IP 无效跳过」？** 实例还没拿到公网 IP，或者不是公网 IPv4。等下一轮同步，或手动刷新那台实例。

**所有成员都超限了怎么办？** 会全局熔断，全部节省停机，等下个月流量重置后手动开机，或调整阈值。

**忘了面板密码？** 删掉数据目录里的 `cdt-management.db` 会重置所有配置。

## 🛠️本地开发

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m app.main              # 后端 :8080
cd web && npm install && npm run dev        # 前端 :5273，已配 /api 代理
CDT_DRY_RUN=1 ./.venv/bin/python -m app.main # 不调真实阿里云
```

`CDT_DRY_RUN=1` 时阿里云调用走内存桩，整条换班链路能在本地跑通，不会碰真实账号。

## 📄致谢与许可

- 面板的信息架构、视觉语言、交互细节参考 [**wang4386/CDT-Monitor**](https://github.com/wang4386/CDT-Monitor)，作者 **青柠**，MIT 协议。
- 一键安装脚本的形态参考 [**BeacherZ/dailycheckin-plus**](https://github.com/BeacherZ/dailycheckin-plus)。

本项目以 MIT 协议发布，`LICENSE` 里同时保留了上游版权声明。
