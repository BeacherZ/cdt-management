#!/usr/bin/env python3
"""巡检对齐秒位：每轮落在网格的固定那一秒，而不是「跑完再睡 N 秒」。

背景：大量保活脚本默认都在每分钟第 0 秒打阿里云接口，一堆项目同时挤过去。
允许用户把巡检挪到第 18 秒，就得保证「真的是第 18 秒」，而不是「大约」——
只要调度还写成「跑完一轮再 sleep(interval)」，执行耗时就会累进周期，一天下来
触发时刻越飘越远，秒位根本控不住。

这组用例盯四件事：
1. 给定 now / interval / offset，下一轮落在 offset 那一秒且严格在未来；
2. 正好踩在刻度上时顺延一格，不在同一秒里跑两轮；
3. 间隔比 60 小（换班组的 10 秒加密窗口）时按取模处理，仍然覆盖用户指定的秒；
4. 每轮都有执行耗时时，刻度不漂移。

跑法：python scripts/test_scheduler_tick.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["CDT_DATA_DIR"] = tempfile.mkdtemp(prefix="cdt-tick-")
os.environ["CDT_DRY_RUN"] = "1"

from app import db, repo, scheduler  # noqa: E402


class TickAlignment(unittest.TestCase):
    """纯函数：下一轮该在什么时候开始。"""

    def test_aligns_to_offset(self) -> None:
        tick = scheduler._next_tick(1000.0, 60, 18)  # noqa: SLF001
        self.assertGreater(tick, 1000.0)
        self.assertEqual(tick % 60, 18)
        self.assertLessEqual(tick - 1000.0, 60)

    def test_offset_zero_is_minute_boundary(self) -> None:
        tick = scheduler._next_tick(1000.5, 60, 0)  # noqa: SLF001
        self.assertEqual(tick, 1020.0)

    def test_exact_slot_rolls_to_next(self) -> None:
        """正好踩在刻度上：顺延一格，避免同一秒跑两轮。"""
        tick = scheduler._next_tick(1038.0, 60, 18)  # noqa: SLF001
        self.assertEqual(tick, 1098.0)

    def test_small_interval_takes_modulo(self) -> None:
        """加密窗口 interval=10、offset=18 → 取模成第 8 秒。"""
        tick = scheduler._next_tick(1000.0, 10, 18)  # noqa: SLF001
        self.assertEqual(tick, 1008.0)
        self.assertEqual(tick % 10, 8)

    def test_non_minute_interval_is_fixed_grid(self) -> None:
        """间隔不是 60 的倍数时退化成等长网格，秒位相对网格起点仍然固定。"""
        tick = scheduler._next_tick(1000.0, 90, 20)  # noqa: SLF001
        self.assertEqual(tick, 1010.0)
        self.assertEqual(tick % 90, 20)

    def test_always_in_future_and_within_interval(self) -> None:
        """扫一遍小数秒：任何时候算出来的刻度都在未来、且不超过一个间隔。"""
        for now in (999.0, 999.99, 1000.0, 1000.01, 1059.5, 1060.0, 1060.001):
            for interval, offset in ((60, 18), (60, 0), (30, 45), (10, 18), (3600, 59)):
                tick = scheduler._next_tick(now, interval, offset)  # noqa: SLF001
                self.assertGreater(tick, now, f"now={now} {interval}/{offset}")
                self.assertLessEqual(
                    tick - now, max(interval, 1) + 0.001, f"now={now} {interval}/{offset}"
                )

    def test_no_drift_across_rounds(self) -> None:
        """每轮假设执行耗时 3 秒：刻度始终落在第 18 秒，相邻差正好 60。"""
        now = 1000.0
        ticks: list[float] = []
        for _ in range(6):
            now = scheduler._next_tick(now, 60, 18)  # noqa: SLF001
            ticks.append(now)
            now += 3.0  # 本轮跑了 3 秒
        for tick in ticks:
            self.assertEqual(tick % 60, 18, ticks)
        gaps = {round(b - a) for a, b in zip(ticks, ticks[1:])}
        self.assertEqual(gaps, {60}, ticks)

    def test_slow_round_skips_slot_instead_of_piling_up(self) -> None:
        """一轮跑了 70 秒（超一个间隔）：跳到下一个刻度，不补跑、不堆积。"""
        first = scheduler._next_tick(1000.0, 60, 18)  # noqa: SLF001
        # 从 :18 起跑了 70 秒，结束时已经过了下一个 :18
        second = scheduler._next_tick(first + 70.0, 60, 18)  # noqa: SLF001
        self.assertEqual(second % 60, 18)
        self.assertEqual(second - first, 120.0)


class OffsetSetting(unittest.TestCase):
    """读设置：缺省 / 越界 / 脏值都得退回 0，不能让调度卡住。"""

    def setUp(self) -> None:
        db.init_db()

    def _set(self, value: str) -> None:
        db.execute(
            "INSERT INTO settings(key, value) VALUES('reconcile_offset', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )

    def test_reads_configured_value(self) -> None:
        self._set("18")
        self.assertEqual(scheduler._offset(), 18)  # noqa: SLF001

    def test_default_is_zero(self) -> None:
        db.execute("DELETE FROM settings WHERE key='reconcile_offset'")
        self.assertEqual(scheduler._offset(), 0)  # noqa: SLF001

    def test_clamps_out_of_range(self) -> None:
        self._set("200")
        self.assertEqual(scheduler._offset(), 59)  # noqa: SLF001
        self._set("-5")
        self.assertEqual(scheduler._offset(), 0)  # noqa: SLF001

    def test_garbage_falls_back_to_zero(self) -> None:
        self._set("abc")
        self.assertEqual(scheduler._offset(), 0)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
