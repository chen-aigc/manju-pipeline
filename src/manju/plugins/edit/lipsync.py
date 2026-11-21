# -*- coding: utf-8 -*-
"""edit 插件：对口型（lip-sync）【接口占位】

为什么需要：
    真人 AI 剧里，画面是生成的人物，台词是 TTS 合成的声音。
    不做对口型，嘴是静止的 —— 观众一眼就看出是假的。

怎么接（三种常见形态）：

    A. 云端 API（HeyGen / 闪剪 / 腾讯智影 / 阿里 EMO）
       传「人脸视频 + 音频」→ 返回口型对齐的视频
       成本约 0.5~2 元/分钟

    B. 本地模型（Wav2Lip / SadTalker）
       免费但 6 GB 显存只能跑低分辨率，且需要额外依赖

    C. 可灵/即梦的口型参数
       部分版本的图生视频支持 audio 输入，直接出带口型的片段

本文件给出 A 形态的骨架：拿成片 + 人声，返回新成片。
"""
import os
import time
from pathlib import Path

from ..base import EditProvider, EditResult, ProviderError
from ..registry import register
from ... import config
from ...core import atomic, integrity
from ...core.cost import Cost, to_si


@register("edit", "lipsync")
class LipSyncEdit(EditProvider):
    """先正常剪辑，再对成片做一遍口型对齐（未实现，仅占位）。

    配置：
        LIPSYNC_PROVIDER   heygen / jianying / tencent / wav2lip
        LIPSYNC_API_KEY    对应平台的 key（默认空 = 不可用）
        LIPSYNC_PRICE_MIN  每分钟价格（默认 1.0 元）
    """

    def available(self):
        return bool(getattr(config, "LIPSYNC_API_KEY", ""))

    def cost_estimate(self):
        return to_si("1.0")

    def build(self, chapter_dir, shots, audio, bgm, srt):
        t0 = time.time()
        chapter_dir = Path(chapter_dir)

        # 1) 先用现有剪辑出一版带声音的成片
        from .auto_cut import RhythmCut
        base = RhythmCut(self.config)
        res = base.build(chapter_dir, shots, audio, bgm, srt)

        video = getattr(res, "video", None)
        if not video or not Path(video).exists():
            return res          # 没出片就不做口型

        # 2) 口型对齐（未实现）
        try:
            fixed = self._align(Path(video), Path(audio) if audio else None,
                                chapter_dir)
        except NotImplementedError:
            print("[剪辑/口型] 未实现，先交付原成片（口型不会动）")
            return res

        # 3) 用对齐后的视频替换
        if fixed and fixed.exists():
            atomic.atomic_copy(fixed, Path(video))
        if audio and Path(audio).exists():
            try:
                from ...tts import probe_duration
                secs = probe_duration(video) or 0
                price = float(getattr(config, "LIPSYNC_PRICE_MIN", 1.0) or 1.0)
                return EditResult(
                    video=video, pack_dir=None,
                    cost=Cost(to_si(price * secs / 60.0), "unit_price",
                              {"seconds": secs, "price_per_min": price}),
                    elapsed=time.time() - t0)
            except Exception:  # noqa: BLE001
                pass
        return res

    # ---------------- 留给实现者 ----------------
    def _align(self, video_path, audio_path, chapter_dir):
        """把视频的人脸口型对齐到音频。

        实现思路（HeyGen 为例）：
            1. POST /v1/video/generate，传 video_url + audio_url
            2. 轮询 status 直到 completed
            3. 下载到 chapter_dir/lipsync.mp4，返回路径

        返回：Path 或 None
        """
        raise NotImplementedError(
            "对口型未实现。需要 LIPSYNC_PROVIDER + LIPSYNC_API_KEY，"
            "并按 plugins/edit/lipsync.py 里的 _align() 注释补齐。")
