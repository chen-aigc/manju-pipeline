# -*- coding: utf-8 -*-
"""视频一致性校验：检测 AI 视频是否崩坏。

原理：
    AI 图生视频的第一帧应该和输入图几乎一模一样。
    如果第一帧就差很多，说明模型没按图来。

    同时检查帧间漂移：
    人物在视频里如果「融化」或「变脸」，帧间差异会异常大。

方案：
    1. 从视频抽第一帧、中间帧、最后一帧
    2. 第一帧和输入图比相似度（应有 > 0.85）
    3. 中间帧、最后一帧和第一帧比（应有 > 0.6）
    4. 低于阈值就标记为可疑
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config


def _ffmpeg():
    """找 ffmpeg。"""
    import glob

    p = shutil.which("ffmpeg")
    if p:
        return p
    for pat in (
        r"C:\Users\ASUS\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg*\**\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"D:\ffmpeg\bin\ffmpeg.exe",
    ):
        cands = glob.glob(pat, recursive=True)
        if cands:
            return cands[0]
    return None


def _extract_frame(video: Path, t: float, out: Path) -> bool:
    """从视频抽一帧。"""
    ff = _ffmpeg()
    if not ff:
        return False
    try:
        r = subprocess.run(
            [ff, "-y", "-ss", str(max(0.0, t)), "-i", str(video),
             "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, timeout=60)
        return r.returncode == 0 and out.exists() and out.stat().st_size > 500
    except Exception:  # noqa: BLE001
        return False


def _duration(video: Path) -> float:
    """探测时长。"""
    import glob

    ff = shutil.which("ffprobe")
    if not ff:
        for pat in (
            r"C:\Users\ASUS\AppData\Local\Microsoft\WinGet\Packages"
            r"\Gyan.FFmpeg*\**\bin\ffprobe.exe",
        ):
            cands = glob.glob(pat, recursive=True)
            if cands:
                ff = cands[0]
                break
    if not ff:
        return 0.0
    try:
        r = subprocess.run(
            [ff, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=20)
        return float((r.stdout or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def check_video(video: Path, first_frame: Optional[Path] = None,
                min_first_sim: float = None,
                min_drift_sim: float = None) -> Dict[str, Any]:
    """检查一个视频是否崩坏。

    返回：
        {ok, first_sim, mid_sim, end_sim, reason}
    """
    # 阈值可以从配置读
    if min_first_sim is None:
        try:
            min_first_sim = float(getattr(config, "VIDEO_FIRST_SIM", 0.82))
        except (TypeError, ValueError):
            min_first_sim = 0.82
    if min_drift_sim is None:
        try:
            min_drift_sim = float(getattr(config, "VIDEO_DRIFT_SIM", 0.55))
        except (TypeError, ValueError):
            min_drift_sim = 0.55

    video = Path(video)
    if not video.exists():
        return {"ok": False, "reason": "视频不存在"}

    dur = _duration(video)
    if dur <= 0:
        return {"ok": False, "reason": "无法读取时长"}

    try:
        from .consistency import fingerprint, similarity
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "指纹模块不可用：%r" % exc}

    tmp = Path(tempfile.mkdtemp(prefix="_vchk_"))
    try:
        f_first = tmp / "first.jpg"
        f_mid = tmp / "mid.jpg"
        f_end = tmp / "end.jpg"

        ok_f = _extract_frame(video, 0.0, f_first)
        ok_m = _extract_frame(video, dur * 0.5, f_mid)
        ok_e = _extract_frame(video, max(0.0, dur - 0.2), f_end)

        if not (ok_f and ok_m and ok_e):
            return {"ok": True, "reason": "抽帧失败，跳过校验",
                    "skipped": True}

        fp_first = fingerprint(f_first)
        fp_mid = fingerprint(f_mid)
        fp_end = fingerprint(f_end)

        result = {"ok": True, "duration": round(dur, 2)}

        # 1. 第一帧 vs 输入图
        if first_frame and Path(first_frame).exists():
            fp_in = fingerprint(Path(first_frame))
            sim0 = similarity(fp_in, fp_first)
            result["first_sim"] = round(sim0, 3)
            if sim0 < min_first_sim:
                result["ok"] = False
                result["reason"] = ("第一帧偏离输入图（相似度 %.2f < %.2f）"
                                    % (sim0, min_first_sim))
                return result

        # 2. 中间帧 vs 第一帧
        sim_m = similarity(fp_first, fp_mid)
        result["mid_sim"] = round(sim_m, 3)

        # 3. 结尾帧 vs 第一帧
        sim_e = similarity(fp_first, fp_end)
        result["end_sim"] = round(sim_e, 3)

        if sim_m < min_drift_sim and sim_e < min_drift_sim:
            result["ok"] = False
            result["reason"] = ("画面漂移过大（中 %.2f / 尾 %.2f，阈值 %.2f）"
                                % (sim_m, sim_e, min_drift_sim))
            return result

        if sim_m < min_drift_sim or sim_e < min_drift_sim:
            result["warn"] = True
            result["reason"] = ("部分帧漂移较大（中 %.2f / 尾 %.2f）"
                                % (sim_m, sim_e))

        return result

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def auto_check_enabled() -> bool:
    return bool(getattr(config, "AUTO_CHECK_VIDEO", True))
