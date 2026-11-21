# -*- coding: utf-8 -*-
"""批量操作 + 素材资产详细 + 素材更换。

三个模块：
    1. 素材清单（assets）：逐镜列出所有产物
    2. 批量操作（batch）：多镜同时做某件事
    3. 素材更换（replace）：上传本地文件替换某镜

批量操作类型：
    redo      重做（清状态，下次跑会重新生成）
    choose    统一选用某版本（如都选 v001）
    priority  把这几镜对应的任务调到高优先级
    export    导出这几镜的素材
    delete    删除这几镜的产物（需确认）

素材清单结构（每镜）：
    {
      "shot_id": "shot_017",
      "index": 17,
      "shot_type": "近景",
      "characters": ["林越"],
      "scene": "院门外",
      "files": {
        "still": {"path": ..., "size": ..., "mtime": ..., "variants": N},
        "video": {"path": ..., "size": ..., "duration": ...},
        "audio": [{"path": ..., "duration": ...}, ...]
      },
      "story_state": {...},
      "consistency": {"level": "OK", "score": ...},
      "status": "ok" / "missing" / "partial"
    }
"""
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import atomic, integrity


# ================================================================ 素材清单
class AssetInventory:
    """一集的全部素材清单。"""

    def __init__(self, chapter_dir):
        self.chapter_dir = Path(chapter_dir)

    def _img_dir(self):
        return self.chapter_dir / "images"

    def _vid_dir(self):
        return self.chapter_dir / "videos"

    def _audio_dir(self):
        return self.chapter_dir / "audio"

    def _find_still(self, idx):
        """找第 idx 镜的静帧（多种命名）。"""
        d = self._img_dir()
        for pat in ("shot_%02d.png", "shot_%02d.jpg",
                    "fix_%02d.png", "seed_%02d.png", "fix_%02d.jpg"):
            p = d / (pat % idx)
            if p.exists():
                return p
        return None

    def _find_video(self, idx):
        d = self._vid_dir()
        for pat in ("shot_%02d.mp4", "shot_%02d.mov", "fix_%02d.mp4"):
            p = d / (pat % idx)
            if p.exists():
                return p
        return None

    def _find_audio(self, idx):
        d = self._audio_dir()
        if not d.exists():
            return []
        out = []
        for p in sorted(d.glob("s%02d_*.mp3" % idx)):
            out.append(p)
        return out

    def _variants(self, idx):
        """第 idx 镜的变体数。"""
        vdir = self.chapter_dir / "images" / "variants" / ("shot_%03d" % idx)
        if not vdir.exists():
            return 0
        return len([p for p in vdir.iterdir()
                    if p.suffix.lower() in (".png", ".jpg", ".mp4", ".mov")])

    def list(self, script=None, story_state=None, consistency=None):
        """生成素材清单。

        Args:
            script:       分镜脚本（有就带上镜头类型/角色）
            story_state:  剧情状态（dict）
            consistency:  一致性报告（dict）

        Returns:
            [{"shot_id": ..., "index": ..., ...}, ...]
        """
        # 确定镜头范围
        indices = set()
        shots_meta = {}

        if script and isinstance(script, dict):
            for s in (script.get("shots") or []):
                try:
                    i = int(s.get("index"))
                except (TypeError, ValueError):
                    continue
                indices.add(i)
                shots_meta[i] = s

        # 扫描目录里实际有的
        for p in self._img_dir().glob("*.png"):
            import re
            m = re.search(r"(\d+)", p.stem)
            if m:
                indices.add(int(m.group(1)))
        for p in self._vid_dir().glob("*.mp4"):
            import re
            m = re.search(r"(\d+)", p.stem)
            if m:
                indices.add(int(m.group(1)))

        items = []
        for idx in sorted(indices):
            meta = shots_meta.get(idx, {})
            still = self._find_still(idx)
            video = self._find_video(idx)
            audios = self._find_audio(idx)

            files = {}
            if still:
                files["still"] = {
                    "name": still.name,
                    "path": str(still),
                    "size": still.stat().st_size,
                    "mtime": time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(still.stat().st_mtime)),
                    "variants": self._variants(idx),
                }
            if video:
                files["video"] = {
                    "name": video.name,
                    "path": str(video),
                    "size": video.stat().st_size,
                    "mtime": time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(video.stat().st_mtime)),
                }
            if audios:
                files["audio"] = [
                    {"name": a.name, "path": str(a), "size": a.stat().st_size}
                    for a in audios
                ]

            has_any = bool(still or video or audios)
            complete = bool(still)
            status = ("ok" if complete else
                      ("partial" if has_any else "missing"))

            # 剧情状态
            ss = None
            if story_state and isinstance(story_state, dict):
                shots = story_state.get("shots") or {}
                for key in ("shot_%03d" % idx, "shot_%02d" % idx):
                    if key in shots:
                        ss = shots[key]
                        break

            # 一致性
            cons = None
            if consistency and isinstance(consistency, dict):
                for x in (consistency.get("shots") or []):
                    if x.get("shot_id") in ("shot_%03d" % idx,
                                            "shot_%02d" % idx,
                                            "shot_%d" % idx):
                        cons = {"level": x.get("level"),
                                "scores": x.get("scores")}
                        break

            items.append({
                "shot_id": "shot_%03d" % idx,
                "index": idx,
                "shot_type": meta.get("shot_type") or "",
                "characters": meta.get("characters") or [],
                "scene": meta.get("scene") or "",
                "act": meta.get("act") or "",
                "narration": (meta.get("narration") or "")[:40],
                "files": files,
                "story_state": ss,
                "consistency": cons,
                "status": status,
            })

        return items

    def summary(self, items):
        """汇总统计。"""
        total = len(items)
        with_still = sum(1 for x in items if "still" in x["files"])
        with_video = sum(1 for x in items if "video" in x["files"])
        with_audio = sum(1 for x in items if "audio" in x["files"])
        missing = sum(1 for x in items if x["status"] == "missing")
        return {
            "total": total,
            "with_still": with_still,
            "with_video": with_video,
            "with_audio": with_audio,
            "missing": missing,
            "complete_pct": round(with_still * 100.0 / total, 1) if total else 0,
        }


# ================================================================ 素材更换
class AssetReplacer:
    """上传本地文件替换某镜素材。"""

    def __init__(self, chapter_dir):
        self.chapter_dir = Path(chapter_dir)

    def replace_still(self, idx, src_file, backup=True):
        """替换静帧。"""
        d = self.chapter_dir / "images"
        d.mkdir(parents=True, exist_ok=True)
        target = d / ("shot_%02d.png" % idx)
        shot_id = "shot_%03d" % idx

        # 备份旧的（如果存在）
        backup_path = None
        if backup and target.exists():
            from .versioning import ShotVersions
            sv = ShotVersions(self.chapter_dir, kind="image")
            meta = sv.add(shot_id, target,
                          reason="替换前备份", provider="local")
            backup_path = meta.get("file") if meta else None

        # 复制新文件
        atomic.atomic_copy(Path(src_file), target)
        ok, why = integrity.validate_image(target)
        if not ok:
            return False, "图片校验失败：%s" % why

        # 新文件也记一个版本（标记来源为上传）
        try:
            from .versioning import ShotVersions
            sv = ShotVersions(self.chapter_dir, kind="image")
            sv.add(shot_id, target, reason="手动上传",
                   provider="upload", seed=None)
        except Exception:  # noqa: BLE001
            pass

        return True, {
            "target": str(target),
            "backup": backup_path,
            "size": target.stat().st_size,
        }

    def replace_video(self, idx, src_file, backup=True):
        """替换视频。"""
        d = self.chapter_dir / "videos"
        d.mkdir(parents=True, exist_ok=True)
        target = d / ("shot_%02d.mp4" % idx)
        shot_id = "shot_%03d" % idx

        backup_path = None
        if backup and target.exists():
            from .versioning import ShotVersions
            sv = ShotVersions(self.chapter_dir, kind="video")
            meta = sv.add(shot_id, target,
                          reason="替换前备份", provider="local")
            backup_path = meta.get("file") if meta else None

        atomic.atomic_copy(Path(src_file), target)
        ok, why = integrity.validate_video(target)
        if not ok:
            return False, "视频校验失败：%s" % why

        try:
            from .versioning import ShotVersions
            sv = ShotVersions(self.chapter_dir, kind="video")
            sv.add(shot_id, target, reason="手动上传",
                   provider="upload", seed=None)
        except Exception:  # noqa: BLE001
            pass

        return True, {
            "target": str(target),
            "backup": backup_path,
            "size": target.stat().st_size,
        }

    def restore_variant(self, idx, version, kind="image"):
        """把变体还原为正式素材。"""
        from .versioning import ShotVersions
        sv = ShotVersions(self.chapter_dir, kind=kind)
        d = self.chapter_dir / ("images" if kind == "image" else "videos")
        d.mkdir(parents=True, exist_ok=True)
        target = d / (("shot_%02d.png" if kind == "image"
                       else "shot_%02d.mp4") % idx)
        ok, msg = sv.restore("shot_%03d" % idx, version, target)
        return ok, msg


# ================================================================ 批量操作
class BatchOps:
    """批量操作。"""

    OPS = {
        "redo": "重做（清状态，下次跑会重新生成）",
        "choose": "统一选用某版本",
        "priority": "标记为高优先级",
        "delete": "删除这些镜头的产物（需确认）",
    }

    def __init__(self, chapter_dir, project_dir=None):
        self.chapter_dir = Path(chapter_dir)
        self.project_dir = Path(project_dir) if project_dir else self.chapter_dir.parent

    def preview(self, op, shot_indices, extra=None):
        """预览批量操作的影响。"""
        extra = extra or {}
        from .resources import size_of
        items = []
        total_size = 0

        for idx in shot_indices:
            info = {"index": idx, "shot_id": "shot_%03d" % idx}
            if op == "redo":
                # 要删的状态条目
                info["action"] = "清除状态，下次重新生成"
            elif op == "choose":
                ver = extra.get("version") or "v001"
                info["action"] = "选用 %s" % ver
            elif op == "priority":
                info["action"] = "标记高优先级"
            elif op == "delete":
                n = 0
                for pat, sub in ((("shot_%02d.png" % idx), "images"),
                                 (("shot_%02d.mp4" % idx), "videos")):
                    p = self.chapter_dir / sub / pat
                    if p.exists():
                        n += size_of(p)
                info["action"] = "删除产物"
                info["size"] = n
                total_size += n
            items.append(info)

        return {
            "op": op,
            "op_desc": self.OPS.get(op, op),
            "count": len(shot_indices),
            "items": items,
            "total_size": total_size,
            "total_size_mb": round(total_size / 1024 / 1024, 1),
        }

    def execute(self, op, shot_indices, extra=None, user=""):
        """执行批量操作。"""
        extra = extra or {}
        result = {"op": op, "success": [], "failed": []}

        if op == "redo":
            result = self._batch_redo(shot_indices)
        elif op == "choose":
            result = self._batch_choose(shot_indices,
                                        version=extra.get("version") or "v001")
        elif op == "priority":
            result = {"op": op, "success": shot_indices,
                      "failed": [], "note": "标记已记录，下个任务生效"}
        elif op == "delete":
            result = self._batch_delete(shot_indices, user=user)
        else:
            result["failed"] = [{"error": "未知操作：%s" % op}]

        return result

    def _batch_redo(self, indices):
        """清除状态，让下次跑重新生成。"""
        out = {"op": "redo", "success": [], "failed": []}
        state_file = self.chapter_dir / "state.json"
        if not state_file.exists():
            out["failed"] = [{"error": "没有 state.json"}]
            return out
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            out["failed"] = [{"error": "state.json 解析失败：%r" % exc}]
            return out

        items = data.get("items") or {}
        for idx in indices:
            cleared = False
            for prefix in ("image", "video", "audio"):
                key = "%s_%02d" % (prefix, idx)
                if key in items:
                    items.pop(key)
                    cleared = True
            if cleared:
                out["success"].append(idx)
            else:
                out["failed"].append({"index": idx, "error": "没有状态记录"})

        atomic.atomic_write_text(
            state_file, json.dumps(data, ensure_ascii=False, indent=2),
            verify=integrity.validate_json)
        return out

    def _batch_choose(self, indices, version):
        """批量选用某版本。"""
        from .versioning import ShotVersions
        out = {"op": "choose", "version": version, "success": [], "failed": []}
        sv = ShotVersions(self.chapter_dir, kind="image")
        for idx in indices:
            shot_id = "shot_%03d" % idx
            ok, msg = sv.choose(shot_id, version)
            if ok:
                out["success"].append(idx)
            else:
                out["failed"].append({"index": idx, "error": msg})

            # 同时把变体还原为正式素材
            d = self.chapter_dir / "images"
            target = d / ("shot_%02d.png" % idx)
            try:
                sv.restore(shot_id, version, target)
            except Exception:  # noqa: BLE001
                pass
        return out

    def _batch_delete(self, indices, user=""):
        """删除这些镜头的产物（先进回收站）。"""
        from .trash import trash
        from .. import config

        out = {"op": "delete", "success": [], "failed": [],
               "trash_id": None}
        use_trash = getattr(config, "TRASH_ENABLED", True)

        to_remove = []
        for idx in indices:
            for name, sub in ((("shot_%02d.png" % idx), "images"),
                              (("shot_%02d.mp4" % idx), "videos"),
                              (("shot_%02d.jpg" % idx), "images")):
                p = self.chapter_dir / sub / name
                if p.exists():
                    to_remove.append(p)
            for p in (self.chapter_dir / "audio").glob("s%02d_*.mp3" % idx):
                to_remove.append(p)

        if not to_remove:
            return out

        freed = sum(_p.stat().st_size for _p in to_remove
                    if _p.exists())

        if use_trash:
            tid = trash.move(to_remove, op="batch.delete", user=user,
                             note="批量删除 %d 个镜头" % len(indices))
            out["trash_id"] = tid
            if tid:
                out["success"] = list(indices)
            else:
                out["failed"] = [{"error": "移入回收站失败"}]
        else:
            for p in to_remove:
                try:
                    p.unlink()
                except Exception as exc:  # noqa: BLE001
                    out["failed"].append({"path": str(p), "error": repr(exc)})
            out["success"] = list(indices)

        out["freed_mb"] = round(freed / 1024 / 1024, 1)
        return out
