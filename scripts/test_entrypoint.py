#!/usr/bin/env python3
"""docker-entrypoint.py 的单元测试。

本机是 Windows，os.setuid/setgid 根本不存在，真机验证不了降权那段。
这里用 mock 把 syscall 挡住，专门盯几件容易写错、写错了又只在容器里才炸的事：

  * 降权的调用顺序（必须是 setgroups → setgid → setuid，顺序反了就降不下去）
  * chown 递归是不是真的递归到了子条目、符号链接有没有被跟过去
  * chown 遇到 EPERM（NFS root_squash）时不能把启动流程掀翻
  * 探针写不进去时要干净退出，而不是抛 traceback

跑法：python scripts/test_entrypoint.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("docker_entrypoint", ROOT / "docker-entrypoint.py")
entry = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(entry)


class EnvIdTest(unittest.TestCase):
    def test_default_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(entry.env_id("PUID", 10001), 10001)

    def test_default_when_blank(self) -> None:
        with mock.patch.dict(os.environ, {"PUID": "   "}, clear=True):
            self.assertEqual(entry.env_id("PUID", 10001), 10001)

    def test_default_when_not_integer(self) -> None:
        with mock.patch.dict(os.environ, {"PUID": "abc"}, clear=True):
            self.assertEqual(entry.env_id("PUID", 10001), 10001)

    def test_default_when_negative(self) -> None:
        with mock.patch.dict(os.environ, {"PGID": "-1"}, clear=True):
            self.assertEqual(entry.env_id("PGID", 10001), 10001)

    def test_accepts_value(self) -> None:
        with mock.patch.dict(os.environ, {"PUID": "1000"}, clear=True):
            self.assertEqual(entry.env_id("PUID", 10001), 1000)


class ChownTreeTest(unittest.TestCase):
    def test_recurses_into_children(self) -> None:
        """顶层已经对了，子条目仍然要被检查 —— 否则老数据目录里的 root 文件永远修不好。"""
        # 用 str(Path(...)) 拼键：Windows 上 "/data" 会被规范成 "\data"，硬编码字符串对不上
        root = Path("/data")
        logs = str(root / "logs")
        db = str(root / "cdt-management.db")
        stat_by_path = {
            str(root): mock.Mock(st_uid=10001, st_gid=10001),
            logs: mock.Mock(st_uid=0, st_gid=0),
            db: mock.Mock(st_uid=0, st_gid=0),
        }
        chowned: list[str] = []

        def fake_walk(top, topdown=True, followlinks=False):
            yield str(root), ["logs"], ["cdt-management.db"]

        with mock.patch.object(entry.os, "lstat", side_effect=lambda p: stat_by_path[p]), \
             mock.patch.object(entry.os, "walk", side_effect=fake_walk), \
             mock.patch.object(entry.os, "chown", side_effect=lambda p, u, g, **kw: chowned.append(p), create=True):
            changed = entry.chown_tree(root, 10001, 10001)

        self.assertEqual(changed, 2)
        self.assertIn(logs, chowned)
        self.assertIn(db, chowned)
        self.assertNotIn(str(root), chowned)

    def test_survives_permission_error(self) -> None:
        """NFS 开 root_squash 时 root 也 chown 不动，此时不能抛异常掀翻启动。"""
        def fake_walk(top, topdown=True, followlinks=False):
            yield str(Path("/data")), [], []

        with mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=0, st_gid=0)), \
             mock.patch.object(entry.os, "walk", side_effect=fake_walk), \
             mock.patch.object(entry.os, "chown", side_effect=PermissionError("EPERM"), create=True):
            changed = entry.chown_tree(Path("/data"), 10001, 10001)

        self.assertEqual(changed, 0)

    def test_does_not_follow_symlinks(self) -> None:
        """符号链接只能改链接本身，跟进去就可能改到宿主机上别的目录。"""
        calls: list[dict] = []

        def fake_walk(top, topdown=True, followlinks=False):
            yield str(Path("/data")), ["link"], []

        with mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=0, st_gid=0)), \
             mock.patch.object(entry.os, "walk", side_effect=fake_walk), \
             mock.patch.object(entry.os, "chown", side_effect=lambda p, u, g, **kw: calls.append(kw), create=True):
            entry.chown_tree(Path("/data"), 10001, 10001)

        self.assertTrue(calls)
        for kwargs in calls:
            self.assertIs(kwargs.get("follow_symlinks"), False)


class ChownTreeScopeTest(unittest.TestCase):
    """deep=False 是常态路径上的优化，但绝不能因此漏掉第一层的错误属主。"""

    def test_shallow_only_touches_first_level(self) -> None:
        chowned: list[str] = []
        root = Path("/data")
        logs = str(root / "logs")

        def fake_walk(*a, **kw):  # 浅模式不该调用 walk
            raise AssertionError("deep=False 时不该递归遍历")

        with mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=0, st_gid=0)), \
             mock.patch.object(entry.os, "listdir", return_value=["logs", "cdt-management.db"]), \
             mock.patch.object(entry.os, "walk", side_effect=fake_walk), \
             mock.patch.object(entry.os, "chown", side_effect=lambda p, u, g, **kw: chowned.append(p), create=True):
            changed = entry.chown_tree(root, 10001, 10001, deep=False)

        self.assertEqual(changed, 3)  # root 本身 + 两个子条目
        self.assertIn(logs, chowned)
        self.assertEqual(len(chowned), 3)

    def test_missing_dir_does_not_raise(self) -> None:
        with mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=0, st_gid=0)), \
             mock.patch.object(entry.os, "listdir", side_effect=PermissionError("EACCES")), \
             mock.patch.object(entry.os, "chown", create=True):
            self.assertEqual(entry.chown_tree(Path("/data"), 10001, 10001, deep=False), 1)


class DropPrivilegesTest(unittest.TestCase):
    def test_order_matters(self) -> None:
        """必须 setgroups → setgid → setuid。setuid 之后再 setgid 就已经没权限了。"""
        order: list[str] = []

        with mock.patch.object(entry.os, "setgroups", side_effect=lambda g: order.append("setgroups"), create=True), \
             mock.patch.object(entry.os, "setgid", side_effect=lambda g: order.append("setgid"), create=True), \
             mock.patch.object(entry.os, "setuid", side_effect=lambda u: order.append("setuid"), create=True):
            entry.drop_privileges(10001, 10001)

        self.assertEqual(order, ["setgroups", "setgid", "setuid"])

    def test_setgroups_failure_is_not_fatal(self) -> None:
        with mock.patch.object(entry.os, "setgroups", side_effect=OSError("not permitted"), create=True), \
             mock.patch.object(entry.os, "setgid", create=True) as setgid, \
             mock.patch.object(entry.os, "setuid", create=True) as setuid:
            entry.drop_privileges(10001, 10001)

        setgid.assert_called_once_with(10001)
        setuid.assert_called_once_with(10001)


class ProbeTest(unittest.TestCase):
    def test_probe_leaves_nothing_behind(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(entry.probe_writable(Path(tmp)))
            self.assertEqual(os.listdir(tmp), [])

    def test_probe_returns_false_on_failure(self) -> None:
        with mock.patch.object(Path, "write_bytes", side_effect=PermissionError("EACCES")):
            self.assertFalse(entry.probe_writable(Path(".")))


class MainTest(unittest.TestCase):
    def test_exits_when_data_dir_is_a_file(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile() as handle:
            with mock.patch.dict(os.environ, {"CDT_DATA_DIR": handle.name}, clear=False), \
                 mock.patch.object(entry, "POSIX", False):
                self.assertEqual(entry.main(), 1)

    def test_root_path_drops_privileges_then_execs(self) -> None:
        """root 进来 → 改属主 → 降权 → exec。任何一步顺序错了业务就会以 root 跑。"""
        events: list[str] = []

        with mock.patch.dict(os.environ, {"CDT_DATA_DIR": "/data", "PUID": "1000", "PGID": "1000"}, clear=False), \
             mock.patch.object(entry, "POSIX", True), \
             mock.patch.object(entry.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(entry.os, "getuid", return_value=1000, create=True), \
             mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=0, st_gid=0), create=True), \
             mock.patch.object(entry.Path, "exists", return_value=True), \
             mock.patch.object(entry.Path, "is_dir", return_value=True), \
             mock.patch.object(entry, "chown_tree", side_effect=lambda *a, **kw: events.append("chown") or 1), \
             mock.patch.object(entry, "drop_privileges", side_effect=lambda u, g: events.append(f"drop:{u}:{g}")), \
             mock.patch.object(entry, "probe_writable", side_effect=lambda p: events.append("probe") or True), \
             mock.patch.object(entry.os, "execvp", side_effect=lambda c, a: events.append(f"exec:{c}")), \
             mock.patch.object(sys, "argv", ["entrypoint.py"]):
            code = entry.main()

        self.assertEqual(code, 0)
        self.assertEqual(events, ["chown", "drop:1000:1000", "probe", f"exec:{sys.executable}"])

    def test_non_root_skips_chown(self) -> None:
        events: list[str] = []

        with mock.patch.dict(os.environ, {"CDT_DATA_DIR": "/data"}, clear=False), \
             mock.patch.object(entry, "POSIX", True), \
             mock.patch.object(entry.os, "geteuid", return_value=10001, create=True), \
             mock.patch.object(entry.os, "getuid", return_value=10001, create=True), \
             mock.patch.object(entry.Path, "exists", return_value=True), \
             mock.patch.object(entry.Path, "is_dir", return_value=True), \
             mock.patch.object(entry, "chown_tree", side_effect=lambda *a: events.append("chown")), \
             mock.patch.object(entry, "probe_writable", side_effect=lambda p: True), \
             mock.patch.object(entry.os, "execvp", side_effect=lambda c, a: events.append("exec")), \
             mock.patch.object(sys, "argv", ["entrypoint.py"]):
            code = entry.main()

        self.assertEqual(code, 0)
        self.assertEqual(events, ["exec"])


    def test_aligned_top_level_uses_shallow_scan(self) -> None:
        """属主已经对的时候只扫第一层，别每次启动都遍历整个数据目录。"""
        deep_args: list[bool] = []

        with mock.patch.dict(os.environ, {"CDT_DATA_DIR": "/data"}, clear=False), \
             mock.patch.object(entry, "POSIX", True), \
             mock.patch.object(entry.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(entry.os, "getuid", return_value=10001, create=True), \
             mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=10001, st_gid=10001), create=True), \
             mock.patch.object(entry.Path, "exists", return_value=True), \
             mock.patch.object(entry.Path, "is_dir", return_value=True), \
             mock.patch.object(entry, "chown_tree", side_effect=lambda r, u, g, deep=True: deep_args.append(deep) or 0), \
             mock.patch.object(entry, "drop_privileges"), \
             mock.patch.object(entry, "probe_writable", return_value=True), \
             mock.patch.object(entry.os, "execvp"), \
             mock.patch.object(sys, "argv", ["entrypoint.py"]):
            self.assertEqual(entry.main(), 0)

        self.assertEqual(deep_args, [False])

    def test_misaligned_top_level_uses_deep_scan(self) -> None:
        """宿主机目录刚开始是 root 的，必须递归整棵树，否则老的 db/日志还是改不动。"""
        deep_args: list[bool] = []

        with mock.patch.dict(os.environ, {"CDT_DATA_DIR": "/data"}, clear=False), \
             mock.patch.object(entry, "POSIX", True), \
             mock.patch.object(entry.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(entry.os, "getuid", return_value=10001, create=True), \
             mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=0, st_gid=0), create=True), \
             mock.patch.object(entry.Path, "exists", return_value=True), \
             mock.patch.object(entry.Path, "is_dir", return_value=True), \
             mock.patch.object(entry, "chown_tree", side_effect=lambda r, u, g, deep=True: deep_args.append(deep) or 3), \
             mock.patch.object(entry, "drop_privileges"), \
             mock.patch.object(entry, "probe_writable", return_value=True), \
             mock.patch.object(entry.os, "execvp"), \
             mock.patch.object(sys, "argv", ["entrypoint.py"]):
            self.assertEqual(entry.main(), 0)

        self.assertEqual(deep_args, [True])

    def test_unwritable_dir_exits_with_code_1(self) -> None:
        """写不进去要干净退出，别让应用在后面抛一堆 traceback。"""
        with mock.patch.dict(os.environ, {"CDT_DATA_DIR": "/data"}, clear=False), \
             mock.patch.object(entry, "POSIX", True), \
             mock.patch.object(entry.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(entry.os, "getuid", return_value=10001, create=True), \
             mock.patch.object(entry.os, "lstat", return_value=mock.Mock(st_uid=10001, st_gid=10001), create=True), \
             mock.patch.object(entry.Path, "exists", return_value=True), \
             mock.patch.object(entry.Path, "is_dir", return_value=True), \
             mock.patch.object(entry, "chown_tree", return_value=0), \
             mock.patch.object(entry, "drop_privileges"), \
             mock.patch.object(entry, "probe_writable", return_value=False), \
             mock.patch.object(entry.os, "execvp") as execvp, \
             mock.patch.object(sys, "argv", ["entrypoint.py"]):
            self.assertEqual(entry.main(), 1)

        execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
