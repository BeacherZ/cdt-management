#!/usr/bin/env python3
"""容器入口：先把数据目录交给运行用户，再降权拉起应用。

为什么需要这一层：
容器里的应用以非 root 运行（默认 uid 10001），而 bind mount 到宿主机的目录
通常由 root 创建、权限 0755。进程一上来就 `mkdir /data/logs`，直接 Permission denied，
容器反复重启，日志里全是 traceback。与其让每个部署的人先手敲一遍 chown
（NAS 上很多人压根没有 shell），不如让容器启动时自己修一次。

三种跑法都能覆盖：
  docker run ...                          → root 启动，修好属主后降权到 10001
  docker run --user 10001:10001 ...       → 本来就是目标用户，跳过降权
  docker run -e PUID=1000 -e PGID=1000    → 按宿主机用户归属，NAS / 家目录部署常用

应用进程拿到的是干净的 uid 10001，不会以 root 身份跑业务代码。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

DEFAULT_UID = 10001
DEFAULT_GID = 10001
POSIX = hasattr(os, "geteuid")


def log(message: str) -> None:
    print(f"[entrypoint] {message}", flush=True)


def env_id(name: str, default: int) -> int:
    """读 PUID / PGID 这类数值环境变量，脏值退回默认值而不是崩掉。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log(f"{name}={raw!r} 不是整数，忽略，用默认值 {default}")
        return default
    if value < 0:
        log(f"{name}={value} 不能为负，忽略，用默认值 {default}")
        return default
    return value


def chown_tree(root: Path, uid: int, gid: int, deep: bool = True) -> int:
    """把 root 交给 uid:gid，返回实际改动的条目数。

    deep=False 只扫第一层，用在「顶层属主已经对了」的常态路径上：
    每次启动都递归遍历整个数据目录没必要，日志攒多了还慢。
    但第一层仍然要扫 —— 常见错误是 `chown 10001 /data` 少了 -R，
    顶层对了而 /data/logs 还是 root 的。

    单个条目失败只记一条警告，不中断：NFS 开了 root_squash 时 root 也改不动
    别人家的文件，这种情况下更要紧的是别把启动流程卡死 —— 后面还有可写性探测兜底。
    """
    changed = 0
    failed = 0

    def one(path: str) -> None:
        nonlocal changed, failed
        try:
            stat = os.lstat(path)
        except FileNotFoundError:
            return
        except OSError:
            failed += 1
            return
        if stat.st_uid == uid and stat.st_gid == gid:
            return
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
            changed += 1
        except OSError:
            # 权限不够（root_squash）、只读挂载等；留给后面的探测给结论
            failed += 1

    one(str(root))
    for path in _children(root, deep):
        one(path)
    if failed:
        log(f"有 {failed} 个条目改不了属主（可能是只读挂载或 NFS root_squash）")
    return changed


def _children(root: Path, deep: bool) -> Iterator[str]:
    """列出 root 下的条目。深模式递归但绝不跟符号链接。"""
    if not deep:
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            yield os.path.join(str(root), name)
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in dirnames + filenames:
            yield os.path.join(dirpath, name)


def drop_privileges(uid: int, gid: int) -> None:
    """丢掉 root，之后进程再想提权也提不回来。"""
    try:
        os.setgroups([])
    except OSError:
        # 没权限清附加组时会失败，不影响主流程
        pass
    os.setgid(gid)
    os.setuid(uid)


def probe_writable(path: Path) -> bool:
    """用当前身份真写一个探针文件 —— os.access 在降权前会被 root 身份骗过去。"""
    probe = path / ".cdt-write-probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def fail_not_writable(path: Path, uid: int, gid: int) -> None:
    log(f"数据目录 {path} 不可写，启动中止。")
    print(
        "\n"
        "容器内的应用以非 root 运行，挂载进来的宿主机目录必须让它可写。三种解法挑一个：\n"
        f"\n"
        f"  1) 在宿主机上把目录交给容器用户（最常见）\n"
        f"       sudo chown -R {uid}:{gid} {path}\n"
        f"\n"
        f"  2) 让容器按宿主机用户的身份运行，不动宿主机权限\n"
        f"       docker run -e PUID=$(id -u) -e PGID=$(id -g) ...\n"
        f"\n"
        f"  3) 换成 docker 具名卷，属主会随镜像初始化\n"
        f"       -v cdt-data:/data\n"
        f"\n"
        "用一键脚本（install.sh）安装会自动处理第 1 步。\n",
        file=sys.stderr,
        flush=True,
    )


def main() -> int:
    data_dir = Path(os.environ.get("CDT_DATA_DIR") or "/data").expanduser()
    try:
        data_dir = data_dir.resolve()
    except OSError:
        pass

    uid = env_id("PUID", DEFAULT_UID)
    gid = env_id("PGID", DEFAULT_GID)
    is_root = POSIX and os.geteuid() == 0

    # 1. 目录得先存在。root 建，或者已经是目标用户自己建。
    if not data_dir.exists():
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log(f"创建数据目录 {data_dir} 失败：{exc}")
            fail_not_writable(data_dir, uid, gid)
            return 1

    if not data_dir.is_dir():
        log(f"{data_dir} 存在但不是目录，检查一下挂载参数")
        return 1

    # 2. 只有 root 能改属主。改完立刻降权，业务代码永远不以 root 跑。
    if is_root:
        # 顶层已经是目标属主时只扫第一层（快）；不对才递归整棵（首次启动/换 PUID 时）
        try:
            top = os.lstat(data_dir)
            aligned = top.st_uid == uid and top.st_gid == gid
        except OSError:
            aligned = False
        changed = chown_tree(data_dir, uid, gid, deep=not aligned)
        if changed:
            scope = "第一层" if aligned else "整棵目录树"
            log(f"{data_dir} 的属主已调整为 {uid}:{gid}（扫了{scope}，改动 {changed} 个条目）")
        drop_privileges(uid, gid)
        if os.getuid() != uid:
            log(f"降权失败，当前仍是 uid {os.getuid()}")
            return 1
    else:
        who = f"uid {os.getuid()}" if POSIX else "非 POSIX 环境（本地调试）"
        log(f"以 {who} 运行，跳过属主调整")

    # 3. 用真实身份探一次可写性，写不进去就别让应用再抛一堆 traceback。
    if not probe_writable(data_dir):
        fail_not_writable(data_dir, uid, gid)
        return 1

    # 用 sys.executable 而不是裸 "python"：容器里两者等价，但本地用虚拟环境跑时
    # 裸 "python" 会解析到系统解释器，装好的依赖一个都看不见。
    command = sys.argv[1:] or [sys.executable, "-m", "app.main"]
    # exec 换掉当前进程：SIGTERM 直达应用，容器 stop 不会留下僵着的父进程。
    os.execvp(command[0], command)
    return 0  # 走不到


if __name__ == "__main__":
    sys.exit(main())
