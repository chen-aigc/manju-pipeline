# -*- coding: utf-8 -*-
"""3D 场景渲染插件：blender。"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ...core import atomic, integrity
from ...core.cost import Cost
from .. import base, registry


@registry.register("scene3d", "blender")
class BlenderRenderer(base.Scene3DProvider):
    """本地 Blender 渲染（blender --background）。

    依赖：
        BLENDER_EXE 指向 blender.exe，或 blender 在 PATH 里
    """
    name = "blender"

    def available(self) -> bool:
        return bool(self._exe())

    def _exe(self):
        import glob as _glob
        import os
        import shutil
        from ... import config

        exe = getattr(config, "BLENDER_EXE", "") or ""
        if exe and Path(exe).exists():
            return exe

        found = shutil.which("blender")
        if found:
            return found

        # WinGet
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            winget = Path(local) / "Microsoft" / "WinGet" / "Packages"
            if winget.exists():
                for p in winget.glob(
                        "BlenderFoundation.Blender*/**/blender.exe"):
                    if p.exists():
                        return str(p)

        # 便携版
        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        for pattern in (
            str(base_dir / "_tools" / "blender" / "**" / "blender.exe"),
            r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
            r"D:\Blender*\blender.exe",
        ):
            matches = sorted(_glob.glob(pattern, recursive=True))
            if matches:
                return matches[0]

        return ""

    def supported_engines(self):
        """检测当前 Blender 实际支持的引擎名。

        Blender 4.2+ 把 EEVEE 改名成 BLENDER_EEVEE_NEXT，
        4.1 及以前叫 BLENDER_EEVEE。这里动态探测。
        """
        exe = self._exe()
        if not exe:
            return []

        code = ("import bpy, json; "
                "items = bpy.types.RenderSettings.bl_rna.properties"
                "['engine'].enum_items; "
                "print('ENGINES=' + json.dumps([i.identifier for i in items]))")
        try:
            proc = subprocess.run(
                [exe, "--background", "--python-expr", code],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
            for line in (proc.stdout or "").split("\n"):
                if line.startswith("ENGINES="):
                    return json.loads(line[len("ENGINES="):])
        except Exception:  # noqa: BLE001
            pass

        # 兜底
        return ["BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES"]

    def list_scenes(self, scenes_dir):
        out = []
        d = Path(scenes_dir) if scenes_dir else None
        if not d or not d.exists():
            return out
        for p in sorted(d.rglob("*.blend")):
            out.append({
                "name": p.stem,
                "path": str(p),
                "size_mb": round(p.stat().st_size / 1024 / 1024, 1),
            })
        return out

    def render(self, scene, camera, spec, out_dir):
        exe = self._exe()
        if not exe:
            raise base.ProviderError(
                "Blender 未安装或 BLENDER_EXE 未配置", provider=self.name)

        if not scene.exists():
            raise base.ProviderError(
                "场景文件不存在：%s" % scene.blend_path, provider=self.name)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        # 引擎名兼容：BLENDER_EEVEE → BLENDER_EEVEE_NEXT（4.2+）
        engines = self.supported_engines()
        if engines and spec.engine not in engines:
            alt = None
            if spec.engine == "BLENDER_EEVEE" and \
                    "BLENDER_EEVEE_NEXT" in engines:
                alt = "BLENDER_EEVEE_NEXT"
            elif spec.engine == "BLENDER_EEVEE_NEXT" and \
                    "BLENDER_EEVEE" in engines:
                alt = "BLENDER_EEVEE"
            if alt:
                print("[3D] 引擎名兼容：%s → %s"
                      % (spec.engine, alt))
                spec.engine = alt
            else:
                print("[3D] 引擎 %s 不支持，改用 %s"
                      % (spec.engine, engines[0]))
                spec.engine = engines[0]

        script = self._build_script(scene, camera, spec, frames_dir)
        script_path = out_dir / "_render.py"
        script_path.write_text(script, encoding="utf-8")

        t0 = time.time()
        cmd = [exe, "--background", scene.blend_path,
               "--python", str(script_path),
               "--python-exit-code", "1"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900)
        except subprocess.TimeoutExpired:
            raise base.ProviderError(
                "Blender 渲染超时（900 秒）", provider=self.name)
        except OSError as exc:
            raise base.ProviderError(
                "调用 Blender 失败：%r" % exc, provider=self.name)

        elapsed = time.time() - t0
        (out_dir / "render.log").write_text(
            (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""),
            encoding="utf-8")

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-500:]
            raise base.ProviderError(
                "Blender 返回 %d：%s" % (proc.returncode, tail),
                provider=self.name)

        frames = sorted(frames_dir.glob("*.png"))
        if not frames:
            raise base.ProviderError(
                "Blender 没有输出 PNG（日志：%s/render.log）" % out_dir,
                provider=self.name)

        ok, why = integrity.validate_image(frames[0])
        if not ok:
            raise base.ProviderError(
                "输出帧校验失败：%s" % why, provider=self.name)

        return base.Scene3DResult(
            frames_dir=frames_dir,
            duration=len(frames) / max(spec.fps, 1),
            cost=Cost.zero(),
            data={
                "frames": len(frames),
                "engine": spec.engine,
                "elapsed": round(elapsed, 1),
                "log": str(out_dir / "render.log"),
            },
        )

    def _build_script(self, scene, camera, spec, frames_dir):
        """生成 Blender 执行的 Python 脚本。

        用字符串列表拼接，避免 % 和 f-string 的转义问题。
        """
        lines = [
            "# -*- coding: utf-8 -*-",
            "# 由 manju 自动生成",
            "import bpy",
            "import sys",
            "",
            "cam_name = " + repr(scene.default_camera or ""),
            "out_dir = " + repr(str(frames_dir).replace("\\", "/")),
            "width, height = %d, %d" % (spec.width, spec.height),
            "fps = %d" % spec.fps,
            "samples = %d" % spec.samples,
            "engine = " + repr(spec.engine),
            "transparent = %s" % ("True" if spec.transparent else "False"),
            "frame_end = %d" % int(camera.duration_sec * camera.fps),
            "",
            "# 渲染设置",
            "sc = bpy.context.scene",
            "sc.render.engine = engine",
            "sc.render.resolution_x = width",
            "sc.render.resolution_y = height",
            "sc.render.resolution_percentage = 100",
            "sc.render.fps = fps",
            "sc.render.film_transparent = transparent",
            "sc.frame_start = 0",
            "sc.frame_end = frame_end",
            "",
            'sc.render.image_settings.file_format = "PNG"',
            'sc.render.image_settings.color_mode = "RGBA"',
            'sc.render.filepath = out_dir + "/frame_"',
            "",
            "try:",
            '    if "CYCLES" in engine:',
            "        sc.cycles.samples = samples",
            "    else:",
            "        sc.eevee.taa_render_samples = samples",
            "except Exception as e:",
            '    print("[警告] 采样数设置失败:", e)',
            "",
            "# 相机",
            "cam = bpy.data.objects.get(cam_name) if cam_name else None",
            "if cam is None:",
            "    cam = bpy.context.scene.camera",
            "if cam is None:",
            "    # 兜底：找场景里第一个相机对象",
            "    cams = [o for o in bpy.data.objects if o.type == 'CAMERA']",
            "    if cams:",
            "        cam = cams[0]",
            "        bpy.context.scene.camera = cam",
            "if cam is None:",
            '    print("[错误] 场景里没有任何相机")',
            "    print('[调试] 可用对象:', [(o.name, o.type) "
            "for o in bpy.data.objects])",
            "    sys.exit(1)",
            "",
            'print("[manju] 相机:", cam.name)',
            "",
            "# 相机关键帧",
        ]

        for kf in camera.keyframes:
            f = int(kf["frame"])
            loc = kf["location"]
            rot = kf["rotation"]
            lens = float(kf.get("lens", 50))
            lines.append(
                "cam.location = (%.4f, %.4f, %.4f)" % (loc[0], loc[1], loc[2]))
            lines.append(
                "cam.rotation_euler = (%.4f, %.4f, %.4f)"
                % (rot[0], rot[1], rot[2]))
            lines.append("cam.data.lens = %.2f" % lens)
            lines.append(
                'cam.keyframe_insert("location", frame=%d)' % f)
            lines.append(
                'cam.keyframe_insert("rotation_euler", frame=%d)' % f)
            lines.append(
                'cam.data.keyframe_insert("lens", frame=%d)' % f)
            lines.append("")

        lines += [
            "# 渲染",
            'print("[manju] 开始渲染", frame_end - sc.frame_start + 1, "帧")',
            "bpy.ops.render.render(animation=True)",
            'print("[manju] 渲染完成")',
        ]

        return "\n".join(lines) + "\n"


@registry.register("scene3d", "placeholder")
class Placeholder3D(base.Scene3DProvider):
    """3D 占位渲染：纯色渐变，用于验证管线。"""

    name = "placeholder"

    def available(self) -> bool:
        try:
            from PIL import Image  # noqa: F401
            return True
        except ImportError:
            return False

    def render(self, scene, camera, spec, out_dir):
        from PIL import Image, ImageDraw

        out_dir = Path(out_dir)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        frame_end = int(camera.duration_sec * camera.fps)
        w, h = spec.size()

        for i in range(frame_end + 1):
            img = Image.new("RGBA", (w, h))
            draw = ImageDraw.Draw(img)
            ratio = i / max(frame_end, 1)
            r = int(40 + 100 * ratio)
            g = int(60 + 80 * (1 - ratio))
            b = int(120 + 60 * ratio)
            for y in range(h):
                alpha = int(255 * (1 - y / h * 0.5))
                draw.line([(0, y), (w, y)], fill=(r, g, b, alpha))
            draw.text((20, 20), "frame %d" % i, fill=(255, 255, 255, 200))
            img.save(frames_dir / ("frame_%04d.png" % i))

        frames = sorted(frames_dir.glob("*.png"))
        return base.Scene3DResult(
            frames_dir=frames_dir,
            duration=camera.duration_sec,
            cost=Cost.zero(),
            data={"frames": len(frames), "engine": "placeholder",
                  "note": "占位渲染，未使用真实 3D"},
        )
