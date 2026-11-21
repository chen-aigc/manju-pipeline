# -*- coding: utf-8 -*-
"""一键体检 / 修复。

用法：
    manju setup          只检查，报告问题
    manju setup --fix    能自动修的就修（装依赖、建目录、写配置）
"""
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

OK = "✅"
WARN = "⚠️ "
BAD = "❌"


class Checker:
    def __init__(self, fix=False):
        self.fix = fix
        self.problems = []
        self.fixed = []
        self.count = 0

    def head(self, title):
        print("\n" + "=" * 52)
        print("  " + title)
        print("=" * 52)

    def item(self, ok, label, detail="", fix_hint="", fix_fn=None):
        self.count += 1
        mark = OK if ok else (WARN if fix_hint else BAD)
        print("%s %-22s %s" % (mark, label, detail))

        if ok:
            return True

        if fix_fn is not None and self.fix:
            print("     → 正在自动修复…")
            try:
                fix_fn()
                self.fixed.append(label)
                print("     → 完成")
                return True
            except Exception as exc:  # noqa: BLE001
                print("     → 修复失败：%s" % exc)
                self.problems.append((label, str(exc)))
                return False

        if fix_hint:
            print("     → %s" % fix_hint)
        self.problems.append((label, fix_hint or detail))
        return False


def project_root():
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "src" / "manju").is_dir():
            return p
    return here.parents[2]


def port_free(host, port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) != 0


def run_setup(fix=False):
    root = project_root()
    c = Checker(fix=fix)

    print()
    print("  漫剧工厂 · 一键体检")
    print("  目录：%s" % root)
    if not fix:
        print("  （只检查不改动；要自动修复，加 --fix）")

    # ---------------- 1. Python ----------------
    c.head("1 / 6  Python 环境")
    ver = sys.version_info
    c.item(ver >= (3, 10), "Python 版本",
           "%d.%d.%d" % (ver[0], ver[1], ver[2]),
           "需要 3.10 以上，去 python.org 装 3.12")

    c.item(sys.prefix != sys.base_prefix or True, "运行位置", sys.executable)

    # ---------------- 2. 依赖 ----------------
    c.head("2 / 6  依赖包")
    deps = [
        ("httpx", "网络请求"),
        ("fastapi", "Web 服务"),
        ("uvicorn", "Web 服务"),
        ("openpyxl", "Excel 报表"),
        ("PIL", "图片处理(pillow)"),
        ("numpy", "数值计算"),
    ]
    missing = []
    for mod, desc in deps:
        try:
            __import__(mod)
            c.item(True, mod, desc)
        except ImportError:
            missing.append(mod)
            c.item(False, mod, "未安装 —— " + desc)

    if missing:
        def _install():
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q"] + missing,
                stdout=subprocess.DEVNULL)
        c.item(False, "安装缺失依赖",
               "缺 %d 个" % len(missing),
               "运行： pip install " + " ".join(missing),
               fix_fn=_install)

    # ---------------- 3. 目录 ----------------
    c.head("3 / 6  目录结构")
    for name in ("projects", "assets", "logs"):
        p = root / name
        c.item(p.exists(), name + "/", str(p) if p.exists() else "不存在",
               "会自动创建",
               fix_fn=lambda p=p: p.mkdir(parents=True, exist_ok=True))

    env_file = root / ".env"
    c.item(env_file.exists(), ".env 配置",
           "已存在" if env_file.exists() else "不存在",
           "会生成一份默认配置")

    if not env_file.exists():
        def _mk_env():
            env_file.write_text(
                "LLM_MAX_TOKENS=8000\n"
                "SCRIPT_PROVIDER=deepseek\n"
                "IMAGE_PROVIDER=comfyui\n"
                "VIDEO_PROVIDER=null\n"
                "TTS_PROVIDER=edge\n"
                "BGM_PROVIDER=synth\n"
                "EDIT_PROVIDER=rhythm\n"
                "LOG_PROVIDER=excel\n",
                encoding="utf-8")
        c.item(False, "生成 .env", "", "会写默认配置", fix_fn=_mk_env)

    # ---------------- 4. 外部程序 ----------------
    c.head("4 / 6  外部程序")

    # ffmpeg（imageio-ffmpeg 自带即可）
    ff = ""
    try:
        from manju.tts import ffmpeg_path
        ff = ffmpeg_path()
        c.item(bool(ff) and Path(ff).exists(), "ffmpeg", ff)
    except Exception as exc:  # noqa: BLE001
        c.item(False, "ffmpeg", str(exc)[:40],
               "pip install imageio-ffmpeg")

    # ComfyUI
    comfy_ok = False
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8188/system_stats",
                                    timeout=2) as r:
            comfy_ok = r.status == 200
    except Exception:  # noqa: BLE001
        comfy_ok = False
    c.item(comfy_ok, "ComfyUI (8188)",
           "在线" if comfy_ok else "未启动",
           "双击 D:\\ComfyUI\\启动ComfyUI.bat")

    # 端口
    c.item(port_free("127.0.0.1", 8501), "端口 8501",
           "空闲" if port_free("127.0.0.1", 8501) else "被占用",
           "换端口： manju web --port 8502")

    # ---------------- 5. 密钥 ----------------
    c.head("5 / 6  AI 密钥")
    from manju import config

    keys = [
        ("LLM_API_KEY", config.LLM_API_KEY, "写剧本（主要，DeepSeek）"),
        ("DASHSCOPE_API_KEY", config.DASHSCOPE_API_KEY, "通义千问（备选）"),
        ("MOONSHOT_API_KEY", config.MOONSHOT_API_KEY, "Kimi（备选）"),
        ("KLING_ACCESS_KEY", config.KLING_ACCESS_KEY, "可灵图生视频"),
    ]
    for name, val, desc in keys:
        got = bool(val)
        if got or name in ("LLM_API_KEY", "KLING_ACCESS_KEY"):
            c.item(got, name, (desc + " · 已配置") if got else (desc + " · 未配置"),
                   "在 .env 里填；不填就只能用 mock 测试模式")

    # ---------------- 6. 磁盘 ----------------
    c.head("6 / 6  磁盘空间")
    try:
        total, used, free = shutil.disk_usage(root)
        free_gb = free / (1024 ** 3)
        c.item(free_gb > 5, "剩余空间",
               "%.1f GB 可用（共 %.0f GB）" % (free_gb, total / (1024 ** 3)),
               "低于 5GB 建议清理 projects/*/history")
    except OSError as exc:
        c.item(False, "剩余空间", str(exc)[:40])

    # 项目占用
    projects_dir = root / "projects"
    if projects_dir.exists():
        size = sum(f.stat().st_size for f in projects_dir.rglob("*")
                   if f.is_file())
        n_proj = len([d for d in projects_dir.iterdir()
                      if d.is_dir() and not d.name.startswith("_")])
        print("   %-22s %d 个项目，共 %.0f MB"
              % ("项目占用", n_proj, size / 1024 / 1024))

    # ---------------- 总结 ----------------
    print("\n" + "=" * 52)
    if not c.problems:
        print("  %s 全部正常，可以开始用了" % OK)
        print("\n  下一步：")
        print("     manju web         启动网页界面")
        print("     manju run 剧名 --chapter 1   命令行跑一集")
    else:
        print("  发现 %d 个问题：" % len(c.problems))
        for label, hint in c.problems:
            print("    · %s：%s" % (label, hint))
        if c.fixed:
            print("\n  已自动修复 %d 项：%s" % (len(c.fixed), "、".join(c.fixed)))
        if not fix:
            print("\n  加 --fix 可以自动修复大部分问题：")
            print("     manju setup --fix")
    print("=" * 52)
    print()

    return 0 if not c.problems or c.fixed else 1
