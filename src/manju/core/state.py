# -*- coding: utf-8 -*-
"""任务状态持久化与断点恢复。

设计要点：
    1. 双文件轮换（state.json / state.json.bak），防止写坏
    2. 每个产物一条记录，支持交叉核对
    3. state 全丢时能扫目录重建
"""
import json
import time
from pathlib import Path

from . import atomic, integrity


class ItemStatus:
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PENDING = "pending"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"

    ALL = (OK, DEGRADED, FAILED, UNKNOWN, PENDING, SUBMITTING, SUBMITTED)
    # 视为"已完成、可跳过"的状态
    DONE = (OK, DEGRADED)


class StateStore:
    """单集任务的状态文件。"""

    def __init__(self, path):
        self.path = Path(path)
        self.bak = self.path.with_name(self.path.name + ".bak")
        self.new = self.path.with_name(self.path.name + ".new")
        self.data = self._empty()
        self.load()

    # ---------------- 基础 ----------------
    def _empty(self):
        return {
            "job_id": "",
            "chapter": 0,
            "stage": "init",
            "items": {},
            "cost_si": 0,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": "",
            "recovered_from": "",   # 记录本次是怎么恢复的
        }

    def load(self):
        """主文件 → 备份 → 空。返回恢复来源标记。"""
        for src, tag in ((self.path, "main"), (self.bak, "bak")):
            if not src.exists():
                continue
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.data = dict(self._empty(), **data)
                    self.data.setdefault("items", {})
                    self.data["recovered_from"] = tag
                    if tag == "bak":
                        print("[state] 主文件损坏，已从备份恢复")
                    return tag
            except Exception as exc:  # noqa: BLE001
                print("[state] %s 解析失败：%r" % (src.name, exc))

        self.data = self._empty()
        self.data["recovered_from"] = "empty"
        return "empty"

    def save(self):
        """双文件轮换保存。"""
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        text = json.dumps(self.data, ensure_ascii=False, indent=2)

        self.new.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_text(self.new, text, verify=integrity.validate_json)

        if self.path.exists():
            try:
                self.path.replace(self.bak)
            except OSError:
                pass
        self.new.replace(self.path)
        return self.path

    # ---------------- 条目 ----------------
    def set_item(self, key, status, **fields):
        item = self.data["items"].get(key) or {}
        item.update(fields)
        item["status"] = status
        item["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.data["items"][key] = item

    def get_item(self, key):
        return self.data["items"].get(key) or {}

    def status_of(self, key):
        return self.get_item(key).get("status", "")

    def is_done(self, key):
        return self.status_of(key) in ItemStatus.DONE

    def add_cost(self, si):
        """累加成本（单位：0.0001 元）。"""
        self.data["cost_si"] = int(self.data.get("cost_si", 0)) + int(si)

    def stats(self):
        """统计各状态数量。"""
        out = {}
        for it in self.data["items"].values():
            s = it.get("status", "unknown")
            out[s] = out.get(s, 0) + 1
        return out

    # ---------------- 交叉核对 ----------------
    def reconcile(self, candidates, kind=None):
        """把 state 和实际文件对齐。

        candidates: {key: 文件路径} 或 可迭代的 (key, path)
        返回 {key: "skip" | "redo" | "adopt" | "quarantine"}
        """
        if isinstance(candidates, dict):
            pairs = candidates.items()
        else:
            pairs = candidates

        plan = {}
        for key, path in pairs:
            path = Path(path)
            st = self.status_of(key)

            if not path.exists():
                # state 说完成但文件不在 → 状态不一致，重做
                plan[key] = "redo"
                if st in ItemStatus.DONE:
                    self.set_item(key, ItemStatus.PENDING, note="文件丢失，状态重置")
                continue

            ok, reason = integrity.validate(path, kind)
            if not ok:
                plan[key] = "quarantine"
                self.set_item(key, ItemStatus.PENDING,
                              note="校验失败：%s" % reason)
                continue

            if st in ItemStatus.DONE:
                plan[key] = "skip"
            else:
                # state 说没完成，但文件有效 → 直接收编，省一次调用
                plan[key] = "adopt"
                self.set_item(key, ItemStatus.OK, note="从文件系统收编")

        return plan

    def rebuild_from_fs(self, files, kind=None):
        """state 全丢时，从文件系统反推进度。

        files: {key: path}
        """
        adopted = 0
        for key, path in files.items():
            path = Path(path)
            if not path.exists():
                continue
            ok, _ = integrity.validate(path, kind)
            if ok:
                self.set_item(key, ItemStatus.OK, path=str(path),
                              note="扫目录重建")
                adopted += 1
        return adopted

    def __repr__(self):
        return "<StateStore %s stage=%s items=%d cost=%d丝>" % (
            self.path.parent.name, self.data.get("stage"),
            len(self.data.get("items", {})), self.data.get("cost_si", 0))


def prepare_workdir(chapter_dir, project_root=None):
    """任务开始前的清理：删 .tmp、隔离坏文件、返回坏文件列表。"""
    chapter_dir = Path(chapter_dir)
    removed = atomic.cleanup_tmp(chapter_dir)

    bad = []
    if chapter_dir.exists():
        _, bad = integrity.scan(chapter_dir)
        for path, reason in bad:
            dest = atomic.quarantine(path, chapter_dir)
            bad[-1] = (path, reason, dest)

    return {"tmp_removed": removed, "quarantined": bad}
