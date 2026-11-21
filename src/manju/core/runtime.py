# -*- coding: utf-8 -*-
"""运行中任务注册表：全局单例。

为什么单独一个模块：
    jobs.py 定义 JobManager 类，server.py 才创建 manager 实例。
    rollback_guard 需要在任何地方都能查到「现在有没有任务在跑」，
    所以用一个独立的进程级注册表，避免循环导入和实例查找。

用法：
    from manju.core.runtime import runtime

    # JobManager 里登记
    runtime.job_started(job_id, stage="image")
    runtime.job_finished(job_id)

    # 任何地方查询
    runtime.running_jobs()        # ["abc123", ...]
    runtime.is_busy()             # True/False
    runtime.describe()            # "1 个任务运行中：abc123（出图 5/40）"
"""
import threading
import time
from typing import Dict, List, Optional


class RuntimeRegistry:
    """进程级运行时注册表。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: Dict[str, dict] = {}
        self._started_ts = time.time()

    # ---------------- 任务登记 ----------------
    def job_started(self, job_id, project="", stages=None, **info):
        with self._lock:
            self._jobs[str(job_id)] = {
                "job_id": str(job_id),
                "project": project,
                "stages": list(stages or []),
                "started_at": time.time(),
                "stage": "",
                "progress": {"done": 0, "total": 0},
                **info,
            }

    def job_stage(self, job_id, stage, done=0, total=0):
        with self._lock:
            j = self._jobs.get(str(job_id))
            if j:
                j["stage"] = stage
                j["progress"] = {"done": done, "total": total}

    def job_finished(self, job_id):
        with self._lock:
            self._jobs.pop(str(job_id), None)

    # ---------------- 查询 ----------------
    def running_jobs(self):
        with self._lock:
            return list(self._jobs.keys())

    def is_busy(self):
        return bool(self.running_jobs())

    def get(self, job_id):
        with self._lock:
            return dict(self._jobs.get(str(job_id)) or {})

    def all(self):
        with self._lock:
            return [dict(v) for v in self._jobs.values()]

    def describe(self):
        """给人看的一句话。"""
        jobs = self.all()
        if not jobs:
            return "没有任务在运行"
        parts = []
        for j in jobs[:3]:
            stage = j.get("stage") or "?"
            prog = j.get("progress") or {}
            d, t = prog.get("done", 0), prog.get("total", 0)
            p = ""
            if t:
                p = " %d/%d" % (d, t)
            parts.append("%s（%s%s）" % (j.get("job_id", "?")[:8], stage, p))
        more = "" if len(jobs) <= 3 else " 等 %d 个" % len(jobs)
        return "%d 个任务运行中：%s%s" % (len(jobs), "；".join(parts), more)

    def uptime(self):
        return round(time.time() - self._started_ts, 1)


# 全局单例
runtime = RuntimeRegistry()
