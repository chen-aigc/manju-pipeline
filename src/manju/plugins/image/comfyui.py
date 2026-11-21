# -*- coding: utf-8 -*-
"""image 插件：ComfyUI（复用现有 image_gen）。"""
import time

from ..base import ImageProvider, ImageResult, ProviderError
from ..registry import register
from ...core.cost import Cost, LocalCostAccumulator, to_si
from ... import image_gen

# 本地出图成本累加器（低于 1 丝的余数保留）
_LOCAL_ACC = LocalCostAccumulator()
_PER_IMAGE_YUAN = "0.0325"      # 电费 + 折旧，实测估算


@register("image", "comfyui")
class ComfyUIImage(ImageProvider):
    """本地 ComfyUI 出图。成本按功耗+折旧估算（不是 0）。"""

    def available(self):
        try:
            return image_gen.is_alive()
        except Exception:  # noqa: BLE001
            return False

    def cost_estimate(self):
        return to_si(_PER_IMAGE_YUAN)

    def generate(self, prompt, negative, seed, size, ref_image=None,
                 keep_seed_on_retry=False):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("comfyui 需要 config['out_path']")

        try:
            path, used_seed, tries = image_gen.generate_one(
                prompt,
                negative_prompt=negative,
                seed=seed,
                size=size,
                out_path=out,
                max_retry=self.config.get("max_retry"),
                use_ref=bool(ref_image) or self.config.get("use_ref", True),
                require_head=self.config.get("require_head", True),
                keep_seed_on_retry=bool(keep_seed_on_retry),
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("出图失败：%s" % exc, provider="comfyui") from exc

        si = _LOCAL_ACC.add(_PER_IMAGE_YUAN)
        return ImageResult(path=path, seed=used_seed, attempts=tries,
                           cost=Cost(si, "local_estimate",
                                     {"per_image_yuan": _PER_IMAGE_YUAN}),
                           elapsed=time.time() - t0)

    def head_ok(self, path):
        try:
            cut, why = image_gen.head_is_cut(path)
            return (not cut), why
        except Exception:  # noqa: BLE001
            return True, ""
