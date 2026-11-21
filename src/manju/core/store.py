# -*- coding: utf-8 -*-
"""多进程共享状态：文件改动自动重载。

问题：
    CLI 进程和 Web 进程各自维护一份内存缓存，
    只在启动时读一次文件。一方改了，另一方不知道。

方案：
    store 包装类负责"读前检查 mtime"，变了就重载。
    写入时用原子替换（先写 tmp 再 rename），避免半截文件。

用法：
    store = JsonStore(path, default={})

    # 读（会自动同步外部改动）
    with store.read() as data:
        data["key"] = value
        # 退出 with 时自动保存

    # 或手动
    data = store.load()
    data["x"] = 1
    store.save(data)

    # 只读不写
    data = store.peek()      # 不触发重载检查的快速读

设计要点：
    1. mtime + size 双重检查（mtime 精度不够时用 size 兜底）
    2. 读-改-写 在锁内完成，避免丢更新
    3. 写入用 atomic_write（跨进程安全）
    4. 文件不存在时返回默认值
    5. 损坏文件不抛异常，用默认值 + 备份
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


# ---------------- 跨进程文件锁 ----------------
class _FileLock:
    """跨进程排他锁。

    Windows: msvcrt.locking
    Linux/Mac: fcntl.flock

    用法：
        with _FileLock(path):
            # 临界区
    """

    def __init__(self, lock_path, timeout=10.0):
        self.lock_path = str(lock_path) if lock_path else None
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        if not self.lock_path:
            return self
        deadline = time.time() + self.timeout
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                self._fh = open(self.lock_path, "a+b")
                self._acquire(self._fh)
                return self
            except (OSError, IOError):
                if self._fh:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
                if time.time() > deadline:
                    raise TimeoutError(
                        "获取文件锁超时：%s" % self.lock_path)
                time.sleep(0.01 + 0.02 * (time.time() % 1))

    def _acquire(self, fh):
        try:
            import msvcrt
            # 锁第一个字节，非阻塞
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self, fh):
        try:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type, exc, tb):
        if self._fh:
            try:
                self._release(self._fh)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        return False


class JsonStore:
    """带外部同步的 JSON 存储。"""

    def __init__(self, path, default=None, validate=None):
        """
        Args:
            path:     JSON 文件路径
            default:  文件不存在时的默认值（callable 或值）
            validate: 校验函数 f(data) -> (ok, msg)，不通过则拒绝保存
        """
        self.path = Path(path) if path else None
        self._default = default if default is not None else {}
        self._validate = validate
        self._lock = threading.RLock()
        self._data = None
        self._stamp = None      # (mtime_ns, size)
        self._save_count = 0
        self._reload_count = 0
        # 跨进程锁文件：和 JSON 同目录，名字是 .xxx.lock
        if self.path:
            self._flock_path = self.path.with_suffix(
                self.path.suffix + ".lock")
        else:
            self._flock_path = None

    def _flock(self):
        return _FileLock(self._flock_path, timeout=15.0)

    # ---------------- 内部：检查文件是否变了 ----------------
    def _current_stamp(self):
        if not self.path or not self.path.exists():
            return None
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _make_default(self):
        d = self._default
        if callable(d):
            try:
                return d()
            except Exception:  # noqa: BLE001
                return {}
        # 深拷贝，避免共享引用
        import copy
        return copy.deepcopy(d)

    def _load_from_disk(self):
        """从磁盘读。失败返回默认值。"""
        if not self.path or not self.path.exists():
            return self._make_default()
        try:
            text = self.path.read_text(encoding="utf-8")
            if not text.strip():
                return self._make_default()
            return json.loads(text)
        except json.JSONDecodeError:
            # 损坏：备份后返回默认值
            try:
                bak = self.path.with_suffix(
                    self.path.suffix + ".corrupt.%d" % int(time.time()))
                self.path.replace(bak)
                print("[store] %s 损坏，已备份到 %s"
                      % (self.path.name, bak.name))
            except OSError:
                pass
            return self._make_default()
        except OSError as exc:
            print("[store] 读取 %s 失败：%r" % (self.path, exc))
            return self._make_default()

    def _sync_locked(self):
        """同步外部改动（调用方已持锁）。"""
        stamp = self._current_stamp()
        if self._data is not None and stamp == self._stamp:
            return self._data       # 没变，直接用缓存

        self._data = self._load_from_disk()
        self._stamp = stamp
        self._reload_count += 1
        return self._data

    # ---------------- 公开 API ----------------
    def load(self):
        """读（自动同步外部改动）。返回内部引用，改了要调 save()。"""
        with self._lock:
            return self._sync_locked()

    def peek(self):
        """快速读（不检查外部改动）。"""
        with self._lock:
            if self._data is None:
                return self._sync_locked()
            return self._data

    def save(self, data=None):
        """写回磁盘（跨进程原子的读-合并-写）。

        注意：如果两个进程都调 save()，后写的会覆盖先写的。
        要避免覆盖，用 update()。
        """
        with self._lock:
            if data is not None:
                self._data = data

            with self._flock():
                return self._save_unlocked()

    def update(self, fn):
        """原子读-改-写（跨进程安全）。

        Args:
            fn: f(data) -> data | None。返回 None 表示不保存。

        Returns:
            (ok, result)
        """
        with self._lock:
            # 跨进程锁包住整个 读-改-写
            with self._flock():
                # 锁内重新读，确保拿到最新
                self._data = self._load_from_disk()
                self._stamp = self._current_stamp()

                try:
                    result = fn(self._data)
                except Exception as exc:  # noqa: BLE001
                    return False, exc

                if result is None:
                    return True, self._data

                ok = self._save_unlocked()
                return ok, self._data

    def _save_unlocked(self):
        """保存（调用方已持跨进程锁）。"""
        if self._validate:
            try:
                ok, msg = self._validate(self._data)
                if not ok:
                    print("[store] 拒绝保存 %s：%s"
                          % (self.path.name if self.path else "?", msg))
                    return False
            except Exception as exc:  # noqa: BLE001
                print("[store] 校验异常：%r" % exc)
                return False

        if not self.path:
            return True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(
                self.path.suffix + ".tmp.%d.%d"
                % (os.getpid(), threading.get_ident()))
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(str(tmp), str(self.path))
            self._stamp = self._current_stamp()
            self._save_count += 1
            return True
        except Exception as exc:  # noqa: BLE001
            print("[store] 保存 %s 失败：%r" % (self.path, exc))
            return False

    def invalidate(self):
        """强制下次读到磁盘最新。"""
        with self._lock:
            self._data = None
            self._stamp = None

    def stats(self):
        with self._lock:
            return {
                "path": str(self.path) if self.path else None,
                "exists": bool(self.path and self.path.exists()),
                "loaded": self._data is not None,
                "saves": self._save_count,
                "reloads": self._reload_count,
                "stamp": self._stamp,
            }

    # ---------------- 上下文管理器 ----------------
    def __enter__(self):
        self._lock.acquire()
        self._data = self._sync_locked()
        return self._data

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.save()
        finally:
            self._lock.release()
        return False


class DeltaStore(JsonStore):
    """只增量的存储（如累计成本），避免读-改-写冲突。

    适合 spend 这类"只加不减"的数据：
        不用先读全量再改，直接原子地加一个 delta。
    """

    def add(self, path_keys, amount):
        """原子地把 amount 加到 data[path_keys[0]][path_keys[1]]..."""
        def _fn(data):
            cur = data
            for k in path_keys[:-1]:
                if not isinstance(cur.get(k), dict):
                    cur[k] = {}
                cur = cur[k]
            last = path_keys[-1]
            cur[last] = (cur.get(last) or 0) + amount
            return data       # 不返回 None，触发保存

        ok, _ = self.update(_fn)
        return ok
