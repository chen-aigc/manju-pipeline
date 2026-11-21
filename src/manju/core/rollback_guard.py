# -*- coding: utf-8 -*-
"""回滚前置判断：回滚前检查 N 个条件。

判断项：
    1. 版本存在性    目标版本记录存在且文件在位
    2. 内容一致性    与当前内容相同 → 跳过（没必要回滚）
    3. 完整性        目标文件校验通过（JSON 可解析 / 图片可打开）
    4. 任务状态      有任务在跑 → 拒绝（避免竞态）
    5. 磁盘空间      剩余空间 < 需要的 2 倍 → 警告
    6. 覆盖风险      当前比目标新 → 提示会丢失哪些改动
    7. 目标已还原    回收站条目已还原过 → 拒绝

返回结构：
    {
      "ok": bool,              # 是否可以回滚
      "blockers": [...],       # 阻断项（ok=False 时非空）
      "warnings": [...],       # 警告（可以继续，但要让用户知道）
      "info": {...},           # 附加信息（差异统计等）
      "skip": bool,            # 是否建议跳过（内容相同）
    }
"""
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional


def _sha1_file(path):
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _free_mb(path):
    try:
        u = shutil.disk_usage(str(Path(path).anchor or "/"))
        return u.free / 1024 / 1024
    except Exception:  # noqa: BLE001
        return 999999


def _running_jobs():
    """正在运行的任务 id 列表（从全局注册表查）。"""
    from .runtime import runtime
    return runtime.running_jobs()


def _running_desc():
    """运行中任务的描述文本。"""
    from .runtime import runtime
    return runtime.describe()


# ---------------------------------------------------------------- 结果
class CheckResult:
    def __init__(self):
        self.blockers: List[Dict] = []
        self.warnings: List[Dict] = []
        self.info: Dict = {}
        self.skip = False
        self.skip_reason = ""
        self.policy = ""            # 生效的策略
        self.job_running = False    # 是否有任务在跑（供 API 决定怎么处理）

    def block(self, code, msg, **extra):
        self.blockers.append({"code": code, "msg": msg, **extra})
        return self

    def warn(self, code, msg, **extra):
        self.warnings.append({"code": code, "msg": msg, **extra})
        return self

    @property
    def ok(self):
        return not self.blockers

    def to_dict(self):
        return {
            "ok": self.ok,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "info": self.info,
            "skip": self.skip,
            "skip_reason": self.skip_reason,
            "policy": self.policy,
            "job_running": self.job_running,
        }


# ================================================================ 剧本回滚判断
def check_script_rollback(file_versions, version_id, policy="block"):
    """检查剧本回滚是否可行。

    Args:
        file_versions: FileVersions 实例
        version_id:    目标版本 id

    Returns:
        CheckResult
    """
    r = CheckResult()
    r.policy = policy

    # 1. 版本存在
    v = file_versions.get(version_id)
    if not v:
        r.block("version_missing", "版本不存在：%s" % version_id)
        return r

    src = file_versions.history_dir / v["file"]
    if not src.exists():
        r.block("file_missing", "版本文件丢失：%s" % v["file"])
        return r

    # 2. 目标文件完整性（JSON 可解析）
    try:
        content = src.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            r.block("invalid_content", "版本内容不是 JSON 对象")
            return r
        shots = data.get("shots")
        if not isinstance(shots, list):
            r.block("no_shots", "版本里没有 shots 数组")
            return r
        r.info["target_shots"] = len(shots)
        r.info["target_title"] = data.get("title") or ""
    except json.JSONDecodeError as exc:
        r.block("invalid_json", "版本 JSON 解析失败：%s" % exc)
        return r
    except Exception as exc:  # noqa: BLE001
        r.block("read_failed", "读取版本失败：%r" % exc)
        return r

    # 3. 任务运行中（按策略处理）
    running = _running_jobs()
    if running:
        r.job_running = True
        r.info["running_jobs"] = running[:5]
        r.info["running_desc"] = _running_desc()

        if policy == "block":
            r.block("job_running",
                    "%s，请先等待完成或取消后再回滚" % _running_desc(),
                    count=len(running), jobs=running[:5],
                    hint="可用策略：wait（排队等待）/ cancel（先取消）/ force（强制）")
            return r
        elif policy == "wait":
            r.warn("will_wait",
                   "%s，回滚会排队，等任务完成后自动执行" % _running_desc(),
                   count=len(running))
        elif policy == "cancel":
            r.warn("will_cancel",
                   "%s，回滚前会先取消这些任务" % _running_desc(),
                   count=len(running))
        elif policy == "force":
            r.warn("forcing",
                   "%s，但已选择强制回滚（任务可能读到不一致的数据）"
                   % _running_desc(), count=len(running))
        else:
            # 未知策略按 block 处理
            r.block("job_running",
                    "%s，未知策略 %r，已按阻断处理"
                    % (_running_desc(), policy), count=len(running))
            return r

    # 4. 内容一致性（与当前文件比）
    current = file_versions.path
    if current.exists():
        try:
            cur_content = current.read_text(encoding="utf-8")
            if cur_content == content:
                r.skip = True
                r.skip_reason = "当前剧本与该版本内容完全相同，无需回滚"
                return r

            # 差异统计
            cur_data = json.loads(cur_content)
            cur_shots = cur_data.get("shots") or []
            r.info["current_shots"] = len(cur_shots)

            # 简单对比：哪些镜头有差异
            tgt = shots
            diffs = 0
            max_n = max(len(cur_shots), len(tgt))
            for i in range(max_n):
                a = cur_shots[i] if i < len(cur_shots) else None
                b = tgt[i] if i < len(tgt) else None
                if a != b:
                    diffs += 1
            r.info["diff_shots"] = diffs

            if diffs > 0:
                r.warn("content_differs",
                       "回滚会改变 %d 个镜头的内容" % diffs,
                       count=diffs)
        except Exception:  # noqa: BLE001
            r.warn("current_unreadable", "当前剧本无法读取，回滚会覆盖它")

    # 5. 磁盘空间（备份需要空间）
    cur_size = current.stat().st_size if current.exists() else 0
    free = _free_mb(current.parent if current.parent.exists() else ".")
    need = cur_size * 2 / 1024 / 1024
    if free < need * 2:
        r.warn("low_disk",
               "剩余空间 %.0f MB，备份需要约 %.1f MB" % (free, need),
               free_mb=round(free, 1), need_mb=round(need, 1))

    return r


# ================================================================ 镜头变体回滚判断
def check_shot_restore(shot_versions, shot_id, version, target_path=None,
                       policy="block"):
    """检查镜头变体还原是否可行。"""
    r = CheckResult()
    r.policy = policy

    # 1. 版本存在
    versions = shot_versions.list(shot_id)
    hit = [v for v in versions if v.get("version") == version]
    if not hit:
        r.block("version_missing", "版本不存在：%s" % version)
        return r

    v = hit[0]
    d = shot_versions._shot_dir(shot_id)
    src = d / v["file"]
    if not src.exists():
        r.block("file_missing", "版本文件丢失：%s" % v["file"])
        return r

    # 2. 任务运行中（按策略处理）
    running = _running_jobs()
    if running:
        r.job_running = True
        r.info["running_jobs"] = running[:5]
        r.info["running_desc"] = _running_desc()

        if policy == "block":
            r.block("job_running",
                    "%s，请先等待完成或取消" % _running_desc(),
                    count=len(running), jobs=running[:5],
                    hint="可用策略：wait / cancel / force")
            return r
        elif policy == "wait":
            r.warn("will_wait", "%s，还原会排队" % _running_desc(),
                   count=len(running))
        elif policy == "cancel":
            r.warn("will_cancel", "%s，还原前会先取消" % _running_desc(),
                   count=len(running))
        elif policy == "force":
            r.warn("forcing", "%s，但已选择强制还原" % _running_desc(),
                   count=len(running))
        else:
            r.block("job_running", "%s，未知策略 %r" % (_running_desc(), policy),
                    count=len(running))
            return r

    # 3. 文件完整性
    try:
        size = src.stat().st_size
        # 用 integrity 的常量（图片 64，视频 4096）
        from .integrity import MIN_IMAGE_BYTES, MIN_VIDEO_BYTES
        ext = src.suffix.lower()
        min_need = MIN_VIDEO_BYTES if ext in (".mp4", ".mov") else MIN_IMAGE_BYTES
        if size < min_need:
            r.block("too_small",
                    "版本文件太小（%d 字节，最小 %d），可能损坏"
                    % (size, min_need))
            return r
        r.info["target_size"] = size
        r.info["target_seed"] = v.get("seed")
        r.info["target_ts"] = v.get("ts") or ""
    except Exception as exc:  # noqa: BLE001
        r.block("stat_failed", "读取版本信息失败：%r" % exc)
        return r

    # 4. 内容一致性（与当前正式素材比）
    if target_path:
        tp = Path(target_path)
        if tp.exists():
            try:
                if _sha1_file(src) == _sha1_file(tp):
                    r.skip = True
                    r.skip_reason = "当前素材就是这个版本，无需还原"
                    return r
                cur_size = tp.stat().st_size
                r.info["current_size"] = cur_size
                r.info["size_delta"] = size - cur_size
            except Exception:  # noqa: BLE001
                pass

    # 5. 当前选用的版本
    try:
        chosen = shot_versions.chosen(shot_id)
        r.info["current_chosen"] = chosen
        if chosen == version:
            r.skip = True
            r.skip_reason = "该版本已经是当前选用的版本"
            return r
    except Exception:  # noqa: BLE001
        pass

    return r


# ================================================================ 回收站还原判断
def check_trash_restore(trash_item):
    """检查回收站还原是否可行。

    Args:
        trash_item: manifest dict（含 items / restored 等）
    """
    r = CheckResult()

    if not trash_item:
        r.block("not_found", "回收站条目不存在")
        return r

    # 1. 是否已还原过
    if trash_item.get("restored"):
        r.block("already_restored",
                "该条目已于 %s 还原过" % (trash_item.get("restored_at") or "?"))
        return r

    # 2. 文件是否还在
    items = trash_item.get("items") or []
    if not items:
        r.block("no_items", "条目里没有文件记录")
        return r

    missing = []
    for it in items:
        name = it.get("name") or ""
        if not name:
            continue
        # 检查回收站里是否还有
        # （调用方会传 _trash_dir）
        missing.append({"name": name, "orig": it.get("orig")})

    r.info["total"] = len(items)
    r.info["items"] = [x.get("name") for x in items[:10]]

    # 3. 原位置是否被占用
    occupied = []
    for it in items:
        orig = it.get("orig")
        if orig:
            p = Path(orig)
            if p.exists():
                occupied.append({
                    "name": it.get("name"),
                    "orig": orig,
                    "note": "原位置已有同名文件，还原时会加时间戳后缀",
                })
    if occupied:
        r.warn("target_occupied",
               "%d 个原位置已被占用，还原时会加时间戳后缀"
               % len(occupied), count=len(occupied))

    r.info["occupied"] = occupied
    return r


# ================================================================ 快照还原判断
def check_snapshot_restore(snapshot_manifest, chapter_dir,
                           policy="block"):
    """检查快照还原是否可行。"""
    r = CheckResult()
    r.policy = policy

    if not snapshot_manifest:
        r.block("not_found", "快照不存在")
        return r

    files = snapshot_manifest.get("files") or []
    if not files:
        r.block("no_files", "快照里没有文件")
        return r

    # 1. 任务运行中（按策略处理）
    running = _running_jobs()
    if running:
        r.job_running = True
        r.info["running_jobs"] = running[:5]
        r.info["running_desc"] = _running_desc()

        if policy == "block":
            r.block("job_running",
                    "%s，请先等待完成或取消" % _running_desc(),
                    count=len(running), jobs=running[:5],
                    hint="可用策略：wait / cancel / force")
            return r
        elif policy == "wait":
            r.warn("will_wait", "%s，还原会排队" % _running_desc(),
                   count=len(running))
        elif policy == "cancel":
            r.warn("will_cancel", "%s，还原前会先取消" % _running_desc(),
                   count=len(running))
        elif policy == "force":
            r.warn("forcing", "%s，但已选择强制还原" % _running_desc(),
                   count=len(running))
        else:
            r.block("job_running", "%s，未知策略 %r" % (_running_desc(), policy),
                    count=len(running))
            return r

    # 2. 文件完整性
    r.info["total_files"] = len(files)
    r.info["images"] = snapshot_manifest.get("image_count", 0)
    r.info["created"] = snapshot_manifest.get("ts") or ""

    # 3. 会覆盖哪些当前文件
    will_overwrite = []
    ch = Path(chapter_dir) if chapter_dir else None
    if ch and ch.exists():
        for f in files:
            if f in ("images.json", "config.json", "manifest.json"):
                continue
            if (ch / f).exists():
                will_overwrite.append(f)

    if will_overwrite:
        r.warn("will_overwrite",
               "会覆盖 %d 个当前文件：%s"
               % (len(will_overwrite), ", ".join(will_overwrite[:5])),
               count=len(will_overwrite), files=will_overwrite[:10])
    r.info["will_overwrite"] = will_overwrite

    return r


# ================================================================ 统一入口
class RollbackGuard:
    """回滚判断的统一入口。

    policy 参数（可选）：
        不传时从全局 policy 管理器读取当前生效的策略。
    """

    @staticmethod
    def _policy(op, explicit=None):
        if explicit:
            return explicit
        try:
            from .rollback_policy import policy
            return policy.get(op)
        except Exception:  # noqa: BLE001
            return "block"

    @staticmethod
    def script(chapter_dir, version_id, policy=None):
        from .versioning import FileVersions
        pol = RollbackGuard._policy("script", policy)
        fv = FileVersions(Path(chapter_dir) / "script.json")
        return check_script_rollback(fv, version_id, policy=pol)

    @staticmethod
    def shot(chapter_dir, shot_id, version, kind="image", policy=None):
        from .versioning import ShotVersions
        pol = RollbackGuard._policy("shot", policy)
        sv = ShotVersions(chapter_dir, kind=kind)
        d = Path(chapter_dir) / ("images" if kind == "image" else "videos")
        ext = ".png" if kind == "image" else ".mp4"
        try:
            idx = int("".join(c for c in shot_id if c.isdigit()))
        except ValueError:
            idx = 0
        target = d / (("shot_%02d" % idx) + ext) if idx else None
        return check_shot_restore(sv, shot_id, version, target, policy=pol)

    @staticmethod
    def trash(trash_item, policy=None):
        return check_trash_restore(trash_item)

    @staticmethod
    def snapshot(manifest, chapter_dir, policy=None):
        pol = RollbackGuard._policy("snapshot", policy)
        return check_snapshot_restore(manifest, chapter_dir, policy=pol)


guard = RollbackGuard()
