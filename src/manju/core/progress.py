# -*- coding: utf-8 -*-
"""详细进度追踪：每个任务每一步都可见。

三层结构：
    Job（任务）
      └─ Stage（阶段：script / image / video / audio / bgm / edit）
           └─ Item（细项：镜 17、角色 林越、章节 3）

每层都有：
    status       pending / running / done / failed / skipped
    started_ts   开始时间
    finished_ts  结束时间
    elapsed      耗时
    cost         成本（元）
    error        错误信息
    detail       任意附加信息
    logs         该细项的日志（最多保留 N 条）

用法：
    from manju.core.progress import ProgressTracker

    pt = ProgressTracker()
    pt.start_job("job123", total_items=120)

    pt.start_stage("job123", "image", total=40)
    pt.start_item("job123", "image", "shot_017")
    ... 干活 ...
    pt.finish_item("job123", "image", "shot_017",
                   ok=True, cost=0.2, elapsed=12.3,
                   detail={"seed": 42, "path": "..."})
    pt.finish_stage("job123", "image")
    pt.finish_job("job123", ok=True)

    # 查询
    snap = pt.snapshot("job123")
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


MAX_LOGS_PER_ITEM = 20
MAX_RECENT_LOGS = 200


@dataclass
class ItemState:
    key: str
    label: str = ""
    status: str = "pending"
    started_ts: float = 0.0
    finished_ts: float = 0.0
    cost: float = 0.0
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)

    @property
    def elapsed(self):
        if not self.started_ts:
            return 0.0
        end = self.finished_ts or time.time()
        return round(end - self.started_ts, 2)

    def to_dict(self):
        return {
            "key": self.key,
            "label": self.label or self.key,
            "status": self.status,
            "elapsed": self.elapsed,
            "cost": round(self.cost, 4),
            "error": self.error,
            "detail": self.detail,
            "logs": self.logs[-MAX_LOGS_PER_ITEM:],
        }


@dataclass
class StageState:
    name: str
    status: str = "pending"
    started_ts: float = 0.0
    finished_ts: float = 0.0
    total: int = 0
    cost: float = 0.0
    error: str = ""
    items: Dict[str, ItemState] = field(default_factory=dict)

    @property
    def elapsed(self):
        if not self.started_ts:
            return 0.0
        end = self.finished_ts or time.time()
        return round(end - self.started_ts, 2)

    def counts(self):
        out = {"pending": 0, "running": 0, "done": 0,
               "failed": 0, "skipped": 0}
        for it in self.items.values():
            out[it.status] = out.get(it.status, 0) + 1
        return out

    def to_dict(self):
        c = self.counts()
        done = c.get("done", 0) + c.get("skipped", 0)
        total = self.total or len(self.items)
        return {
            "name": self.name,
            "status": self.status,
            "elapsed": self.elapsed,
            "cost": round(self.cost, 4),
            "error": self.error,
            "total": total,
            "counts": c,
            "done": done,
            "percent": round(done * 100.0 / total, 1) if total else 0.0,
            "eta_sec": self._eta(),
            "items": {k: v.to_dict() for k, v in self.items.items()},
        }

    def _eta(self):
        """估算剩余时间（基于已完成项的平均耗时）。"""
        done_items = [it for it in self.items.values()
                      if it.status in ("done", "skipped") and it.elapsed > 0]
        if not done_items:
            return None
        avg = sum(it.elapsed for it in done_items) / len(done_items)
        c = self.counts()
        remaining = (self.total or len(self.items)) - \
            (c.get("done", 0) + c.get("skipped", 0))
        if remaining <= 0:
            return 0
        return round(avg * remaining, 1)


@dataclass
class JobState:
    job_id: str
    status: str = "pending"
    started_ts: float = 0.0
    finished_ts: float = 0.0
    total_items: int = 0
    cost: float = 0.0
    error: str = ""
    stages: Dict[str, StageState] = field(default_factory=dict)
    recent_logs: List[str] = field(default_factory=list)

    @property
    def elapsed(self):
        if not self.started_ts:
            return 0.0
        end = self.finished_ts or time.time()
        return round(end - self.started_ts, 2)

    def current(self):
        """当前正在做什么（给前端显示）。"""
        for sname, st in self.stages.items():
            if st.status == "running":
                running = [it for it in st.items.values()
                           if it.status == "running"]
                if running:
                    return {
                        "stage": sname,
                        "item": running[0].label or running[0].key,
                        "item_key": running[0].key,
                        "elapsed": running[0].elapsed,
                    }
                return {"stage": sname, "item": "", "elapsed": st.elapsed}
        return None

    def overall(self):
        """总体进度。"""
        total = 0
        done = 0
        for st in self.stages.values():
            c = st.counts()
            total += st.total or len(st.items)
            done += c.get("done", 0) + c.get("skipped", 0)
        pct = round(done * 100.0 / total, 1) if total else 0.0

        # ETA：各阶段剩余时间之和
        eta = 0
        for st in self.stages.values():
            e = st._eta()
            if e is not None:
                eta += e
            else:
                eta = None
                break

        return {
            "total": total,
            "done": done,
            "percent": pct,
            "eta_sec": eta,
            "elapsed": self.elapsed,
            "cost": round(self.cost, 4),
        }

    def to_dict(self, include_items=True):
        out = {
            "job_id": self.job_id,
            "status": self.status,
            "elapsed": self.elapsed,
            "cost": round(self.cost, 4),
            "error": self.error,
            "current": self.current(),
            "overall": self.overall(),
            "stages": {},
            "recent_logs": self.recent_logs[-MAX_RECENT_LOGS:],
        }
        for sname, st in self.stages.items():
            d = st.to_dict()
            if not include_items:
                d.pop("items", None)
            out["stages"][sname] = d
        return out


class ProgressTracker:
    """线程安全的进度追踪器。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: Dict[str, JobState] = {}

    # ---------------- 任务级 ----------------
    def start_job(self, job_id, total_items=0):
        with self._lock:
            j = self._jobs.get(job_id) or JobState(job_id=job_id)
            j.status = "running"
            j.started_ts = time.time()
            j.total_items = total_items
            self._jobs[job_id] = j
            return j

    def finish_job(self, job_id, ok=True, error=""):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            j.status = "done" if ok else "failed"
            j.finished_ts = time.time()
            if error:
                j.error = error

    def log(self, job_id, message, stage=None, item=None):
        """记录一条日志（会同时写到 job 和对应 item）。"""
        ts = time.strftime("%H:%M:%S")
        line = "[%s] %s" % (ts, message)
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            j.recent_logs.append(line)
            if len(j.recent_logs) > MAX_RECENT_LOGS * 2:
                j.recent_logs = j.recent_logs[-MAX_RECENT_LOGS:]
            if stage and stage in j.stages and item:
                st = j.stages[stage]
                if item in st.items:
                    it = st.items[item]
                    it.logs.append(line)
                    if len(it.logs) > MAX_LOGS_PER_ITEM * 2:
                        it.logs = it.logs[-MAX_LOGS_PER_ITEM:]

    # ---------------- 阶段级 ----------------
    def start_stage(self, job_id, stage, total=0, label=""):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                j = JobState(job_id=job_id)
                self._jobs[job_id] = j
            st = j.stages.get(stage) or StageState(name=stage)
            st.status = "running"
            st.started_ts = time.time()
            st.total = total or st.total
            j.stages[stage] = st
            return st

    def finish_stage(self, job_id, stage, ok=True, error=""):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            st = j.stages.get(stage)
            if not st:
                return
            st.status = "done" if ok else "failed"
            st.finished_ts = time.time()
            if error:
                st.error = error

    # ---------------- 细项级 ----------------
    def ensure_item(self, job_id, stage, item_key, label=""):
        """确保细项存在（不改变状态）。"""
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return None
            st = j.stages.get(stage)
            if not st:
                return None
            if item_key not in st.items:
                st.items[item_key] = ItemState(
                    key=item_key, label=label or item_key)
            elif label:
                st.items[item_key].label = label
            return st.items[item_key]

    def start_item(self, job_id, stage, item_key, label=""):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return None
            st = j.stages.get(stage)
            if not st:
                return None
            it = st.items.get(item_key)
            if not it:
                it = ItemState(key=item_key, label=label or item_key)
                st.items[item_key] = it
            it.status = "running"
            it.started_ts = time.time()
            return it

    def finish_item(self, job_id, stage, item_key,
                    ok=True, cost=0.0, elapsed=None, error="", detail=None):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            st = j.stages.get(stage)
            if not st:
                return
            it = st.items.get(item_key)
            if not it:
                it = ItemState(key=item_key)
                st.items[item_key] = it
            it.status = "done" if ok else "failed"
            it.finished_ts = time.time()
            if elapsed is not None and it.started_ts:
                # 用传入的耗时校准
                it.started_ts = it.finished_ts - float(elapsed)
            it.cost = float(cost or 0)
            if error:
                it.error = error
            if detail:
                it.detail.update(detail)
            # 累计到阶段和任务
            st.cost += it.cost
            j.cost += it.cost

    def skip_item(self, job_id, stage, item_key, reason=""):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            st = j.stages.get(stage)
            if not st:
                return
            it = st.items.get(item_key)
            if not it:
                it = ItemState(key=item_key)
                st.items[item_key] = it
            it.status = "skipped"
            it.detail["skip_reason"] = reason

    # ---------------- 查询 ----------------
    def snapshot(self, job_id, include_items=True):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return None
            return j.to_dict(include_items=include_items)

    def summary(self, job_id):
        """精简版（不含每项明细，给轮询用）。"""
        snap = self.snapshot(job_id, include_items=False)
        if not snap:
            return None
        return {
            "job_id": snap["job_id"],
            "status": snap["status"],
            "elapsed": snap["elapsed"],
            "cost": snap["cost"],
            "current": snap["current"],
            "overall": snap["overall"],
            "stages": snap["stages"],
        }

    def drop(self, job_id):
        with self._lock:
            self._jobs.pop(job_id, None)

    def list_jobs(self):
        with self._lock:
            return list(self._jobs.keys())


# 全局单例
tracker = ProgressTracker()


# ---------------------------------------------------------------- 便捷封装
class StageContext:
    """with 语句封装：自动 start/finish 阶段。"""

    def __init__(self, job_id, stage, total=0, label=""):
        self.job_id = job_id
        self.stage = stage
        self.total = total
        self.label = label

    def __enter__(self):
        tracker.start_stage(self.job_id, self.stage,
                            total=self.total, label=self.label)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ok = exc_type is None
        tracker.finish_stage(self.job_id, self.stage, ok=ok,
                             error="" if ok else str(exc_val))
        return False


class ItemContext:
    """with 语句封装：自动 start/finish 细项。"""

    def __init__(self, job_id, stage, item_key, label=""):
        self.job_id = job_id
        self.stage = stage
        self.item_key = item_key
        self.label = label
        self.t0 = 0.0
        self.result = None

    def __enter__(self):
        tracker.start_item(self.job_id, self.stage, self.item_key,
                           label=self.label)
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.t0
        if exc_type is None:
            tracker.finish_item(self.job_id, self.stage, self.item_key,
                                ok=True, elapsed=elapsed)
        else:
            tracker.finish_item(self.job_id, self.stage, self.item_key,
                                ok=False, elapsed=elapsed,
                                error=str(exc_val)[:200])
        return False
