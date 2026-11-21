# -*- coding: utf-8 -*-
"""bgm 插件：synth（复用现有分段合成）。"""
from pathlib import Path

from ..base import BGMProvider, BGMResult, ProviderError
from ..registry import register
from ...core.cost import Cost, to_si
from ... import video as _video


@register("bgm", "synth")
class SynthBGM(BGMProvider):
    """按镜头情绪分段 + 本地合成音轨。

    成本 = 本地 CPU 渲染（估算，不是 0）。
    """

    def available(self):
        return True

    def cost_estimate(self):
        return to_si("0.005")

    def build(self, shots, chapter_dir):
        chapter_dir = Path(chapter_dir)
        audio_dir = chapter_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        try:
            plan = _video.build_bgm_plan(shots, chapter_dir)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("BGM 编排失败：%s" % exc, provider="synth") from exc

        if not plan:
            return BGMResult(path=None, plan=[], cost=Cost.zero())

        out = audio_dir / "bgm_track.mp3"
        track = _video.render_bgm_track(plan, out)
        cost = Cost(to_si("0.005"), "local_estimate", {"segments": len(plan)})
        return BGMResult(path=track, plan=plan, cost=cost)
