# -*- coding: utf-8 -*-
"""版本与快照管理。

四类版本对象：

1. 文件版本（file）
    每次覆盖前自动留旧版。
    目录结构：<base>/history/<name>/<timestamp>_<reason>.<ext>
    支持：列表、回滚、对比

2. 镜头版本（shot）
    同一镜多次生成的图/视频都保留。
    目录结构：<chapter>/images/variants/shot_017/v001_<seed>.png
    支持：列表、选用某版、删除某版

3. 项目快照（snapshot）
    关键节点打整包（剧本 + 状态 + 配置 + 图片索引）。
    目录结构：<project>/snapshots/<name>_<timestamp>/
    支持：创建、列表、恢复

4. 配置版本（config）
    所有配置改动留痕。
    目录结构：<project>/history/config/<timestamp>.json
    支持：列表、回滚、diff

设计原则：
    - 一切不可变：已归档的版本只读
    - 一切可追溯：每个版本带 reason / source / ts
    - 一切可回滚：任何版本都能一键恢复
"""
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------- 工具
def _ts():
    return time.strftime("%Y%m%d_%H%M%S")


def _sha1(path, limit=None):
    """算文件哈希（大文件只算前后各 1MB）。"""
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha1()
    size = p.stat().st_size
    if limit and size > limit:
        with p.open("rb") as f:
            h.update(f.read(1024 * 1024))
            f.seek(max(0, size - 1024 * 1024))
            h.update(f.read(1024 * 1024))
        h.update(str(size).encode())
    else:
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()[:16]


def _safe_name(s):
    """把任意字符串变成可做目录名的安全形式。"""
    out = []
    for ch in str(s):
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:60] or "unnamed"


# ================================================================ 文件版本
class FileVersions:
    """单个文件的版本历史。

    用法：
        fv = FileVersions(project_dir / "script.json")
        fv.archive("重新生成", source="deepseek")   # 覆盖前调用
        versions = fv.list()                        # 列出所有版本
        fv.rollback(versions[-1]["id"])             # 回滚
    """

    def __init__(self, path):
        self.path = Path(path)
        self.history_dir = self.path.parent / "history" / _safe_name(self.path.stem)

    def archive(self, reason="", source="", note=""):
        """把当前文件归档为一个版本。文件不存在则跳过。"""
        if not self.path.exists():
            return None
        self.history_dir.mkdir(parents=True, exist_ok=True)

        stamp = _ts()
        ext = self.path.suffix
        stem = "%s_%s" % (stamp, _safe_name(reason or "auto"))
        target = self.history_dir / (stem + ext)

        # 防重名
        n = 1
        while target.exists():
            target = self.history_dir / ("%s_%d%s" % (stem, n, ext))
            n += 1

        shutil.copy2(self.path, target)

        # 记元数据
        meta = {
            "id": target.stem,
            "file": target.name,
            "reason": reason,
            "source": source,
            "note": note,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "size": target.stat().st_size,
            "sha1": _sha1(target),
        }
        (self.history_dir / (target.stem + ".meta.json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def list(self):
        """列出所有版本（按时间倒序）。"""
        if not self.history_dir.exists():
            return []
        out = []
        for meta_file in self.history_dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                target = self.history_dir / meta.get("file", "")
                meta["exists"] = target.exists()
                out.append(meta)
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return out

    def get(self, version_id):
        for v in self.list():
            if v.get("id") == version_id:
                return v
        return None

    def rollback(self, version_id, keep_current=True):
        """回滚到指定版本。"""
        v = self.get(version_id)
        if not v:
            return False, "版本不存在：%s" % version_id
        src = self.history_dir / v["file"]
        if not src.exists():
            return False, "版本文件丢失：%s" % v["file"]

        if keep_current and self.path.exists():
            self.archive("回滚前备份", note="回滚到 %s" % version_id)

        shutil.copy2(src, self.path)
        return True, "已回滚到 %s（%s）" % (version_id, v.get("reason", ""))

    def read(self, version_id):
        """读某版本内容（文本文件）。"""
        v = self.get(version_id)
        if not v:
            return None
        src = self.history_dir / v["file"]
        if not src.exists():
            return None
        try:
            return src.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None

    def diff(self, version_a, version_b):
        """比较两个版本（文本文件，返回简单的行差异）。"""
        import difflib
        a = self.read(version_a)
        b = self.read(version_b)
        if a is None or b is None:
            return ""
        la = a.splitlines()
        lb = b.splitlines()
        return "\n".join(difflib.unified_diff(
            la, lb, fromfile=version_a, tofile=version_b, lineterm=""))


# ================================================================ 镜头版本
class ShotVersions:
    """同一镜头的多版本（图/视频）。

    目录结构：
        <chapter>/images/variants/shot_017/
            v001_20261118_120000_seed42.png
            v002_20261118_120530_seed99.png
            chosen.json   （记录当前选用哪个）
    """

    def __init__(self, chapter_dir, kind="image"):
        self.chapter_dir = Path(chapter_dir)
        self.kind = kind
        sub = "images" if kind == "image" else "videos"
        ext = ".png" if kind == "image" else ".mp4"
        self.ext = ext
        self.base_dir = self.chapter_dir / sub / "variants"

    def _shot_dir(self, shot_id):
        return self.base_dir / _safe_name(shot_id)

    def add(self, shot_id, src_path, reason="", seed=None, provider=""):
        """把一版归档到该镜的版本目录。"""
        src = Path(src_path)
        if not src.exists():
            return None
        d = self._shot_dir(shot_id)
        d.mkdir(parents=True, exist_ok=True)

        # 找下一个序号
        existing = sorted(d.glob("v[0-9][0-9][0-9]_*" + self.ext))
        n = 1
        if existing:
            try:
                n = int(existing[-1].name[1:4]) + 1
            except (ValueError, IndexError):
                n = len(existing) + 1

        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = "v%03d_%s" % (n, stamp)
        if seed is not None:
            name += "_seed%s" % seed
        target = d / (name + self.ext)

        # 防重名
        k = 1
        while target.exists():
            target = d / ("%s_%d%s" % (name, k, self.ext))
            k += 1

        shutil.copy2(src, target)

        meta = {
            "version": "v%03d" % n,
            "file": target.name,
            "reason": reason,
            "seed": seed,
            "provider": provider,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "size": target.stat().st_size,
            "sha1": _sha1(target, limit=4 * 1024 * 1024),
        }
        (d / (name + ".meta.json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def list(self, shot_id):
        d = self._shot_dir(shot_id)
        if not d.exists():
            return []
        out = []
        for mf in d.glob("*.meta.json"):
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
                f = d / meta.get("file", "")
                meta["exists"] = f.exists()
                out.append(meta)
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x.get("version", ""))
        return out

    def choose(self, shot_id, version):
        """标记选用哪一版。"""
        d = self._shot_dir(shot_id)
        if not d.exists():
            return False, "没有该镜头的版本目录"
        versions = self.list(shot_id)
        hit = [v for v in versions if v.get("version") == version]
        if not hit:
            return False, "版本不存在：%s" % version
        (d / "chosen.json").write_text(
            json.dumps({"chosen": version,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return True, "已选用 %s" % version

    def chosen(self, shot_id):
        d = self._shot_dir(shot_id)
        f = d / "chosen.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("chosen")
        except Exception:  # noqa: BLE001
            return None

    def restore(self, shot_id, version, target_path):
        """把某版本还原成正式的 shot_XX。"""
        d = self._shot_dir(shot_id)
        versions = self.list(shot_id)
        hit = [v for v in versions if v.get("version") == version]
        if not hit:
            return False, "版本不存在：%s" % version
        src = d / hit[0]["file"]
        if not src.exists():
            return False, "版本文件丢失"
        shutil.copy2(src, target_path)
        self.choose(shot_id, version)
        return True, "已还原 %s 到 %s" % (version, Path(target_path).name)


# ================================================================ 项目快照
class ProjectSnapshots:
    """项目级快照：剧本 + 状态 + 配置 + 图片索引。

    目录结构：
        <project>/snapshots/<timestamp>_<name>/
            manifest.json      快照元数据
            script.json        剧本
            state.json         任务状态
            story_state.json   剧情状态
            consistency_report.json
            images/            图片索引（不是图片本身）
            config.json        配置副本
    """

    # 要包含的文件（相对 chapter_dir 或 project_dir）
    CHAPTER_FILES = (
        "script.json",
        "state.json",
        "story_state.json",
        "consistency_report.json",
        "chapter_%03d.srt",
        "cost.json",
    )

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.snapshots_dir = self.project_dir / "snapshots"

    def create(self, name="", chapter=None, note=""):
        """创建快照。"""
        chapter = int(chapter or 1)
        chapter_dir = self.project_dir / ("chapter_%03d" % chapter)
        if not chapter_dir.exists():
            return False, "集目录不存在：%s" % chapter_dir

        stamp = _ts()
        label = _safe_name(name or "snap")
        target = self.snapshots_dir / ("%s_%s" % (stamp, label))
        target.mkdir(parents=True, exist_ok=True)

        included = []

        # 1. 章节文件
        for pat in self.CHAPTER_FILES:
            p = chapter_dir / (pat % chapter if "%" in pat else pat)
            if p.exists():
                shutil.copy2(p, target / p.name)
                included.append(p.name)

        # 2. 图片索引（只记文件名+哈希，不复制图本身）
        img_dir = chapter_dir / "images"
        images = []
        if img_dir.exists():
            for p in sorted(img_dir.glob("shot_*.*")):
                if p.suffix.lower() not in (".png", ".jpg"):
                    continue
                images.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "sha1": _sha1(p, limit=1024 * 1024),
                })
        (target / "images.json").write_text(
            json.dumps(images, ensure_ascii=False, indent=2), encoding="utf-8")
        included.append("images.json")

        # 3. 配置副本
        cfg = {}
        try:
            from .. import config as C
            for k in ("IMAGE_PROVIDER", "VIDEO_PROVIDER", "TTS_PROVIDER",
                      "BGM_PROVIDER", "EDIT_PROVIDER", "SCRIPT_PROVIDER",
                      "IMAGE_MODEL", "OUTPUT_RESOLUTION", "JOB_MAX_CONCURRENT"):
                if hasattr(C, k):
                    cfg[k] = getattr(C, k)
        except Exception:  # noqa: BLE001
            pass
        (target / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        included.append("config.json")

        # 4. manifest
        manifest = {
            "name": name or label,
            "chapter": chapter,
            "note": note,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": included,
            "image_count": len(images),
            "dir": target.name,
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        return True, manifest

    def list(self):
        """列出所有快照（按时间倒序）。"""
        if not self.snapshots_dir.exists():
            return []
        out = []
        for d in self.snapshots_dir.iterdir():
            if not d.is_dir():
                continue
            mf = d / "manifest.json"
            if not mf.exists():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                m["dir"] = d.name
                out.append(m)
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return out

    def restore(self, snapshot_dir, chapter=None, keep_current=True):
        """从快照还原。"""
        src = self.snapshots_dir / snapshot_dir
        if not src.exists():
            return False, "快照不存在：%s" % snapshot_dir

        mf = src / "manifest.json"
        if not mf.exists():
            return False, "快照缺少 manifest.json"
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, "manifest 解析失败：%r" % exc

        chapter = int(chapter or manifest.get("chapter") or 1)
        chapter_dir = self.project_dir / ("chapter_%03d" % chapter)
        chapter_dir.mkdir(parents=True, exist_ok=True)

        # 先备份当前
        if keep_current:
            try:
                self.create("restore前备份", chapter=chapter,
                            note="还原 %s 之前" % snapshot_dir)
            except Exception:  # noqa: BLE001
                pass

        restored = []
        for fname in manifest.get("files") or []:
            if fname in ("images.json", "config.json", "manifest.json"):
                continue
            s = src / fname
            if not s.exists():
                continue
            shutil.copy2(s, chapter_dir / fname)
            restored.append(fname)

        return True, "已还原 %d 个文件：%s" % (len(restored), ", ".join(restored))

    def get(self, snapshot_dir):
        d = self.snapshots_dir / snapshot_dir
        mf = d / "manifest.json"
        if not mf.exists():
            return None
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            m["dir"] = d.name
            return m
        except Exception:  # noqa: BLE001
            return None

    def delete(self, snapshot_dir):
        d = self.snapshots_dir / snapshot_dir
        if not d.exists():
            return False, "快照不存在"
        shutil.rmtree(d)
        return True, "已删除 %s" % snapshot_dir


# ================================================================ 统一的版本管理器
class VersionManager:
    """一个项目所有版本操作的统一入口。"""

    def __init__(self, project_dir, chapter=1):
        self.project_dir = Path(project_dir)
        self.chapter = int(chapter)
        self.chapter_dir = self.project_dir / ("chapter_%03d" % self.chapter)
        self.snapshots = ProjectSnapshots(self.project_dir)

    # -------- 文件版本 --------
    def file_versions(self, rel_path):
        """取某个文件的版本管理器（rel_path 相对 chapter_dir）。"""
        return FileVersions(self.chapter_dir / rel_path)

    def archive_script(self, reason="重新生成", source=""):
        return self.file_versions("script.json").archive(reason, source)

    def list_script_versions(self):
        return self.file_versions("script.json").list()

    def rollback_script(self, version_id):
        return self.file_versions("script.json").rollback(version_id)

    # -------- 镜头版本 --------
    def shot_versions(self, kind="image"):
        return ShotVersions(self.chapter_dir, kind=kind)

    def list_shot_versions(self, shot_id, kind="image"):
        return self.shot_versions(kind).list(shot_id)

    def add_shot_version(self, shot_id, src, **kw):
        return self.shot_versions(kw.pop("kind", "image")).add(
            shot_id, src, **kw)

    def choose_shot_version(self, shot_id, version, kind="image"):
        return self.shot_versions(kind).choose(shot_id, version)

    def restore_shot_version(self, shot_id, version, target, kind="image"):
        return self.shot_versions(kind).restore(shot_id, version, target)

    # -------- 快照 --------
    def create_snapshot(self, name="", note=""):
        return self.snapshots.create(name, chapter=self.chapter, note=note)

    def list_snapshots(self):
        return self.snapshots.list()

    def restore_snapshot(self, snapshot_dir):
        return self.snapshots.restore(snapshot_dir, chapter=self.chapter)

    # -------- 总览 --------
    def overview(self):
        """一次拿全部版本信息（给 Web 用）。"""
        script_v = self.list_script_versions()
        snaps = self.list_snapshots()

        # 镜头版本统计
        shot_counts = {}
        vdir = self.chapter_dir / "images" / "variants"
        if vdir.exists():
            for d in vdir.iterdir():
                if not d.is_dir():
                    continue
                # 只数图片/视频，不数 .meta.json
                n = len([p for p in d.glob("v[0-9][0-9][0-9]_*")
                         if p.suffix.lower() in (".png", ".jpg", ".mp4", ".mov")])
                if n:
                    shot_counts[d.name] = n

        return {
            "chapter": self.chapter,
            "script_versions": len(script_v),
            "script_latest": script_v[0] if script_v else None,
            "snapshots": len(snaps),
            "snapshot_list": snaps[:10],
            "shot_variants": shot_counts,
            "shot_variant_total": sum(shot_counts.values()),
            "paths": {
                "chapter": str(self.chapter_dir),
                "history": str(self.chapter_dir / "history"),
                "variants": str(vdir),
                "snapshots": str(self.snapshots.snapshots_dir),
            },
        }
