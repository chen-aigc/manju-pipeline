# -*- coding: utf-8 -*-
"""人工素材自动整理：把 manual/_inbox/ 里的文件自动分类归档。

用法：
    python 整理素材.py <项目名> [集数]        整理
    python 整理素材.py <项目名> [集数] --dry  只看会怎么分，不动文件

识别规则：
    1. 按后缀分三类：图 / 视频 / 音频
    2. 文件名里找镜号（支持多种写法）
    3. 音频额外找句号
    4. 认不出的放 _待确认/
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

BASE = Path(r'D:\deepseek文件区\manju')
sys.path.insert(0, str(BASE / 'src'))

from manju import config  # noqa: E402

# 后缀 → 目标目录
EXT_MAP = {
    '.png': 'images', '.jpg': 'images', '.jpeg': 'images',
    '.webp': 'images', '.bmp': 'images',
    '.mp4': 'videos_ok', '.mov': 'videos_ok', '.webm': 'videos_ok',
    '.mkv': 'videos_ok',
    '.mp3': 'audio', '.wav': 'audio', '.m4a': 'audio',
    '.aac': 'audio', '.flac': 'audio', '.ogg': 'audio',
}


def parse_shot(name: str) -> int | None:
    """从文件名里提取镜号。

    支持：
        shot_01 / shot01 / shot 1
        01 / 1 / 001
        第1镜 / 镜1 / 第01镜
        _01_ / -1-
    """
    stem = Path(name).stem.lower()

    # 音频命名 s01_02：第一个数字是镜号，第二个是句号
    m = re.match(r'^s\s*0*(\d{1,3})[_\-\s]0*(\d{1,2})', stem)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 999:
            return v

    # shot_01 / shot01
    m = re.search(r'shot[_\-\s]*0*(\d{1,3})', stem)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 999:
            return v

    # 第1镜 / 镜1（数字可在「镜」前后）
    m = re.search(r'第?\s*0*(\d{1,3})\s*镜', stem)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 999:
            return v
    m = re.search(r'镜\s*0*(\d{1,3})', stem)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 999:
            return v

    # 纯数字（整个 stem 就是数字）
    if re.fullmatch(r'0*\d{1,3}', stem):
        v = int(stem)
        if 1 <= v <= 999:
            return v

    # 前后有分隔符的数字：_01_ / -1- / 开头数字
    m = re.match(r'^0*(\d{1,3})[_\-\.]', stem)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 999:
            return v

    # 结尾数字：xxx_01
    m = re.search(r'[_\-\.]0*(\d{1,3})$', stem)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 999:
            return v

    return None


def parse_line(name: str) -> int | None:
    """从音频文件名里提取句号。

    支持：
        s01_02   → 句号 2
        s01-02
    """
    stem = Path(name).stem.lower()

    # s01_02 / s01-02
    m = re.match(r'^s\s*0*(\d{1,3})[_\-\s]0*(\d{1,2})', stem)
    if m:
        return int(m.group(2)) or 1

    # 纯 xxx_02
    m = re.search(r'[_\-\s]0*(\d{1,2})$', stem)
    if m:
        return int(m.group(1)) or 1

    return None


def classify(path: Path) -> dict:
    """判断一个文件该怎么归。

    返回：{kind, target_dir, new_name, shot, line, reason}
    """
    ext = path.suffix.lower()
    kind = EXT_MAP.get(ext)

    if not kind:
        return {"ok": False, "reason": "不支持的后缀 %s" % ext}

    shot = parse_shot(path.name)
    if shot is None:
        return {"ok": False, "reason": "识别不出镜号", "kind": kind}

    if kind == 'images':
        new_name = "shot_%02d%s" % (shot, ext)
        return {"ok": True, "kind": kind, "target": 'images',
                "new_name": new_name, "shot": shot}

    if kind == 'videos_ok':
        new_name = "shot_%02d%s" % (shot, ext)
        return {"ok": True, "kind": kind, "target": 'videos_ok',
                "new_name": new_name, "shot": shot}

    # 音频：需要句号
    line = parse_line(path.name)
    if line is None:
        # 没有句号，默认第 1 句
        line = 1
    new_name = "s%02d_%02d%s" % (shot, line, ext)
    return {"ok": True, "kind": kind, "target": 'audio',
            "new_name": new_name, "shot": shot, "line": line}


def main():
    if len(sys.argv) < 2:
        print("用法: python 整理素材.py <项目名> [集数] [--dry]")
        return 1

    project = sys.argv[1]
    dry = '--dry' in sys.argv

    args = [a for a in sys.argv[2:] if not a.startswith('--')]
    chapter = int(args[0]) if args else 1

    ch_dir = Path(config.PROJECTS_DIR) / project / ("chapter_%03d" % chapter)
    manual = ch_dir / "manual"
    inbox = manual / "_inbox"

    if not ch_dir.exists():
        print("❌ 项目不存在:", ch_dir)
        return 1

    inbox.mkdir(parents=True, exist_ok=True)
    for sub in ('images', 'videos_ok', 'audio', '_待确认'):
        (manual / sub).mkdir(parents=True, exist_ok=True)

    files = [f for f in inbox.iterdir() if f.is_file()]
    if not files:
        print("=" * 66)
        print("_inbox 是空的")
        print("=" * 66)
        print()
        print("把要替换的素材全拖进这个目录:")
        print(" ", inbox)
        print()
        print("然后重新运行本脚本。")
        return 0

    print("=" * 66)
    print("整理人工素材%s" % ("（预演，不动文件）" if dry else ""))
    print("=" * 66)
    print()
    print("来源:", inbox)
    print("文件: %d 个" % len(files))
    print()

    plan = []       # (src, target_dir, new_name, info)
    failed = []     # (src, reason)

    for f in sorted(files):
        r = classify(f)
        if not r.get("ok"):
            failed.append((f, r.get("reason", "?")))
            continue
        plan.append((f, r["target"], r["new_name"], r))

    # 显示计划
    print("=" * 66)
    print("分类结果")
    print("=" * 66)
    print()

    by_type = {}
    for src, target, newname, info in plan:
        by_type.setdefault(target, []).append((src, newname, info))

    for target in ('images', 'videos_ok', 'audio'):
        items = by_type.get(target) or []
        if not items:
            continue
        print("【%s】%d 个" % (target, len(items)))
        for src, newname, info in items[:10]:
            extra = ""
            if target == 'audio':
                extra = " (镜%d 句%d)" % (info['shot'], info['line'])
            print("  %-28s → %s%s" % (src.name[:28], newname, extra))
        if len(items) > 10:
            print("  ... 共 %d 个" % len(items))
        print()

    if failed:
        print("【识别失败】%d 个 → 放 _待确认/" % len(failed))
        for src, reason in failed[:10]:
            print("  %-28s  %s" % (src.name[:28], reason))
        if len(failed) > 10:
            print("  ... 共 %d 个" % len(failed))
        print()

    if dry:
        print("（预演模式，没有移动任何文件）")
        print("去掉 --dry 真正执行")
        return 0

    # 执行
    print("=" * 66)
    print("执行")
    print("=" * 66)
    print()

    moved = 0
    conflicts = 0
    for src, target, newname, info in plan:
        dst = manual / target / newname
        if dst.exists():
            conflicts += 1
            # 加后缀避免覆盖
            stem = dst.stem
            ext = dst.suffix
            n = 2
            while dst.exists():
                dst = manual / target / ("%s_%d%s" % (stem, n, ext))
                n += 1
        try:
            shutil.move(str(src), str(dst))
            moved += 1
            print("  ✅ %-24s → %s" % (src.name[:24], dst.name))
        except Exception as exc:  # noqa: BLE001
            print("  ❌ %s: %s" % (src.name[:24], exc))

    for src, reason in failed:
        dst = manual / "_待确认" / src.name
        n = 2
        while dst.exists():
            dst = manual / "_待确认" / ("%s_%d%s" % (src.stem, n, src.suffix))
            n += 1
        try:
            shutil.move(str(src), str(dst))
            print("  ⚠️ %-24s → _待确认/（%s）" % (src.name[:24], reason))
        except Exception:  # noqa: BLE001
            pass

    print()
    print("=" * 66)
    print("完成")
    print("=" * 66)
    print()
    print("  移动: %d 个" % moved)
    print("  冲突: %d 个（已自动改名 _2）" % conflicts)
    print("  待确认: %d 个" % len(failed))
    print()
    print("现在重跑任务，系统会优先用这些人工素材。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
