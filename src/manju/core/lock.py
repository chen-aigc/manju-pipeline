# -*- coding: utf-8 -*-
"""任务锁：项目级文件锁 + 心跳 + 死锁自愈。

设计要点：
    1. 原子创建（O_EXCL），不是"先检查再写"
    2. 锁里存 PID + 心跳时间
    3. 心跳超过 timeout 分钟 → 判定 stale，可接管
    4. 进程被杀不会永久卡死
"""
import json
import os
import socket
import time
from pathlib import Path

from . import atomic


DEFAULT_TIMEOUT_MIN = 3
HEARTBEAT_INTERVAL = 30      # 秒


class LockError(Exception):
    pass


class LockBusy(LockError):
    """锁被别人占着。"""

    def __init__(self, info):
        self.info = info or {}
        super().__init__(
            "项目正在运行中（进程 %s，已跑 %s 分钟）"
            % (self.info.get("pid", "?"),
               int((time.time() - self.info.get("started_ts", time.time())) / 60)))


class ProjectLock:
    """项目锁。

    用法：
        with ProjectLock(project_dir) as lk:
            ... 干活 ...
    """

    def __init__(self, project_dir, job_id="", timeout_min=None, wait_max_min=0):
        self.path = Path(project_dir) / ".lock"
        self.project = Path(project_dir).name
        self.job_id = job_id or ("job_" + os.urandom(3).hex())
        self.timeout_min = (timeout_min
                            if timeout_min is not None
                            else DEFAULT_TIMEOUT_MIN)
        self.wait_max_min = wait_max_min
        self._held = False
        self._last_hb = 0.0

    # ---------------- 状态判断 ----------------
    def read_info(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def is_stale(self, info=None):
        """心跳超过 timeout_min 没更新 → 视为死锁。"""
        info = info if info is not None else self.read_info()
        if not info:
            return True
        hb = float(info.get("heartbeat_ts") or info.get("started_ts") or 0)
        return (time.time() - hb) > self.timeout_min * 60

    def owner_alive(self, info=None):
        """同机检查 PID 是否还活着。"""
        info = info if info is not None else self.read_info()
        if info.get("hostname") != socket.gethostname():
            return True          # 别的机器，无法判断，按活着算
        pid = info.get("pid")
        if not pid:
            return False
        try:
            if os.name == "nt":
                import subprocess
                r = subprocess.run(
                    ["tasklist", "/FI", "PID eq %s" % pid, "/NH"],
                    capture_output=True, text=True, timeout=10)
                return str(pid) in (r.stdout or "")
            os.kill(int(pid), 0)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------------- 获取/释放 ----------------
    def acquire(self, force=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        deadline = time.time() + self.wait_max_min * 60
        while True:
            try:
                self._create_exclusive()
                return self
            except FileExistsError:
                info = self.read_info()
                can_take = force or self.is_stale(info) or \
                    not self.owner_alive(info)

                if can_take:
                    print("[lock] 接管陈旧锁（原进程 %s，心跳 %s）" % (
                        info.get("pid", "?"),
                        info.get("heartbeat", "未知")))
                    self._break_lock()
                    continue

                if time.time() >= deadline:
                    raise LockBusy(info)

                print("[lock] 项目被占用，等待中……")
                time.sleep(min(30, max(5, self.timeout_min * 60 / 10)))

    def _create_exclusive(self):
        """原子创建锁文件（O_EXCL）。"""
        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            info = self._payload()
            os.write(fd, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        self._held = True
        self._last_hb = time.time()

    def _payload(self):
        now = time.time()
        return {
            "job_id": self.job_id,
            "project": self.project,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_ts": getattr(self, "_started_ts", now),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(
                getattr(self, "_started_ts", now))),
            "heartbeat_ts": now,
            "heartbeat": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _break_lock(self):
        try:
            self.path.unlink()
        except OSError:
            pass

    def heartbeat(self, force=False):
        """更新心跳。间隔小于 HEARTBEAT_INTERVAL 且非强制时跳过。"""
        if not self._held:
            return
        now = time.time()
        if not force and (now - self._last_hb) < HEARTBEAT_INTERVAL:
            return
        info = self.read_info()
        if info.get("job_id") != self.job_id:
            # 锁已经不是我们的了，别覆盖
            self._held = False
            raise LockError("锁已被其他进程接管")
        info.update({
            "heartbeat_ts": now,
            "heartbeat": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        atomic.atomic_write_text(
            self.path, json.dumps(info, ensure_ascii=False, indent=2))
        self._last_hb = now

    def release(self):
        if not self._held:
            return
        info = self.read_info()
        if info.get("job_id") == self.job_id:
            self._break_lock()
        self._held = False

    # ---------------- 上下文管理 ----------------
    def __enter__(self):
        if not hasattr(self, "_started_ts"):
            self._started_ts = time.time()
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def __repr__(self):
        return "<ProjectLock %s held=%s>" % (self.project, self._held)


def list_locks(root):
    """扫描所有项目的锁，返回 [(项目, 信息, 是否 stale)]。"""
    root = Path(root)
    out = []
    if not root.exists():
        return out
    for f in root.glob("*/.lock"):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            info = {}
        lk = ProjectLock(f.parent)
        out.append((f.parent.name, info, lk.is_stale(info)))
    return out


def unlock(project_dir, force=False):
    """手动清锁。force=True 时不检查是否活着。"""
    lk = ProjectLock(project_dir)
    info = lk.read_info()
    if not info:
        return False, "没有锁"
    if not force and not lk.is_stale(info) and lk.owner_alive(info):
        return False, "锁仍被活着的进程 %s 持有，加 --force 强制清" % info.get("pid")
    lk._break_lock()
    return True, "已清除（原进程 %s）" % info.get("pid", "?")
