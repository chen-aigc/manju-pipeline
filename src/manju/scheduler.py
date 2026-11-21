# -*- coding: utf-8 -*-
"""定时批处理：凌晨低价时段自动跑。

两种用法：
    1. 单次执行（推荐配合 Windows 任务计划程序 / Linux cron）
       python -m manju.scheduler

    2. 常驻，到点自动跑
       python -m manju.scheduler --daemon

配置在 .env：
    BATCH_HOUR=0
    BATCH_MINUTE=30
    BATCH_PROJECTS=项目A,项目B
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

from . import config, notify, pipeline


def run_batch(projects=None, mock=False):
    """处理所有 project 目录里带 pending 标记的章节。"""
    root = Path(config.PROJECTS_DIR)
    if not root.exists():
        print("没有 projects 目录")
        return

    names = projects or [p.name for p in root.iterdir() if p.is_dir()]
    print("[批处理] 共 %d 个项目：%s" % (len(names), ", ".join(names)))

    for name in names:
        proj = pipeline.Project(name)
        pending = proj.dir / "pending.txt"
        if not pending.exists():
            continue

        topics = [x.strip() for x in pending.read_text(encoding="utf-8").splitlines()
                  if x.strip()]
        if not topics:
            pending.unlink(missing_ok=True)
            continue

        print("[批处理] %s 有 %d 集待生成" % (name, len(topics)))
        for topic in topics:
            ch = proj.memory.data["plot"].get("chapter", 0) + 1
            try:
                mp4 = pipeline.run_full(name, topic, ch, mock=mock)
                print("[批处理] 完成：%s" % mp4)
            except Exception as exc:  # noqa: BLE001
                print("[批处理] 失败：%r" % (exc,))
                notify.fail_card(name, ch, str(exc)[:300])

        pending.unlink(missing_ok=True)

    notify.send("批处理完成", "所有待生成章节已处理完毕", "success")


def seconds_until_hms(hour, minute):
    now = time.localtime()
    target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                          hour, minute, 0, 0, 0, -1))
    if target <= time.time():
        target += 86400
    return target - time.time()


def daemon(projects=None, mock=False):
    print("[守护] 每天 %02d:%02d 执行一次" % (config.BATCH_HOUR, config.BATCH_MINUTE))
    while True:
        wait = seconds_until_hms(config.BATCH_HOUR, config.BATCH_MINUTE)
        print("[守护] 下次执行在 %.1f 分钟后" % (wait / 60))
        time.sleep(max(10, wait))
        try:
            run_batch(projects, mock=mock)
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            print(tb)
            notify.send("批处理异常", tb[-500:], "error")


def main(argv=None):
    ap = argparse.ArgumentParser(description="定时批处理")
    ap.add_argument("--daemon", action="store_true", help="常驻模式")
    ap.add_argument("--projects", default="", help="逗号分隔的项目名")
    ap.add_argument("--mock", action="store_true", help="用内置示例脚本")
    args = ap.parse_args(argv)

    projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()] or None

    if args.daemon:
        daemon(projects, mock=args.mock)
    else:
        if not projects:
            projects = [p.strip() for p in
                        config.env("BATCH_PROJECTS", "").split(",") if p.strip()] or None
        run_batch(projects, mock=args.mock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
