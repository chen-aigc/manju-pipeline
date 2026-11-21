# -*- coding: utf-8 -*-
"""tts 插件：mock、manual。"""
import subprocess
import time
from pathlib import Path

from ..base import ProviderError, TTSProvider, TTSResult
from ..registry import register
from ...core import atomic
from ...core.cost import Cost


def _silence(out_path, duration=1.5, sr=24000):
    """生成静音 mp3（占位用）。"""
    from ...tts import ffmpeg_path
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic.tmp_path(out_path)
    cmd = [ffmpeg_path(), "-y", "-f", "lavfi",
           "-i", "anullsrc=r=%d:cl=mono" % sr,
           "-t", "%.2f" % duration,
           "-c:a", "libmp3lame", "-b:a", "96k",
           "-f", "mp3", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ProviderError("静音生成失败：%s" % (r.stderr or "")[-200:],
                            provider="mock")
    tmp.replace(out_path)
    return out_path


@register("tts", "mock")
class MockTTS(TTSProvider):
    """不联网：按字数估算时长，生成等长静音。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def synthesize(self, text, voice, rate=None, pitch=None):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("mock tts 需要 config['out_path']")
        dur = max(0.8, len(str(text or "")) / 5.5)
        _silence(out, dur)
        return TTSResult(Path(out), duration=dur, cost=Cost.zero(),
                         elapsed=time.time() - t0)

    def voices(self):
        return ["mock"]


@register("tts", "manual")
class ManualTTS(TTSProvider):
    """人工录音优先：manual/audio/<同名>.mp3 存在就用。"""

    def __init__(self, config=None):
        super().__init__(config)
        self.manual_dir = None
        self.fallback = None

    def bind(self, manual_dir, fallback=None):
        self.manual_dir = Path(manual_dir)
        self.fallback = fallback
        return self

    def available(self):
        return self.manual_dir is not None

    def _find(self, out_path):
        if self.manual_dir is None:
            return None
        stem = Path(out_path).stem
        for ext in (".mp3", ".wav", ".m4a"):
            cand = self.manual_dir / ("%s%s" % (stem, ext))
            if cand.exists():
                return cand
        return None

    def synthesize(self, text, voice, rate=None, pitch=None):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("manual tts 需要 config['out_path']")

        hit = self._find(out)
        if hit is not None:
            atomic.atomic_copy(hit, out)
            from ...tts import probe_duration
            return TTSResult(Path(out), duration=probe_duration(out),
                             cost=Cost.zero(), elapsed=time.time() - t0)

        if self.fallback is not None:
            return self.fallback.synthesize(text, voice, rate, pitch)

        raise ProviderError("没有人工音频，也没有可用兜底",
                            provider="manual", retryable=False)

    def voices(self):
        return []
