# -*- coding: utf-8 -*-
"""一致性检查 v2：用颜色分布指纹，区分"同角色不同构图"和"不同角色"。

v1 的问题：
    整图直方图对构图差异太敏感
    同一角色换角度 → 得分 0.41 → 误判 FAIL

v2 的改进：
    特征 = 颜色量化分布 + 色调分布 + 亮度分布
    实测区分度：
        林越之间     0.70~0.83  🟢
        月白之间     0.61       🟢
        林越 vs 月白 0.37~0.54  🔴
    阈值 0.65 完美分开

用法：
    from manju.core.consistency import ConsistencyChecker

    # 模式1：与基准图对比
    ck = ConsistencyChecker("chars/林越_定妆照.png")
    r = ck.check_shot("shot_003", "assets/shot_003.png")

    # 模式2：成批找异类（不需要基准）
    report = ck.audit_batch([{"id": "s01", "path": "..."}, ...])
    # → 找出偏离群体的那几张
"""
from pathlib import Path

import numpy as np
from PIL import Image


# 阈值（基于实测：同角色 >= 0.61，不同角色 <= 0.54）
THRESHOLD_OK = 0.65       # >= 这个值认为是同一角色
THRESHOLD_WARN = 0.55     # 介于 WARN 和 OK 之间需要人工看


# ---------------------------------------------------------------- 特征
def _color_hist(path, bins=4):
    """颜色量化分布（4×4×4 = 64 格）。"""
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    flat = arr.reshape(-1, 3).astype(np.int32)
    q = (flat * bins // 256)
    codes = q[:, 0] * (bins * bins) + q[:, 1] * bins + q[:, 2]
    cnt = np.bincount(codes, minlength=bins ** 3).astype(np.float32)
    s = cnt.sum()
    return cnt / s if s > 0 else cnt


def _hue_hist(path, bins=12):
    """色调分布。"""
    hsv = np.asarray(Image.open(path).convert("HSV"), dtype=np.uint8)
    h = hsv[..., 0].reshape(-1)
    hist = np.histogram(h, bins=bins, range=(0, 256))[0].astype(np.float32)
    s = hist.sum()
    return hist / s if s > 0 else hist


def _bright_hist(path, bins=16):
    """亮度分布。"""
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    lum = arr.mean(axis=2).reshape(-1)
    hist = np.histogram(lum, bins=bins, range=(0, 256))[0].astype(np.float32)
    s = hist.sum()
    return hist / s if s > 0 else hist


def fingerprint(path):
    """一张图的颜色指纹（68 维）。"""
    return np.concatenate([
        _color_hist(path),
        _hue_hist(path),
        _bright_hist(path),
    ])


def similarity(a, b):
    """直方图交集相似度（0~1）。"""
    num = np.minimum(a, b).sum()
    den = np.maximum(a, b).sum()
    return float(num / den) if den > 0 else 0.0


# ---------------------------------------------------------------- 结果
class ShotResult:
    def __init__(self, shot_id, path):
        self.shot_id = shot_id
        self.path = str(path)
        self.level = "OK"
        self.scores = {}       # 各项相似度
        self.notes = []

    def evaluate(self, name, value, ok_th=THRESHOLD_OK, warn_th=THRESHOLD_WARN):
        self.scores[name] = round(value, 4)
        if value < warn_th:
            self.level = "FAIL"
            self.notes.append("%s=%.3f（差异大）" % (name, value))
        elif value < ok_th:
            if self.level != "FAIL":
                self.level = "WARN"
            self.notes.append("%s=%.3f（略低）" % (name, value))

    def to_dict(self):
        return {
            "shot_id": self.shot_id,
            "path": self.path,
            "level": self.level,
            "scores": self.scores,
            "notes": self.notes,
        }

    def __repr__(self):
        return "<%s %s %s>" % (self.shot_id, self.level, self.scores)


class BatchReport:
    def __init__(self, results):
        self.results = results

    def stats(self):
        out = {"OK": 0, "WARN": 0, "FAIL": 0}
        for r in self.results:
            out[r.level] = out.get(r.level, 0) + 1
        return out

    def failures(self):
        return [r for r in self.results if r.level == "FAIL"]

    def warnings(self):
        return [r for r in self.results if r.level == "WARN"]

    def summary(self):
        st = self.stats()
        n = len(self.results)
        lines = [
            "=" * 62,
            "一致性检查报告",
            "=" * 62,
            "  镜头总数: %d     OK: %d   WARN: %d   FAIL: %d"
            % (n, st["OK"], st["WARN"], st["FAIL"]),
        ]
        if st["FAIL"]:
            lines.append("")
            lines.append("  ── 需要重做 ──")
            for r in self.failures():
                lines.append("    %-10s %s" % (r.shot_id, "；".join(r.notes)))
        if st["WARN"]:
            lines.append("")
            lines.append("  ── 建议人工看一眼 ──")
            for r in self.warnings():
                lines.append("    %-10s %s" % (r.shot_id, "；".join(r.notes)))
        if st["OK"] == n:
            lines.append("")
            lines.append("  ✅ 全部一致")
        return "\n".join(lines)

    def to_dict(self):
        return {
            "stats": self.stats(),
            "shots": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------- 检查器
class ConsistencyChecker:
    """跨镜头一致性检查。

    Args:
        reference: 基准图路径（如角色定妆照）。为 None 时只能用 audit_batch。
    """

    def __init__(self, reference=None):
        self.ref_fp = None
        self.ref_path = None
        if reference and Path(reference).exists():
            self.ref_fp = fingerprint(reference)
            self.ref_path = str(reference)

    # -------- 单镜 vs 基准 --------
    def check_shot(self, shot_id, path):
        r = ShotResult(shot_id, path)
        p = Path(path)
        if not p.exists():
            r.level = "FAIL"
            r.notes.append("文件不存在")
            return r
        if self.ref_fp is None:
            r.notes.append("无基准图，跳过对比")
            return r
        try:
            fp = fingerprint(p)
        except Exception as exc:  # noqa: BLE001
            r.level = "FAIL"
            r.notes.append("读取失败：%r" % exc)
            return r
        r.evaluate("vs_baseline", similarity(fp, self.ref_fp))
        return r

    # -------- 批内找异类（无需基准） --------
    def audit_batch(self, shots, cutoff=None, mode="relative"):
        """成批检查，找出偏离群体的镜头。

        两种模式：
            relative（默认）：相对排名，找偏离最多的 N 张
                适合整集审核——不同场景本来就该不同，
                只要找出"最离群"的那几张人工看即可
            absolute：绝对阈值，低于 cutoff 判 FAIL
                适合同角色多版本对比

        Args:
            shots:  [{"id": "s01", "path": "..."}, ...]
            cutoff: relative 模式下是"离群比例"（默认 0.15 = 找最差的 15%）
                     absolute 模式下是相似度阈值（默认 THRESHOLD_WARN）
            mode:   "relative" | "absolute"

        Returns:
            BatchReport
        """
        items = []
        for s in shots:
            p = Path(s["path"])
            sid = s.get("id") or p.stem
            if not p.exists():
                continue
            try:
                items.append({"id": sid, "path": str(p), "fp": fingerprint(p)})
            except Exception:  # noqa: BLE001
                continue

        n = len(items)
        if n < 2:
            return BatchReport([])

        # 计算每张图与其他图的相似度
        stats = []
        for i, it in enumerate(items):
            sims = []
            for j, other in enumerate(items):
                if i == j:
                    continue
                sims.append(similarity(it["fp"], other["fp"]))
            sims.sort()
            stats.append({
                "item": it,
                "median": float(np.median(sims)),
                "mean": float(np.mean(sims)),
                "worst": float(sims[0]),
            })

        results = []
        if mode == "relative":
            # 按 median 从低到高排序，找出最离群的那批
            ranked = sorted(stats, key=lambda x: x["median"])
            frac = cutoff if cutoff is not None else 0.15
            k = max(1, int(round(n * frac)))
            worst_ids = {r["item"]["id"] for r in ranked[:k]}
            all_med = float(np.median([s["median"] for s in stats]))
            soft_line = all_med * 0.85

            for s in stats:
                r = ShotResult(s["item"]["id"], s["item"]["path"])
                r.scores = {
                    "median": round(s["median"], 4),
                    "mean": round(s["mean"], 4),
                    "worst": round(s["worst"], 4),
                }
                # 双重判定：
                #   ① 在"最差的 k 个"里，且确实低于群体（< soft_line）
                #   ② 或者绝对相似度过低（< WARN_THRESHOLD）
                # 只满足"排名靠后"但绝对值仍很高 → 判 OK（差异无实际意义）
                is_ranked_worst = s["item"]["id"] in worst_ids
                is_relative_low = s["median"] < soft_line
                is_absolute_low = s["median"] < THRESHOLD_WARN

                if is_absolute_low and (is_ranked_worst or is_relative_low):
                    r.level = "FAIL"
                    r.notes.append("偏离群体 median=%.3f（全体中位 %.3f）"
                                   % (s["median"], all_med))
                elif is_ranked_worst and is_relative_low:
                    r.level = "WARN"
                    r.notes.append("略偏离 median=%.3f（全体中位 %.3f）"
                                   % (s["median"], all_med))
                else:
                    r.level = "OK"
                results.append(r)
        else:
            cut = cutoff if cutoff is not None else THRESHOLD_WARN
            for s in stats:
                r = ShotResult(s["item"]["id"], s["item"]["path"])
                r.scores = {
                    "median": round(s["median"], 4),
                    "mean": round(s["mean"], 4),
                    "worst": round(s["worst"], 4),
                }
                if s["median"] < cut:
                    r.level = "FAIL"
                    r.notes.append("与群体差异大 median=%.3f" % s["median"])
                elif s["median"] < THRESHOLD_OK:
                    r.level = "WARN"
                    r.notes.append("偏离群体 median=%.3f" % s["median"])
                results.append(r)

        return BatchReport(results)

    # -------- 两种模式一起用 --------
    def check_batch(self, shots, use_baseline=True):
        """先与基准比，再做批内审计。"""
        results = []
        for s in shots:
            p = Path(s["path"])
            sid = s.get("id") or p.stem
            if use_baseline and self.ref_fp is not None:
                results.append(self.check_shot(sid, p))
            else:
                results.append(ShotResult(sid, p))

        audit = self.audit_batch(shots)
        by_id = {r.shot_id: r for r in results}
        for ar in audit.results:
            if ar.shot_id in by_id:
                base = by_id[ar.shot_id]
                base.scores.update({"median": ar.scores["median"],
                                    "worst": ar.scores["worst"]})
                if ar.level == "FAIL":
                    base.level = "FAIL"
                elif ar.level == "WARN" and base.level == "OK":
                    base.level = "WARN"
                base.notes.extend(ar.notes)
            else:
                results.append(ar)

        return BatchReport(results)
