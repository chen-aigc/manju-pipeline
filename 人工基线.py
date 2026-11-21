# -*- coding: utf-8 -*-
"""人工成本基线测量表。

用途：记录"纯手工做一集"各环节的真实耗时和成本，
      与系统自动化的数据对比。

用法：
    python 人工基线.py           显示空白表
    python 人工基线.py --fill    逐项输入实际用时
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

BASE = Path(r'D:\deepseek文件区\manju')
OUT = BASE / '人工基线记录.json'

# 手工流程的 7 个环节
STEPS = [
    ('write', '写剧本', '用 ChatGPT/DeepSeek 网页版，手动调 prompt 直到满意', 30),
    ('split', '分镜', '手动把剧本拆成镜头表（Excel 或文档）', 20),
    ('image', '出图', '在即梦/方舟网页版逐张生成，每张不满意就重出', 180),
    ('video', '出视频', '逐张上传、选参数、等待、下载', 120),
    ('tts', '配音', '逐句生成、试听、重新生成', 30),
    ('edit', '剪辑', '导入剪映、对齐、加字幕、调音', 60),
    ('export', '导出交付', '整理文件、命名、打包', 20),
]


def show_table():
    """显示空白/已填的测量表。"""
    data = {}
    if OUT.exists():
        try:
            data = json.loads(OUT.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            pass

    print('=' * 74)
    print('人工成本基线')
    print('=' * 74)
    print()
    print('  %-8s %-8s %-10s %s' % ('环节', '实际', '预估', '说明'))
    print('  ' + '-' * 70)

    total_real = 0
    for key, name, desc, est in STEPS:
        rec = data.get(key) or {}
        real = rec.get('minutes', '')
        if real:
            total_real += float(real)
        print('  %-8s %-8s %-10s %s' % (
            name,
            ('%d 分' % real) if real else '?',
            '%d 分' % est,
            desc[:40]))

    print('  ' + '-' * 70)
    print('  合计     %-8s %-10s' % (
        ('%d 分' % total_real) if total_real else '?',
        '%d 分' % sum(s[3] for s in STEPS)))
    print()

    if data:
        print('  记录文件:', OUT.name)
    else:
        print('  还没有记录。运行: python 人工基线.py --fill')
    print()


def fill():
    """逐项输入实际用时。"""
    data = {}
    if OUT.exists():
        try:
            data = json.loads(OUT.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            pass

    print('=' * 74)
    print('记录人工耗时')
    print('=' * 74)
    print()
    print('  每完成一步，回来填实际用了多少分钟。')
    print('  直接回车 = 跳过')
    print()

    for key, name, desc, est in STEPS:
        cur = (data.get(key) or {}).get('minutes')
        hint = '（上次 %s 分）' % cur if cur else '（预计 %d 分）' % est
        print('  %s %s' % (name, hint))
        print('    %s' % desc)
        s = input('    实际分钟数: ').strip()
        if not s:
            print()
            continue
        try:
            m = float(s)
        except ValueError:
            print('    跳过（不是数字）')
            print()
            continue

        data[key] = {
            'name': name,
            'minutes': m,
            'ts': time.strftime('%Y-%m-%d %H:%M'),
        }
        print('    ✅ 记录 %g 分钟' % m)
        print()

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    print('=' * 74)
    print('已保存:', OUT)
    print()
    show_table()


def compare():
    """和系统自动化的数据对比。"""
    if not OUT.exists():
        print('还没有人工基线记录')
        return

    data = json.loads(OUT.read_text(encoding='utf-8'))

    # 系统实测数据（来自用量库）
    SYS = {
        'shots': 40,
        'wall_clock_min': 60,          # 实际等了多少分钟
        'human_minutes': 10,           # 人实际盯着的时间
        'cost_yuan': 168,              # API 成本
    }

    manual_total = sum(
        float((data.get(k) or {}).get('minutes') or 0) for k, *_ in STEPS)

    if not manual_total:
        print('人工基线还没填完')
        return

    print('=' * 74)
    print('对比结果')
    print('=' * 74)
    print()
    print('  %-16s %-14s %-14s %s' % ('指标', '人工', '系统', '差异'))
    print('  ' + '-' * 70)

    rows = [
        ('墙钟时间', '%d 分钟' % manual_total,
         '%d 分钟' % SYS['wall_clock_min'],
         '快 %.1f 倍' % (manual_total / SYS['wall_clock_min'])),
        ('人的工时', '%d 分钟' % manual_total,
         '%d 分钟' % SYS['human_minutes'],
         '省 %d%%' % ((1 - SYS['human_minutes'] / manual_total) * 100)),
        ('API 成本', '看工具', '¥%d' % SYS['cost_yuan'], '—'),
        ('可复现', '无', '版本快照', '—'),
        ('可回溯', '无', '回收站+回滚', '—'),
    ]

    for name, a, b, c in rows:
        print('  %-16s %-14s %-14s %s' % (name, a, b, c))

    print()
    print('=' * 74)
    print('结论')
    print('=' * 74)
    print()
    print('  一集 40 镜：')
    print('    人工 %d 分钟 → 系统 %d 分钟（人只看 %d 分钟）'
          % (manual_total, SYS['wall_clock_min'], SYS['human_minutes']))
    print('    人力投入省 %.0f%%'
          % ((1 - SYS['human_minutes'] / manual_total) * 100))
    print()


def main():
    if '--fill' in sys.argv:
        fill()
    elif '--compare' in sys.argv:
        compare()
    else:
        show_table()
        print('命令：')
        print('  python 人工基线.py --fill      记录实际耗时')
        print('  python 人工基线.py --compare   看对比结果')
        print()


if __name__ == '__main__':
    main()
