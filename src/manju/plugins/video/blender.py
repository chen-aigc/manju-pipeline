# -*- coding: utf-8 -*-
"""video 插件：3D 渲染管线【接口占位】

状态：只有接口，没有实现。

两种「3D」要分清：

    ① 3D 风格画面（假 3D）
       用 SD1.5 + 3D 渲染风 LoRA 画出来，本质还是 2D 图。
       → 这个用 image 插件做，不需要本文件。

    ② 真 3D 渲染（本文件）
       建模 → 骨骼绑定 → 动画 → 渲染成帧序列 → 编码成视频。
       和 AI 出图是两条完全不同的流水线，只对应「视频」这一环。

要用的时候怎么补：
    1. 装 Blender（或 UE），确认命令行能跑：
           blender -b scene.blend -o //frames/f_#### -F PNG -a
    2. .env 设 BLENDER_EXE=<blender.exe 路径>
    3. 实现下面 _render()，把帧序列交给 video.frames_to_clip()
    4. 分镜脚本里补一个 scene / camera / action 字段，告诉 3D 场景怎么演
"""
import subprocess
import time
from pathlib import Path

from ..base import ProviderError, VideoProvider, VideoResult
from ..registry import register
from ... import config
from ...core.cost import Cost, to_si


@register("video", "blender")
class BlenderRender(VideoProvider):
    """Blender 离线渲染（未实现，仅占位）。

    配置：
        BLENDER_EXE        blender 可执行文件路径（默认空 = 不可用）
        BLENDER_BLEND_FILE 场景文件路径
        BLENDER_SAMPLES    采样数（默认 64）
    """

    def available(self):
        exe = getattr(config, "BLENDER_EXE", "")
        return bool(exe and Path(exe).exists())

    def cost_estimate(self, duration=3.0):
        # 3D 渲染只烧电，按 1 分钟渲染 ≈ 0.02 元估
        return to_si("0.02")

    def takes_seconds_async(self):
        return True

    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None):
        raise ProviderError(
            "Blender 3D 渲染还没实现。\n"
            "  需要的输入：场景文件 + 相机/动作参数（不是一张图）\n"
            "  补法见 plugins/video/blender.py 顶部注释\n"
            "  当前 BLENDER_EXE=%r" % (getattr(config, "BLENDER_EXE", "") or "",),
            provider="blender", retryable=False)

    def supports_keyframes(self):
        return True

    # ---------------- 给实现者留的工具 ----------------
    @staticmethod
    def _render(blend_file, out_dir, frame_start=1, frame_end=90,
                samples=64, timeout=1800):
        """示例：调 Blender 渲染成 PNG 帧序列。实现时直接复用。"""
        exe = getattr(config, "BLENDER_EXE", "")
        if not exe or not Path(exe).exists():
            raise ProviderError("BLENDER_EXE 未设置或不存在",
                                provider="blender", retryable=False)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        cmd = [exe, "-b", str(blend_file),
               "-o", str(Path(out_dir) / "f_####"),
               "-F", "PNG",
               "-x", "1",
               "-s", str(frame_start), "-e", str(frame_end),
               "-a"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout)
        if r.returncode != 0:
            raise ProviderError("Blender 渲染失败：%s"
                                % (r.stderr or "")[-300:],
                                provider="blender")
        return sorted(Path(out_dir).glob("f_*.png"))
