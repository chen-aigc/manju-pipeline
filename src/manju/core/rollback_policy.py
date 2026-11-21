# -*- coding: utf-8 -*-
"""任务运行中的回滚策略。

四种策略：
    block   阻断（默认）—— 最安全，让用户自己决定
    wait    等待 —— 排队，等任务完成后自动回滚
    cancel  取消 —— 先取消所有运行中任务，再回滚
    force   强制 —— 不管任务，直接回滚（需 admin，有风险）

每种策略的语义：
    block   返回 409，前端提示"有任务在跑"
    wait    先登记到等待队列，任务结束后触发回滚
    cancel  调用 cancel 接口取消所有运行中任务，等它们停下，再回滚
    force   跳过任务检查，直接回滚（可能让任务读到不一致的剧本）

策略持久化：
    <projects_dir>/_rollback_policy.json
    {"default": "block", "per_op": {"script": "block", ...}}

等待队列实现：
    用后台线程轮询 runtime.is_busy()，空了就执行回调
"""
import json
from .store import JsonStore
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional


POLICIES = ("block", "wait", "cancel", "force")

POLICY_INFO = {
    "block": {
        "name": "阻断",
        "desc": "任务运行中直接拒绝回滚（最安全）",
        "danger": "none",
    },
    "wait": {
        "name": "等待",
        "desc": "把回滚排进队列，等任务完成后自动执行",
        "danger": "low",
    },
    "cancel": {
        "name": "取消",
        "desc": "先取消所有运行中的任务，再回滚",
        "danger": "medium",
    },
    "force": {
        "name": "强制",
        "desc": "不等任务，直接回滚（任务可能读到不一致的数据）",
        "danger": "high",
    },
}


class RollbackPolicy:
    """回滚策略管理。"""

    def __init__(self, store_path=None):
        self._lock = threading.RLock()
        self.store_path = Path(store_path) if store_path else None
        self.default = "block"
        self.per_op: Dict[str, str] = {}
        self._store = JsonStore(self.store_path, default=lambda: {
            "default": "block", "per_op": {},
        }) if self.store_path else None
        self.wait_queue: List[Dict] = []
        self._stop = threading.Event()
        self._thread = None
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        if not self.store_path or not self.store_path.exists():
            return
        try:
            d = json.loads(self.store_path.read_text(encoding="utf-8"))
            self.default = d.get("default") or "block"
            self.per_op = d.get("per_op") or {}
        except Exception:  # noqa: BLE001
            pass

    def _save(self):
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(json.dumps({
                "default": self.default,
                "per_op": self.per_op,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 配置 ----------------
    def set(self, default=None, per_op=None):
        self._load()
        with self._lock:
            if default in POLICIES:
                self.default = default
            if isinstance(per_op, dict):
                for k, v in per_op.items():
                    if v in POLICIES:
                        self.per_op[k] = v
            self._save()
        return self.summary()

    def get(self, op=""):
        """取某操作生效的策略。"""
        self._load()
        with self._lock:
            return self.per_op.get(op) or self.default

    def summary(self):
        self._load()
        return {
            "default": self.default,
            "per_op": dict(self.per_op),
            "policies": [{"name": k, **v} for k, v in POLICY_INFO.items()],
            "waiting": len(self.wait_queue),
        }

    # ---------------- 等待队列 ----------------
    def enqueue(self, op, payload, callback, user=""):
        """把一个回滚操作排进等待队列。

        Args:
            op:        操作类型（记录用）
            payload:   这次回滚的参数（给前端展示）
            callback:  任务结束后要执行的回调（无参，返回 (ok, msg)）
            user:      发起人

        Returns:
            queue_id
        """
        with self._lock:
            qid = "wq_%d_%s" % (int(time.time() * 1000),
                                secrets_token(6))
            self.wait_queue.append({
                "id": qid,
                "op": op,
                "payload": payload,
                "user": user,
                "callback": callback,
                "created": time.time(),
                "status": "waiting",
            })
            self._ensure_worker()
        return qid

    def list_waiting(self):
        self._load()
        with self._lock:
            return [
                {
                    "id": x["id"],
                    "op": x["op"],
                    "payload": x["payload"],
                    "user": x["user"],
                    "status": x["status"],
                    "waited": round(time.time() - x["created"], 1),
                }
                for x in self.wait_queue
            ]

    def cancel_waiting(self, qid):
        with self._lock:
            self.wait_queue = [x for x in self.wait_queue if x["id"] != qid]
        return True, "已取消"

    def _ensure_worker(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                # 检查 runtime 是否空闲
                try:
                    from .runtime import runtime
                    busy = runtime.is_busy()
                except Exception:  # noqa: BLE001
                    busy = False

                if not busy:
                    with self._lock:
                        ready = [x for x in self.wait_queue
                                 if x["status"] == "waiting"]
                        for x in ready:
                            x["status"] = "running"
                    for x in ready:
                        try:
                            ok, msg = x["callback"]()
                            with self._lock:
                                x["status"] = "done" if ok else "failed"
                                x["result"] = {"ok": ok, "msg": msg}
                        except Exception as exc:  # noqa: BLE001
                            with self._lock:
                                x["status"] = "failed"
                                x["result"] = {"ok": False, "error": repr(exc)}
                    # 清理已完成
                    if ready:
                        with self._lock:
                            self.wait_queue = [
                                x for x in self.wait_queue
                                if x["status"] not in ("done", "failed")
                            ]

                self._stop.wait(2.0)

        self._thread = threading.Thread(target=loop, name="rollback-waiter",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def secrets_token(n=6):
    import secrets
    return secrets.token_hex(n)[:n]


# ---------------------------------------------------------------- 全局
def make_policy():
    try:
        from .. import config
        path = Path(config.PROJECTS_DIR) / "_rollback_policy.json"
        return RollbackPolicy(path)
    except Exception:  # noqa: BLE001
        return RollbackPolicy(None)


policy = make_policy()
