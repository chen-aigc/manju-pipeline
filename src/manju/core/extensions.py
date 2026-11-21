# -*- coding: utf-8 -*-
"""扩展接口预留：3D 场景 + 电台。

设计原则：
    预留接口不等于实现。这里定义"契约"和"占位实现"，
    真正的接入（Blender 渲染、电台编排）在需要时补。

一、3D（video 环节的扩展）
    现有 video/blender 插件已经在，但只做"单帧 → 视频"。
    真正的 3D 流程需要：
        ① 场景资产管理（.blend 文件）
        ② 相机轨迹定义
        ③ 渲染参数（分辨率、采样、引擎）
        ④ 与 2D 人物合成

    预留接口：
        SceneAsset  - 3D 场景描述
        CameraPath  - 相机轨迹
        RenderSpec  - 渲染参数

二、电台（新的环节 radio）
    电台 = 纯音频节目：
        TTS 口播 + BGM 垫底 + 音效 + 字幕文件

    与现有管线的区别：
        不需要图像、不需要视频
        需要时间轴对齐（口播和 BGM 的节拍）

    预留接口：
        RadioScript  - 电台稿（主播 + 段落）
        RadioSegment - 单个段落（口播 / 垫乐 / 音效）
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any


# ================================================================ 3D
@dataclass
class SceneAsset:
    """3D 场景资产。"""
    name: str
    blend_path: str                    # .blend 文件路径
    collection: str = ""               # 场景集合名
    default_camera: str = ""           # 默认相机名
    notes: str = ""

    def exists(self):
        return bool(self.blend_path) and Path(self.blend_path).exists()


@dataclass
class CameraPath:
    """相机轨迹。

    keyframes: [{"frame": 0, "location": (x,y,z), "rotation": (rx,ry,rz),
                 "lens": 50}, ...]
    """
    name: str = "default"
    keyframes: List[Dict[str, Any]] = field(default_factory=list)
    fps: int = 24
    duration_sec: float = 5.0

    def add_keyframe(self, frame, location, rotation=(0, 0, 0), lens=50):
        self.keyframes.append({
            "frame": int(frame),
            "location": tuple(location),
            "rotation": tuple(rotation),
            "lens": float(lens),
        })
        return self


@dataclass
class RenderSpec:
    """渲染参数。"""
    width: int = 1080
    height: int = 1920
    fps: int = 24
    samples: int = 64
    engine: str = "BLENDER_EEVEE"      # BLENDER_EEVEE | CYCLES
    transparent: bool = True            # 透明背景（用于合成）
    output_format: str = "PNG"

    def size(self):
        return (self.width, self.height)


# ---------------------------------------------------------------- 3D 接口
class BlenderScene3D:
    """3D 场景接口（占位实现）。

    真正的实现在 video/blender.py 里做；
    这里定义上层使用的契约，让编排层可以提前接入。
    """

    def __init__(self, scenes_dir=None):
        self.scenes_dir = Path(scenes_dir) if scenes_dir else None

    def list_scenes(self):
        """列出所有可用场景。"""
        out = []
        if not self.scenes_dir or not self.scenes_dir.exists():
            return out
        for p in self.scenes_dir.rglob("*.blend"):
            out.append(SceneAsset(
                name=p.stem,
                blend_path=str(p),
            ))
        return out

    def available(self):
        """3D 能力是否可用（需要 Blender 可执行文件）。"""
        import shutil
        from .. import config
        exe = getattr(config, "BLENDER_EXE", "") or ""
        if exe and Path(exe).exists():
            return True
        return shutil.which("blender") is not None

    def render(self, scene, camera_path, spec, out_path):
        """渲染一段视频（占位，未实现）。"""
        raise NotImplementedError(
            "3D 渲染未实现。下一步：\n"
            "  ① 在 video/blender.py 里补 render() 调用 blender --background\n"
            "  ② 用 CameraPath 生成关键帧脚本\n"
            "  ③ 输出透明背景 PNG 序列供合成"
        )


# ================================================================ 电台
@dataclass
class RadioSegment:
    """电台段落。"""
    kind: str ="speech"                # speech | music | sfx | silence
    text: str = ""                     # 口播文本（speech 用）
    voice: str = ""                    # 音色（speech 用）
    music: str = ""                    # BGM 文件（music 用）
    duration: float = 0.0              # 时长（silence/music 用）
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    notes: str = ""

    def to_dict(self):
        return {
            "kind": self.kind, "text": self.text, "voice": self.voice,
            "music": self.music, "duration": self.duration,
            "volume": self.volume, "fade_in": self.fade_in,
            "fade_out": self.fade_out, "notes": self.notes,
        }


@dataclass
class RadioScript:
    """电台稿。"""
    title: str = ""
    host: str = ""                     # 主播名
    voice: str = ""                    # 主播音色
    segments: List[RadioSegment] = field(default_factory=list)
    bgm: str = ""                      # 全片垫乐
    bgm_volume: float = 0.25

    def add(self, kind="speech", text="", **kw):
        self.segments.append(RadioSegment(kind=kind, text=text, **kw))
        return self

    def total_duration_estimate(self):
        """估算总时长（口播按字数估，中文约 4 字/秒）。"""
        total = 0.0
        for seg in self.segments:
            if seg.kind == "speech":
                total += len(seg.text) / 4.0
            elif seg.duration > 0:
                total += seg.duration
        return total

    def to_dict(self):
        return {
            "title": self.title, "host": self.host, "voice": self.voice,
            "bgm": self.bgm, "bgm_volume": self.bgm_volume,
            "segments": [s.to_dict() for s in self.segments],
            "estimated_duration": round(self.total_duration_estimate(), 1),
        }


class RadioPipeline:
    """电台编排接口（占位实现）。

    与视频管线的区别：
        - 只有音频输出（mp3/wav）
        - 不需要图像/视频
        - 需要精确的时间轴对齐

    依赖的现有插件：
        TTS   - 口播
        BGM   - 垫乐
        ffmpeg - 混音/淡入淡出
    """

    def __init__(self, tts_provider=None, bgm_provider=None):
        self.tts = tts_provider
        self.bgm = bgm_provider

    def available(self):
        return self.tts is not None

    def render(self, script, out_path):
        """渲染电台（占位，未实现）。"""
        raise NotImplementedError(
            "电台渲染未实现。下一步：\n"
            "  ① 按 segments 顺序调 TTS 生成口播\n"
            "  ② 用 ffmpeg 的 adelay/acrossfade/volume 拼时间轴\n"
            "  ③ 叠 BGM（衰减 25%）\n"
            "  ④ 输出 mp3 + 字幕 srt"
        )

    @staticmethod
    def from_dict(data):
        """从 dict 构造 RadioScript（给 Web 用）。"""
        rs = RadioScript(
            title=data.get("title", ""),
            host=data.get("host", ""),
            voice=data.get("voice", ""),
            bgm=data.get("bgm", ""),
            bgm_volume=float(data.get("bgm_volume", 0.25)),
        )
        for seg in data.get("segments") or []:
            rs.segments.append(RadioSegment(
                kind=seg.get("kind", "speech"),
                text=seg.get("text", ""),
                voice=seg.get("voice", ""),
                music=seg.get("music", ""),
                duration=float(seg.get("duration", 0) or 0),
                volume=float(seg.get("volume", 1.0) or 1.0),
                fade_in=float(seg.get("fade_in", 0) or 0),
                fade_out=float(seg.get("fade_out", 0) or 0),
                notes=seg.get("notes", ""),
            ))
        return rs


# ================================================================ 能力探测
def probe_3d():
    """探测 3D 能力。"""
    from .. import config
    scene = BlenderScene3D(Path(config.ASSETS_DIR) / "scenes")
    exe = getattr(config, "BLENDER_EXE", "") or ""
    return {
        "available": scene.available(),
        "blender_exe": exe,
        "scenes": [{"name": s.name, "path": s.blend_path}
                   for s in scene.list_scenes()],
        "note": ("未配置 BLENDER_EXE" if not exe
                 else "已配置，可以渲染" if Path(exe).exists()
                 else "BLENDER_EXE 指向的文件不存在"),
    }


def probe_radio():
    """探测电台能力。"""
    from ..plugins import registry
    registry.load_all(force=True)
    snap = registry.registry_snapshot()
    return {
        "available": bool(snap.get("tts")),
        "tts": snap.get("tts", []),
        "bgm": snap.get("bgm", []),
        "note": "电台 = TTS 口播 + BGM 垫底 + 字幕，复用现有插件",
    }
