# -*- coding: utf-8 -*-
"""原子写入与安全落盘。

核心原则：主文件要么不存在，要么完整 —— 永远不会是半截。

做法：先写 .tmp → 校验 → 原子改名。
断电时只可能留下 .tmp，不会留下损坏的主文件。
"""
import os
import shutil
import tempfile
import threading
from pathlib import Path

TMP_SUFFIX = ".tmp"
CORRUPT_DIR = "_corrupt"

_TMP_SEQ = [0]
_TMP_LOCK = threading.Lock()


def tmp_path(target):
    """返回目标文件对应的临时路径（同目录，保证同盘改名）。

    名字带 进程号 + 线程号 + 序号，避免多线程同时写同一目标时互相覆盖
    （Windows 下会出现 PermissionError）。
    """
    target = Path(target)
    with _TMP_LOCK:
        _TMP_SEQ[0] += 1
        seq = _TMP_SEQ[0]
    token = "%d_%d_%d" % (os.getpid(), threading.get_ident() % 100000, seq)
    return target.with_name("%s.%s%s" % (target.name, token, TMP_SUFFIX))


def atomic_write_bytes(target, data, verify=None):
    """原子写入字节。

    verify: 可选回调 verify(tmp_path) -> (ok, reason)
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path(target)

    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass  # 某些文件系统不支持 fsync

    if verify is not None:
        ok, reason = verify(tmp)
        if not ok:
            tmp.unlink(missing_ok=True)
            raise IOError("写入校验失败：%s（%s）" % (target.name, reason))

    tmp.replace(target)
    return target


def atomic_write_text(target, text, encoding="utf-8", verify=None):
    return atomic_write_bytes(target, text.encode(encoding), verify=verify)


def atomic_copy(src, target, verify=None):
    """把已有文件原子复制到目标（先拷到 tmp 再改名）。"""
    src = Path(src)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path(target)
    shutil.copyfile(src, tmp)

    if verify is not None:
        ok, reason = verify(tmp)
        if not ok:
            tmp.unlink(missing_ok=True)
            raise IOError("复制校验失败：%s（%s）" % (target.name, reason))

    tmp.replace(target)
    return target


def link_or_copy(src, target, verify=None):
    """优先用硬链接（不占额外空间），失败才复制。

    适用场景：同一份成片需要两个名字（插件产物 + 标准命名）。
    硬链接在同一卷上零额外空间，改一个两个都变。

    Returns:
        ("link" | "copy" | "skip", Path)
    """
    import os

    src = Path(src)
    target = Path(target)
    if not src.exists():
        raise FileNotFoundError(str(src))

    target.parent.mkdir(parents=True, exist_ok=True)

    # 已存在且大小一致、目标更新或相同 → 跳过
    try:
        if target.exists():
            ss, ts = src.stat(), target.stat()
            if ss.st_size == ts.st_size and ts.st_mtime >= ss.st_mtime - 1:
                return "skip", target
            target.unlink()
    except OSError:
        pass

    # 尝试硬链接
    try:
        os.link(src, target)
        return "link", target
    except (OSError, AttributeError, NotImplementedError):
        pass

    # 退回复制
    return "copy", atomic_copy(src, target, verify)


def atomic_save_via_tmpdir(writer, target, suffix=None, verify=None):
    """让 writer(tmp_path) 自己写，再校验改名。

    适合 ffmpeg 这类只能输出到文件的场景。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path(target)
    if suffix:
        tmp = tmp.with_name(tmp.stem + suffix)

    writer(tmp)

    if not tmp.exists():
        raise IOError("生成失败：%s 未产出" % target.name)

    if verify is not None:
        ok, reason = verify(tmp)
        if not ok:
            tmp.unlink(missing_ok=True)
            raise IOError("产出校验失败：%s（%s）" % (target.name, reason))

    tmp.replace(target)
    return target


def cleanup_tmp(root):
    """启动时清理所有残留 .tmp 文件。返回清理数量。"""
    root = Path(root)
    if not root.exists():
        return 0
    n = 0
    for f in root.rglob("*" + TMP_SUFFIX):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def quarantine(path, root):
    """把损坏文件移到 _corrupt/ 目录（保留，不删）。"""
    path = Path(path)
    root = Path(root)
    dest_dir = root / CORRUPT_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / path.name
    if dest.exists():
        i = 1
        while True:
            cand = dest_dir / ("%s_%d%s" % (path.stem, i, path.suffix))
            if not cand.exists():
                dest = cand
                break
            i += 1

    try:
        shutil.move(str(path), str(dest))
        return dest
    except OSError:
        return None


def archive_stale(path, history_dir, stamp=None):
    """把要覆盖的旧文件归档到 history/，不直接删。"""
    path = Path(path)
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)

    import time as _time
    stamp = stamp or _time.strftime("%Y%m%d_%H%M%S")
    dest = history_dir / ("%s_%s%s" % (path.stem, stamp, path.suffix))

    try:
        shutil.move(str(path), str(dest))
        return dest
    except OSError:
        return None


def safe_replace(src, target, history_dir=None, verify=None):
    """替换目标文件：旧的先归档，再原子换新。"""
    target = Path(target)
    if target.exists() and history_dir is not None:
        archive_stale(target, history_dir)
    return atomic_copy(src, target, verify=verify)


def temp_file(suffix="", dir=None):
    """创建一个临时文件路径（调用方负责删除）。"""
    fd, name = tempfile.mkstemp(suffix=suffix, dir=str(dir) if dir else None)
    os.close(fd)
    return Path(name)
