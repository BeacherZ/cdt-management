"""进程级配置：全部来自环境变量，容器里用 -e 传。"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "cdt-management"
VERSION = "1.8.6"

DATA_DIR = Path(os.environ.get("CDT_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "cdt-management.db"
MASTER_KEY_PATH = DATA_DIR / "master.key"
LOG_DIR = DATA_DIR / "logs"

LISTEN_HOST = os.environ.get("CDT_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("CDT_LISTEN_PORT", "8080"))

# 首次启动时若提供，则跳过初始化向导直接创建管理员
BOOTSTRAP_USERNAME = os.environ.get("CDT_ADMIN_USERNAME", "").strip()
BOOTSTRAP_PASSWORD = os.environ.get("CDT_ADMIN_PASSWORD", "").strip()

BRAND = os.environ.get("CDT_BRAND", "CDT-Management")

# 「关于」里检查更新用的仓库
REPO = os.environ.get("CDT_REPO", "BeacherZ/cdt-management")

# 对账间隔（秒）。低于 30 秒会打爆阿里云 API 配额。
RECONCILE_INTERVAL = max(30, int(os.environ.get("CDT_RECONCILE_INTERVAL", "60")))

# 单次阿里云 / HTTP 调用超时
HTTP_TIMEOUT = float(os.environ.get("CDT_HTTP_TIMEOUT", "12"))

# 事件保留天数
EVENT_RETAIN_DAYS = max(1, int(os.environ.get("CDT_EVENT_RETAIN_DAYS", "14")))

# 换班超时（分钟）：超时仍未就绪判定为换班异常
SHIFT_TIMEOUT_MINUTES = max(2, int(os.environ.get("CDT_SHIFT_TIMEOUT_MINUTES", "10")))

# 当班被抢占后「尝试几次仍拉不起来」才做故障转移，换一台顶这一班。
# 判据是**次数**，不是墙钟：巡检间隔可配（CDT_RECONCILE_INTERVAL，最小 30 秒），
# 按固定秒数算的话，间隔调成 5 分钟就变成「试 1 次就换人」、调成 30 秒又变成
# 试 6 次，和说明里写的「尝试 3 次无法拉起」对不上。次数只统计真正下发过
# start_instance 的那些轮（卡在启动中的轮次不会再下发，不计数）。
DUTY_START_TRIES = max(2, int(os.environ.get("CDT_DUTY_START_TRIES", "3")))

# 当班迟迟起不来的容忍秒数。两个用处：
#   1) 中间态兜底：实例停在 Starting / Pending 时不会再下发启动指令、次数不涨，
#      只能按时间判死。
#   2) 换班加速窗口内的次数判死下限：加速时 10 秒一轮，「试满 3 次」只有 30 秒，
#      一次状态同步稍慢就会判成起不来。所以加速轮要「次数够 + 这段已经持续
#      DUTY_STALL_SECONDS」两个条件同时满足；日常巡检轮不加这条，行为和以前一致
#      （否则把巡检间隔调到 30 秒的用户，换人时机会被无谓推迟）。
# 不能设太短：竞价实例冷启动本身就要几十秒，太短会把「慢」当成「死」。
DUTY_STALL_SECONDS = max(60, int(os.environ.get("CDT_DUTY_STALL_SECONDS", "180")))

# 当班已经进入 Starting / Pending（启动指令下发成功、阿里云正在拉机器）时的
# 额外宽限。这个阶段不是「没起来」，而是「正在起」：Windows 实例正常开机就要
# 2–3 分钟，拿 180 秒去卡它，等于把正常的慢启动判成起不来，换人之后还会把
# 正在启动的原当班主动停机 —— 白启动一次。
#
# 取 480（8 分钟）而不是 600：换班超时默认就是 10 分钟，两者相等时，一台真正
# 卡死的机器会在同一轮同时触发「故障转移」和「换班超时」两条通知。提前 2 分钟
# 先转移，用户先看到「换人了」，超时那条就不会再来添乱。
#
# 但换班超时是可配的（CDT_SHIFT_TIMEOUT_MINUTES）。用户调到 8 分钟以下时，
# 固定的 480 秒就会反超，变成「先超时报警、后转移」。所以这里跟着它收紧：
# 宽限至少比超时早 60 秒，同时不低于「确实没在跑」的判定阈值。
_GRACE_CONFIGURED = max(60, int(os.environ.get("CDT_DUTY_START_GRACE", "480")))
DUTY_START_GRACE = max(
    DUTY_STALL_SECONDS,
    min(_GRACE_CONFIGURED, SHIFT_TIMEOUT_MINUTES * 60 - 60),
)
# 超时设得比 stall 阈值还短（最小 2 分钟）时，顺序无法再靠宽限修正。
GRACE_ORDER_BROKEN = DUTY_START_GRACE >= SHIFT_TIMEOUT_MINUTES * 60

# 同一班次两次下发开机指令的最小间隔（秒）。日常巡检间隔本来就大，这条不起作用；
# 只有换班加速（下发指令后 10 秒一轮读状态）时才生效 —— 否则「每轮没起来就重发」
# 会变成每 10 秒砸一次 StartInstance，白烧写接口、也让「尝试 3 次」从 6 分钟
# 缩成 30 秒，把慢启动判成起不来。
DUTY_START_RETRY_SECONDS = max(30, int(os.environ.get("CDT_DUTY_START_RETRY_SECONDS", "60")))

# 换班加速：下发开机/停机指令之后、预期状态还没到位的这段窗口，单独加密读取。
# 只读「这一组」的实例状态，不查流量，**不改变日常巡检间隔**。
SHIFT_AWAIT_INTERVAL = max(5, int(os.environ.get("CDT_SHIFT_AWAIT_INTERVAL", "10")))# 单个等待窗口最长持续多久（秒）。卡死时到点自动退出，不会把加速变成常态。
# 取 300（5 分钟）：正常换班几十秒就走完，Windows 慢启动 3–4 分钟也够；
# 再长就跟换班超时（默认 10 分钟）比肩，超时通知发出后还在加速读，没有意义。
SHIFT_AWAIT_TTL = max(60, int(os.environ.get("CDT_SHIFT_AWAIT_TTL", "300")))
# 同一班次累计允许加速多久（秒）。竞价库存不足、欠费冻结这类卡死场景里，每轮
# 日常巡检都会下发一次重启，窗口会被一次次重新打开 —— 光靠 TTL 挡不住，10 秒
# 一轮会变成常态（3 台组 ≈ 每天 2.6 万次 DescribeInstances）。预算用尽就不再
# 打开窗口，回落成用户配的巡检间隔；只有换了班次（真正的新一轮换班）才重置。
SHIFT_AWAIT_BUDGET = max(60, int(os.environ.get("CDT_SHIFT_AWAIT_BUDGET", "600")))
# 同一台机器两次下发停机指令的最小间隔（秒），只在加速轮生效：停机接口成功但
# 实例没停（被锁定 / 被立即拉起）时，10 秒一轮会变成 10 秒砸一次 StopInstance。
SHIFT_STOP_RETRY_SECONDS = max(30, int(os.environ.get("CDT_SHIFT_STOP_RETRY_SECONDS", "60")))

# 流量均衡的加速通道：只推进「开机 / 切换进行中」的组，和换班那条同构。
# 均衡没有班次表，一切都在等「接替那台什么时候 Running」：没有这条通道，
# 下发开机后要等下一轮巡检（默认 60 秒）才结算，评估周期 24 小时的组更久。
# 同样只读状态、不查流量，日常巡检间隔不受影响。
BALANCE_AWAIT_INTERVAL = max(5, int(os.environ.get("CDT_BALANCE_AWAIT_INTERVAL", "10")))

# dry-run：不调真实阿里云接口，用于本地 UI 调试
DRY_RUN = os.environ.get("CDT_DRY_RUN", "0") == "1"

# dry-run 桩模拟实例异步操作（开机/停机）到位的延迟秒数。默认 3 秒便于手动
# 观察「转圈确认」流程；冒烟测试设 0 保持同步语义，专注测业务流程而非轮询时序。
MOCK_OP_DELAY = max(0.0, float(os.environ.get("CDT_MOCK_OP_DELAY", "3")))

SESSION_COOKIE = "cdt_mgmt_session"
CSRF_COOKIE = "cdt_mgmt_csrf"
SESSION_TTL_SECONDS = 14 * 24 * 3600


def ensure_dirs() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 正常路径下 docker-entrypoint.py 已经把属主修好了，走到这里基本是
        # 只读挂载、NFS root_squash，或者有人绕过了入口脚本直接跑 app.main。
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        raise RuntimeError(
            f"数据目录 {DATA_DIR} 不可写（当前 uid {uid}，{type(exc).__name__}: {exc}）。\n"
            "容器里的应用以非 root 运行，挂载进来的宿主机目录必须让它可写。三种解法挑一个：\n"
            f"  1) sudo chown -R 10001:10001 {DATA_DIR}\n"
            "  2) docker run -e PUID=$(id -u) -e PGID=$(id -g) ...（按宿主机用户身份跑，不动权限）\n"
            "  3) 换成 docker 具名卷：-v cdt-data:/data（属主随镜像初始化）\n"
            "用一键脚本（install.sh）安装会自动处理第 1 步。"
        ) from exc
