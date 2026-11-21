# -*- coding: utf-8 -*-
"""后台任务队列 v2：优先级 + 动态并发 + 限速。

相比 v1 的改进：
    1. 优先级       high / normal / low（高优先级插队）
    2. 动态并发     按 GPU 和 provider 自动调整，不再硬编码 1
    3. 限速         集成 ratelimit（QPS / 并发 / 日配额）
    4. 公平调度      同优先级 FIFO，低优先级不会饿死（有年龄加成）

设计：
    - 真正的优先级队列 + 调度线程
    - 任务信息落盘 jobs.json，重启后历史还在
    - 排队位置可查询
"""
import json
import threading
import time
import traceback
import uuid
from pathlib import Path

from ..core import atomic
from ..core.orchestrator import Orchestrator
from ..core.runtime import runtime

# 优先级：数字越大越优先
PRIORITY = {
    "high": 100,
    "normal": 50,
    "low": 10,
}

# 默认并发（可在 config 里覆盖）
DEFAULT_MAX_CONCURRENT = 1


class Job:
    def __init__(self, project, chapters, source_text="", target_shots=40,
                 stages=None, priority="normal", providers=None):
        if isinstance(chapters, int):
            chapters = [chapters]
        self.id = uuid.uuid4().hex[:10]
        self.project = project
        self.chapters = list(chapters) or [1]
        self.source_text = source_text
        self.target_shots = int(target_shots or 40)
        self.stages = list(stages) if stages else None
        self.priority = priority if priority in PRIORITY else "normal"
        # 本次任务指定的引擎（None = 用配置默认值）
        self.providers = dict(providers) if providers else None

        self.status = "queued"      # queued/running/done/failed/cancelled/pending/stale
        self.stage = ""
        self.stage_times = {}
        self._stage_t0 = 0.0
        self.progress = {"done": 0, "total": 0}
        self.error = ""
        self.result = {}
        self.results = []           # 每集的结果
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.started_at = ""
        self.finished_at = ""
        self.started_ts = 0.0
        self.finished_ts = 0.0
        self.created_ts = time.time()
        self.queue_position = 0     # 0 = 不在排队

        self._cancel = threading.Event()

    @property
    def chapter(self):
        return self.chapters[0] if self.chapters else 1

    @property
    def title(self):
        if len(self.chapters) > 1:
            return "%s 第%d-%d集" % (self.project, self.chapters[0],
                                     self.chapters[-1])
        return "%s 第%d集" % (self.project, self.chapter)

    @property
    def effective_priority(self):
        """有效优先级 = 基础优先级 + 等待时长加成（防饿死）。

        每等 60 秒 +5 分，最多加 50（等于升一档）。
        """
        base = PRIORITY.get(self.priority, 50)
        waited = time.time() - self.created_ts
        bonus = min(50, int(waited / 60.0) * 5)
        return base + bonus

    def to_dict(self):
        now = self.finished_ts or time.time()
        elapsed = (now - self.started_ts) if self.started_ts else 0.0
        return {
            "id": self.id, "project": self.project,
            "chapter": self.chapter,
            "chapters": self.chapters,
            "title": self.title,
            "status": self.status, "stage": self.stage,
            "priority": self.priority,
            "priority_score": self.effective_priority if self.status == "queued" else PRIORITY.get(self.priority, 50),
            "progress": self.progress, "error": self.error,
            "result": self.result, "results": self.results,
            "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "elapsed": round(elapsed, 1),
            "stage_times": self.stage_times,
            "source_text": self.source_text[:200],
            "target_shots": self.target_shots,
            "queue_position": self.queue_position,
            "providers": self.providers,
        }

    def cancel(self):
        self._cancel.set()


class JobManager:
    def __init__(self, projects_dir, max_concurrent=None):
        self.projects_dir = Path(projects_dir)
        self.jobs = {}
        self._queue = []            # 等待执行的 job id（优先级排序）
        self._running_ids = []
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._jobs_file = self.projects_dir / "_jobs.json"
        self._save_lock = threading.Lock()
        # 跨进程同步的持久化层（CLI 也可能读任务列表）
        try:
            from ..core.store import JsonStore
            self._store = JsonStore(self._jobs_file, default=lambda: [])
        except Exception:  # noqa: BLE001
            self._store = None

        # 并发配置
        if max_concurrent is None:
            from .. import config
            max_concurrent = int(getattr(config, "JOB_MAX_CONCURRENT",
                                         DEFAULT_MAX_CONCURRENT) or 1)
        self.max_concurrent = max(1, int(max_concurrent))

        # 限速器
        try:
            from ..core.ratelimit import configure_from_config
            self.limiter = configure_from_config()
        except Exception:  # noqa: BLE001
            self.limiter = None

        self._load()
        self._start_dispatchers()

    # ---------------- 持久化 ----------------
    def _load(self):
        if not self._jobs_file.exists():
            return
        try:
            data = json.loads(self._jobs_file.read_text(encoding="utf-8"))
            for item in data:
                chs = item.get("chapters") or [item.get("chapter") or 1]
                j = Job(item["project"], chs,
                        priority=item.get("priority", "normal"))
                j.id = item["id"]
                j.status = item.get("status", "unknown")
                if j.status in ("running", "queued"):
                    j.status = "stale"
                    j.error = "服务重启，任务中断"
                j.stage = item.get("stage", "")
                j.progress = item.get("progress") or {"done": 0, "total": 0}
                j.result = item.get("result") or {}
                j.results = item.get("results") or []
                j.created_at = item.get("created_at", "")
                j.started_at = item.get("started_at", "")
                j.finished_at = item.get("finished_at", "")
                self.jobs[j.id] = j
        except Exception as exc:  # noqa: BLE001
            print("[jobs] 读取历史失败：%r" % (exc,))

    def _save(self):
        with self._save_lock:
            with self._lock:
                data = [j.to_dict() for j in
                        sorted(self.jobs.values(), key=lambda x: x.created_at,
                               reverse=True)[:100]]
            try:
                self.projects_dir.mkdir(parents=True, exist_ok=True)
                if self._store:
                    self._store.save(data)
                else:
                    atomic.atomic_write_text(
                        self._jobs_file,
                        json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as exc:  # noqa: BLE001
                print("[jobs] 保存失败：%r" % (exc,))

    # ---------------- 调度 ----------------
    def _start_dispatchers(self):
        for i in range(self.max_concurrent):
            t = threading.Thread(target=self._dispatch_loop,
                                 name="job-disp-%d" % i, daemon=True)
            t.start()

    def _sort_queue(self):
        """按有效优先级排序队列（高的在前）。"""
        self._queue.sort(
            key=lambda jid: (
                -self.jobs[jid].effective_priority if jid in self.jobs else 0,
                self.jobs[jid].created_ts if jid in self.jobs else 0,
            ))

    def _dispatch_loop(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                self._sort_queue()
                job_id = self._queue.pop(0)
                self._running_ids.append(job_id)
            self._refresh_positions()

            job = self.jobs.get(job_id)
            if job is not None:
                if job._cancel.is_set():
                    job.status = "cancelled"
                elif job.status == "queued":
                    self._execute(job)
            with self._cond:
                if job_id in self._running_ids:
                    self._running_ids.remove(job_id)
            self._refresh_positions()
            self._save()

    def _refresh_positions(self):
        with self._lock:
            self._sort_queue()
            for pos, jid in enumerate(self._queue, 1):
                j = self.jobs.get(jid)
                if j:
                    j.queue_position = pos
            for j in self.jobs.values():
                if j.status != "queued":
                    j.queue_position = 0

    # ---------------- 提交 ----------------
    def submit(self, project, chapters, source_text="", target_shots=40,
               stages=None, priority="normal", providers=None):
        job = Job(project, chapters, source_text, target_shots, stages,
                  priority=priority, providers=providers)
        with self._cond:
            self.jobs[job.id] = job
            self._queue.append(job.id)
            self._cond.notify()
        self._refresh_positions()
        self._save()
        return job

    def _execute(self, job):
        job.status = "running"
        job.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        job.started_ts = time.time()

        # 登记到全局注册表（供回滚判断等使用）
        try:
            runtime.job_started(job.id, project=job.project,
                                stages=job.stages or [],
                                chapters=job.chapters)
        except Exception as exc:  # noqa: BLE001
            print("[jobs] runtime 登记失败：%r" % exc)

        self._save()

        n_ch = len(job.chapters)
        n_st = len(job.stages or ("script", "image", "video", "audio",
                                  "bgm", "edit", "report"))
        job.progress = {"done": 0, "total": n_ch * n_st}

        all_ok = True
        needs_manual = False

        try:
            for idx, ch in enumerate(job.chapters):
                if job._cancel.is_set():
                    break

                orc = Orchestrator(
                    job.project, ch,
                    source_text=job.source_text,
                    target_shots=job.target_shots,
                    providers=job.providers,
                    project_dir=self.projects_dir / job.project,
                    cancel_event=job._cancel,
                    on_stage=self._make_progress_cb(job, idx, n_ch, n_st),
                )
                # 把限速器注入编排器（如果有）
                if self.limiter is not None:
                    orc.limiter = self.limiter

                # 绑定进度追踪器
                try:
                    orc.bind_job(job.id)
                    from ..core.progress import tracker as _pt
                    _pt.start_job(job.id, total_items=n_ch * n_st)
                except Exception:  # noqa: BLE001
                    pass

                try:
                    res = orc.run(stages=job.stages)
                except Exception as exc:  # noqa: BLE001
                    job.error = "第%d集：%s" % (ch, exc)
                    print("[jobs] 第%d集失败：%s"
                          % (ch, traceback.format_exc()[-600:]))
                    all_ok = False
                    continue

                one = {
                    "chapter": ch,
                    "ok": bool(res.ok),
                    "video": str(res.video) if res.video else None,
                    "report": str(res.report) if res.report else None,
                    "cost": (res.ledger.summary() if res.ledger else {}),
                    "counts": res.summary().get("counts", {}),
                    "todos": len(res.todos),
                    "errors": res.errors[:3],
                }
                job.results.append(one)
                if not res.ok:
                    all_ok = False
                    if getattr(res, "needs_manual", False):
                        needs_manual = True

                self._save()

            if job._cancel.is_set():
                job.status = "cancelled"
            elif all_ok:
                job.status = "done"
            elif needs_manual and all(
                    (r.get("ok") or not r.get("errors")) for r in job.results):
                job.status = "pending"
            else:
                job.status = "failed"

            total_cost = 0
            for r in job.results:
                try:
                    total_cost += float(
                        (r.get("cost") or {}).get("total_min_yuan") or 0)
                except (TypeError, ValueError):
                    pass
            job.result = {
                "chapters": len(job.results),
                "cost_total": "%.4f" % total_cost,
                "results": job.results,
            }

            # 进度追踪器收尾
            try:
                from ..core.progress import tracker as _pt
                _pt.finish_job(job.id,
                               ok=(job.status == "done"),
                               error=job.error)
            except Exception:  # noqa: BLE001
                pass

        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)[:500]
            print("[jobs] %s 失败：%s" % (job.id, traceback.format_exc()[-800:]))
        finally:
            job.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            job.finished_ts = time.time()
            try:
                runtime.job_finished(job.id)
            except Exception:  # noqa: BLE001
                pass
            self._save()

    def _make_progress_cb(self, job, idx, n_ch, n_st):
        def cb(stage, done, total):
            now = time.time()
            if job._stage_t0:
                prev = job.stage or "?"
                job.stage_times[prev] = round(
                    job.stage_times.get(prev, 0) + (now - job._stage_t0), 1)
            job._stage_t0 = now
            job.stage = stage
            job.progress = {"done": idx * n_st + done,
                            "total": n_ch * n_st}
            try:
                runtime.job_stage(job.id, stage,
                                  idx * n_st + done, n_ch * n_st)
            except Exception:  # noqa: BLE001
                pass
            self._save()
        return cb

    # ---------------- 查询 ----------------
    def get(self, job_id):
        return self.jobs.get(job_id)

    def list(self, limit=50):
        with self._lock:
            items = sorted(self.jobs.values(), key=lambda x: x.created_at,
                           reverse=True)
            return [j.to_dict() for j in items[:limit]]

    def queue_state(self):
        """当前队列：谁在跑、谁在等、各排第几、优先级。"""
        with self._lock:
            self._sort_queue()
            running = [self.jobs[i].to_dict() for i in self._running_ids
                       if i in self.jobs]
            queued = []
            for pos, jid in enumerate(self._queue, 1):
                j = self.jobs.get(jid)
                if j is None:
                    continue
                d = j.to_dict()
                d["queue_position"] = pos
                queued.append(d)
            return {
                "max_concurrent": self.max_concurrent,
                "running": running,
                "queued": queued,
                "running_count": len(running),
                "queued_count": len(queued),
                "priority_levels": list(PRIORITY.keys()),
                "rate": (self.limiter.status() if self.limiter else {}),
            }

    @property
    def _running(self):
        """兼容旧代码：当前运行中的任务数。"""
        return len(self._running_ids)

    def cancel(self, job_id):
        with self._cond:
            j = self.jobs.get(job_id)
            if not j:
                return False, "任务不存在"
            if j.status not in ("queued", "running"):
                return False, "任务已结束（%s）" % j.status

            j.cancel()
            if j.status == "queued" and job_id in self._queue:
                self._queue.remove(job_id)
                j.queue_position = 0
            j.status = "cancelled"
            try:
                runtime.job_finished(job_id)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_positions()
        self._save()
        return True, "已请求取消"

    # ---------------- 优先级调整 ----------------
    def set_priority(self, job_id, priority):
        """调整排队中任务的优先级。"""
        if priority not in PRIORITY:
            return False, "未知优先级：%s（可选：%s）" % (
                priority, ", ".join(PRIORITY.keys()))
        with self._cond:
            j = self.jobs.get(job_id)
            if not j:
                return False, "任务不存在"
            if j.status != "queued":
                return False, "只能调整排队中的任务（当前：%s）" % j.status
            j.priority = priority
            self._refresh_positions()
        self._save()
        return True, "已调整为 %s" % priority

    def set_max_concurrent(self, n):
        """动态调整并发数（只影响新启动的调度线程）。"""
        n = max(1, min(8, int(n)))
        old = self.max_concurrent
        self.max_concurrent = n
        # 新增调度线程
        for i in range(old, n):
            t = threading.Thread(target=self._dispatch_loop,
                                 name="job-disp-%d" % i, daemon=True)
            t.start()
        self._save()
        return True, "并发数 %d -> %d（新加 %d 个调度线程）" % (
            old, n, max(0, n - old))
