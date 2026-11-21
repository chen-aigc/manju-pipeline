# -*- coding: utf-8 -*-
"""image 插件：placeholder（占位图）、manual（人工图优先）。"""
import time
from pathlib import Path

from ..base import ImageProvider, ImageResult
from ..registry import register
from ...core import atomic
from ...core.cost import Cost


def _make_placeholder(path, size, text=""):
    """生成一张纯色占位图（带简单渐变，避免全平）。"""
    from PIL import Image, ImageDraw
    w, h = size
    im = Image.new("RGB", (w, h), (28, 34, 40))
    d = ImageDraw.Draw(im)
    # 自上而下的浅色渐变，便于看出不是纯黑
    for y in range(h):
        k = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=(int(28 + 40 * k), int(34 + 46 * k), int(40 + 52 * k)))
    if text:
        d.text((w * 0.08, h * 0.08), text, fill=(220, 220, 220))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic.tmp_path(path)
    im.save(tmp, format="PNG")
    tmp.replace(path)
    return path


@register("image", "placeholder")
class PlaceholderImage(ImageProvider):
    """不依赖显卡，直接画占位图。用于无 GPU 环境跑通流程。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def generate(self, prompt, negative, seed, size, ref_image=None, **kwargs):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("placeholder 需要 config['out_path']")
        size = size or (720, 1280)
        _make_placeholder(out, size, (prompt or "")[:40])
        return ImageResult(Path(out), seed=seed or 0, attempts=1,
                           cost=Cost.zero(), elapsed=time.time() - t0)

    def head_ok(self, path):
        # 占位图没有人物，不参与切头检测
        return True, ""


@register("image", "manual")
class ManualImage(ImageProvider):
    """人工图优先：manual/images/shot_XX.png 存在就用它。"""

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

    def _find(self, shot_index, out_path):
        if self.manual_dir is None:
            return None
        stem = Path(out_path).stem
        for cand in (self.manual_dir / ("%s.png" % stem),
                     self.manual_dir / ("%s.jpg" % stem)):
            if cand.exists():
                return cand
        # 按镜头号找（兼容 shot_03.png 命名）
        if shot_index is not None:
            for ext in (".png", ".jpg"):
                cand = self.manual_dir / ("shot_%02d%s" % (int(shot_index), ext))
                if cand.exists():
                    return cand
        return None

    def generate(self, prompt, negative, seed, size, ref_image=None, **kwargs):
        t0 = time.time()
        out = Path(self.config.get("out_path") or "")
        hit = self._find(self.config.get("shot_index"), out)
        if hit is not None:
            atomic.atomic_copy(hit, out)
            return ImageResult(out, seed=seed or 0, attempts=0,
                               cost=Cost.zero(), elapsed=time.time() - t0)

        if self.fallback is not None:
            return self.fallback.generate(prompt, negative, seed, size, ref_image)

        raise FileNotFoundError("没有人工图，也没有可用的兜底出图")
