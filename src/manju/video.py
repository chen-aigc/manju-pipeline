# -*- coding: utf-8 -*-
"""视频合成 v2：子镜头切换 + 运镜 + 淡入淡出 + 字幕 + BGM。"""
import random
import subprocess
from pathlib import Path

from . import config
from .tts import ffmpeg_path

_encoder_cache = None


def best_encoder():
    global _encoder_cache
    if _encoder_cache:
        return _encoder_cache
    exe = ffmpeg_path()
    try:
        r = subprocess.run([exe, "-hide_banner", "-encoders"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        text = r.stdout or ""
        if config.USE_NVENC and "h264_nvenc" in text:
            _encoder_cache = "h264_nvenc"
        elif "libx264" in text:
            _encoder_cache = "libx264"
        else:
            _encoder_cache = "mpeg4"
    except Exception:  # noqa: BLE001
        _encoder_cache = "libx264"
    return _encoder_cache


def _run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 失败：%s" % (r.stderr or "")[-800:])
    return r


def _img_size(path):
    """读取图片真实宽高。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return (768, 1152)


def _crop_expr(crop, iw, ih):
    """把 0~1 的相对裁切区域换算成 ffmpeg crop 表达式。

    必须用图片的真实尺寸换算，否则裁切区域会超出图片边界报错。
    """
    x, y, w, h = crop
    # 先缩放到 2 倍目标尺寸（zoompan 需要更大画布才不抖）
    cw = max(2, int(iw * w) // 2 * 2)
    ch = max(2, int(ih * h) // 2 * 2)
    cx = max(0, min(int(iw * x), iw - cw)) // 2 * 2
    cy = max(0, min(int(ih * y), ih - ch)) // 2 * 2
    return "crop=%d:%d:%d:%d" % (cw, ch, cx, cy)


def make_shot_clip(image_path, duration, out_path, sub=None, size=None, fps=None):
    """单张图 → 带运镜的无声片段。

    关键改动：
        1. 图片「完整显示」（contain），不裁剪——避免切头切脸
        2. 画面外的空白用同图的模糊放大版填充（像短视频常见的背景）
        3. 缩放幅度压到 1.05，避免整体推近时把头顶推出画面
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    W, H = size or (config.VIDEO_W, config.VIDEO_H)
    fps = fps or config.VIDEO_FPS
    duration = max(0.4, float(duration))

    sub = sub or {}
    zoom = sub.get("zoom") or "in"
    pan = sub.get("pan") or "none"

    frames = max(2, int(duration * fps))
    if zoom == "out":
        zexpr = "if(eq(on,1),1.05,max(zoom-0.0008,1.0))"
    else:
        zexpr = "min(zoom+0.0008,1.05)"

    if pan == "left":
        xexpr = "iw/2-(iw/zoom/2)-((iw*0.02)*on/%d)" % frames
    elif pan == "right":
        xexpr = "iw/2-(iw/zoom/2)+((iw*0.02)*on/%d)" % frames
    else:
        xexpr = "iw/2-(iw/zoom/2)"
    # 关键：y 锚定顶部（0），放大时只从画面下方裁，
    # 这样人物头部永远不会被推出画面。
    yexpr = "0"

    # 背景：铺满 + 模糊；前景：完整显示 + 居中；再叠加
    fc = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
        "crop={W}:{H},boxblur=24:4,eq=brightness=-0.12[bgb];"
        "[fg]scale={WH}:{H}:force_original_aspect_ratio=increase,"
        "crop={W}:{H}[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        "scale={W2}:{H2},"
        "zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={fps},"
        "trim=duration={dur},setpts=PTS-STARTPTS[vout]"
    ).format(W=W, H=H, WH=W, W2=W * 2, H2=H * 2,
             z=zexpr, x=xexpr, y=yexpr, fps=fps, dur=duration)

    cmd = [
        ffmpeg_path(), "-y",
        "-loop", "1", "-i", str(image_path),
        "-t", "%.3f" % duration,
        "-filter_complex", fc,
        "-map", "[vout]",
        "-r", str(fps),
        "-c:v", best_encoder(),
        "-pix_fmt", "yuv420p",
        "-an",
        str(out_path),
    ]
    _run(cmd)
    return Path(out_path)


def _concat_file(clips, out_path, fade=False, fade_dur=0.25):
    """拼接片段；fade=True 时用 xfade 做交叉淡化。"""
    clips = [Path(c) for c in clips if c and Path(c).exists()]
    if not clips:
        raise ValueError("没有可拼接的片段")
    if len(clips) == 1:
        return Path(clips[0])

    if not fade or len(clips) < 2:
        list_file = Path(str(out_path) + ".concat.txt")
        with open(list_file, "w", encoding="utf-8") as fh:
            for c in clips:
                fh.write("file '%s'\n" % str(c).replace("\\", "/"))
        # 不用 -c copy：逐段时长不一致时时间戳会错乱，
        # 造成画面重影/叠帧。统一重编码。
        enc = "libx264" if best_encoder() == "h264_nvenc" else best_encoder()
        cmd = [ffmpeg_path(), "-y", "-f", "concat", "-safe", "0",
               "-i", str(list_file),
               "-c:v", enc, "-pix_fmt", "yuv420p",
               "-preset", "fast", "-crf", "20",
               "-fps_mode", "cfr", "-r", str(config.VIDEO_FPS),
               "-an", str(out_path)]
        _run(cmd)
        list_file.unlink(missing_ok=True)
        return Path(out_path)

    # xfade：交叉淡化会「吃掉」fade_dur 的时长，
    # 所以每段的 offset 要按累计的 xfade 扣除量来算，保证总时长不变。
    from .tts import probe_duration
    durs = [probe_duration(c) or 2.0 for c in clips]

    args = []
    for c in clips:
        args += ["-i", str(c)]

    filt = []
    prev = "[0:v]"
    acc = durs[0]
    for i in range(1, len(clips)):
        # 第 i 次转场的起始时刻 = 已拼接总长 - 本次淡化时长
        offset = acc - fade_dur
        out_label = "[v%d]" % i
        filt.append("%s[%d:v]xfade=transition=fade:duration=%.2f:offset=%.2f%s"
                    % (prev, i, fade_dur, max(0, offset), out_label))
        prev = out_label
        # 交叉淡化后，总长 = 已拼接 + 本段 - fade_dur
        acc = acc + durs[i] - fade_dur

    # 注意：xfade 输出的 yuv444 格式 NVENC 不支持，必须用 libx264
    enc = "libx264" if best_encoder() == "h264_nvenc" else best_encoder()
    cmd = [ffmpeg_path(), "-y"] + args + [
        "-filter_complex", ";".join(filt),
        "-map", prev,
        "-c:v", enc, "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "20",
        "-an", str(out_path),
    ]
    _run(cmd)
    return Path(out_path)


def concat_clips(clips, out_path, fade=False):
    return _concat_file(clips, out_path, fade=fade)


def probe_size(path):
    """读视频分辨率。没有 ffprobe，从 ffmpeg -i 的 stderr 里抠。

    返回 (w, h)；读不出来返回 (0, 0)。
    """
    import re
    import subprocess
    path = Path(path)
    if not path.exists():
        return (0, 0)
    try:
        r = subprocess.run([ffmpeg_path(), "-i", str(path)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        text = r.stderr or ""
        for m in re.finditer(r"Video:.*?(\d{2,5})x(\d{3,5})", text):
            return (int(m.group(1)), int(m.group(2)))
        m = re.search(r"(\d{2,5})x(\d{3,5})", text)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:  # noqa: BLE001
        pass
    return (0, 0)


def upscale_video(src, dst, width, height, sharpen=0.5, crf=18):
    """把成片放大到目标分辨率（Lanczos + 可选锐化）。

    用途：源素材是 1080p，输出要 4K 做展示。
    注意：这是上采样，不增加真实细节；面试时应当如实说明。
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise ValueError("源视频不存在：%s" % src)

    vf = ["scale=%d:%d:flags=lanczos" % (int(width), int(height))]
    try:
        s = float(sharpen or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s > 0:
        # 轻度反锐化，让放大后的边缘不糊
        vf.append("unsharp=5:5:%.2f:5:5:0.0" % s)

    enc = best_encoder()
    tmp = dst.with_name(dst.stem + ".up.tmp" + dst.suffix)
    cmd = [ffmpeg_path(), "-y", "-i", str(src),
           "-vf", ",".join(vf),
           "-c:v", enc, "-pix_fmt", "yuv420p",
           "-preset", "medium", "-crf", str(crf),
           "-c:a", "copy",
           "-movflags", "+faststart",
           str(tmp)]
    try:
        _run(cmd)
    except Exception:
        # 音频 copy 有时会失败（容器不兼容），去掉音轨再试一次
        cmd = [ffmpeg_path(), "-y", "-i", str(src),
               "-vf", ",".join(vf),
               "-c:v", enc, "-pix_fmt", "yuv420p",
               "-preset", "medium", "-crf", str(crf),
               "-an", str(tmp)]
        _run(cmd)

    tmp.replace(dst)
    return dst


def mux_audio(video_path, audio_path, out_path, bgm_path=None,
              bgm_volume=0.9, shortest=True):
    """合成音轨：人声 + （可选）BGM。"""
    from .tts import probe_duration

    cmd = [ffmpeg_path(), "-y", "-i", str(video_path)]
    inputs = 1
    audio_only = (audio_path and Path(audio_path).exists())
    audio_dur = 0.0
    if audio_only:
        try:
            audio_dur = probe_duration(audio_path)
        except Exception:  # noqa: BLE001
            audio_dur = 0.0

    if audio_only:
        cmd += ["-i", str(audio_path)]
        inputs += 1

    use_bgm = bgm_path and Path(bgm_path).exists()
    if use_bgm:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
        inputs += 1

    if audio_only and use_bgm:
        # 人声 + BGM 混音，BGM 压低
        cmd += [
            "-filter_complex",
            # normalize=0 很关键：否则 amix 会把两路各减半，人声变小
            "[1:a]volume=1.0[voice];[2:a]volume=%.2f[bg];"
            "[voice][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            % bgm_volume,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        ]
        if shortest:
            cmd += ["-shortest"]
    elif audio_only:
        cmd += ["-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
        if shortest:
            cmd += ["-shortest"]
    else:
        cmd += ["-c:v", "copy", "-an"]

    # 关键：用音频长度硬裁视频，防止逐段累积的时间漂移
    if audio_only and audio_dur > 1:
        cmd += ["-t", "%.3f" % audio_dur]

    # 输出路径必须无条件追加（之前条件分支里漏了，导致 ffmpeg 报“没有输出文件”）
    cmd += [str(out_path)]

    _run(cmd)
    return Path(out_path)


def burn_subtitles(video_path, srt_path, out_path):
    """字幕：亮黄 + 黑描边，下方 1/4，每行≤10字。

    坑：SRT 路径里含中文和反斜杠时，ffmpeg 的 subtitles 滤镜
    解析参数会错乱（把剩余路径当成 original_size）。
    解法：先把 SRT 复制到系统临时目录（纯英文、无反斜杠）再压制。
    """
    import shutil
    import tempfile

    src_srt = Path(srt_path)
    tmp_dir = Path(tempfile.gettempdir()) / "manju_sub"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_srt = tmp_dir / "sub.srt"
    shutil.copyfile(src_srt, safe_srt)

    # 相对路径更安全：切到 tmp_dir 执行，只用文件名
    style = (
        "FontName=Microsoft YaHei,FontSize=15,"
        "PrimaryColour=&H0000E5FF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2.5,Shadow=1,"
        "Alignment=2,MarginV=320,MarginL=80,MarginR=80"
    )
    vf = "subtitles=sub.srt:force_style='%s'" % style

    enc = "libx264" if best_encoder() == "h264_nvenc" else best_encoder()
    cmd = [ffmpeg_path(), "-y", "-i", str(Path(video_path).resolve()),
           "-vf", vf, "-c:v", enc, "-pix_fmt", "yuv420p",
           "-preset", "fast", "-crf", "20",
           "-c:a", "copy", str(Path(out_path).resolve())]

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=1800, cwd=str(tmp_dir))
    if r.returncode != 0:
        err = (r.stderr or "")[-500:]
        print("[字幕] 压制失败：%s" % err)
        return None
    return Path(out_path)


def add_denoise(image_path, out_path, strength=0.008, seed=None):
    """轻微噪点，规避平台画面去重（P3）。"""
    from PIL import Image
    import random as _r

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    px = img.load()
    rnd = _r.Random(seed)
    for _ in range(int(w * h * strength)):
        x = rnd.randrange(w)
        y = rnd.randrange(h)
        r, g, b = px[x, y]
        d = rnd.randint(-6, 6)
        px[x, y] = (max(0, min(255, r + d)),
                    max(0, min(255, g + d)),
                    max(0, min(255, b + d)))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# --------------------------------------------------------------------------
# BGM 分段编排：按剧情强度切歌
# --------------------------------------------------------------------------
BGM_BY_MOOD = {
    "calm": "suspense.mp3",      # 低潮、对话、铺垫
    "tense": "suspense.mp3",     # 紧张、对峙
    "climax": "epic.mp3",        # 高潮、爆发、打斗
    "sad": "sorrow.mp3",         # 悲伤、回忆
}


def mood_for_shot(shot):
    """粗判一个镜头属于哪种情绪，决定用哪条 BGM。"""
    st = str(shot.get("shot_type") or "")
    emo = ""
    for dl in shot.get("dialogues") or []:
        emo += str(dl.get("emotion") or "")
    txt = (str(shot.get("narration") or "") + emo + str(shot.get("act") or ""))

    if any(k in txt for k in ("震惊", "愤怒", "狠", "爆发", "击", "斩", "杀", "冲")):
        return "climax"
    if any(k in txt for k in ("悲", "泪", "死", "别", "离开", "低")):
        return "sad"
    if st in ("特写", "死物") and any(k in txt for k in ("怒", "决", "咬牙")):
        return "climax"
    if st in ("远景", "空镜"):
        return "calm"
    return "tense"


def build_bgm_plan(shots, chapter_dir):
    """按镜头顺序生成 [(时长, 音轨路径), ...]，并把相邻同曲合并。"""
    from pathlib import Path as _P

    bgm_dir = _P(config.ASSETS_DIR) / "bgm"
    plan = []
    for shot in shots:
        mood = mood_for_shot(shot)
        name = BGM_BY_MOOD.get(mood, "suspense.mp3")
        path = bgm_dir / name
        if not path.exists():
            continue
        dur = sum(x.get("duration", 0) for x in shot.get("sub_shots") or [])
        if dur <= 0:
            dur = float(shot.get("_duration") or 3.0)
        if plan and plan[-1][1] == path:
            plan[-1][0] += dur
        else:
            plan.append([dur, path])

    return [(d, p) for d, p in plan]


def render_bgm_track(plan, out_path, fade=0.8):
    """把分段 BGM 接成一条完整音轨，段与段之间交叉淡化。"""
    from pathlib import Path as _P

    plan = [(d, _P(p)) for d, p in plan if _P(p).exists() and d > 0.2]
    if not plan:
        return None

    if len(plan) == 1:
        dur, src = plan[0]
        cmd = [ffmpeg_path(), "-y", "-stream_loop", "-1", "-i", str(src),
               "-t", "%.2f" % dur,
               "-af", "afade=t=in:d=1.5,afade=t=out:st=%.2f:d=2" % max(0, dur - 2),
               "-c:a", "libmp3lame", "-b:a", "160k", str(out_path)]
        _run(cmd)
        return Path(out_path)

    # 多段：每段先截取对应时长，再用 acrossfade 串起来
    args, filt = [], []
    for i, (dur, src) in enumerate(plan):
        args += ["-stream_loop", "-1", "-i", str(src)]
        filt.append(
            "[%d:a]atrim=0:%.2f,asetpts=PTS-STARTPTS,"
            "afade=t=in:d=%.2f,afade=t=out:st=%.2f:d=%.2f[a%d]"
            % (i, dur, min(fade, dur / 4), max(0, dur - fade), fade, i))

    # 串联
    cur = "[a0]"
    for i in range(1, len(plan)):
        nxt = "[x%d]" % i
        filt.append("%s[a%d]acrossfade=d=%.2f:c1=tri:c2=tri%s"
                    % (cur, i, fade, nxt))
        cur = nxt

    cmd = [ffmpeg_path(), "-y"] + args + [
        "-filter_complex", ";".join(filt),
        "-map", cur,
        "-c:a", "libmp3lame", "-b:a", "160k",
        str(out_path),
    ]
    _run(cmd)
    return Path(out_path)


# --------------------------------------------------------------------------
# 电影调色
# --------------------------------------------------------------------------
def grade_filter(strength=1.0):
    """返回电影感调色的滤镜串。

    做四件事：
        1. S 形曲线——压暗部、提中间调对比
        2. 色彩平衡——暗部偏冷青、高光偏暖，制造冷暖对比
        3. 暗角——把视线收到人物身上
        4. 轻颗粒 + 锐化——去数码感、提质感
    """
    k = max(0.2, min(1.5, float(strength)))
    # 柔和的 S 曲线
    curves = ("curves=all='0/0 0.22/0.18 0.5/0.52 0.78/0.84 1/1'")
    # 冷暖分离
    cb = ("colorbalance="
          "rs=%.3f:gs=%.3f:bs=%.3f:"
          "rm=%.3f:gm=%.3f:bm=%.3f:"
          "rh=%.3f:gh=%.3f:bh=%.3f"
          % (-0.05 * k, -0.01 * k, 0.07 * k,
             0.01 * k, 0.0, -0.01 * k,
             0.06 * k, 0.02 * k, -0.05 * k))
    # 暗角
    vig = "vignette=angle=PI/4.5:mode=forward"
    # 锐化（只锐化亮度，避免彩边）
    sharp = "unsharp=5:5:0.7:5:5:0.0"
    # 颗粒
    grain = "noise=alls=%d:allf=t+u" % max(1, int(3 * k))
    return ",".join([curves, cb, vig, sharp, grain])


def apply_grade(video_path, out_path=None, strength=1.0):
    """对无声视频做调色。返回新路径。"""
    src = Path(video_path)
    dst = Path(out_path) if out_path else src.with_name(src.stem + "_graded.mp4")
    vf = grade_filter(strength)

    enc = best_encoder()
    cmd = [ffmpeg_path(), "-y", "-i", str(src),
           "-vf", vf + ",format=yuv420p",
           "-c:v", enc, "-pix_fmt", "yuv420p",
           "-preset", "fast", "-crf", "19",
           "-an", str(dst)]
    try:
        _run(cmd)
    except Exception:  # noqa: BLE001
        # 某些滤镜输出格式 NVENC 不认，退回 CPU 编码
        cmd[cmd.index(enc)] = "libx264"
        _run(cmd)
    return dst
