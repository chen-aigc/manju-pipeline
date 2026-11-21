# -*- coding: utf-8 -*-
"""成本追踪：单镜头成本、单任务成本、告警。

三层统计：
    1. 单镜头：每镜的静帧 + 视频 + 配音成本
    2. 单任务：整个任务累计
    3. 全局：今天的总花费

告警：
    1. 单镜超阈值
    2. 单任务超阈值
    3. 日累计超阈值
    4. 显存超阈值

用法：
    from manju.core.spend import spend

    spend.add_shot_cost("job123", "shot_017", "image", 0.20)
    spend.add_shot_cost("job123", "shot_017", "video", 1.30)

    s = spend.shot_summary("job123", "shot_017")   # {"total": 1.50, "breakdown": {...}}
    a = spend.job_summary("job123")
    alerts = spend.check_alerts()
"""
import json
from .store import JsonStore
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional


class SpendTracker:
    """成本追踪器（线程安全，可持久化）。"""

    def __init__(self, store_path=None):
        self._lock = threading.RLock()
        # {job_id: {shot_id: {kind: amount}}}
        self._shots: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(float)))
        # {job_id: {kind: amount}}
        self._jobs: Dict[str, Dict[str, float]] = defaultdict(
            lambda: defaultdict(float))
        # 日累计
        self._daily: Dict[str, float] = defaultdict(float)   # {date: amount}
        # 预算配置
        self.budget = {
            "per_shot": 0.0,       # 单镜上限（0 = 不限）
            "per_job": 0.0,        # 单任务上限
            "daily": 0.0,          # 日累计上限
            "vram_mb": 0.0,        # 显存上限（MB）
        }
        self.store_path = Path(store_path) if store_path else None
        # 跨进程同步的持久化层
        self._store = JsonStore(self.store_path, default=lambda: {
            "shots": {}, "jobs": {}, "daily": {}, "budget": {},
        }) if self.store_path else None
        self._load()

    # ---------------- 记录 ----------------
    def add_shot_cost(self, job_id, shot_id, kind, amount):
        """记一笔成本。

        Args:
            job_id:  任务 id
            shot_id: 镜头 id（如 shot_017）
            kind:    类型（image / video / audio / bgm / edit）
            amount:  金额（元）
        """
        self._load()
        if not amount or amount <= 0:
            return
        with self._lock:
            self._shots[job_id][shot_id][kind] += float(amount)
            self._jobs[job_id][kind] += float(amount)
            today = time.strftime("%Y-%m-%d")
            self._daily[today] += float(amount)
            self._save()

    def add_job_cost(self, job_id, kind, amount):
        """记一笔不属于具体镜头的成本（如剧本、审计）。"""
        self._load()
        if not amount or amount <= 0:
            return
        with self._lock:
            self._jobs[job_id][kind] += float(amount)
            today = time.strftime("%Y-%m-%d")
            self._daily[today] += float(amount)
            self._save()

    # ---------------- 查询 ----------------
    def shot_summary(self, job_id, shot_id):
        """单镜头成本。"""
        self._load()
        with self._lock:
            d = dict(self._shots.get(job_id, {}).get(shot_id, {}))
        total = sum(d.values())
        return {
            "shot_id": shot_id,
            "total": round(total, 4),
            "breakdown": {k: round(v, 4) for k, v in d.items()},
        }

    def job_summary(self, job_id):
        """单任务成本。"""
        self._load()
        with self._lock:
            d = dict(self._jobs.get(job_id, {}))
            shots = self._shots.get(job_id, {})
            shot_totals = {sid: round(sum(k.values()), 4)
                           for sid, k in shots.items()}
        total = sum(d.values())
        return {
            "job_id": job_id,
            "total": round(total, 4),
            "breakdown": {k: round(v, 4) for k, v in d.items()},
            "shot_count": len(shot_totals),
            "shots": shot_totals,
            "avg_per_shot": round(total / len(shot_totals), 4)
                            if shot_totals else 0.0,
        }

    def daily_summary(self, days=7):
        """最近 N 天。"""
        self._load()
        with self._lock:
            items = sorted(self._daily.items(), reverse=True)[:days]
        return {
            "today": round(self._daily.get(time.strftime("%Y-%m-%d"), 0), 4),
            "days": [{"date": d, "amount": round(a, 4)} for d, a in items],
            "total": round(sum(a for _, a in items), 4),
        }

    def all_jobs(self):
        self._load()
        with self._lock:
            return sorted(self._jobs.keys())

    # ---------------- 告警 ----------------
    def set_budget(self, per_shot=0, per_job=0, daily=0, vram_mb=0):
        self._load()
        with self._lock:
            self.budget = {
                "per_shot": float(per_shot or 0),
                "per_job": float(per_job or 0),
                "daily": float(daily or 0),
                "vram_mb": float(vram_mb or 0),
            }
        return self.budget

    def load_budget_from_config(self):
        """从 manju.config 读取告警阈值。"""
        try:
            from .. import config
            self.set_budget(
                per_shot=getattr(config, "ALERT_PER_SHOT", 0),
                per_job=getattr(config, "ALERT_PER_JOB", 0),
                daily=getattr(config, "ALERT_DAILY", 0),
                vram_mb=getattr(config, "ALERT_VRAM_MB", 0),
            )
        except Exception:  # noqa: BLE001
            pass
        return self.budget

    def check_alerts(self, job_id=None, shot_id=None, vram_mb=None):
        """检查是否超阈值，返回告警列表。

        Args:
            job_id:   检查这个任务的成本
            shot_id:  检查这个镜头
            vram_mb:  检查显存（传当前值）

        Returns:
            [{"level": "warn"/"error", "kind": ..., "msg": ..., "value": ..., "limit": ...}]
        """
        alerts = []
        b = self.budget

        # 1. 单镜
        if shot_id and job_id and b["per_shot"] > 0:
            s = self.shot_summary(job_id, shot_id)
            if s["total"] > b["per_shot"]:
                alerts.append({
                    "level": "warn",
                    "kind": "per_shot",
                    "value": s["total"],
                    "limit": b["per_shot"],
                    "msg": "镜头 %s 成本 ¥%.3f 超过阈值 ¥%.3f"
                           % (shot_id, s["total"], b["per_shot"]),
                })

        # 2. 单任务
        if job_id and b["per_job"] > 0:
            j = self.job_summary(job_id)
            if j["total"] > b["per_job"]:
                alerts.append({
                    "level": "error",
                    "kind": "per_job",
                    "value": j["total"],
                    "limit": b["per_job"],
                    "msg": "任务 %s 累计 ¥%.3f 超过阈值 ¥%.3f"
                           % (job_id, j["total"], b["per_job"]),
                })

        # 3. 日累计
        if b["daily"] > 0:
            today = self._daily.get(time.strftime("%Y-%m-%d"), 0)
            if today > b["daily"]:
                alerts.append({
                    "level": "error",
                    "kind": "daily",
                    "value": round(today, 4),
                    "limit": b["daily"],
                    "msg": "今日累计 ¥%.3f 超过阈值 ¥%.3f"
                           % (today, b["daily"]),
                })

        # 4. 显存
        if vram_mb is not None and b["vram_mb"] > 0:
            if vram_mb > b["vram_mb"]:
                alerts.append({
                    "level": "error",
                    "kind": "vram",
                    "value": round(vram_mb, 1),
                    "limit": b["vram_mb"],
                    "msg": "显存占用 %.0f MB 超过阈值 %.0f MB"
                           % (vram_mb, b["vram_mb"]),
                })

        return alerts

    # ---------------- 持久化 ----------------
    def _save(self):
        if not self.store_path:
            return
        try:
            data = {
                "shots": {j: {s: dict(k) for s, k in v.items()}
                          for j, v in self._shots.items()},
                "jobs": {j: dict(v) for j, v in self._jobs.items()},
                "daily": dict(self._daily),
                "budget": self.budget,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            if self._store:
                self._store.save(data)
                return
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _load(self):
        if not self.store_path:
            return
        try:
            if self._store:
                data = self._store.load()   # 自动检测外部改动
            elif self.store_path.exists():
                data = json.loads(
                    self.store_path.read_text(encoding="utf-8"))
            else:
                return
            for j, shots in (data.get("shots") or {}).items():
                for s, kinds in shots.items():
                    for k, v in kinds.items():
                        self._shots[j][s][k] = float(v)
            for j, kinds in (data.get("jobs") or {}).items():
                for k, v in kinds.items():
                    self._jobs[j][k] = float(v)
            for d, a in (data.get("daily") or {}).items():
                self._daily[d] = float(a)
            if data.get("budget"):
                self.budget.update(data["budget"])
        except Exception:  # noqa: BLE001
            pass


# 全局单例（持久化到项目目录）
def make_tracker(path=None):
    if path is None:
        try:
            from .. import config
            path = Path(config.PROJECTS_DIR) / "_spend.json"
        except Exception:  # noqa: BLE001
            path = None
    return SpendTracker(path)


spend = make_tracker()
