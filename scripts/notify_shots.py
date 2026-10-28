"""README 用的通知示例图：先用真实的通知生成函数出 HTML，再交给 CDP 截图。

不手写文案，改 messages.py 之后重跑一遍，图就跟着变，不会和代码脱节。

第一步（本脚本）：
    .venv/Scripts/python.exe scripts/notify_shots.py
    → 写出 test-data/notify/*.html（该目录已在 .gitignore 里）

第二步：
    node scripts/notify_shots.cjs
    → 截成 docs/screenshots/16~21-notify-*.png

编号顺序 = README「通知」一节里那张表的顺序，6 张正好凑两行（README 里一行排 3 张）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CDT_DATA_DIR", str(ROOT / "test-data"))
os.environ.setdefault("CDT_DRY_RUN", "1")

from app import messages  # noqa: E402

OUT = ROOT / "test-data" / "notify"

CARD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{name}</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: #eef1f6; }}
  body {{
    padding: 26px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  .card {{
    width: 560px;
    background: #fff;
    border-radius: 16px;
    box-shadow: 0 10px 30px rgba(23, 35, 54, 0.13);
    overflow: hidden;
  }}
  .head {{
    display: flex; align-items: center; gap: 10px;
    padding: 12px 18px;
    background: #fafbfe;
    border-bottom: 1px solid #eaeef5;
  }}
  .logo {{
    width: 28px; height: 28px; border-radius: 9px; flex: none;
    background: linear-gradient(135deg, #4f8cff, #1f5fd6);
    color: #fff; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;
    display: flex; align-items: center; justify-content: center;
  }}
  .who {{ display: flex; flex-direction: column; line-height: 1.3; }}
  .who b {{ font-size: 13px; font-weight: 650; color: #16202e; }}
  .ch {{ font-size: 11px; color: #8492a6; }}
  .time {{ margin-left: auto; font-size: 11px; color: #98a4b6; }}
  .msg {{
    padding: 16px 20px 20px;
    font-size: 13px; line-height: 1.64; color: #1e2836;
    white-space: pre-wrap; word-break: break-word;
  }}
  .msg b {{ font-weight: 700; color: #0f1724; }}
  .msg code {{
    font-family: ui-monospace, Consolas, monospace;
    font-size: 12px; background: #f1f4f9; color: #324863;
    padding: 1px 5px; border-radius: 4px;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="head">
    <div class="logo">CDT</div>
    <div class="who"><b>CDT-Management</b><span class="ch">Telegram · 通知机器人</span></div>
    <div class="time">{time}</div>
  </div>
  <div class="msg">{html}</div>
</div>
</body>
</html>
"""


def server(
    label: str,
    instance_id: str,
    ip: str,
    region: str,
    quota: float,
    used: float,
    status: str = "Running",
    stopped_mode: str = "",
) -> dict:
    return {
        "label": label,
        "instance_id": instance_id,
        "public_ip": ip,
        "region_id": region,
        "quota_gb": quota,
        "traffic_used": used,
        "threshold_gb": round(quota * 0.94, 1),
        "instance_status": status,
        "stopped_mode": stopped_mode,
    }


MAIN = server("香港-主力", "i-demo00000000000001", "47.243.12.32", "cn-hongkong", 200, 148.62)
BACKUP = server(
    "香港-备用", "i-demo00000000000002", "47.243.13.33", "cn-hongkong", 200, 121.35,
    "Stopped", "KeepCharging",
)
THIRD = server(
    "新加坡-二线", "i-demo00000000000003", "47.243.14.34", "ap-southeast-1", 200, 96.08,
    "Stopped", "StopCharging",
)

# 流量保护场景：阈值 188GB（200 × 0.94），下面这几台都已过线
MAIN_OVER = server(
    "香港-主力", "i-demo00000000000001", "47.243.12.32", "cn-hongkong", 200, 189.40,
    "Stopped", "StopCharging",
)
BACKUP_OVER = server(
    "香港-备用", "i-demo00000000000002", "47.243.13.33", "cn-hongkong", 200, 191.20,
    "Stopped", "KeepCharging",
)
THIRD_OVER = server(
    "新加坡-二线", "i-demo00000000000003", "47.243.14.34", "ap-southeast-1", 200, 188.75,
    "Stopped", "StopCharging",
)
THIRD_TAKEOVER = server(
    "新加坡-二线", "i-demo00000000000003", "47.243.14.34", "ap-southeast-1", 200, 96.08,
)

GROUP = "港新联合保活"

SHOTS = [
    (
        "16-notify-shift",
        "换班完成报告",
        messages.shift_done(
            MAIN,
            [BACKUP, THIRD],
            messages.shift_period("第 2 班", "12:00", "24:00"),
            "✅ 已同步",
            group_name=GROUP,
        ),
        "12:00",
    ),
    (
        "17-notify-failover",
        "故障转移报告",
        messages.shift_failover(
            THIRD_TAKEOVER,
            [MAIN_OVER],
            [BACKUP],
            "✅ 已同步",
            group_name=GROUP,
        ),
        "18:26",
    ),
    (
        "18-notify-breaker",
        "全局熔断报告",
        messages.breaker([MAIN_OVER, BACKUP_OVER, THIRD_OVER], group_name=GROUP),
        "18:31",
    ),
    (
        "19-notify-recovery",
        "抢占恢复报告",
        messages.recovery(MAIN, "✅ 已同步"),
        "15:42",
    ),
    (
        "20-notify-error",
        "换班异常报告",
        messages.shift_error(MAIN, [BACKUP, THIRD], 8, group_name=GROUP),
        "00:08",
    ),
    (
        "21-notify-daily",
        "每日流量报告",
        messages.daily_report([MAIN, BACKUP, THIRD], "2026-10-27 09:00"),
        "09:00",
    ),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, name, message, clock in SHOTS:
        target = OUT / f"{slug}.html"
        target.write_text(
            CARD.format(name=name, time=clock, html=message.html),
            encoding="utf-8",
        )
        print(f"{target}  ← {name}（{message.title}）")


if __name__ == "__main__":
    main()
