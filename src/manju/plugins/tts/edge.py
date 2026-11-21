# -*- coding: utf-8 -*-
"""tts 插件：edge-tts（免费，成本记 0）。"""
import time
from pathlib import Path

from ..base import ProviderError, TTSProvider, TTSResult
from ..registry import register
from ...core.cost import Cost
from ... import tts as _tts


@register("tts", "edge")
class EdgeTTS(TTSProvider):
    """edge-tts 完全免费，成本明确为 0（不是"未知"）。"""

    def available(self):
        try:
            import edge_tts  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def cost_estimate(self):
        return 0

    def synthesize(self, text, voice, rate=None, pitch=None):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("edge tts 需要 config['out_path']")

        text = str(text or "").strip()
        if not text:
            raise ProviderError("空文本", provider="edge", retryable=False)

        try:
            _tts.synthesize(text, voice or _tts.voice_for("旁白"),
                            out, rate=rate, pitch=pitch)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("配音失败：%s" % exc, provider="edge") from exc

        dur = _tts.probe_duration(out)
        return TTSResult(Path(out), duration=dur, cost=Cost.zero(),
                         elapsed=time.time() - t0)

    def voices(self):
        return list(getattr(_tts, "VOICE_MAP", {}).values())
