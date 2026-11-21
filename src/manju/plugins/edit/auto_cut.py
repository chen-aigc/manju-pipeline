# -*- coding: utf-8 -*-
"""edit 插件：auto_cut（全自动硬切）、rhythm（节奏化）、pack（素材包）。"""
import shutil
from pathlib import Path

from ..base import EditProvider, EditResult, ProviderError
from ..registry import register
from ...core import atomic
from ...core.cost import Cost
from ... import video as _video


def _apply_output_resolution(out, chapter_dir):
    """把成片统一到配置的分辨率。

    - 抽卡可能是 720×1280，配置却是 1080p → 这里补齐
    - 配置 4K → Lanczos 上采样（不是原生，画质提升有限）
    尺寸一致时不动，避免无谓重编码。
    """
    from ... import config as cfg

    target = cfg.OUTPUT_SIZES.get(cfg.OUTPUT_RESOLUTION)
    if not target:
        return out

    cur = _video.probe_size(out)
    if cur == tuple(target):
        return out

    up = Path(chapter_dir) / ("_out_%s.mp4" % cfg.OUTPUT_RESOLUTION)
    try:
        _video.upscale_video(out, up, target[0], target[1],
                             sharpen=cfg.OUTPUT_UPSCALE_SHARPEN)
        Path(out).unlink(missing_ok=True)
        up.replace(out)
        if cur[0] and cur[1]:
            kind = "上采样" if target[0] > cur[0] else "缩小"
            print("[剪辑] %dx%d → %dx%d（%s，%s）"
                  % (cur[0], cur[1], target[0], target[1],
                     cfg.OUTPUT_RESOLUTION, kind))
        else:
            print("[剪辑] 已输出 %dx%d（%s）"
                  % (target[0], target[1], cfg.OUTPUT_RESOLUTION))
    except Exception as exc:  # noqa: BLE001
        print("[剪辑] 统一到 %s 失败，保留原尺寸：%s"
              % (cfg.OUTPUT_RESOLUTION, exc))
        up.unlink(missing_ok=True)
    return out


def _concat_and_mux(chapter_dir, shots, audio, bgm, srt, clip_builder):
    """公共流程：逐子镜头出片 → 拼接 → 调色 → 混音 → 字幕。"""
    chapter_dir = Path(chapter_dir)
    images = chapter_dir / "images"
    clips = []

    for shot in shots:
        img = images / ("shot_%02d.png" % int(shot["index"]))
        if not img.exists():
            continue
        for sub in shot.get("sub_shots") or []:
            tmp = chapter_dir / ("_clip_%s_%s.mp4" % (
                int(shot["index"]), sub.get("index", 0)))
            sub2 = dict(sub)
            if clip_builder is not None:
                sub2 = clip_builder(shot, sub2)
            _video.make_shot_clip(img, sub2.get("duration") or 2.0, tmp, sub=sub2)
            clips.append(tmp)

    if not clips:
        raise ProviderError("没有可用的子镜头", provider="edit",
                            retryable=False)

    silent = _video.concat_clips(clips, chapter_dir / "silent.mp4", fade=False)
    for c in clips:
        Path(c).unlink(missing_ok=True)

    return silent


@register("edit", "auto_cut")
class AutoCut(EditProvider):
    """现有流程：等长硬切 + 调色 + 混音 + 字幕。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def build(self, chapter_dir, shots, audio, bgm, srt):
        import time
        t0 = time.time()
        chapter_dir = Path(chapter_dir)

        silent = _concat_and_mux(chapter_dir, shots, audio, bgm, srt, None)

        graded = None
        try:
            graded = _video.apply_grade(silent, chapter_dir / "silent_graded.mp4")
            Path(silent).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            graded = silent

        out = chapter_dir / "成片.mp4"
        _video.mux_audio(graded, audio, out, bgm_path=bgm, bgm_volume=0.16)
        Path(graded).unlink(missing_ok=True)

        if srt and Path(srt).exists():
            burned = chapter_dir / "成片_sub.mp4"
            from ... import subtitles
            if subtitles.burn_with_drawtext(out, srt, burned):
                out.unlink(missing_ok=True)
                burned.replace(out)

        out = _apply_output_resolution(out, chapter_dir)
        return EditResult(video=out, pack_dir=None, cost=Cost.zero(),
                          elapsed=time.time() - t0)


@register("edit", "rhythm")
class RhythmCut(AutoCut):
    """节奏化：按镜头类型调整时长（空镜/死物缩短、特写延长）。"""

    RULES = {
        "空镜": 0.8,
        "死物": 0.8,
        "特写": 1.3,
        "远景": 1.1,
    }

    def _builder(self, shot, sub):
        st = str(shot.get("shot_type") or "")
        factor = self.RULES.get(st, 1.0)
        # 只在 0.6~1.6 区间内缩放，避免极端
        factor = max(0.6, min(1.6, factor))
        sub = dict(sub)
        sub["duration"] = max(0.5, float(sub.get("duration") or 2.0) * factor)
        return sub

    def build(self, chapter_dir, shots, audio, bgm, srt):
        import time
        t0 = time.time()
        chapter_dir = Path(chapter_dir)

        # 以真实音频长度为准（mp3 帧对齐会让实测比累加略长）
        total_target = 0.0
        if audio and Path(audio).exists():
            try:
                from ...tts import probe_duration
                total_target = probe_duration(audio)
            except Exception:  # noqa: BLE001
                total_target = 0.0
        if total_target <= 0:
            total_target = sum(float(s.get("_duration") or 0) for s in shots)

        raw = 0.0
        for s in shots:
            st = str(s.get("shot_type") or "")
            f = max(0.6, min(1.6, self.RULES.get(st, 1.0)))
            for sub in s.get("sub_shots") or []:
                raw += float(sub.get("duration") or 0) * f
        scale = (total_target / raw) if raw > 0 else 1.0

        def builder(shot, sub):
            sub = self._builder(shot, sub)
            sub["duration"] = max(0.5, sub["duration"] * scale)
            return sub

        silent = _concat_and_mux(chapter_dir, shots, audio, bgm, srt, builder)

        graded = None
        try:
            graded = _video.apply_grade(silent, chapter_dir / "silent_graded.mp4")
            Path(silent).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            graded = silent

        out = chapter_dir / "成片.mp4"
        _video.mux_audio(graded, audio, out, bgm_path=bgm, bgm_volume=0.16)
        Path(graded).unlink(missing_ok=True)

        if srt and Path(srt).exists():
            burned = chapter_dir / "成片_sub.mp4"
            from ... import subtitles
            if subtitles.burn_with_drawtext(out, srt, burned):
                out.unlink(missing_ok=True)
                burned.replace(out)

        out = _apply_output_resolution(out, chapter_dir)
        return EditResult(video=out, pack_dir=None, cost=Cost.zero(),
                          elapsed=time.time() - t0)


@register("edit", "pack")
class PackCut(RhythmCut):
    """节奏化 + 导出素材包（给剪映精修）。"""

    def build(self, chapter_dir, shots, audio, bgm, srt):
        res = super().build(chapter_dir, shots, audio, bgm, srt)
        chapter_dir = Path(chapter_dir)
        pack = chapter_dir / "pack"
        (pack / "shots").mkdir(parents=True, exist_ok=True)
        (pack / "audio").mkdir(parents=True, exist_ok=True)

        # 分镜单文件（每镜头一个完整片段）
        for shot in shots:
            idx = int(shot["index"])
            img = chapter_dir / "images" / ("shot_%02d.png" % idx)
            if not img.exists():
                continue
            dst = pack / "shots" / ("shot_%02d.mp4" % idx)
            dur = float(shot.get("_duration") or 2.0)
            try:
                _video.make_shot_clip(img, max(0.5, dur), dst, sub={})
            except Exception:  # noqa: BLE001
                continue

        # 音频分轨
        if audio and Path(audio).exists():
            shutil.copyfile(audio, pack / "audio" / "voice.mp3")
        if bgm and Path(bgm).exists():
            shutil.copyfile(bgm, pack / "audio" / "bgm.mp3")
        if srt and Path(srt).exists():
            shutil.copyfile(srt, pack / "subs.srt")

        # 时间轴
        lines = ["index,start,duration,shot_type,text"]
        t = 0.0
        for shot in shots:
            for sub in shot.get("sub_shots") or []:
                d = float(sub.get("duration") or 0)
                lines.append("%s,%.3f,%.3f,%s,%s" % (
                    sub.get("index"), t, d, shot.get("shot_type", ""),
                    (sub.get("display") or "").replace(",", "，")))
                t += d
        atomic.atomic_write_text(pack / "timeline.csv", "\n".join(lines))

        atomic.atomic_write_text(pack / "README.txt", (
            "素材包导入说明\n"
            "1. 把 shots/ 里的片段批量导入剪映\n"
            "2. 按 timeline.csv 的顺序排列\n"
            "3. 导入 subs.srt 作为字幕\n"
            "4. audio/voice.mp3 和 bgm.mp3 分开放在两条音轨\n"
        ))

        res.pack_dir = pack
        return res
