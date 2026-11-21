# -*- coding: utf-8 -*-
"""流水线：文本 → 分镜 → 配音 → 画面 → 合成 → 成片。

每个项目一个目录：
    projects/<项目名>/
        memory.json
        input.txt
        chapter_001/
            script.json        分镜脚本
            audio/shot_01.mp3  分镜配音
            audio/full.mp3     整集音轨
            images/shot_01.png 分镜画面
            chapter_001.mp4    成片
            chapter_001.srt    字幕
"""
import json
import random
import time
import traceback
from pathlib import Path

from . import config, image_gen, llm, notify, shots, subtitles, tts, video
from .memory import Memory


class PipelineError(Exception):
    pass


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
class Project:
    def __init__(self, name):
        self.name = name
        self.dir = Path(config.PROJECTS_DIR) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.memory = Memory(self.dir / "memory.json")

    def chapter_dir(self, chapter):
        d = self.dir / ("chapter_%03d" % int(chapter))
        (d / "audio").mkdir(parents=True, exist_ok=True)
        (d / "images").mkdir(parents=True, exist_ok=True)
        return d

    def save_script(self, chapter, script):
        d = self.chapter_dir(chapter)
        p = d / "script.json"
        p.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    def load_script(self, chapter):
        p = self.chapter_dir(chapter) / "script.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 阶段一：生成脚本
# --------------------------------------------------------------------------
def step_script(project, source_text=None, chapter=None, shots=8):
    if source_text is None:
        f = project.dir / "input.txt"
        if not f.exists():
            raise PipelineError("没有输入文本，请提供 input.txt 或传 source_text")
        source_text = f.read_text(encoding="utf-8")

    script, usage = llm.generate_script(source_text, project.memory,
                                        chapter=chapter, target_shots=shots)

    ch = script.get("chapter")
    project.save_script(ch, script)

    # 同步记忆库
    for shot in script.get("shots", []):
        for role in shot.get("characters") or []:
            project.memory.ensure_character(role)
    for upd in script.get("character_updates", []) or []:
        name = upd.get("name")
        if name:
            project.memory.ensure_character(name, upd.get("appearance") or "")
            project.memory.set_state(name, upd.get("state") or "")

    old = project.memory.data["plot"].get("foreshadowing") or []
    new_fs = [x for x in (script.get("new_foreshadowing") or []) if x]
    project.memory.set_plot(
        chapter=ch,
        progress=(script.get("summary") or "")[:120],
        foreshadowing=old + new_fs,
    )
    if script.get("summary"):
        project.memory.add_summary("第%s章：%s" % (ch, script["summary"]))
    project.memory.save()

    print("[脚本] 第 %s 章，%d 个分镜（tokens: %s）"
          % (ch, len(script["shots"]), usage.get("total_tokens", "-")))
    return script


# --------------------------------------------------------------------------
# 阶段二：配音 + 画面
# --------------------------------------------------------------------------
def _shot_text(shot):
    """一个分镜的完整朗读文本。"""
    parts = []
    if shot.get("narration"):
        parts.append(str(shot["narration"]).strip())
    for d in shot.get("dialogues") or []:
        role = d.get("role") or ""
        text = (d.get("text") or "").strip()
        if text:
            parts.append(("%s：%s" % (role, text)) if role else text)
    return "\n".join(parts)


def step_audio(project, chapter):
    """逐行配音：每句单独合成，按情绪调语速音调。

    好处：
        1. 每句可以用不同的情绪（震惊/愤怒/轻蔑）
        2. 每句都有真实音频时长，子镜头和字幕能精确对齐
    """
    script = project.load_script(chapter)
    if not script:
        raise PipelineError("第 %s 章还没有脚本，先跑 step_script" % chapter)
    d = project.chapter_dir(chapter)

    all_files = []
    cursor = 0.0

    for shot in script["shots"]:
        idx = int(shot.get("index") or 1)
        lines = shots.shot_lines(shot)
        if not lines:
            continue

        roles = shot.get("characters") or []
        default_role = roles[0] if roles else "旁白"

        line_files, line_durs = [], []
        for k, (role, text, emo) in enumerate(lines):
            # 音色：旁白固定用旁白音色，角色用角色音色
            if role and role != "旁白":
                voice = tts.voice_for(role, project.memory, fallback_role="旁白")
            else:
                voice = tts.voice_for("旁白", project.memory, fallback_role="旁白")

            rate, pitch = tts.emotion_params(emo)

            out = d / "audio" / ("s%02d_%02d.mp3" % (idx, k + 1))
            tts.synthesize(text, voice, out, rate=rate, pitch=pitch)

            dur = tts.probe_duration(out)
            if dur <= 0:
                dur = max(1.2, len(text) / 5.5)

            line_files.append(out)
            line_durs.append(dur)
            all_files.append(out)

        # 拼成这一镜的完整音频
        shot_audio = d / "audio" / ("shot_%02d.mp3" % idx)
        tts.concat_audio(line_files, shot_audio)
        total = sum(line_durs)
        shot["_duration"] = total
        shot["_line_durs"] = line_durs

        # 子镜头直接用真实行时长
        subs = shots.build_sub_shots(shot, total, line_durations=line_durs)
        if subs:
            shot["sub_shots"] = subs

        emos = "/".join(e for _, _, e in lines[:3])
        print("[配音] 分镜 %02d  %.1fs  %d 句  情绪=%s" % (idx, total, len(lines), emos))

    # 整集音轨
    full = tts.concat_audio(all_files, d / "audio" / "full.mp3")

    # 字幕按子镜头生成，时间轴用真实时长累加
    sub_entries = []
    cursor = 0.0
    for shot in script["shots"]:
        for sub in shot.get("sub_shots") or []:
            sd = sub.get("duration") or 2.0
            sub_entries.append((cursor, cursor + sd,
                                sub.get("display") or sub.get("text") or ""))
            cursor += sd

    srt = tts.build_srt(sub_entries, d / ("chapter_%03d.srt" % chapter))

    project.save_script(chapter, script)
    print("[配音] 合计 %.1fs，%d 条字幕" % (cursor, len(sub_entries)))
    return full, srt, cursor


def _placeholder_image(text, out_path, size=None):
    """没有 ComfyUI 时的占位图：深色渐变 + 淡淡的水印序号。

    注意：**不渲染剧情文字**——文字由 SRT 字幕负责，
    这里再画一遍会和字幕叠在一起，糊成一团。
    """
    from PIL import Image, ImageDraw, ImageFont

    W, H = size or (config.IMAGE_FINAL_W, config.IMAGE_FINAL_H)
    img = Image.new("RGB", (W, H), (12, 14, 20))
    d = ImageDraw.Draw(img)

    # 上下渐变
    for y in range(H):
        c = int(10 + (y / H) * 28)
        d.line([(0, y), (W, y)], fill=(c, c + 2, c + 8))

    # 中间一条柔光带，让画面不至于死平（也便于质检通过）
    for y in range(int(H * 0.35), int(H * 0.65)):
        t = 1 - abs(y - H * 0.5) / (H * 0.15)
        if t <= 0:
            continue
        a = int(14 * t)
        d.line([(0, y), (W, y)],
               fill=(12 + a, 14 + a, 20 + a * 2))

    # 角落水印（用分镜序号，不显示完整剧情文字）
    font = None
    for fp in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        try:
            font = ImageFont.truetype(fp, int(W / 26))
            break
        except Exception:  # noqa: BLE001
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((W // 2, int(H * 0.16)), "· · ·", font=font,
           fill=(70, 80, 100), anchor="mm")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return Path(out_path)


def step_images(project, chapter, use_comfy=None):
    """生成每个分镜的画面。use_comfy=False 时用占位图。"""
    script = project.load_script(chapter)
    if not script:
        raise PipelineError("没有脚本")
    d = project.chapter_dir(chapter)

    if use_comfy is None:
        use_comfy = image_gen.is_alive()

    if use_comfy:
        print("[画面] ComfyUI 在线，开始抽卡")
    else:
        print("[画面] ComfyUI 未启动，使用占位图（链路测试用）")

    results = []
    for shot in script["shots"]:
        idx = int(shot.get("index") or len(results) + 1)
        prompt = shot.get("image_prompt") or shot.get("narration") or "场景"
        out = d / "images" / ("shot_%02d.png" % idx)

        if use_comfy:
            roles = shot.get("characters") or []
            # 同一角色的 seed 固定，不同角色各自不同
            seed = None
            if roles:
                seed = project.memory.get_seed(roles[0])
                if seed is None:
                    ch, _ = project.memory.ensure_character(roles[0])
                    seed = ch["seed"]
                    project.memory.save()
            try:
                pos, neg = image_gen.build_prompts(shot, project.memory)
                # 切头检测只对「有人物的实拍景别」有意义：
                # 死物（道具）、空镜（纯景色）、特写（局部）本来就不该有头
                st = str(shot.get("shot_type") or "")
                need_head = bool(roles) and st not in ("死物", "空镜", "特写")

                path, used_seed, tries = image_gen.generate_one(
                    pos, negative_prompt=neg, seed=seed, out_path=out,
                    use_ref=True, require_head=need_head)
                image_gen.upscale_hires(path, out)
                print("[画面] 分镜 %02d  seed=%s 重试%d次" % (idx, used_seed, tries))
            except Exception as exc:  # noqa: BLE001
                print("[画面] 分镜 %02d 失败：%r，退回占位图" % (idx, exc))
                _placeholder_image(prompt, out)
        else:
            _placeholder_image(prompt, out)

        results.append(out)

    print("[画面] 完成 %d 张" % len(results))
    return results


# --------------------------------------------------------------------------
# 阶段三：合成
# --------------------------------------------------------------------------
def step_render(project, chapter, bgm=None):
    """按子镜头渲染：一图多镜头 + 运镜 + 淡入淡出 + 字幕 + BGM。"""
    script = project.load_script(chapter)
    if not script:
        raise PipelineError("没有脚本")
    d = project.chapter_dir(chapter)

    clips = []
    total_subs = 0
    for shot in script["shots"]:
        idx = shot.get("index") or 1
        img = d / "images" / ("shot_%02d.png" % idx)
        if not img.exists():
            imgs = sorted((d / "images").glob("shot_*.png"))
            if not imgs:
                raise PipelineError("还没有画面，先跑 step_images")
            img = imgs[min(idx - 1, len(imgs) - 1)]

        subs = shot.get("sub_shots") or []
        if not subs:
            dur = shot.get("_duration") or 3.0
            subs = [{"duration": dur, "crop": [0, 0, 1, 1],
                     "zoom": "in", "pan": "none"}]

        for k, sub in enumerate(subs):
            clip = d / "images" / ("_c%02d_%02d.mp4" % (idx, k))
            video.make_shot_clip(img, sub.get("duration") or 2.0, clip, sub=sub)
            clips.append(clip)
            total_subs += 1

    print("[合成] 共 %d 个子镜头，开始拼接" % total_subs)
    silent = video.concat_clips(clips, d / "silent.mp4", fade=False)
    for c in clips:
        Path(c).unlink(missing_ok=True)

    # 电影调色：放在字幕之前，避免把字幕也调灰
    try:
        graded = video.apply_grade(silent, d / "silent_graded.mp4",
                                   strength=config.GRADE_STRENGTH)
        Path(silent).unlink(missing_ok=True)
        silent = graded
        print("[合成] 已套电影调色（强度 %.1f）" % config.GRADE_STRENGTH)
    except Exception as exc:  # noqa: BLE001
        print("[合成] 调色失败，用原片继续：%s" % exc)

    audio = d / "audio" / "full.mp3"

    # BGM：优先用调用方指定的；否则按剧情强度分段编排
    bgm_path = None
    if bgm:
        bgm_path = Path(bgm)
    else:
        plan = video.build_bgm_plan(script["shots"], d)
        if plan:
            track = video.render_bgm_track(plan, d / "audio" / "bgm_track.mp3")
            if track:
                bgm_path = track
                names = sorted({Path(p).stem for _, p in plan})
                print("[合成] BGM 已分段编排：%d 段，用到 %s"
                      % (len(plan), "/".join(names)))

    out = d / ("chapter_%03d.mp4" % chapter)
    video.mux_audio(silent, audio if audio.exists() else None, out,
                    bgm_path=bgm_path, bgm_volume=0.16)
    Path(silent).unlink(missing_ok=True)

    srt = d / ("chapter_%03d.srt" % chapter)
    if srt.exists():
        final = d / ("chapter_%03d_sub.mp4" % chapter)
        # 用 drawtext 而不是 subtitles 滤镜：
        # 当前 ffmpeg 构建的 libass 加载不到字体，subtitles 会静默失败
        burned = subtitles.burn_with_drawtext(out, srt, final)
        if burned and final.exists() and final.stat().st_size > 1024:
            out.unlink(missing_ok=True)
            final.replace(out)
            print("[合成] 字幕已压入画面（drawtext）")
        else:
            print("[合成] 字幕压制失败，SRT 保留")

    size = out.stat().st_size / 1024 / 1024
    dur = tts.probe_duration(out)
    print("[合成] %s  %.1fMB  %.1f秒  编码=%s"
          % (out.name, size, dur, video.best_encoder()))
    return out


def _pick_bgm():
    """从 assets/bgm 里随机挑一首背景音乐。"""
    d = Path(config.ASSETS_DIR) / "bgm"
    if not d.exists():
        return None
    files = sorted(list(d.glob("*.mp3")) + list(d.glob("*.m4a")) + list(d.glob("*.wav")))
    if not files:
        return None
    return random.choice(files)


# --------------------------------------------------------------------------
# 一键全流程
# --------------------------------------------------------------------------
def run_full(name, source_text=None, chapter=None, shots=8,
             use_comfy=None, notify_review=True, mock=False):
    t0 = time.time()
    project = Project(name)
    chapter = chapter or (project.memory.data["plot"].get("chapter", 0) + 1)

    try:
        if mock:
            from .cli import mock_script
            mock_script(project, chapter)
        else:
            step_script(project, source_text, chapter, shots)
        step_audio(project, chapter)
        step_images(project, chapter, use_comfy=use_comfy)
        mp4 = step_render(project, chapter)
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        err_file = project.chapter_dir(chapter) / "error.log"
        err_file.write_text(tb, encoding="utf-8")
        print("[失败] %r\n详细日志：%s" % (exc, err_file))
        notify.fail_card(name, chapter, str(exc)[:300])
        raise

    cost = time.time() - t0
    print("[完成] %s  用时 %.1fs" % (mp4, cost))
    if notify_review:
        notify.review_card(name, chapter, str(mp4))
    return mp4


def init_project(name, characters=None, topic=None):
    """初始化项目：建目录、写记忆库、放示例输入。"""
    p = Project(name)
    for c in characters or []:
        p.memory.ensure_character(
            c.get("name"), c.get("appearance", ""), c.get("voice", ""))
    p.memory.save()

    if topic:
        f = p.dir / "input.txt"
        if not f.exists():
            f.write_text(topic, encoding="utf-8")

    print("[初始化] %s\n  记忆库：%s\n  输入：%s"
          % (p.dir, p.memory.path, p.dir / "input.txt"))
    return p
