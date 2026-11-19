"""README 用的通知示例图：先用真实的通知生成函数出 HTML，再交给 CDP 截图。

不手写文案，改 messages.py 之后重跑一遍，图就跟着变，不会和代码脱节。

第一步（本脚本）：
    ./.venv/bin/python scripts/notify_shots.py
    → 写出 test-data/notify/*.html（该目录已在 .gitignore 里）

第二步：
    node scripts/notify_shots.cjs
    → 截成 docs/screenshots/16~24-notify-*.png，外加 25-notify-overview.png 总览

README 只用到总览那一张：3×3 排九条，四种归属各占一条
（换班组 / 单机保活 / 流量均衡 / 闲散手动），每日报告必在其中。
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

# 模板里的 CSS 用双花括号，因为整段会经过一次 str.format（{time} / {html}），
# 塞回模板时先 STYLE.format() 把双括号还原成单括号，否则样式整段失效。
STYLE = """  * {{ box-sizing: border-box; }}
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
  }}"""

HEAD = """  <div class="head">
    <div class="logo">CDT</div>
    <div class="who"><b>CDT-Management</b><span class="ch">Telegram · 通知机器人</span></div>
    <div class="time">{time}</div>
  </div>"""

CARD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{name}</title>
<style>
{style}
</style>
</head>
<body>
<div class="card">
{head}
  <div class="msg">{html}</div>
</div>
</body>
</html>
"""

# 总览：三列并排，卡片之间 22px，四周 28px —— README 里整张图缩到 96% 宽度，
# 单张卡片仍能看清标题和状态行
OVERVIEW = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>通知效果总览</title>
<style>
{style}
  body {{ padding: 28px; }}
  /* 三列「瀑布式」排：九张卡高度差得远（每日报告最长、超限停机最短），
     用等高行会留下大片空白，按列往下摞才紧凑 */
  .grid {{
    column-count: 3;
    column-gap: 22px;
    width: 1664px;
  }}
  .grid .card {{
    display: inline-block;
    width: 100%;
    margin: 0 0 22px;
    break-inside: avoid;
  }}
</style>
</head>
<body>
<div class="grid">
{cards}
</div>
</body>
</html>
"""


def card_html(message, clock: str) -> str:
    return f"""<div class="card">
{HEAD.format(time=clock)}
  <div class="msg">{message.html}</div>
</div>"""


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
        # 均衡通知按服务器 id 去重（messages._member_id 认 id / server_id），
        # 演示数据里就用实例号末两位
        "id": int(instance_id[-2:]) if instance_id[-2:].isdigit() else 0,
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

# 超限停机：单机保活 / 闲散手动的机器各自到阈值，被迫节省停机
KEEPALIVE_OVER = server(
    "香港-保活机", "i-demo00000000000004", "47.243.15.35", "cn-hongkong", 200, 189.40,
    "Stopped", "StopCharging",
)
IDLE_OVER = server(
    "新加坡-闲散机", "i-demo00000000000005", "47.243.16.36", "ap-southeast-1", 200, 189.40,
    "Stopped", "StopCharging",
)

# 流量均衡：组内两台都开着，域名从用得多的那台换到用得最少的那台
BAL_A = server("新加坡-均衡A", "i-demo00000000000006", "47.243.17.37", "ap-southeast-1", 200, 62.40)
BAL_B = server(
    "新加坡-均衡B", "i-demo00000000000007", "47.243.18.38", "ap-southeast-1", 200, 148.62,
    "Stopped", "KeepCharging",
)

# 换班异常：当班该运行却超时没运行（Stopped → ⚪），standby 该停却还赖在 Running（→ 🟢）
ERROR_DUTY = server(
    "香港-主力", "i-demo00000000000001", "47.243.12.32", "cn-hongkong", 200, 148.62,
    "Stopped", "StopCharging",
)
ERROR_STANDBY = server(
    "香港-备用", "i-demo00000000000002", "47.243.13.33", "cn-hongkong", 200, 121.35,
)
ERROR_STANDBY2 = server(
    "新加坡-二线", "i-demo00000000000003", "47.243.14.34", "ap-southeast-1", 200, 96.08,
)

GROUP = "港新联合保活"
BALANCE = "新加坡均衡"

# 九条 = README 那张总览的 3×3，顺序即阅读顺序：
# 换班组 → 单机保活 → 流量均衡 → 闲散手动，最后一条固定是每日报告。
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
        "19-notify-over-keepalive",
        "超限停机报告（单机保活）",
        messages.over_limit(KEEPALIVE_OVER, messages.SCOPE_KEEPALIVE),
        "22:10",
    ),
    (
        "20-notify-balance",
        "均衡完成报告（流量均衡）",
        messages.balance_switch(
            BAL_A,
            [BAL_A, BAL_B],
            "✅ 已同步",
            group_name=BALANCE,
            prev=BAL_B,
        ),
        "20:07",
    ),
    (
        "21-notify-over-idle",
        "超限停机报告（闲散手动）",
        messages.over_limit(IDLE_OVER, messages.SCOPE_IDLE),
        "22:12",
    ),
    (
        "22-notify-recovery",
        "抢占恢复报告",
        messages.recovery(MAIN, "✅ 已同步", group_name=GROUP),
        "15:42",
    ),
    (
        "23-notify-error",
        "换班异常报告",
        messages.shift_error(ERROR_DUTY, [ERROR_STANDBY, ERROR_STANDBY2], 8, group_name=GROUP),
        "00:08",
    ),
    (
        "24-notify-daily",
        "每日报告",
        messages.daily_report(
            [
                {**MAIN, "site": "china", "monthly_cost": 12.34, "balance_amount": 1234.56, "billing_currency": "CNY"},
                {**BACKUP, "site": "china", "monthly_cost": 5.67, "balance_amount": 2345.67, "billing_currency": "CNY"},
                {**THIRD, "site": "international", "monthly_cost": 3.21, "balance_amount": 0.0, "billing_currency": "USD"},
            ],
            "2026-11-19 09:00",
        ),
        "09:00",
    ),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, name, message, clock in SHOTS:
        target = OUT / f"{slug}.html"
        target.write_text(
            CARD.format(
                name=name,
                time=clock,
                html=message.html,
                style=STYLE.format(),
                head=HEAD.format(time=clock),
            ),
            encoding="utf-8",
        )
        print(f"{target}  ← {name}（{message.title}）")

    overview = OUT / "_overview.html"
    overview.write_text(
        OVERVIEW.format(
            style=STYLE.format(),
            cards="\n".join(card_html(message, clock) for _, _, message, clock in SHOTS),
        ),
        encoding="utf-8",
    )
    print(f"{overview}  ← 九条通知总览")


if __name__ == "__main__":
    main()
