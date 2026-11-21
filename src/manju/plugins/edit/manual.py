# -*- coding: utf-8 -*-
"""edit 插件：manual（人工剪辑）。

工作流：
    1. 选 manual 后，系统不自动出片，只导出「素材包」
       pack/shots/*.mp4 + audio/ + subs.srt + timeline.csv + README
    2. 人工在剪映里剪好，导出成片，放到：
           manual/final.mp4   （或 manual/成片.mp4 / manual/edit.mp4）
    3. 再跑一次（或在网页点「重做」），系统自动把人工成片接进来

可选 half_auto：先自动出一版初剪，人在这版基础上改（config.half_auto=True）。
"""
import shutil
import time
from pathlib import Path

from ..base import EditProvider, EditResult, ProviderError
from ..registry import register
from ...core import atomic, integrity
from ...core.cost import Cost

FINAL_NAMES = ("final.mp4", "成片.mp4", "edit.mp4", "final.mov", "成片.mov")


@register("edit", "manual")
class ManualEdit(EditProvider):
    """人工剪辑：导出素材包 + 回收人工成片。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def _find_final(self, chapter_dir):
        manual = Path(chapter_dir) / "manual"
        for name in FINAL_NAMES:
            cand = manual / name
            if cand.exists():
                ok, _ = integrity.validate_video(cand)
                if ok:
                    return cand
        return None

    def build(self, chapter_dir, shots, audio, bgm, srt):
        t0 = time.time()
        chapter_dir = Path(chapter_dir)
        manual_dir = chapter_dir / "manual"
        manual_dir.mkdir(parents=True, exist_ok=True)

        pack = chapter_dir / "pack"
        out = chapter_dir / ("chapter_%03d.mp4"
                             % self._chapter_no(chapter_dir))

        final = self._find_final(chapter_dir)
        if final is not None:
            atomic.atomic_copy(final, out)
            print("[剪辑/人工] 已接入人工成片：%s" % final.name)
            return EditResult(video=out, pack_dir=pack if pack.exists() else None,
                              cost=Cost.zero(), elapsed=time.time() - t0)

        # 没人工成片 → 导出素材包，等人工剪
        self._export_pack(chapter_dir, shots, audio, bgm, srt, pack)
        self._write_guide(pack)

        print("[剪辑/人工] 已导出素材包：%s" % pack)
        print("          剪好后把成片放到 manual/final.mp4，再重跑一次即可")

        # 半自动：如果开了 half_auto，顺便出一版自动初剪给人做底
        if self.config.get("half_auto"):
            try:
                from .auto_cut import RhythmCut
                helper = RhythmCut(self.config)
                res = helper.build(chapter_dir, shots, audio, bgm, srt)
                res.pack_dir = pack
                print("[剪辑/人工] 同时生成了一版自动初剪，可在其基础上修改")
                return res
            except Exception as exc:  # noqa: BLE001
                print("[剪辑/人工] 自动初剪失败（不影响素材包）：%s" % exc)

        return EditResult(video=None, pack_dir=pack, cost=Cost.zero(),
                          elapsed=time.time() - t0)

    # ---------------- 内部 ----------------
    @staticmethod
    def _chapter_no(chapter_dir):
        name = Path(chapter_dir).name
        try:
            return int(name.replace("chapter_", ""))
        except ValueError:
            return 1

    def _export_pack(self, chapter_dir, shots, audio, bgm, srt, pack):
        from ... import video as _video

        (pack / "shots").mkdir(parents=True, exist_ok=True)
        (pack / "audio").mkdir(parents=True, exist_ok=True)

        for shot in shots:
            idx = int(shot["index"])
            img = chapter_dir / "images" / ("shot_%02d.png" % idx)
            if not img.exists():
                continue
            dst = pack / "shots" / ("shot_%02d.mp4" % idx)
            dur = float(shot.get("_duration") or 2.0)
            sub = (shot.get("sub_shots") or [{}])[0]
            try:
                _video.make_shot_clip(img, max(0.5, dur), dst, sub=sub)
            except Exception as exc:  # noqa: BLE001
                print("[剪辑/人工] 镜头 %d 导出失败：%s" % (idx, exc))

        if audio and Path(audio).exists():
            shutil.copyfile(audio, pack / "audio" / "voice.mp3")
        if bgm and Path(bgm).exists():
            shutil.copyfile(bgm, pack / "audio" / "bgm.mp3")
        if srt and Path(srt).exists():
            shutil.copyfile(srt, pack / "subs.srt")

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

    @staticmethod
    def _write_guide(pack):
        atomic.atomic_write_text(pack / "README.txt", """素材包 · 人工剪辑说明
=====================================

一、目录内容
  shots/        每个镜头一个 mp4（按 shot_01、shot_02 顺序）
  audio/voice.mp3   人声（整条）
  audio/bgm.mp3     背景音乐（如果有）
  subs.srt      字幕（可导入剪映自动对齐）
  timeline.csv  镜头顺序和起始时间

二、在剪映里怎么用
  1. 新建项目，比例选 9:16（1080×1920）
  2. 把 shots/ 里所有片段拖进轨道，按文件名顺序排列
  3. 右键字幕 → 导入字幕 → 选 subs.srt
  4. 把 voice.mp3 放第一条音轨，bgm.mp3 放第二条，音量调到 15%~20%
  5. 按 timeline.csv 的时间点校正节奏
  6. 关键处加音效、调色、加转场

三、剪完怎么交回系统
  导出 mp4，命名为 final.mp4（或 成片.mp4），放到：
      projects/<项目>/chapter_XXX/manual/final.mp4

  然后在网页点「重做出错项」，或在命令行重新跑一次：
      manju run <项目> --chapter <集数>

  系统会自动接管这个成片，后续报表里记为「人工剪辑」。
""")
