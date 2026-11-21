# -*- coding: utf-8 -*-
"""
把长分镜拆成短镜头（解决「一张图停 8~14 秒像 PPT」的问题）

思路：
    1. 按句号 / 问号 / 感叹号把旁白与台词切成短句
    2. 每句长度控制在 10~20 字（约 2~4 秒朗读）
    3. 每句生成一个「子镜头」，共用同一张底图，但用不同的
       裁切区域 / 缩放方向 / 轻微位移，视觉上像换了镜头

子镜头信息写进 script.json 的 shot["sub_shots"]，渲染时按它切时间。
"""
import re

# 目标：一句 10~22 字，对应 2~4 秒
MIN_LEN = 6
MAX_LEN = 18

# 镜头时长（秒）：整体压到 1~1.5，高潮 0.8
SHOT_MIN = 1.0
SHOT_MAX = 1.5
SHOT_CLIMAX = 0.8

# 这些景别给更短的时长，节奏更快
FAST_SHOT_TYPES = {"特写", "死物", "空镜"}



def shot_duration_cap(shot_type, emotion="", text=""):
    """根据景别和情绪决定这个镜头的时长上限。

    特写/死物/空镜 → 更短，制造节奏
    爆发的情绪（震惊/愤怒/狠）→ 更短
    """
    st = str(shot_type or "")
    if st in FAST_SHOT_TYPES:
        return SHOT_CLIMAX
    if any(k in str(emotion) for k in ("震惊", "愤怒", "狠", "惊")):
        return SHOT_CLIMAX
    if len(text) <= 8:
        return SHOT_MIN
    return SHOT_MAX


def slice_line(text, duration, cap):
    """把一句话切成 n 段，每段时长接近 cap。

    关键思路：先按「这段音频有多长」反推该切几段，
    再按标点把文字均匀分给各段。这样每段时长天然落在 cap 附近，
    不会出现「最后一段特别长」的问题。
    """
    import re as _re
    import math

    text = str(text or "").strip()
    if not text:
        return []

    # 想要的目标时长：普通镜头 1.3s，快切镜头 0.9s
    target = max(0.8, min(cap, 1.3))
    n = max(1, int(round(duration / target)))
    # 段数不能超过有意义的字数
    word_count = len(_re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text))
    n = max(1, min(n, max(1, word_count // 4)))

    if n == 1:
        return [(text, duration)]

    # 按标点切成语义块
    blocks, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "，。！？…；：、,;:" and len(buf) >= 3:
            blocks.append(buf)
            buf = ""
    if buf:
        blocks.append(buf)

    # 块数少于需要的段数，就在最长的块上继续硬切
    while len(blocks) < n:
        idx = max(range(len(blocks)), key=lambda i: len(blocks[i]))
        b = blocks[idx]
        if len(b) < 4:
            break
        half = len(b) // 2
        blocks[idx:idx + 1] = [b[:half], b[half:]]

    # 块数多于 n，就把相邻的短块合并
    while len(blocks) > n and len(blocks) > 1:
        shortest = min(range(len(blocks)), key=lambda i: len(blocks[i]))
        if shortest == 0:
            blocks[0:2] = [blocks[0] + blocks[1]]
        else:
            blocks[shortest - 1:shortest + 1] = [blocks[shortest - 1] + blocks[shortest]]

    # 丢弃纯标点
    blocks = [b for b in blocks
              if len(_re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", b)) >= 2]
    if not blocks:
        return []

    # 时长按字数比例分，但不超过 cap
    total_chars = sum(len(b) for b in blocks) or 1
    out = []
    for b in blocks:
        share = duration * (len(b) / total_chars)
        out.append((b, min(share, cap)))
    return out


def split_sentences(text):
    """按中文标点切句，保留标点在句尾。"""
    text = " ".join(str(text or "").split())
    if not text:
        return []

    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？…；!?;":
            s = buf.strip()
            if s:
                parts.append(s)
            buf = ""
    if buf.strip():
        parts.append(buf.strip())

    # 太长的句子再按逗号切
    out = []
    for s in parts:
        if len(s) <= MAX_LEN:
            out.append(s)
            continue
        b = ""
        for ch in s:
            b += ch
            if ch in "，、,：" and len(b) >= MIN_LEN:
                out.append(b.strip())
                b = ""
        if b.strip():
            out.append(b.strip())

    # 过滤纯标点片段（如「林越：…」里的「…」），
    # 这类文本 edge-tts 会返回 NoAudioReceived
    import re as _re
    cleaned = []
    for x in out:
        x = x.strip()
        if not x:
            continue
        has_word = len(_re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", x))
        if has_word < 2:
            # 并入上一句，保留停顿感
            if cleaned:
                cleaned[-1] = cleaned[-1] + x
            continue
        cleaned.append(x)
    return cleaned


def _estimate_duration(text):
    """按中文语速估时：约 5.5 字/秒。"""
    n = len(re.sub(r"\s", "", text))
    return round(max(1.6, n / 5.5), 2)


def shot_lines(shot):
    """把分镜拆成逐行台词，带角色与情绪。

    返回 [(role, text, emotion), ...]
    情绪从 dialogues 的 emotion 字段取，旁白默认「平静」。
    """
    lines = []

    narration = (shot.get("narration") or "").strip()
    if narration:
        for s in split_sentences(narration):
            lines.append(("旁白", s, "平静"))

    for dl in shot.get("dialogues") or []:
        role = dl.get("role") or ""
        txt = (dl.get("text") or "").strip()
        emo = dl.get("emotion") or "平静"
        if not txt:
            continue
        for s in split_sentences(txt):
            lines.append((role, s, emo))

    return lines


def build_sub_shots(shot, total_duration, line_durations=None):
    """把一个分镜拆成若干子镜头（每个 1~1.5 秒）。

    返回 [{"text", "display", "duration", "shot_type", "crop", "zoom", "pan"}, ...]
    """
    lines = shot_lines(shot)
    if not lines:
        return []

    # 真实逐行时长优先；否则按字数估算
    if line_durations and len(line_durations) == len(lines):
        real = list(line_durations)
    else:
        est = sum(_estimate_duration("%s%s" % (r, t)) for r, t, _ in lines) or 1.0
        scale = total_duration / est
        real = [_estimate_duration("%s%s" % (r, t)) * scale for r, t, _ in lines]

    shot_type = shot.get("shot_type") or ""

    # 只用缩放+平移制造变化，不做裁切（避免切头）
    moves = [
        {"zoom": "in",  "pan": "none"},
        {"zoom": "in",  "pan": "left"},
        {"zoom": "out", "pan": "none"},
        {"zoom": "in",  "pan": "right"},
        {"zoom": "in",  "pan": "none"},
    ]

    subs = []
    for i, (role, text, emo) in enumerate(lines):
        cap = shot_duration_cap(shot_type, emo, text)
        dur = real[i] if i < len(real) else _estimate_duration(text)

        # 放不进一个镜头就切段——这是「镜头数变多」的关键
        for frag, frag_dur in slice_line(text, dur, cap):
            mv = moves[len(subs) % len(moves)]
            display = ("%s：%s" % (role, frag)) if role and role != "旁白" else frag
            subs.append({
                "index": len(subs) + 1,
                "role": role,
                "text": frag,
                "display": display,
                "duration": round(frag_dur, 3),
                "shot_type": shot_type,
                "crop": [0.0, 0.0, 1.0, 1.0],
                "zoom": mv["zoom"],
                "pan": mv["pan"],
            })

    # 误差均摊到每个镜头，避免最后一个变成超长镜头
    if subs:
        diff = total_duration - sum(x["duration"] for x in subs)
        if abs(diff) > 0.01:
            share = diff / len(subs)
            for x in subs:
                x["duration"] = round(max(0.6, x["duration"] + share), 3)
    return subs


def apply_to_script(script):
    """给整份脚本补上 sub_shots（幂等）。"""
    changed = 0
    for shot in script.get("shots") or []:
        dur = shot.get("_duration") or 0
        if not dur:
            continue
        subs = build_sub_shots(shot, dur)
        if subs:
            shot["sub_shots"] = subs
            changed += 1
    return changed
