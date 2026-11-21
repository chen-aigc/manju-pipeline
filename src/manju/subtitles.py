# -*- coding: utf-8 -*-
"""用 drawtext 绘制字幕（不依赖 libass，避免字体加载失败）。"""
import re
import subprocess
import tempfile
from pathlib import Path

from . import config
from .tts import ffmpeg_path
from .video import best_encoder


def parse_srt(path):
    """解析 SRT → [(start, end, text), ...]"""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", raw.strip())
    out = []
    for blk in blocks:
        lines = [x for x in blk.splitlines() if x.strip()]
        if len(lines) < 3:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text = " ".join(lines[2:])
        out.append((start, end, text))
    return out


def _esc(text):
    """drawtext 的转义：冒号、单引号、反斜杠、百分号"""
    t = text.replace("\\", "")
    t = t.replace(":", "\\:")
    t = t.replace("'", "\u2019")
    t = t.replace("%", "\\%")
    t = t.replace("\n", " ")
    return t


def burn_with_drawtext(video_path, srt_path, out_path,
                       fontsize=42, color="white", border=3):
    """逐条用 drawtext 画字幕。

    参数按 1080x1920 视频设计：
        fontsize 44   约等于屏幕宽度的 4%
        位置          底部往上 340 像素（画面下方约 1/4）
        bordercolor   黑色描边 3 像素
    """
    entries = parse_srt(srt_path)
    if not entries:
        return None

    W, H = config.VIDEO_W, config.VIDEO_H

    # 字体文件优先指定，避免 ffmpeg 找不到系统字体
    font_file = None
    for fp in (r"C:\Windows\Fonts\msyhbd.ttc",
               r"C:\Windows\Fonts\msyh.ttc",
               r"C:\Windows\Fonts\simhei.ttf"):
        if Path(fp).exists():
            font_file = fp.replace("\\", "/").replace(":", "\\:")
            break

    filters = []
    for start, end, text in entries:
        if not text.strip():
            continue
        e = _esc(text)
        parts = [
            "fontsize=%d" % fontsize,
            "fontcolor=%s" % color,
            "bordercolor=black",
            "borderw=%d" % border,
            "x=(w-text_w)/2",
            "y=h-300",
            "enable='between(t,%.3f,%.3f)'" % (start, end),
        ]
        if font_file:
            parts.insert(0, "fontfile='%s'" % font_file)
        # 单行显示：超过 12 字就压缩字号，不换行
        one_line = text.replace("\n", " ").strip()
        if len(one_line) > 12:
            shrink = max(0.62, 12.0 / len(one_line))
            p2 = list(parts)
            for idx, x in enumerate(p2):
                if x.startswith("fontsize="):
                    p2[idx] = "fontsize=%d" % int(fontsize * shrink)
            filters.append("drawtext=text='%s':%s" % (_esc(one_line), ":".join(p2)))
        else:
            filters.append("drawtext=text='%s':%s" % (_esc(one_line), ":".join(parts)))

    if not filters:
        return None

    vf = ",".join(filters)

    enc = "libx264" if best_encoder() == "h264_nvenc" else best_encoder()
    cmd = [
        ffmpeg_path(), "-y",
        "-i", str(Path(video_path).resolve()),
        "-filter_complex", "[0:v]" + vf + "[vout]",
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", enc, "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "20",
        "-c:a", "copy",
        str(Path(out_path).resolve()),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    if r.returncode != 0:
        print("[字幕] drawtext 失败：%s" % (r.stderr or "")[-600:])
        return None
    return Path(out_path)


def _split(text, per_line=10):
    text = " ".join(str(text).split())
    if len(text) <= per_line:
        return [text]
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？，；：、" and len(buf) >= per_line * 0.6:
            out.append(buf)
            buf = ""
        elif len(buf) >= per_line:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out[:3]
