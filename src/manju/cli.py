# -*- coding: utf-8 -*-
"""命令行入口。

用法（在 manju 目录下）：
    python -m manju.cli init 测试项目 --topic "主角穿越成废柴，觉醒签到系统"
    python -m manju.cli script 测试项目 --mock
    python -m manju.cli audio 测试项目
    python -m manju.cli images 测试项目 --no-comfy
    python -m manju.cli render 测试项目
    python -m manju.cli full 测试项目 --mock --no-comfy
"""
import argparse
import json
import sys
from pathlib import Path

from . import config, image_gen, pipeline, queue_tasks, tts, video


def _sync_path():
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


# --------------------------------------------------------------------------
# 无 API 时的本地假脚本（用于打通链路）
# --------------------------------------------------------------------------
def mock_script(project, chapter):
    script = {
        "chapter": chapter,
        "title": "第%d章 系统觉醒" % chapter,
        "shots": [
            {
                "index": 1,
                "narration": "深夜，城郊仓库的灯还亮着。林越蹲在角落，手里攥着一张退婚书。",
                "dialogues": [
                    {"role": "林越", "text": "三年了。我真的一无所有了。", "emotion": "低落"}
                ],
                "image_prompt": "夜晚仓库，昏暗灯光，少年蹲在角落低头，背影孤独，电影感构图",
                "characters": ["林越"],
                "duration_hint": 5,
            },
            {
                "index": 2,
                "narration": "就在这时，他的眼前突然弹出一行蓝色光字。",
                "dialogues": [
                    {"role": "系统", "text": "叮！签到系统已绑定。", "emotion": "冰冷"},
                ],
                "image_prompt": "半空悬浮蓝色全息文字，科技感光效，少年抬头惊讶，夜景",
                "characters": ["林越"],
                "duration_hint": 4,
            },
            {
                "index": 3,
                "narration": "签到第一天，一把锈剑落入掌心。",
                "dialogues": [
                    {"role": "林越", "text": "这……断的？", "emotion": "困惑"},
                ],
                "image_prompt": "少年手中握着一把锈迹斑斑的断剑，月光照亮，特写",
                "characters": ["林越"],
                "duration_hint": 4,
            },
            {
                "index": 4,
                "narration": "白发老者从阴影里走出，眼中闪过异色。",
                "dialogues": [
                    {"role": "老者", "text": "小子，你手上那把剑……哪来的？", "emotion": "警惕"},
                ],
                "image_prompt": "白发老者穿黑袍缓步走出阴影，眼神锐利，仓库背景",
                "characters": ["老者"],
                "duration_hint": 5,
            },
            {
                "index": 5,
                "narration": "林越握紧断剑，第一次感觉到，命运的车轮开始转动。",
                "dialogues": [],
                "image_prompt": "少年紧握断剑站直身体，眼神坚定，逆光剪影，电影感",
                "characters": ["林越"],
                "duration_hint": 4,
            },
        ],
        "summary": "林越在仓库中拿到退婚书，绝望之际绑定签到系统，第一天签到得到一把锈断剑。"
                   "神秘白发老者出现并追问断剑来历，林越第一次感到命运改变。",
        "new_foreshadowing": ["断剑的来历", "白发老者认识这把剑"],
        "character_updates": [
            {"name": "林越", "state": "觉醒签到系统，手握断剑，决心逆袭"},
            {"name": "老者", "state": "对断剑产生兴趣，来意不明"},
        ],
    }
    project.save_script(chapter, script)
    for c in project.memory.data["characters"]:
        pass
    for c in ("林越", "老者"):
        project.memory.ensure_character(c)
    project.memory.set_plot(chapter=chapter, progress=script["summary"][:120],
                            foreshadowing=script["new_foreshadowing"])
    project.memory.add_summary("第%s章：%s" % (chapter, script["summary"]))
    project.memory.save()
    print("[脚本] [MOCK] 第 %d 章，%d 个分镜" % (chapter, len(script["shots"])))
    return script


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def cmd_init(args):
    chars = []
    if args.chars:
        for c in args.chars:
            name, _, appear = c.partition("=")
            chars.append({"name": name.strip(), "appearance": appear.strip()})
    pipeline.init_project(args.name, chars, args.topic)
    return 0


def cmd_script(args):
    p = pipeline.Project(args.name)
    ch = args.chapter or (p.memory.data["plot"].get("chapter", 0) + 1)
    if args.mock:
        mock_script(p, ch)
    else:
        pipeline.step_script(p, chapter=ch, shots=args.shots)
    return 0


def cmd_audio(args):
    p = pipeline.Project(args.name)
    ch = args.chapter or p.memory.data["plot"].get("chapter", 1)
    pipeline.step_audio(p, ch)
    return 0


def cmd_images(args):
    p = pipeline.Project(args.name)
    ch = args.chapter or p.memory.data["plot"].get("chapter", 1)
    pipeline.step_images(p, ch, use_comfy=(not args.no_comfy))
    return 0


def cmd_render(args):
    p = pipeline.Project(args.name)
    ch = args.chapter or p.memory.data["plot"].get("chapter", 1)
    pipeline.step_render(p, ch)
    return 0


def cmd_full(args):
    use_comfy = None if args.auto_comfy else (not args.no_comfy)
    src = args.text
    mp4 = pipeline.run_full(args.name, src, args.chapter, args.shots,
                            use_comfy=use_comfy, notify_review=not args.no_notify,
                            mock=args.mock)
    print("成片：%s" % mp4)
    return 0


def cmd_memory(args):
    p = pipeline.Project(args.name)
    print(json.dumps(p.memory.data, ensure_ascii=False, indent=2))
    return 0


def cmd_env(args):
    print("Python :", sys.version.split()[0])
    print("ffmpeg :", tts.ffmpeg_path())
    print("编码器 :", video.best_encoder())
    print("ComfyUI:", "在线" if image_gen.is_alive() else "离线 (%s)" % config.COMFY_HOST)
    print("LLM    :", config.LLM_MODEL, "@", config.LLM_BASE_URL,
          "key=" + ("已配置" if config.LLM_API_KEY else "未配置"))
    print("飞书   :", "已配置" if config.FEISHU_WEBHOOK else "未配置")
    return 0


def cmd_tasks(args):
    for t in queue_tasks.list_tasks(args.project):
        print("%s  %-10s %-8s %3s%%  %s" % (
            t.get("id"), t.get("project"), t.get("status"),
            t.get("progress", 0), t.get("stage", "")))
    return 0


def cmd_setup(args):
    from .setup_check import run_setup
    return run_setup(fix=bool(getattr(args, "fix", False)))


def cmd_probe(args):
    """检查某个云端服务配好了没（不创建任务、不扣费）。"""
    from .plugins import registry
    registry.load_all(force=True)

    kind = args.kind
    names = [args.name] if args.name else registry.list_providers(kind)
    if not names:
        print("没有这个环节：%s" % kind)
        return 1

    print()
    print("  检查 %s 环节" % kind)
    print("  " + "-" * 46)
    rc = 0
    for name in names:
        try:
            p = registry.make(kind, name)
        except Exception as exc:  # noqa: BLE001
            print("  ✗ %-14s 构造失败：%s" % (name, exc))
            continue

        avail = False
        try:
            avail = bool(p.available())
        except Exception:  # noqa: BLE001
            avail = False

        if not avail:
            print("  ○ %-14s 未配置（跳过）" % name)
            continue

        check = getattr(p, "check", None)
        if callable(check):
            try:
                ok, msg = check()
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)[:160]
            print("  %s %-14s %s" % ("✓" if ok else "✗", name, msg))
            if not ok:
                rc = 1
        else:
            print("  ✓ %-14s 已配置（无自检方法）" % name)

    print("  " + "-" * 46)
    print("  说明：只查询、不创建任务，不会扣费")
    print()
    return rc


def cmd_web(args):
    """启动 Web 界面。"""
    try:
        from .web import server
    except Exception as exc:  # noqa: BLE001
        print("[错误] 无法加载 Web 模块：%s" % exc)
        print("请先安装：pip install fastapi uvicorn openpyxl")
        return 1

    host = args.host
    port = args.port
    print("=" * 46)
    print("  漫剧工厂 Web")
    print("  地址：http://%s:%d" % (host, port))
    print("  关闭窗口即停止服务")
    print("=" * 46)

    if not args.no_open:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(
            "http://%s:%d" % (host, port))).start()

    server.serve(host=host, port=port)
    return 0


def cmd_run(args):
    """用新编排层跑一集（插件化流程）。"""
    from .core import Orchestrator

    orc = Orchestrator(args.name, args.chapter,
                       source_text=args.text or "",
                       target_shots=args.shots)
    if args.stages:
        stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    else:
        stages = None
    res = orc.run(stages=stages)
    print()
    print("成片:", res.video)
    print("报表:", res.report)
    print("待处理:", len(res.todos), "项")
    return 0 if res.ok else 1


def cmd_story(args):
    """查看/管理剧情状态（服装、伤痕的跨镜传递）。"""
    from .core.story_hooks import StoryHooks
    from .core.story_state import StoryState

    chapter_dir = (Path(config.PROJECTS_DIR) / args.name
                   / ("chapter_%03d" % args.chapter))
    sp = chapter_dir / "story_state.json"

    if args.reset:
        if sp.exists():
            sp.unlink()
            print("[OK] 已清除剧情状态：%s" % sp)
        else:
            print("没有状态文件")
        return 0

    st = StoryState(sp, chapter_dir / "state_rules.json")

    if args.shot:
        rec = st.get_shot(args.shot)
        if not rec:
            print("没有 %s 的记录" % args.shot)
            return 1
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0

    if args.inherit:
        # 演示：这一镜会自动继承什么
        chars = [c.strip() for c in (args.inherit or "").split(",") if c.strip()]
        addon, warns = st.build_prompt("__next__", chars)
        print("【会自动拼进提示词的内容】")
        print("  " + (addon if addon else "（无，可能是第一镜）"))
        if warns:
            print()
            print("【警告】")
            for w in warns:
                print("  " + w)
        return 0

    # 默认：打印全部状态
    s = st.summary()
    if not s:
        print("还没有剧情状态记录。跑一次出图后会自动生成。")
        print("状态文件位置：%s" % sp)
        return 0

    print("=" * 62)
    print("剧情状态（%s）" % chapter_dir.name)
    print("=" * 62)
    print(s)

    # 顺带显示一致性报告
    rp = chapter_dir / "consistency_report.json"
    if rp.exists() and args.report:
        print()
        print("=" * 62)
        print("最近一次一致性检查（%s）" % rp.name)
        print("=" * 62)
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
            st_ = data.get("stats") or {}
            print("  OK:%d  WARN:%d  FAIL:%d" % (
                st_.get("OK", 0), st_.get("WARN", 0), st_.get("FAIL", 0)))
            for sh in data.get("shots") or []:
                if sh.get("level") in ("WARN", "FAIL"):
                    print("  %-12s %s  %s" % (
                        sh.get("shot_id"), sh.get("level"),
                        "；".join(sh.get("notes") or [])[:80]))
        except Exception as exc:  # noqa: BLE001
            print("  报告读取失败：%r" % exc)
    return 0


def cmd_audit(args):
    """整集一致性审核：批内找异类（不需要基准图）。"""
    from .core.consistency import ConsistencyChecker

    chapter_dir = (Path(config.PROJECTS_DIR) / args.name
                   / ("chapter_%03d" % args.chapter))
    img_dir = chapter_dir / "images"
    if not img_dir.exists():
        print("没有图片目录：%s" % img_dir)
        return 1

    shots = []
    for p in sorted(img_dir.glob("shot_*.png")):
        shots.append({"id": p.stem, "path": str(p)})
    for p in sorted(img_dir.glob("shot_*.jpg")):
        shots.append({"id": p.stem, "path": str(p)})

    if not shots:
        print("没有找到 shot_*.png / shot_*.jpg")
        return 1

    print("扫描 %d 张图..." % len(shots))
    ck = ConsistencyChecker(None)
    report = ck.audit_batch(shots, cutoff=args.cutoff, mode=args.mode)
    print()
    print(report.summary())

    # 写报告
    rp = chapter_dir / "consistency_report.json"
    rp.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print()
    print("报告已写入：%s" % rp)
    return 0


def cmd_plugins(args):
    """插件接口规范校验。"""
    from .plugins import registry
    from .plugins import interface

    registry.load_all(force=True)

    if args.kind:
        # 只查某一环节
        names = registry.list_providers(args.kind)
        if not names:
            print("没有 %s 环节的插件" % args.kind)
            return 1
        bad = 0
        for name in names:
            try:
                p = registry.pick(args.kind, name)
                errs = interface.check_provider(type(p), args.kind)
            except Exception as exc:  # noqa: BLE001
                errs = ["加载失败：%r" % exc]
            if errs:
                print("  ❌ %-20s" % ("%s/%s" % (args.kind, name)))
                for e in errs:
                    print("       - %s" % e)
                bad += 1
            else:
                print("  ✅ %-20s" % ("%s/%s" % (args.kind, name)))
        return 0 if bad == 0 else 1

    print(interface.report())
    return 0


def cmd_locks(args):
    """查看/清理任务锁。"""
    from .core import lock as lockmod

    if args.unlock:
        ok, msg = lockmod.unlock(
            Path(config.PROJECTS_DIR) / args.unlock, force=args.force)
        print(("[OK] " if ok else "[跳过] ") + msg)
        return 0 if ok else 1

    items = lockmod.list_locks(config.PROJECTS_DIR)
    if not items:
        print("没有锁")
        return 0
    for name, info, stale in items:
        print("%-16s pid=%-7s %s  心跳=%s" % (
            name, info.get("pid", "?"),
            "【已超时】" if stale else "【占用中】",
            info.get("heartbeat", "?")))
    return 0


def main(argv=None):
    config.ensure_dirs()
    ap = argparse.ArgumentParser(prog="manju", description="漫剧 AI 工厂")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="初始化项目")
    s.add_argument("name")
    s.add_argument("--topic", default="")
    s.add_argument("--chars", nargs="*", help="角色，格式 名字=外貌描述")
    s.set_defaults(func=cmd_init)

    for name, fn, help_ in (
        ("script", cmd_script, "生成分镜脚本"),
        ("audio", cmd_audio, "配音 + 字幕"),
        ("images", cmd_images, "生成画面"),
        ("render", cmd_render, "合成成片"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("name")
        s.add_argument("--chapter", type=int, default=0)
        if name == "script":
            s.add_argument("--shots", type=int, default=8)
            s.add_argument("--mock", action="store_true", help="不调 API，用内置假脚本")
        if name == "images":
            s.add_argument("--no-comfy", action="store_true", help="用占位图，不调 ComfyUI")
        s.set_defaults(func=fn)

    s = sub.add_parser("setup", help="一键体检（--fix 自动修复）")
    s.add_argument("--fix", action="store_true", help="能自动修的自动修")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("probe", help="检查云端服务 key 是否可用（不扣费）")
    s.add_argument("kind", nargs="?", default="video",
                   help="环节：video/image/tts/script，默认 video")
    s.add_argument("--name", default="", help="只查某一个实现，如 kling")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("web", help="启动 Web 界面")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8501)
    s.add_argument("--no-open", action="store_true", help="不自动开浏览器")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("run", help="用插件化编排层跑一集")
    s.add_argument("name")
    s.add_argument("--chapter", type=int, default=1)
    s.add_argument("--text", default="")
    s.add_argument("--shots", type=int, default=40)
    s.add_argument("--stages", default="", help="逗号分隔，如 script,image,audio,edit")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("story", help="查看剧情状态（服装/伤痕跨镜传递）")
    s.add_argument("name")
    s.add_argument("--chapter", type=int, default=1)
    s.add_argument("--shot", default="", help="只看某一镜，如 shot_003")
    s.add_argument("--inherit", default="",
                   help="模拟下一镜会继承什么，如 林越,月白")
    s.add_argument("--report", action="store_true", help="顺带显示一致性报告")
    s.add_argument("--reset", action="store_true", help="清除剧情状态")
    s.set_defaults(func=cmd_story)

    s = sub.add_parser("audit", help="整集一致性审核（批内找异类）")
    s.add_argument("name")
    s.add_argument("--chapter", type=int, default=1)
    s.add_argument("--mode", default="relative", choices=("relative", "absolute"),
                   help="relative=找最离群的（默认）；absolute=按绝对阈值")
    s.add_argument("--cutoff", type=float, default=None,
                   help="relative 模式=离群比例(默认0.15)；absolute 模式=相似度阈值(默认0.55)")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("plugins", help="插件接口规范校验")
    s.add_argument("--kind", default="", help="只查某一环节：image/video/tts/...")
    s.set_defaults(func=cmd_plugins)

    s = sub.add_parser("locks", help="查看/清理任务锁")
    s.add_argument("--unlock", default="", help="项目名")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_locks)

    s = sub.add_parser("full", help="一键全流程")
    s.add_argument("name")
    s.add_argument("--text", default=None)
    s.add_argument("--chapter", type=int, default=0)
    s.add_argument("--shots", type=int, default=8)
    s.add_argument("--mock", action="store_true")
    s.add_argument("--no-comfy", action="store_true")
    s.add_argument("--auto-comfy", action="store_true", default=True)
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(func=cmd_full)

    s = sub.add_parser("memory", help="打印记忆库")
    s.add_argument("name")
    s.set_defaults(func=cmd_memory)

    s = sub.add_parser("env", help="检查环境")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("tasks", help="查看任务队列")
    s.add_argument("--project", default=None)
    s.set_defaults(func=cmd_tasks)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
