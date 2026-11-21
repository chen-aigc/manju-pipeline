# -*- coding: utf-8 -*-
"""回收站：让所有删除都能撤销。

设计：
    删除时不真删，先移到回收站（保留 N 天）
    回收站里的条目可以还原或永久删除

目录结构：
    <projects_dir>/_trash/
        <timestamp>_<rand>/
            manifest.json       记录了原始路径和元数据
            <原文件名>          原文件

manifest.json:
    {
      "id": "20261119_001234_ab12",
      "op": "resource.cleanup" / "batch.delete" / "project.delete" / ...,
      "user": "admin",
      "ts": "2026-11-19T00:12:34",
      "items": [
        {"orig": "/abs/path/to/file.png", "name": "file.png",
         "size": 123456, "type": "file"},
        {"orig": "/abs/path/to/dir", "name": "dir",
         "size": 999999, "type": "dir"}
      ],
      "total_size": 1234567,
      "expires_days": 7
    }

用法：
    from manju.core.trash import trash

    # 删除（移到回收站）
    tid = trash.move([path1, path2], op="batch.delete", user="admin")

    # 还原
    ok, msg = trash.restore(tid)

    # 列出回收站
    items = trash.list()

    # 永久删除
    trash.purge(tid)

    # 清理过期条目
    trash.purge_expired()
"""
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional


def _size(p):
    p = Path(p)
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    import os
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class TrashManager:
    """回收站管理器。"""

    def __init__(self, trash_dir, keep_days=7):
        self.trash_dir = Path(trash_dir) if trash_dir else None
        self.keep_days = int(keep_days)

    # ---------------- 移入回收站 ----------------
    def move(self, paths, op="", user="", note=""):
        """把文件/目录移到回收站。

        Args:
            paths: 文件或目录路径列表
            op:    操作类型（记录用）
            user:  发起人

        Returns:
            trash_id 或 None（失败）
        """
        if not self.trash_dir:
            return None
        if not paths:
            return None

        # 生成 id
        stamp = time.strftime("%Y%m%d_%H%M%S")
        rand = uuid.uuid4().hex[:6]
        tid = "%s_%s" % (stamp, rand)
        target_dir = self.trash_dir / tid
        target_dir.mkdir(parents=True, exist_ok=True)

        items = []
        total = 0
        for p in paths:
            src = Path(p)
            if not src.exists():
                continue

            # 防止重名
            name = src.name
            dest = target_dir / name
            k = 1
            while dest.exists():
                dest = target_dir / ("%s_%d%s" % (src.stem, k, src.suffix))
                k += 1

            try:
                sz = _size(src)
                shutil.move(str(src), str(dest))
                items.append({
                    "orig": str(src),
                    "name": dest.name,
                    "size": sz,
                    "type": "dir" if dest.is_dir() else "file",
                })
                total += sz
            except Exception as exc:  # noqa: BLE001
                print("[回收站] 移动失败 %s: %r" % (src, exc))

        if not items:
            # 什么都没移进去，删掉空目录
            try:
                target_dir.rmdir()
            except OSError:
                pass
            return None

        manifest = {
            "id": tid,
            "op": op,
            "user": user,
            "note": note,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "items": items,
            "total_size": total,
            "total_mb": round(total / 1024 / 1024, 2),
            "expires_days": self.keep_days,
            "restored": False,
        }
        (target_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return tid

    # ---------------- 还原 ----------------
    def restore(self, tid):
        """把回收站的某个条目还原到原位置。"""
        if not self.trash_dir:
            return False, "回收站未启用"
        d = self.trash_dir / tid
        mf = d / "manifest.json"
        if not mf.exists():
            return False, "回收站条目不存在：%s" % tid

        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, "manifest 解析失败：%r" % exc

        restored = []
        skipped = []
        for it in manifest.get("items") or []:
            src = d / it["name"]
            dest = Path(it["orig"])
            if not src.exists():
                skipped.append({"name": it["name"], "reason": "回收站里已丢失"})
                continue

            # 原位置可能已被新文件占用
            if dest.exists():
                # 加时间戳后缀，不覆盖
                stem = dest.stem
                suffix = dest.suffix
                dest = dest.with_name(
                    "%s_restored_%s%s"
                    % (stem, time.strftime("%H%M%S"), suffix))

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                restored.append({"orig": it["orig"], "to": str(dest)})
            except Exception as exc:  # noqa: BLE001
                skipped.append({"name": it["name"], "reason": repr(exc)})

        # 更新 manifest
        manifest["restored"] = True
        manifest["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        manifest["restored_count"] = len(restored)
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                      encoding="utf-8")

        # 如果全还原了，删空目录
        remaining = [x for x in (d.iterdir()) if x.name != "manifest.json"]
        if not remaining:
            try:
                d.rmdir()
            except OSError:
                pass

        return True, {
            "restored": len(restored),
            "skipped": len(skipped),
            "details": restored,
            "skipped_details": skipped,
        }

    # ---------------- 查询 ----------------
    def list(self, limit=100):
        """列出回收站条目（按时间倒序）。"""
        if not self.trash_dir or not self.trash_dir.exists():
            return []
        out = []
        for d in sorted(self.trash_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            mf = d / "manifest.json"
            if not mf.exists():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                # 剩余天数
                try:
                    created = time.mktime(time.strptime(
                        m.get("ts", ""), "%Y-%m-%dT%H:%M:%S"))
                    age_days = (time.time() - created) / 86400
                except (ValueError, TypeError):
                    age_days = 0
                m["age_days"] = round(age_days, 1)
                m["days_left"] = max(0, round(self.keep_days - age_days, 1))
                out.append(m)
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return out[:limit]

    def get(self, tid):
        for m in self.list(limit=9999):
            if m.get("id") == tid:
                return m
        return None

    def stats(self):
        items = self.list(limit=9999)
        total_size = sum(x.get("total_size", 0) for x in items)
        return {
            "count": len(items),
            "total_mb": round(total_size / 1024 / 1024, 1),
            "keep_days": self.keep_days,
            "restorable": sum(1 for x in items if not x.get("restored")),
        }

    # ---------------- 永久删除 ----------------
    def purge(self, tid):
        """永久删除某条回收站条目。"""
        if not self.trash_dir:
            return False, "回收站未启用"
        d = self.trash_dir / tid
        if not d.exists():
            return False, "条目不存在"
        try:
            shutil.rmtree(d)
            return True, "已永久删除"
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)

    def purge_expired(self):
        """清理超过保留期的条目。"""
        if not self.trash_dir or not self.trash_dir.exists():
            return {"purged": 0, "freed_mb": 0}

        now = time.time()
        purged = 0
        freed = 0
        for d in self.trash_dir.iterdir():
            if not d.is_dir():
                continue
            mf = d / "manifest.json"
            if not mf.exists():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                created = time.mktime(time.strptime(
                    m.get("ts", ""), "%Y-%m-%dT%H:%M:%S"))
                if (now - created) / 86400 > self.keep_days:
                    sz = _size(d)
                    shutil.rmtree(d)
                    purged += 1
                    freed += sz
            except Exception:  # noqa: BLE001
                continue

        return {"purged": purged,
                "freed_mb": round(freed / 1024 / 1024, 1)}

    def clear(self):
        """清空回收站（永久删除全部）。"""
        if not self.trash_dir or not self.trash_dir.exists():
            return {"purged": 0, "freed_mb": 0}
        freed = 0
        count = 0
        for d in self.trash_dir.iterdir():
            if d.is_dir():
                try:
                    freed += _size(d)
                    shutil.rmtree(d)
                    count += 1
                except Exception:  # noqa: BLE001
                    pass
        return {"purged": count,
                "freed_mb": round(freed / 1024 / 1024, 1)}


# ---------------------------------------------------------------- 全局
def make_trash():
    try:
        from .. import config
        path = Path(config.PROJECTS_DIR) / "_trash"
        keep = int(getattr(config, "TRASH_KEEP_DAYS", 7))
        return TrashManager(path, keep_days=keep)
    except Exception:  # noqa: BLE001
        return TrashManager(None)


trash = make_trash()
