# -*- coding: utf-8 -*-
"""语音合成（edge-tts，免费）+ 时长探测 + SRT 生成。"""
import asyncio
import re
import shutil
import subprocess
from pathlib import Path

from . import config

_FFMPEG = None


def ffmpeg_path():
    """优先用 imageio-ffmpeg 自带的二进制，找不到再退回系统 PATH。"""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    try:
        import imageio_ffmpeg
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        _FFMPEG = "ffmpeg"
    return _FFMPEG


def probe_duration(path):
    """用 ffmpeg 读取音频/视频时长（秒）。"""
    exe = ffmpeg_path()
    try:
        r = subprocess.run([exe, "-i", str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        out = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


async def _speak(text, voice, out_path, rate, volume, pitch="+0Hz"):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate,
                                       volume=volume, pitch=pitch)
    await communicate.save(str(out_path))


# 情绪 → (语速, 音调) 调整
# edge-tts 的 pitch 单位是 Hz，rate 是百分比
EMOTION_PRESETS = {
    "震惊": ("+15%", "+15Hz"),
    "惊愕": ("+15%", "+15Hz"),
    "愤怒": ("+10%", "-8Hz"),
    "狠": ("+12%", "-5Hz"),
    "轻蔑": ("-8%", "-12Hz"),
    "嘲讽": ("-5%", "-10Hz"),
    "悲伤": ("-15%", "-15Hz"),
    "低落": ("-12%", "-12Hz"),
    "冰冷": ("-10%", "-8Hz"),
    "平静": ("+0%", "+0Hz"),
    "紧张": ("+10%", "+8Hz"),
    "坚定": ("+5%", "+5Hz"),
}


def emotion_params(emotion, base_rate=None):
    """按情绪返回 (rate, pitch)。没匹配到就用默认。"""
    e = str(emotion or "").strip()
    for k, (r, pi) in EMOTION_PRESETS.items():
        if k in e:
            return r, pi
    return base_rate or config.TTS_RATE, "+0Hz"


def synthesize(text, voice, out_path, rate=None, volume=None, pitch=None):
    """合成单句，返回文件路径。"""
    text = (text or "").strip()
    if not text:
        return None
    # 纯标点没有可朗读内容，直接跳过（否则 edge-tts 抛 NoAudioReceived）
    import re as _re
    if len(_re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)) < 1:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_speak(text, voice, out_path,
                       rate or config.TTS_RATE,
                       volume or config.TTS_VOLUME,
                       pitch or "+0Hz"))
    return out_path


def voice_for(role, memory=None, fallback_role=None):
    """给角色分配音色：记忆库 > 配置表 > 按名字猜性别 > 默认。

    「按名字猜性别」很关键——否则未登记的角色会全部落到旁白女声，
    听感上就像主角没说话。
    """
    role = str(role or "").strip()

    if memory and role:
        v = (memory.data["characters"].get(role) or {}).get("voice")
        if v:
            return v

    if role in config.VOICE_MAP:
        return config.VOICE_MAP[role]

    # 名字里带「女/娘/姐」等 → 女声
    if any(k in role for k in getattr(config, "FEMALE_HINTS", ())):
        return "zh-CN-XiaoyiNeural"

    # 名字里带「爷/叔/丁/生」等 → 男声
    if any(k in role for k in getattr(config, "MALE_HINTS", ())):
        return "zh-CN-YunxiNeural"

    if fallback_role and fallback_role in config.VOICE_MAP:
        return config.VOICE_MAP[fallback_role]
    return config.DEFAULT_VOICE


def concat_audio(paths, out_path):
    """把多段音频按顺序拼成一条。"""
    paths = [str(p) for p in paths if p and Path(p).exists()]
    if not paths:
        return None

    out_path = Path(out_path)
    if len(paths) == 1:
        # 单段也要落到 out_path，否则调用方拿不到预期文件名
        src = Path(paths[0])
        try:
            same = src.resolve() == out_path.resolve()
        except OSError:
            same = False
        if not same:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out_path)
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".list.txt")
    with open(list_file, "w", encoding="utf-8") as fh:
        for p in paths:
            fh.write("file '%s'\n" % str(p).replace("\\", "/"))

    exe = ffmpeg_path()
    cmd = [exe, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c", "copy", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    if r.returncode != 0 or not out_path.exists():
        # 退回重编码
        cmd = [exe, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
               "-ar", "24000", "-ac", "1", str(out_path)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        if r.returncode != 0:
            raise RuntimeError("音频拼接失败：%s" % (r.stderr or "")[-400:])
    list_file.unlink(missing_ok=True)
    return out_path


def srt_time(seconds):
    """秒 → SRT 时间戳 00:00:03,450"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def wrap_text(text, per_line=14, max_lines=2):
    """把长文本切成不超过 max_lines 行的字幕。

    规则：先按句号切句；单句超长再按逗号切；仍超长才硬切。
    """
    text = " ".join(str(text or "").split())
    if not text:
        return text

    # 1) 按句末标点切句
    sentences, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？…":
            sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())

    lines = []
    for sent in sentences:
        if len(sent) <= per_line:
            lines.append(sent)
            continue
        # 2) 超长句按逗号切
        parts, b = [], ""
        for ch in sent:
            b += ch
            if ch in "，；：、,;:" and len(b) >= per_line * 0.65:
                parts.append(b)
                b = ""
        if b:
            parts.append(b)
        # 3) 仍超长的硬切
        for part in parts:
            while len(part) > per_line * 1.6:
                lines.append(part[:per_line])
                part = part[per_line:]
            if part:
                lines.append(part)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        # 截断时补省略号，避免半句话说一半
        lines[-1] = lines[-1][:per_line].rstrip("，、；：") + "…"
    return "\n".join(lines)


def build_srt(entries, out_path):
    """entries: [(start_sec, end_sec, text), ...] → .srt 文件"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (start, end, text) in enumerate(entries, 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (srt_time(start), srt_time(end)))
        lines.append(wrap_text(text))
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
