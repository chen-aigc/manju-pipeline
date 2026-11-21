# -*- coding: utf-8 -*-
"""测试项目自动清理工具。

用法：
    # 列出所有项目
    python 清项目.py

    # 删除名字匹配的项目
    python 清项目.py Test Concurrent Auth
    python 清项目.py --stale 0        # 删除 0 MB 的空项目
    python 清项目.py --older-than 1   # 删除 1 天没动过的

    # 加 --yes 不确认直接删
    python 清项目.py Test --yes

保护：名字以 _ 开头的系统目录、以及 KEEP 里列出的项目永不删除。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\deepseek文件区\manju\src')

from manju.core.trash import trash

PROJECTS = Path(r'D:\deepseek文件区\manju\projects')

# 永不删除
KEEP = {"断剑月白"}

# 明显的测试项目特征
TEST_PATTERNS = [
    "test", "Test", "debug", "Debug", "run", "Run",
    "sync", "Sync", "concurrent", "Concurrent",
    "verify", "Verify", "demo", "Demo",
    "fix", "Fix", "temp", "Temp", "tmp",
    "auth", "Auth", "perm", "Perm",
    "count", "Count", "spend", "Spend",
    "monitor", "Monitor", "progress", "Progress",
    "rollback", "Rollback", "cost", "Cost",
    "write", "Write", "final", "Final",
]


def size_mb(d):
    try:
        return sum(f.stat().st_size for f in d.rglob('*')
                   if f.is_file()) / 1024 / 1024
    except OSError:
        return 0.0


def is_test_project(name):
    """名字里含测试特征，或全大写/单字母。"""
    if name in KEEP:
        return False
    if name.startswith('_'):
        return False
    # 含测试关键词
    for pat in TEST_PATTERNS:
        if pat.lower() in name.lower():
            return True
    # 单字母或少字母
    if len(name) <= 2 and name.isascii():
        return True
    return False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = set(a for a in sys.argv[1:] if a.startswith('--'))

    auto_yes = '--yes' in flags or '-y' in flags

    # 列出所有项目
    all_projects = []
    for d in sorted(PROJECTS.iterdir()):
        if not d.is_dir() or d.name.startswith('_'):
            continue
        all_projects.append((d.name, size_mb(d)))

    if not all_projects:
        print("没有项目")
        return

    # 决定删哪些
    if '--stale' in flags:
        # 删除 0 MB 的空项目
        targets = [n for n, s in all_projects if s < 0.01 and n not in KEEP]
        print("模式: 删除空项目（< 0.01 MB）")
    elif args:
        # 名字匹配
        targets = []
        for n, s in all_projects:
            if n in KEEP:
                continue
            for a in args:
                if a.lower() in n.lower():
                    targets.append(n)
                    break
            else:
                if is_test_project(n):
                    targets.append(n)
        print("模式: 名字匹配 %s（+ 自动识别的测试项目）" % args)
    else:
        # 只列出，并标记哪些是测试项目
        print("=" * 66)
        print("%-26s %8s  %s" % ("项目", "大小", "判定"))
        print("=" * 66)
        for n, s in all_projects:
            if n in KEEP:
                mark = "🔒 保留"
            elif is_test_project(n):
                mark = "🗑 测试项目"
            else:
                mark = ""
            print("%-26s %6.1f MB  %s" % (n, s, mark))
        print()
        print("用法:")
        print("  python 清项目.py --stale       删除空项目")
        print("  python 清项目.py Test Auth     删除名字匹配的")
        print("  python 清项目.py --yes         自动删除所有测试项目")
        return

    if not targets:
        print("没有匹配的项目")
        return

    total = sum(s for n, s in all_projects if n in targets)
    print()
    print("将删除 %d 个项目（%.1f MB）:" % (len(targets), total))
    for n in targets:
        s = next((x for nm, x in all_projects if nm == n), 0)
        print("  %-26s %6.1f MB" % (n, s))
    print()

    if not auto_yes:
        ans = input("确认删除？（走回收站，可还原）[y/N] ").strip().lower()
        if ans not in ('y', 'yes'):
            print("已取消")
            return

    paths = [PROJECTS / n for n in targets]
    tid = trash.move(paths, op="cleanup", user="cli",
                     note="清理测试项目")

    if tid:
        print("✅ 已移入回收站: %s" % tid)
        print("   还原: python -c \"import sys;sys.path.insert(0,'src');"
              "from manju.core.trash import trash;"
              "trash.restore('%s')\"" % tid)
    else:
        print("❌ 失败")

    print()
    print("剩余项目:")
    for d in sorted(PROJECTS.iterdir()):
        if d.is_dir() and not d.name.startswith('_'):
            print("  %-26s %6.1f MB" % (d.name, size_mb(d)))


if __name__ == "__main__":
    main()
