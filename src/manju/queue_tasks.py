# -*- coding: utf-8 -*-
"""任务队列：轻量实现，不依赖 Redis。线程池 + 磁盘持久化状态。

为什么要它：
    生成一集要跑图、配音、合成，十几分钟。如果同步等，网页会卡死。
    这里改成「提交即返回任务 ID」，前端轮询进度。

设计：
    · 内存里维护 task_id -> {status, progress, stage, error}
    · 状态同时写 projects/<项目>/tasks/<task_id>.json，进程重启可查
    · 同一项目同一时间只允许一个任务在跑（避免抢显卡）
"""
import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

_executor = ThreadPoolExecutor(max_workers=1)   # 只有一块 3060，串行跑
_lock = threading.Lock()
_tasks = {}
_project_busy = {}   # project -> task_id


def _task_path(project, task_id):
    return Path(config.PROJECTS_DIR) / project / "tasks" / ("%s.json" % task_id)


def _persist(task):
    try:
        p = _task_path(task["project"], task["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def submit(project, fn, *args, **kwargs):
    """提交任务，立即返回 task_id。fn 里可以调 update()。"""
    with _lock:
        busy = _project_busy.get(project)
        if busy and _tasks.get(busy, {}).get("status") in ("queued", "running"):
            raise RuntimeError("该项目已有任务在跑：%s" % busy)

        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "project": project,
            "status": "queued",
            "stage": "排队中",
            "progress": 0,
            "error": "",
            "result": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": "",
        }
        _tasks[task_id] = task
        _project_busy[project] = task_id
        _persist(task)

    def wrapper():
        update(task_id, status="running", stage="开始")
        try:
            result = fn(task_id, *args, **kwargs)
            update(task_id, status="success", stage="完成", progress=100,
                   result=str(result) if result else "")
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            update(task_id, status="failed", stage="失败",
                   error="%s\n%s" % (exc, tb[-1500:]))
        finally:
            with _lock:
                if _project_busy.get(project) == task_id:
                    _project_busy.pop(project, None)

    _executor.submit(wrapper)
    return task_id


def update(task_id, **fields):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t.update(fields)
        t["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        snapshot = dict(t)
    _persist(snapshot)


def stage(task_id, name, progress=None):
    """更新阶段与进度，供流水线内部调用。"""
    fields = {"stage": name}
    if progress is not None:
        fields["progress"] = int(progress)
    update(task_id, **fields)


def get(task_id):
    with _lock:
        t = _tasks.get(task_id)
        if t:
            return dict(t)
    # 内存没有就去磁盘找
    root = Path(config.PROJECTS_DIR)
    if root.exists():
        for f in root.glob("*/tasks/%s.json" % task_id):
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    return None


def list_tasks(project=None, limit=50):
    out = []
    with _lock:
        for t in _tasks.values():
            if project and t["project"] != project:
                continue
            out.append(dict(t))
    # 补上磁盘里的历史
    root = Path(config.PROJECTS_DIR)
    if root.exists():
        seen = {t["id"] for t in out}
        for f in root.glob("*/tasks/*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if d.get("id") in seen:
                continue
            if project and d.get("project") != project:
                continue
            out.append(d)

    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out[:limit]


def is_busy(project):
    with _lock:
        tid = _project_busy.get(project)
        return bool(tid and _tasks.get(tid, {}).get("status") in ("queued", "running"))
