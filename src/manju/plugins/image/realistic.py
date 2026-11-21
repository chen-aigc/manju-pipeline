# -*- coding: utf-8 -*-
"""image 插件：真人写实风格【接口占位】

状态：只有接口，没有实现生成逻辑。

为什么留着：
    真人 AI 剧需要写实底模（SDXL / Flux / 写实 SD1.5），
    6 GB 显存跑 SDXL 要 60~120 秒/张，跑 Flux 基本不可用。
    真正落地建议走云端出图（openai_image 已支持）。

要用的时候怎么补：
    1. 把写实模型放进 ComfyUI/models/checkpoints/
    2. .env 里设 REALISTIC_MODEL=<模型文件名>
    3. 实现下面的 generate()，参照 plugins/image/comfyui.py 的写法
       （改 workflow 的 ckpt_name、steps、cfg 即可）
"""
import time
from pathlib import Path

from ..base import ImageProvider, ImageResult, ProviderError
from ..registry import register
from ... import config
from ...core.cost import Cost, to_si


@register("image", "realistic")
class RealisticImage(ImageProvider):
    """真人写实出图（未实现，仅占位）。

    配置：
        REALISTIC_MODEL      写实底模文件名（默认空 = 不可用）
        REALISTIC_STEPS      采样步数（默认 30）
        REALISTIC_CFG        CFG（默认 5.5，写实要低一点）
    """

    def available(self):
        # 只有真的配了写实模型才算可用，避免下拉里出现假选项
        return bool(config.REALISTIC_MODEL)

    def cost_estimate(self):
        return to_si("0.05")     # 本地电费估算

    def generate(self, prompt, negative, seed, size, ref_image=None, **kwargs):
        raise ProviderError(
            "真人写实出图还没实现。\n"
            "  方案一（推荐）：把「画图」换成 sdwebui 或 openai_image，用写实模型/云端出图\n"
            "  方案二：按 plugins/image/realistic.py 顶部注释补完 generate()\n"
            "  当前配置 REALISTIC_MODEL=%r" % (config.REALISTIC_MODEL or "",),
            provider="realistic", retryable=False)

    def head_ok(self, path):
        try:
            from ... import image_gen
            cut, why = image_gen.head_is_cut(path)
            return (not cut), why
        except Exception:  # noqa: BLE001
            return True, ""


@register("image", "sdxl")
class SDXLImage(ImageProvider):
    """SDXL 出图（未实现，仅占位）。

    6 GB 显存提示：
        1080×1920 会爆显存；实际要 832×1216 生成再放大，
        单张约 60~120 秒，40 张要 40~80 分钟。

    配置：
        SDXL_MODEL    SDXL 底模文件名（默认空 = 不可用）
    """

    def available(self):
        return bool(getattr(config, "SDXL_MODEL", ""))

    def cost_estimate(self):
        return to_si("0.08")

    def generate(self, prompt, negative, seed, size, ref_image=None, **kwargs):
        raise ProviderError(
            "SDXL 出图还没实现。\n"
            "  6 GB 显存跑 1080×1920 会爆，建议改 832×1216 后放大；\n"
            "  或直接用云端出图（openai_image）。\n"
            "  当前 SDXL_MODEL=%r" % (getattr(config, "SDXL_MODEL", "") or "",),
            provider="sdxl", retryable=False)

    def head_ok(self, path):
        return True, ""
