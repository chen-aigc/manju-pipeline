# -*- coding: utf-8 -*-
"""缓存与资源管理。

四个维度：
    1. 磁盘扫描     找出哪些目录/文件占空间
    2. 缓存清理     临时文件、旧日志、过期版本
    3. 配额检查     超过阈值预警（走告警系统）
    4. LRU 淘汰     超过配额时自动删最久不用的

清理策略（按安全性分级）：
    safe      绝对安全：.tmp / .part / 空目录 / 损坏隔离区
    normal    一般安全：旧日志（>N 天）/ 旧抽帧 / 旧版本（保留最新 M 个）
    aggressive 需确认：未选用的镜头变体 / 旧快照

永不自动删除：
    - 当前选用的镜头版本
    - 最新的快照
    - projects/<主项目>/chapter_* 的正式产物
    - 配置文件

用法：
    from manju.core.resources import resources

    # 扫描
    report = resources.scan(projects_dir, scan_extra=[comfyui_models])

    # 清理
    result = resources.cleanup(dry_run=True)     # 先看能省多少
    result = resources.cleanup(dry_run=False)    # 真删

    # 检查配额
    alerts = resources.check_quota()
"""
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------- 配置
@dataclass
class CleanupPolicy:
    """清理策略。"""
    # 临时文件
    remove_tmp: bool = True

    # 日志：超过 N 天的删
    log_keep_days: int = 14

    # 抽帧目录（_*抽帧）：超过 N 天的删
    frames_keep_days: int = 7

    # 剧本版本：保留最新 N 个
    script_versions_keep: int = 10

    # 镜头变体：每镜保留最新 N 个（当前选用的永不删）
    shot_variants_keep: int = 5

    # 快照：保留最新 N 个
    snapshots_keep: int = 10

    # 实验目录：超过 N 天没访问的可以归档（不自动删）
    experiments_archive_days: int = 30

    # 测试项目：超过 N 天没动的可清
    stale_project_days: int = 7

    # 配额（MB，0 = 不限）
    quota_project_mb: int = 2048
    quota_total_mb: int = 8192


@dataclass
class ScanEntry:
    """一个待清理项。"""
    kind: str                  # tmp / log / frames / script_version / shot_variant / snapshot / experiment
    path: str
    size: int = 0
    reason: str = ""
    safe: str = "normal"       # safe / normal / aggressive


@dataclass
class ScanReport:
    entries: List[ScanEntry] = field(default_factory=list)
    dirs: List[Dict] = field(default_factory=list)

    def total(self):
        return sum(e.size for e in self.entries)

    def by_kind(self):
        out = {}
        for e in self.entries:
            out[e.kind] = out.get(e.kind, 0) + e.size
        return out

    def by_safety(self):
        out = {}
        for e in self.entries:
            out[e.safe] = out.get(e.safe, 0) + e.size
        return out

    def to_dict(self):
        return {
            "entries": [
                {"kind": e.kind, "path": e.path, "size": e.size,
                 "size_mb": round(e.size / 1024 / 1024, 1),
                 "reason": e.reason, "safe": e.safe}
                for e in sorted(self.entries, key=lambda x: -x.size)
            ],
            "total_size": self.total(),
            "total_mb": round(self.total() / 1024 / 1024, 1),
            "by_kind": {k: round(v / 1024 / 1024, 1)
                        for k, v in self.by_kind().items()},
            "by_safety": {k: round(v / 1024 / 1024, 1)
                          for k, v in self.by_safety().items()},
            "dirs": self.dirs,
        }


# ---------------------------------------------------------------- 工具
def _size(path):
    """文件或目录的大小（字节）。"""
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# 公开别名（web 层用）
size_of = _size


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0


def _age_days(path):
    return (time.time() - _mtime(path)) / 86400.0


def _remove(path, use_trash=True, op="resource.cleanup", user=""):
    """删文件或目录，返回 (ok, msg)。

    默认走回收站（可撤销）。回收站不可用时才真删。
    """
    p = Path(path)
    if not p.exists():
        return False, "路径不存在"

    # 优先走回收站
    if use_trash:
        try:
            from .trash import trash
            from .. import config
            if getattr(config, "TRASH_ENABLED", True):
                tid = trash.move([p], op=op, user=user)
                if tid:
                    return True, "已移入回收站（%s）" % tid
        except Exception:  # noqa: BLE001
            pass

    # 兜底：真删
    try:
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
        return True, "已删除"
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


# ================================================================ 管理器
class ResourceManager:
    """缓存与资源管理器。"""

    def __init__(self, projects_dir=None, root_dir=None):
        self.projects_dir = Path(projects_dir) if projects_dir else None
        self.root_dir = Path(root_dir) if root_dir else (
            self.projects_dir.parent if self.projects_dir else None)
        self.policy = CleanupPolicy()
        self._lock = threading.RLock()

    # ---------------- 扫描 ----------------
    def scan(self, extra_dirs=None):
        """扫描所有可清理项。"""
        rep = ScanReport()
        if not self.projects_dir:
            return rep

        # 1. 临时文件
        if self.policy.remove_tmp:
            for pat in ("**/*.tmp", "**/*.part", "**/*.new"):
                for p in self.projects_dir.glob(pat):
                    if p.is_file():
                        rep.entries.append(ScanEntry(
                            kind="tmp", path=str(p), size=_size(p),
                            reason="临时文件残留", safe="safe"))

        # 2. 旧日志
        if self.policy.log_keep_days > 0:
            for p in self.projects_dir.glob("**/logs/*.jsonl"):
                if p.is_file() and _age_days(p) > self.policy.log_keep_days:
                    rep.entries.append(ScanEntry(
                        kind="log", path=str(p), size=_size(p),
                        reason="%.0f 天前的日志" % _age_days(p),
                        safe="normal"))

        # 3. 抽帧目录
        if self.root_dir and self.policy.frames_keep_days > 0:
            for p in self.root_dir.glob("_*抽帧*"):
                if p.is_dir() and _age_days(p) > self.policy.frames_keep_days:
                    rep.entries.append(ScanEntry(
                        kind="frames", path=str(p), size=_size(p),
                        reason="%.0f 天前的抽帧目录" % _age_days(p),
                        safe="normal"))

        # 4. 剧本版本（每个项目每集保留最新 N 个）
        for vdir in self.projects_dir.glob("*/chapter_*/history/script"):
            if not vdir.is_dir():
                continue
            files = sorted([p for p in vdir.glob("*.json")
                            if not p.name.endswith(".meta.json")],
                           key=lambda x: x.name, reverse=True)
            keep = self.policy.script_versions_keep
            for p in files[keep:]:
                meta = p.with_suffix(p.suffix + ".meta.json")
                rep.entries.append(ScanEntry(
                    kind="script_version", path=str(p), size=_size(p),
                    reason="超出保留数（最新 %d 个）" % keep,
                    safe="normal"))
                if meta.exists():
                    rep.entries.append(ScanEntry(
                        kind="script_version", path=str(meta), size=_size(meta),
                        reason="对应的元数据", safe="normal"))

        # 5. 镜头变体（每镜保留最新 N 个，选用的永不删）
        for vdir in self.projects_dir.glob("*/chapter_*/images/variants/*"):
            if not vdir.is_dir():
                continue
            chosen = None
            cf = vdir / "chosen.json"
            if cf.exists():
                try:
                    chosen = json.loads(cf.read_text(encoding="utf-8")).get("chosen")
                except Exception:  # noqa: BLE001
                    pass
            imgs = sorted([p for p in vdir.iterdir()
                           if p.suffix.lower() in (".png", ".jpg", ".mp4", ".mov")],
                          key=lambda x: x.name, reverse=True)
            keep = self.policy.shot_variants_keep
            for p in imgs[keep:]:
                # 选用的那版永不删
                if chosen and p.name.startswith(chosen + "_"):
                    continue
                rep.entries.append(ScanEntry(
                    kind="shot_variant", path=str(p), size=_size(p),
                    reason="超出保留数（最新 %d 个）" % keep,
                    safe="aggressive"))
                meta = p.with_suffix(p.suffix + ".meta.json")
                # 实际元数据文件名是 <stem>.meta.json
                meta2 = p.parent / (p.stem + ".meta.json")
                for m in (meta, meta2):
                    if m.exists():
                        rep.entries.append(ScanEntry(
                            kind="shot_variant", path=str(m), size=_size(m),
                            reason="对应的元数据", safe="aggressive"))
                        break

        # 6. 快照（保留最新 N 个）
        for sdir in self.projects_dir.glob("*/snapshots"):
            if not sdir.is_dir():
                continue
            snaps = sorted([p for p in sdir.iterdir() if p.is_dir()],
                           key=lambda x: x.name, reverse=True)
            keep = self.policy.snapshots_keep
            for p in snaps[keep:]:
                rep.entries.append(ScanEntry(
                    kind="snapshot", path=str(p), size=_size(p),
                    reason="超出保留数（最新 %d 个）" % keep,
                    safe="aggressive"))

        # 7. 实验目录（只统计，不自动删）
        if self.root_dir and self.policy.experiments_archive_days > 0:
            for p in self.root_dir.glob("_*"):
                if not p.is_dir():
                    continue
                age = _age_days(p)
                if age > self.policy.experiments_archive_days:
                    rep.entries.append(ScanEntry(
                        kind="experiment", path=str(p), size=_size(p),
                        reason="%.0f 天未访问的实验目录" % age,
                        safe="aggressive"))

        # 7b. 空实验目录（安全清理）
        if self.root_dir:
            for p in self.root_dir.glob("_*"):
                if p.is_dir():
                    try:
                        empty = not any(p.iterdir())
                    except OSError:
                        empty = False
                    if empty:
                        rep.entries.append(ScanEntry(
                            kind="empty_dir", path=str(p), size=0,
                            reason="空目录", safe="safe"))

        # 7c. 废弃测试项目（名字像测试且超过 N 天没动）
        test_pat = re.compile(
            r'^(Test[A-Z]?|AuthTest\d*|ProgressTest|ProgressDemo|'
            r'MonitorDemo|PermTest|测试项目|_trash|_试水)$')
        if self.projects_dir and self.projects_dir.exists():
            for p in self.projects_dir.iterdir():
                if not p.is_dir():
                    continue
                if test_pat.match(p.name):
                    age = _age_days(p)
                    if age > self.policy.stale_project_days:
                        rep.entries.append(ScanEntry(
                            kind="stale_project", path=str(p), size=_size(p),
                            reason="测试项目，%.0f 天未动" % age,
                            safe="normal"))

        # 7d. 超大项目（只提示，不删）
        if self.projects_dir and self.projects_dir.exists():
            for p in self.projects_dir.iterdir():
                if not p.is_dir():
                    continue
                mb = _size(p) / 1024 / 1024
                if mb > 300:
                    rep.entries.append(ScanEntry(
                        kind="large_project", path=str(p), size=int(mb * 1024 * 1024),
                        reason="项目过大 %.0f MB，建议归档" % mb,
                        safe="aggressive"))

        # 8. 目录统计（不删，只展示）
        for p in sorted(self.projects_dir.iterdir()):
            if p.is_dir():
                rep.dirs.append({
                    "name": p.name,
                    "size_mb": round(_size(p) / 1024 / 1024, 1),
                    "age_days": round(_age_days(p), 1),
                })
        rep.dirs.sort(key=lambda x: -x["size_mb"])

        # 9. 外部目录（如 ComfyUI 模型）
        if extra_dirs:
            for name, path in extra_dirs:
                if Path(path).exists():
                    rep.dirs.append({
                        "name": "[外部] " + name,
                        "size_mb": round(_size(path) / 1024 / 1024, 1),
                        "age_days": 0,
                        "external": True,
                        "path": str(path),
                    })

        return rep

    # ---------------- 清理 ----------------
    def cleanup(self, dry_run=True, max_safety="aggressive", kinds=None,
                user="", use_trash=True):
        """执行清理。

        Args:
            dry_run:     True 只统计不删
            max_safety:  最高清理等级（safe / normal / aggressive）
            kinds:       只清这几类（None = 全部）

        Returns:
            {"removed": [...], "freed_mb": ..., "failed": [...]}
        """
        order = {"safe": 0, "normal": 1, "aggressive": 2}
        limit = order.get(max_safety, 2)

        rep = self.scan()
        removed = []
        freed = 0
        failed = []

        for e in rep.entries:
            if order.get(e.safe, 2) > limit:
                continue
            if kinds and e.kind not in kinds:
                continue

            if dry_run:
                removed.append({"path": e.path, "size": e.size,
                                "kind": e.kind, "reason": e.reason})
                freed += e.size
                continue

            ok, msg = _remove(e.path, use_trash=use_trash,
                              op="resource.cleanup", user=user)
            if ok:
                removed.append({"path": e.path, "size": e.size,
                                "kind": e.kind, "reason": e.reason})
                freed += e.size
            else:
                failed.append({"path": e.path, "error": msg})

        return {
            "dry_run": dry_run,
            "removed": removed,
            "removed_count": len(removed),
            "freed_mb": round(freed / 1024 / 1024, 1),
            "freed_bytes": freed,
            "failed": failed,
            "failed_count": len(failed),
        }

    # ---------------- 配额检查 ----------------
    def check_quota(self):
        """检查磁盘配额，超了返回告警列表。"""
        alerts = []
        if not self.projects_dir or not self.root_dir:
            return alerts

        # 1. 项目目录总量
        total_mb = 0
        for p in self.projects_dir.iterdir() if self.projects_dir.exists() else []:
            if p.is_dir():
                total_mb += _size(p) / 1024 / 1024

        if self.policy.quota_total_mb > 0 and total_mb > self.policy.quota_total_mb:
            alerts.append({
                "kind": "disk_total",
                "level": "warn",
                "msg": "项目总占用 %.0f MB 超过配额 %.0f MB"
                       % (total_mb, self.policy.quota_total_mb),
                "value": round(total_mb, 1),
                "limit": self.policy.quota_total_mb,
            })

        # 2. 单个项目
        if self.projects_dir.exists():
            for p in self.projects_dir.iterdir():
                if not p.is_dir():
                    continue
                mb = _size(p) / 1024 / 1024
                if (self.policy.quota_project_mb > 0
                        and mb > self.policy.quota_project_mb):
                    alerts.append({
                        "kind": "disk_project",
                        "level": "warn",
                        "msg": "项目 %s 占用 %.0f MB 超过配额 %.0f MB"
                               % (p.name, mb, self.policy.quota_project_mb),
                        "value": round(mb, 1),
                        "limit": self.policy.quota_project_mb,
                        "project": p.name,
                    })

        # 3. 磁盘剩余空间
        try:
            usage = shutil.disk_usage(str(self.root_dir))
            free_gb = usage.free / 1024 / 1024 / 1024
            if free_gb < 10:
                alerts.append({
                    "kind": "disk_free",
                    "level": "error" if free_gb < 5 else "warn",
                    "msg": "磁盘剩余 %.1f GB，建议清理" % free_gb,
                    "value": round(free_gb, 1),
                    "limit": 10,
                })
        except Exception:  # noqa: BLE001
            pass

        return alerts

    def set_policy(self, **kw):
        """修改清理策略。"""
        for k, v in kw.items():
            if hasattr(self.policy, k):
                setattr(self.policy, k, v)
        return self.policy.__dict__

    def summary(self, extra_dirs=None):
        """一次拿全部资源信息（给 Web 用）。"""
        rep = self.scan(extra_dirs=extra_dirs)
        d = rep.to_dict()
        d["quota_alerts"] = self.check_quota()
        d["policy"] = self.policy.__dict__
        return d

    # ---------------- LRU 淘汰 ----------------
    def lru_evict(self, target_free_mb=1000, dry_run=True,
                  user="", use_trash=True):
        """按"最久未用"的顺序删，直到腾出 target_free_mb。

        只删 aggressive 级（镜头变体/快照/实验目录），
        且永不删选用的版本和最新快照。
        """
        rep = self.scan()
        # 按 mtime 升序（最旧的先删）
        candidates = [e for e in rep.entries if e.safe in ("normal", "aggressive")]
        candidates.sort(key=lambda x: _mtime(x.path))

        freed = 0
        removed = []
        for e in candidates:
            if freed >= target_free_mb * 1024 * 1024:
                break
            if dry_run:
                removed.append({"path": e.path, "size": e.size,
                                "kind": e.kind})
                freed += e.size
            else:
                ok, _ = _remove(e.path, use_trash=use_trash,
                               op="resource.lru", user=user)
                if ok:
                    removed.append({"path": e.path, "size": e.size,
                                    "kind": e.kind})
                    freed += e.size

        return {
            "dry_run": dry_run,
            "target_mb": target_free_mb,
            "freed_mb": round(freed / 1024 / 1024, 1),
            "removed_count": len(removed),
            "removed": removed,
        }


# ---------------------------------------------------------------- 全局
def make_resources():
    try:
        from .. import config
        mgr = ResourceManager(projects_dir=config.PROJECTS_DIR)
        # 从配置读配额
        mgr.set_policy(
            quota_project_mb=int(getattr(config, "RES_QUOTA_PROJECT_MB", 2048)),
            quota_total_mb=int(getattr(config, "RES_QUOTA_TOTAL_MB", 8192)),
            log_keep_days=int(getattr(config, "RES_LOG_KEEP_DAYS", 14)),
            frames_keep_days=int(getattr(config, "RES_FRAMES_KEEP_DAYS", 7)),
            script_versions_keep=int(getattr(config, "RES_SCRIPT_VERSIONS_KEEP", 10)),
            shot_variants_keep=int(getattr(config, "RES_SHOT_VARIANTS_KEEP", 5)),
            snapshots_keep=int(getattr(config, "RES_SNAPSHOTS_KEEP", 10)),
        )
        return mgr
    except Exception:  # noqa: BLE001
        return ResourceManager()


resources = make_resources()
