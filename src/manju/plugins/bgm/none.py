# -*- coding: utf-8 -*-
"""bgm 插件：none、manual、library。"""
from pathlib import Path

from ..base import BGMProvider, BGMResult
from ..registry import register
from ...core.cost import Cost


@register("bgm", "none")
class NoBGM(BGMProvider):
    """不加 BGM。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def build(self, shots, chapter_dir):
        return BGMResult(path=None, plan=[], cost=Cost.zero())


@register("bgm", "manual")
class ManualBGM(BGMProvider):
    """人工指定：manual/bgm.<ext> 或 manual/bgm_plan.json。"""

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

    def build(self, shots, chapter_dir):
        if self.manual_dir is None:
            raise FileNotFoundError("manual bgm 未绑定目录")

        for ext in (".mp3", ".wav", ".m4a"):
            cand = self.manual_dir / ("bgm%s" % ext)
            if cand.exists():
                return BGMResult(path=cand, plan=[], cost=Cost.zero())

        if self.fallback is not None:
            return self.fallback.build(shots, chapter_dir)

        return BGMResult(path=None, plan=[], cost=Cost.zero())


@register("bgm", "library")
class LibraryBGM(BGMProvider):
    """从素材库按时长随机挑一首。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def build(self, shots, chapter_dir):
        from ... import config
        d = Path(config.ASSETS_DIR) / "bgm"
        total = sum(float(s.get("_duration") or 3.0) for s in (shots or []))
        if not d.exists():
            return BGMResult(path=None, plan=[], cost=Cost.zero())

        files = []
        for ext in ("*.mp3", "*.wav", "*.m4a"):
            files += list(d.glob(ext))
        if not files:
            return BGMResult(path=None, plan=[], cost=Cost.zero())

        import random
        pick = random.choice(sorted(files))
        return BGMResult(path=pick, plan=[(total, pick)], cost=Cost.zero())
