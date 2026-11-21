# -*- coding: utf-8 -*-
"""电台节目渲染插件。

统一签名：
    render(script, out_dir) -> RadioResult

依赖：
    TTS 插件（口播）
    BGM 插件（垫乐，可选）
    ffmpeg（混音、淡入淡出、拼接）

实现类：
    simple    自动合成（TTS + BGM + ffmpeg）
    manual    只导出清单，人工处理
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ...core import atomic, integrity
from ...core.cost import Cost
from .. import base, registry


def _ffmpeg():
    """找 ffmpeg 可执行文件。

    查找顺序：
        1. config.FFMPEG_EXE
        2. PATH 里的 ffmpeg
        3. WinGet 安装目录（PATH 未刷新的场景）
        4. 常见安装位置
    """
    from ... import config

    exe = getattr(config, "FFMPEG_EXE", "") or ""
    if exe and Path(exe).exists():
        return exe

    found = shutil.which("ffmpeg")
    if found:
        return found

    # WinGet 安装目录（PATH 没刷新时也能找到）
    import os
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        winget_pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.exists():
            for p in winget_pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
                if p.exists():
                    return str(p)

    # 常见位置
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"D:\ffmpeg\bin\ffmpeg.exe",
    ):
        if Path(cand).exists():
            return cand

    return ""


def _ffprobe():
    """找 ffprobe 可执行文件（优先和 ffmpeg 同目录）。"""
    from ... import config

    exe = getattr(config, "FFPROBE_EXE", "") or ""
    if exe and Path(exe).exists():
        return exe

    ff = _ffmpeg()
    if ff:
        name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        p = Path(ff).parent / name
        if p.exists():
            return str(p)

    found = shutil.which("ffprobe")
    return found or ""


def probe_duration(path):
    """探测音频时长（秒）。失败返回 0。"""
    exe = _ffprobe()
    if not exe:
        return 0.0
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20)
        if proc.returncode == 0:
            return float(proc.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


@registry.register("radio", "simple")
class SimpleRadio(base.RadioProvider):
    """电台自动合成：TTS 逐段 + BGM 垫底 + ffmpeg 混音。

    流程：
        1. 遍历 segments，speech 段落调 TTS 生成 mp3
        2. silence 段落生成等长静音
        3. 用 ffmpeg concat 拼接所有段落
        4. 叠加 BGM（按 bgm_volume 衰减）
        5. 生成 srt 字幕
    """
    name = "simple"

    def __init__(self, config=None, tts=None, bgm=None):
        super().__init__(config)
        self._tts = tts
        self._bgm = bgm

    def available(self) -> bool:
        return bool(_ffmpeg())

    def _get_tts(self, out_path=None):
        """取 TTS 插件实例。

        注意：TTS 插件普遍通过 config['out_path'] 指定输出路径，
        所以每次要给新路径时必须新建实例，不能复用。
        """
        if self._tts is not None:
            return self._tts

        from .. import registry as reg
        reg.load_all()

        cfg = {"out_path": str(out_path)} if out_path else {}

        # 优先用配置指定的 TTS，失败则自动选
        try:
            return reg.make("tts", None, cfg)
        except Exception:  # noqa: BLE001
            pass

        try:
            return reg.pick("tts")
        except Exception as exc:  # noqa: BLE001
            raise base.ProviderError(
                "没有可用的 TTS 插件：%r" % exc, provider=self.name)

    def render(self, script, out_dir):
        """渲染电台节目。"""
        ff = _ffmpeg()
        if not ff:
            raise base.ProviderError(
                "ffmpeg 未安装或未加入 PATH", provider=self.name)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        seg_dir = out_dir / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        seg_files = []
        seg_meta = []
        cursor = 0.0

        for i, seg in enumerate(script.segments):
            seg_path = seg_dir / ("seg_%03d.mp3" % i)
            seg_start = cursor

            if seg.kind == "speech":
                if not seg.text.strip():
                    seg_meta.append({
                        "index": i, "kind": "skip",
                        "reason": "空文本"})
                    continue
                tts = self._get_tts(seg_path)
                try:
                    res = tts.synthesize(
                        seg.text,
                        voice=seg.voice or script.voice or None,
                    )
                    actual = Path(getattr(res, "path", None) or seg_path)
                except Exception as exc:  # noqa: BLE001
                    raise base.ProviderError(
                        "第 %d 段 TTS 失败：%r" % (i, exc),
                        provider=self.name)

                if not actual.exists():
                    raise base.ProviderError(
                        "第 %d 段音频文件未生成" % i, provider=self.name)

                ok, why = integrity.validate_audio(actual)
                if not ok:
                    raise base.ProviderError(
                        "第 %d 段音频校验失败：%s" % (i, why),
                        provider=self.name)

                dur = getattr(res, "duration", 0) or probe_duration(actual)
                seg_files.append(actual)
                seg_meta.append({
                    "index": i, "kind": "speech",
                    "text": seg.text[:40],
                    "audio": str(actual),
                    "start": round(seg_start, 2),
                    "duration": round(dur, 2),
                })
                cursor += dur

            elif seg.kind == "silence":
                dur = seg.duration or 1.0
                self._make_silence(ff, seg_path, dur)
                seg_files.append(seg_path)
                seg_meta.append({
                    "index": i, "kind": "silence",
                    "start": round(seg_start, 2),
                    "duration": round(dur, 2),
                })
                cursor += dur

            elif seg.kind == "music":
                if seg.music and Path(seg.music).exists():
                    dur = probe_duration(seg.music)
                    seg_files.append(Path(seg.music))
                    seg_meta.append({
                        "index": i, "kind": "music",
                        "music": seg.music,
                        "start": round(seg_start, 2),
                        "duration": round(dur, 2),
                    })
                    cursor += dur
                else:
                    seg_meta.append({
                        "index": i, "kind": "skip",
                        "reason": "music 文件不存在：%s" % seg.music})

            elif seg.kind == "sfx":
                seg_meta.append({
                    "index": i, "kind": "skip",
                    "reason": "音效暂未实现"})

        if not seg_files:
            raise base.ProviderError(
                "没有任何可用的音频片段", provider=self.name)

        # ---------- 拼接 ----------
        concat_txt = out_dir / "concat.txt"
        concat_txt.write_text(
            "\n".join("file '%s'" % p.as_posix() for p in seg_files),
            encoding="utf-8")

        merged = out_dir / "voice.mp3"
        proc = subprocess.run(
            [ff, "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_txt), "-c", "copy", str(merged)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300)
        if proc.returncode != 0:
            raise base.ProviderError(
                "音频拼接失败：%s" % (proc.stderr or "")[-300:],
                provider=self.name)

        voice_dur = probe_duration(merged)

        # ---------- 叠加 BGM ----------
        final_audio = merged
        if script.bgm and Path(script.bgm).exists():
            bgm_out = out_dir / "full.mp3"
            vol = max(0.0, min(1.0, script.bgm_volume))
            proc2 = subprocess.run(
                [ff, "-y",
                 "-i", str(merged),
                 "-stream_loop", "-1", "-i", script.bgm,
                 "-filter_complex",
                 "[1:a]volume=%.2f[bg];"
                 "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2"
                 % vol,
                 "-c:a", "libmp3lame", "-q:a", "3",
                 str(bgm_out)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300)
            if proc2.returncode == 0:
                final_audio = bgm_out
            else:
                print("[电台] BGM 叠加失败，使用纯口播版")

        # ---------- 字幕 ----------
        srt_path = out_dir / ("%s.srt" % (script.title or "radio"))
        self._write_srt(srt_path, seg_meta)

        ok, why = integrity.validate_audio(final_audio)
        if not ok:
            raise base.ProviderError(
                "输出音频校验失败：%s" % why, provider=self.name)

        return base.RadioResult(
            audio=final_audio,
            subtitle=srt_path,
            duration=round(probe_duration(final_audio) or voice_dur, 2),
            cost=Cost.zero(),
            segments=seg_meta,
        )

    @staticmethod
    def _make_silence(ff, out, duration):
        """生成等长静音。"""
        subprocess.run(
            [ff, "-y", "-f", "lavfi",
             "-i", "anullsrc=r=44100:cl=mono",
             "-t", "%.2f" % duration,
             "-c:a", "libmp3lame", "-q:a", "5", str(out)],
            capture_output=True, timeout=60)

    @staticmethod
    def _write_srt(path, seg_meta):
        """写 srt 字幕。"""
        lines = []
        n = 1
        for seg in seg_meta:
            if seg.get("kind") != "speech":
                continue
            start = seg.get("start", 0)
            dur = seg.get("duration", 0)
            lines.append(str(n))
            lines.append("%s --> %s" % (_srt_time(start),
                                        _srt_time(start + dur)))
            lines.append(seg.get("text", ""))
            lines.append("")
            n += 1
        path.write_text("\n".join(lines), encoding="utf-8")


def _srt_time(sec):
    """秒 → srt 时间格式。"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


@registry.register("radio", "manual")
class ManualRadio(base.RadioProvider):
    """电台人工处理：只导出清单和脚本，不自动合成。"""

    name = "manual"

    def available(self) -> bool:
        return True

    def render(self, script, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 导出可读脚本
        lines = []
        lines.append("=" * 60)
        lines.append(script.title or "（未命名电台）")
        if script.host:
            lines.append("主播：%s" % script.host)
        lines.append("=" * 60)
        lines.append("")

        for i, seg in enumerate(script.segments):
            if seg.kind == "speech":
                lines.append("[%03d] 口播：" % i)
                lines.append(seg.text)
                lines.append("")
            elif seg.kind == "silence":
                lines.append("[%03d] 静音 %.1f 秒" % (i, seg.duration))
            elif seg.kind == "music":
                lines.append("[%03d] 垫乐：%s" % (i, seg.music))
            elif seg.kind == "sfx":
                lines.append("[%03d] 音效：%s" % (i, seg.notes or "未指定"))

        script_path = out_dir / "脚本.txt"
        script_path.write_text("\n".join(lines), encoding="utf-8")

        # 导出 JSON（给其他工具用）
        json_path = out_dir / "script.json"
        atomic.atomic_write_text(
            json_path,
            json.dumps(script.to_dict(), ensure_ascii=False, indent=2),
            verify=integrity.validate_json)

        return base.RadioResult(
            audio=None,
            subtitle=None,
            duration=script.total_duration_estimate(),
            cost=Cost.zero(),
            segments=[{"note": "人工处理，见 %s" % script_path.name}],
        )
