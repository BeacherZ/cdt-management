"""进程级配置：全部来自环境变量，容器里用 -e 传。"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "cdt-management"
VERSION = "1.0.0"

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

# dry-run：不调真实阿里云接口，用于本地 UI 调试
DRY_RUN = os.environ.get("CDT_DRY_RUN", "0") == "1"

SESSION_COOKIE = "cdt_mgmt_session"
CSRF_COOKIE = "cdt_mgmt_csrf"
SESSION_TTL_SECONDS = 14 * 24 * 3600


def ensure_dirs() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"数据目录 {DATA_DIR} 不可写。容器内以 uid 10001 运行，宿主机上的挂载目录必须让它可写：\n"
            f"  sudo chown -R 10001:10001 {DATA_DIR}\n"
            "（用一键脚本安装会自动处理这一步）"
        ) from exc
