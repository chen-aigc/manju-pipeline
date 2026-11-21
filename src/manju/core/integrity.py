# -*- coding: utf-8 -*-
"""产物完整性校验。

原则：宁可判坏也不要放过半截文件。
     mp4 缺 moov、png 写一半，都要能被识别出来。
"""
import json
import subprocess
from pathlib import Path

# 最小体积阈值（只做快速预过滤，真正的截断检测靠 PIL.verify）
# 生成图通常数百 KB~ 数 MB；纯色小图可能只有几百字节，不能卡太死
MIN_IMAGE_BYTES = 64
MIN_AUDIO_BYTES = 1024
MIN_VIDEO_BYTES = 4096
MIN_JSON_BYTES = 8


def _ffmpeg():
    from ..tts import ffmpeg_path
    return ffmpeg_path()


def validate_image(path, min_bytes=MIN_IMAGE_BYTES):
    """图片校验：能打开 + 尺寸有效 + 体积够。

    体积阈值只是防"空文件/截断"的粗筛，
    真正的正确性靠 PIL 解码 + verify() + 尺寸检查。
    所以阈值不宜太高：纯色小图可能只有几百字节。
    """
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    size = path.stat().st_size
    if size < min_bytes:
        return False, "体积过小（%d 字节，最小 %d）" % (size, min_bytes)
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()            # 检测截断
        with Image.open(path) as im:
            w, h = im.size
            if w < 8 or h < 8:
                return False, "尺寸异常 %dx%d" % (w, h)
    except Exception as exc:  # noqa: BLE001
        return False, "无法解码：%s" % exc
    return True, ""


def _probe(path, timeout=30):
    """读 ffmpeg -i 输出（含时长、流信息）。

    注意：不能用 -v error —— 那样连 Duration 行也会被吞掉。
    """
    try:
        r = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stderr or "") + (r.stdout or "")
    except Exception as exc:  # noqa: BLE001
        return "PROBE_ERROR: %s" % exc


def _duration_of(text):
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", text or "")
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def validate_media(path, min_bytes, has_video=False, deep=False):
    """音视频通用校验。

    浅校验：ffmpeg 能解析出时长 + 有对应流。
    深校验：完整解码一遍（慢，但能抓出中间损坏）。
    """
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    size = path.stat().st_size
    if size < min_bytes:
        return False, "体积过小（%d 字节）" % size

    info = _probe(path)
    if info.startswith("PROBE_ERROR"):
        return False, "无法读取：%s" % info
    if "Duration: N/A" in info or "Invalid data" in info:
        return False, "文件头损坏（可能缺 moov）"

    dur = _duration_of(info)
    if dur <= 0:
        return False, "时长无效（可能是半截文件）"

    if has_video and "Video:" not in info:
        return False, "缺少视频流"
    if not has_video and "Audio:" not in info:
        return False, "缺少音频流"

    if deep:
        try:
            r = subprocess.run(
                [_ffmpeg(), "-v", "error", "-i", str(path),
                 "-f", "null", "-"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300)
            err = (r.stderr or "").strip()
            if err:
                return False, "解码出错：%s" % err.splitlines()[0][:80]
        except Exception as exc:  # noqa: BLE001
            return False, "深校验失败：%s" % exc

    return True, ""


def validate_audio(path, deep=False):
    return validate_media(path, MIN_AUDIO_BYTES, has_video=False, deep=deep)


def validate_video(path, deep=False):
    return validate_media(path, MIN_VIDEO_BYTES, has_video=True, deep=deep)


def validate_json(path, required=None, min_bytes=MIN_JSON_BYTES):
    """JSON 校验：能解析 + 必填字段存在。"""
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    if path.stat().st_size < min_bytes:
        return False, "体积过小"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, "JSON 解析失败：%s" % exc

    for key in (required or []):
        if not isinstance(data, dict) or key not in data:
            return False, "缺少字段 %s" % key
    return True, ""


def validate_xlsx(path):
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True)
        n = len(wb.sheetnames)
        wb.close()
        if n == 0:
            return False, "没有任何工作表"
    except Exception as exc:  # noqa: BLE001
        return False, "无法打开：%s" % exc
    return True, ""


_KIND_BY_SUFFIX = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio",
    ".mp4": "video", ".mov": "video", ".mkv": "video",
    ".json": "json",
    ".xlsx": "xlsx",
    ".srt": "text", ".txt": "text", ".md": "text",
}


def kind_of(path):
    return _KIND_BY_SUFFIX.get(Path(path).suffix.lower(), "unknown")


def validate(path, kind=None, **kw):
    """按扩展名分派校验。返回 (ok, reason)。"""
    kind = kind or kind_of(path)
    path = Path(path)

    if kind == "image":
        return validate_image(path, **kw)
    if kind == "audio":
        return validate_audio(path, **kw)
    if kind == "video":
        return validate_video(path, **kw)
    if kind == "json":
        return validate_json(path, **kw)
    if kind == "xlsx":
        return validate_xlsx(path, **kw)
    if kind == "text":
        if not path.exists():
            return False, "文件不存在"
        if path.stat().st_size < 1:
            return False, "空文件"
        return True, ""
    # 未知类型：只要存在且非空就放过
    if not path.exists():
        return False, "文件不存在"
    if path.stat().st_size < 1:
        return False, "空文件"
    return True, ""


def scan(root, kinds=None):
    """扫描目录，返回 (好文件列表, 坏文件列表[(路径, 原因)])。"""
    root = Path(root)
    good, bad = [], []
    if not root.exists():
        return good, bad

    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.name.endswith(".tmp"):
            continue
        k = kind_of(f)
        if kinds and k not in kinds:
            continue
        ok, reason = validate(f, k)
        if ok:
            good.append(f)
        else:
            bad.append((f, reason))
    return good, bad
