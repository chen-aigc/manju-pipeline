# -*- coding: utf-8 -*-
"""插件基类：七个环节的统一接口。

约定：
    - 上层（编排层）只依赖这些基类，不 import 具体实现
    - 每个实现必须声明 name / available / cost_estimate
    - 失败一律抛 ProviderError，由编排层决定重试或降级
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.cost import Cost


class ProviderError(Exception):
    """插件层统一异常。"""

    def __init__(self, message, provider="", retryable=True, cost=None):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.cost = cost          # 失败也可能已经花钱


# ---------------------------------------------------------------- 结果对象
@dataclass
class ImageResult:
    path: Path
    seed: int = 0
    attempts: int = 1
    cost: Cost = field(default_factory=Cost.zero)
    elapsed: float = 0.0


@dataclass
class VideoResult:
    path: Path = None
    task_id: str = ""
    pending: bool = False
    duration: float = 0.0
    cost: Cost = field(default_factory=Cost.zero)
    elapsed: float = 0.0


@dataclass
class EditResult:
    video: Path = None
    pack_dir: Path = None
    cost: Cost = field(default_factory=Cost.zero)
    elapsed: float = 0.0


@dataclass
class ScriptResult:
    data: dict
    cost: Cost = field(default_factory=Cost.zero)
    elapsed: float = 0.0


@dataclass
class TTSResult:
    path: Path
    duration: float = 0.0
    cost: Cost = field(default_factory=Cost.zero)
    elapsed: float = 0.0


@dataclass
class BGMResult:
    path: Path = None
    plan: list = field(default_factory=list)
    cost: Cost = field(default_factory=Cost.zero)


# ---------------------------------------------------------------- 基类
class BaseProvider(ABC):
    name = "base"

    def __init__(self, config=None):
        self.config = config or {}

    @abstractmethod
    def available(self) -> bool:
        """服务是否可用。不可用时编排层走降级链。"""

    def cost_estimate(self):
        """预估本次成本（丝）。用于预算保护，未知返回 None。"""
        return None

    def describe(self):
        return {"name": self.name, "available": self.available()}

    def __repr__(self):
        return "<%s name=%s>" % (type(self).__name__, self.name)


class ScriptProvider(BaseProvider):
    @abstractmethod
    def generate(self, source, memory, chapter, target_shots) -> ScriptResult:
        """生成分镜表。"""

    def summarize(self, text):
        return ""


class ImageProvider(BaseProvider):
    @abstractmethod
    def generate(self, prompt, negative, seed, size, ref_image=None) -> ImageResult:
        """文生图。

        统一签名（所有实现必须一致）：
            prompt   正向提示词
            negative 负向提示词（可为空）
            seed     随机种子（-1 = 随机）
            size     (w,h) / 'WxH' / 预设名 / None
            ref_image 参考图路径或 URL（None = 纯文生图）

        可选参数（实现可以支持，也可以忽略）：
            keep_seed_on_retry  bool，重试时是否固定种子

        为了让上层统一调用，建议实现时这样写：
            def generate(self, prompt, negative, seed, size,
                         ref_image=None, **kwargs):
                keep = kwargs.get("keep_seed_on_retry", False)
                ...

        **kwargs 兜底是必须的：编排层可能传额外参数，
        没有 **kwargs 的实现会直接 TypeError。
        """

    def head_ok(self, path):
        """切头检测。默认不检测。"""
        return True, ""


class VideoProvider(BaseProvider):
    @abstractmethod
    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None) -> VideoResult:
        """图生视频。同步实现直接返回完整结果；异步实现返回 pending。"""

    def query(self, task_id) -> VideoResult:
        raise NotImplementedError

    def supports_keyframes(self):
        return False

    def takes_seconds_async(self):
        return False


class TTSProvider(BaseProvider):
    @abstractmethod
    def synthesize(self, text, voice, rate=None, pitch=None) -> TTSResult:
        """合成一句。"""

    def voices(self):
        return []


class BGMProvider(BaseProvider):
    @abstractmethod
    def build(self, shots, chapter_dir) -> BGMResult:
        """按镜头情绪产出 BGM 音轨。"""


class EditProvider(BaseProvider):
    @abstractmethod
    def build(self, chapter_dir, shots, audio, bgm, srt) -> EditResult:
        """初剪：成片 + 可选素材包。"""


class LogProvider(BaseProvider):
    @abstractmethod
    def log(self, event):
        """记录一条事件。"""

    def emit(self, out_path=None):
        """落盘报表。返回路径或 None。"""
        return None


# ================================================================ 3D
@dataclass
class Scene3DResult:
    """3D 渲染结果。"""
    video: Optional[Path] = None        # 渲染出的视频（或 PNG 序列的目录）
    frames_dir: Optional[Path] = None   # 透明背景 PNG 序列目录
    duration: float = 0.0
    cost: Cost = field(default_factory=Cost.zero)
    data: Dict[str, Any] = field(default_factory=dict)


class Scene3DProvider(BaseProvider):
    """3D 场景渲染（video 环节的一种特殊实现）。

    统一签名：
        render(scene, camera, spec, out_dir) -> Scene3DResult

    实现类：
        blender     本地 Blender（blender --background）
        unreal      本地 Unreal Engine（未实现）
        placeholder 占位（纯色 / 雪花屏，用于验证管线）
    """

    @abstractmethod
    def render(self, scene, camera, spec, out_dir) -> Scene3DResult:
        """渲染一段 3D 视频。

        Args:
            scene:   SceneAsset（来自 core.extensions）
            camera:  CameraPath（来自 core.extensions）
            spec:    RenderSpec（来自 core.extensions）
            out_dir: 输出目录

        Returns:
            Scene3DResult
        """

    def list_scenes(self, scenes_dir):
        """列出该渲染器支持的场景。"""
        return []

    def supported_engines(self):
        """支持的渲染引擎名。"""
        return []


# ================================================================ 电台
@dataclass
class RadioResult:
    """电台渲染结果。"""
    audio: Optional[Path] = None        # 输出音频（mp3/wav）
    subtitle: Optional[Path] = None     # 字幕（srt）
    duration: float = 0.0
    cost: Cost = field(default_factory=Cost.zero)
    segments: List[Dict[str, Any]] = field(default_factory=list)


class RadioProvider(BaseProvider):
    """电台节目渲染。

    统一签名：
        render(script, out_dir) -> RadioResult

    与视频管线的区别：
        只需要 TTS + BGM + 混音，不需要图像和视频帧。

    实现类：
        simple    TTS 逐段生成 + ffmpeg 混音（当前实现）
        manual    只导出脚本清单，人工剪
    """

    @abstractmethod
    def render(self, script, out_dir) -> RadioResult:
        """渲染电台节目。

        Args:
            script:  RadioScript（来自 core.extensions）
            out_dir: 输出目录

        Returns:
            RadioResult
        """
