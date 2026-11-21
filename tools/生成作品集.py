# -*- coding: utf-8 -*-
"""生成作品集图片：把关键界面和成片帧拼成一张长图。

用于：
    - 投递时作为图片附件
    - 面试时快速展示
    - 简历里如果允许贴图
"""
import glob
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

from PIL import Image, ImageDraw, ImageFont

BASE = Path(r'D:\deepseek文件区\manju')
OUT = BASE / '作品集'
OUT.mkdir(exist_ok=True)


def get_font(size=20):
    """找一个能显示中文的字体。"""
    cands = [
        r'C:\Windows\Fonts\msyh.ttc',
        r'C:\Windows\Fonts\msyhbd.ttc',
        r'C:\Windows\Fonts\simhei.ttf',
        r'C:\Windows\Fonts\simsun.ttc',
    ]
    for c in cands:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _ffmpeg():
    import shutil
    p = shutil.which('ffmpeg')
    if p:
        return p
    for pat in (
        r'C:\Users\ASUS\AppData\Local\Microsoft\WinGet\Packages'
        r'\Gyan.FFmpeg*\**\bin\ffmpeg.exe',
    ):
        c = glob.glob(pat, recursive=True)
        if c:
            return c[0]
    return None


def extract_frame(video, t, out):
    ff = _ffmpeg()
    if not ff:
        return False
    r = subprocess.run(
        [ff, '-y', '-ss', str(t), '-i', str(video),
         '-frames:v', '1', '-vf', 'scale=540:-1', '-q:v', '3', str(out)],
        capture_output=True, timeout=60)
    return r.returncode == 0 and out.exists()


def make_poster():
    """拼一张 4 格海报：水墨 / 国漫 / 写实 / 动漫。"""
    font_big = get_font(30)
    font_small = get_font(18)

    items = [
        (BASE / '字幕输出' / '断剑月白_字幕版.mp4', 8.0, '《断剑月白》水墨 · 81 秒'),
        (BASE / '成片交付' / '国漫_山巅剑气_10秒.mp4', 1.0, '山巅剑气 · 国漫 · 10 秒'),
        (BASE / '成片交付' / '写实_便利店_10秒.mp4', 1.0, '深夜便利店 · 写实 · 10 秒'),
        (BASE / '动漫测试' / 'shot_02.mp4', 1.0, '便利店日常 · 日系动漫'),
    ]

    cells = []
    for video, t, label in items:
        if not video.exists():
            print('  跳过（不存在）:', video.name)
            continue
        tmp = OUT / ('_tmp_%s.jpg' % video.stem[:8])
        if not extract_frame(video, t, tmp):
            print('  抽帧失败:', video.name)
            continue
        im = Image.open(tmp).convert('RGB')
        # 统一高度
        ratio = 900 / im.height
        im = im.resize((int(im.width * ratio), 900), Image.LANCZOS)
        cells.append((im, label))
        tmp.unlink(missing_ok=True)

    if not cells:
        print('没有可用素材')
        return

    W = sum(c[0].width for c in cells)
    H = 900 + 60
    canvas = Image.new('RGB', (W, H), (250, 250, 250))
    d = ImageDraw.Draw(canvas)

    x = 0
    for im, label in cells:
        canvas.paste(im, (x, 60))
        d.text((x + 12, 14), label, fill=(30, 30, 30), font=font_small)
        x += im.width

    out = OUT / '作品海报.jpg'
    canvas.save(out, quality=90)
    print('生成海报: %s  %dx%d' % (out, W, H))


def make_pipeline_diagram():
    """画一张管线流程图。"""
    font_t = get_font(32)
    font_m = get_font(22)
    font_s = get_font(17)

    W, H = 1600, 900
    canvas = Image.new('RGB', (W, H), (255, 255, 255))
    d = ImageDraw.Draw(canvas)

    # 标题
    d.text((40, 30), 'AI 漫剧自动化生产管线', fill=(20, 20, 20), font=font_t)
    d.text((40, 80), '剧本 → 分镜 → 出图 → 出视频 → 配音 → 剪辑 → 素材包交付',
           fill=(90, 90, 90), font=font_m)

    # 七环节
    stages = ['剧本', '分镜', '出图', '视频', '配音', '剪辑', '交付']
    n = len(stages)
    box_w, box_h = 180, 90
    gap = (W - 80 - box_w * n) / (n - 1)
    y = 160

    for i, s in enumerate(stages):
        x = 40 + i * (box_w + gap)
        d.rounded_rectangle([x, y, x + box_w, y + box_h],
                            radius=12, fill=(235, 243, 255),
                            outline=(70, 130, 220), width=2)
        # 居中文字
        bbox = d.textbbox((0, 0), s, font=font_m)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        d.text((x + (box_w - tw) / 2, y + (box_h - th) / 2 - 4),
               s, fill=(30, 60, 120), font=font_m)

        if i < n - 1:
            ax = x + box_w + 5
            ay = y + box_h / 2
            d.line([ax, ay, ax + gap - 10, ay], fill=(150, 160, 180), width=3)
            d.polygon([(ax + gap - 12, ay - 6), (ax + gap - 4, ay),
                       (ax + gap - 12, ay + 6)], fill=(150, 160, 180))

    # 核心机制（下面）
    d.text((40, 300), '核心机制', fill=(20, 20, 20), font=font_m)

    mechs = [
        ('插件化 Provider', '9 类能力 47 个实现\n换供应商 0 行改动'),
        ('角色定妆照', '首跑生成并缓存\n跨镜服装/脸一致'),
        ('场景色调约束', '剧本声明色温\n消除同镜突变'),
        ('跨进程同步', '文件锁 + 原子替换\n78% 丢失 → 0%'),
        ('成本记账', '微元整数\n精确到分'),
        ('回收站 + 回滚', '18 类危险操作\n六重校验'),
        ('用量审计', 'SQLite 逐次记录\ntoken/金额/耗时'),
        ('素材包交付', '六子目录 + 时间轴\n40 镜 7.6 秒'),
    ]

    mw, mh = 355, 130
    cols = 4
    for i, (title, desc) in enumerate(mechs):
        cx = 40 + (i % cols) * (mw + 25)
        cy = 340 + (i // cols) * (mh + 20)
        d.rounded_rectangle([cx, cy, cx + mw, cy + mh],
                            radius=10, fill=(248, 250, 252),
                            outline=(200, 210, 225), width=2)
        d.text((cx + 16, cy + 14), title, fill=(30, 60, 130), font=font_m)
        dy = cy + 50
        for ln in desc.split('\n'):
            d.text((cx + 16, dy), ln, fill=(80, 85, 95), font=font_s)
            dy += 26

    # 底部数字
    d.text((40, 810),
           '27,996 行 Python   ·   122 个 REST 端点   ·   18 个页面   ·   '
           '81 个测试脚本   ·   一集 40 镜 ¥168',
           fill=(100, 105, 115), font=font_s)

    out = OUT / '管线架构图.jpg'
    canvas.save(out, quality=92)
    print('生成架构图: %s  %dx%d' % (out, W, H))


def make_number_card():
    """关键数字卡片。"""
    font_num = get_font(64)
    font_lab = get_font(20)
    font_t = get_font(34)

    W, H = 1600, 900
    canvas = Image.new('RGB', (W, H), (255, 255, 255))
    d = ImageDraw.Draw(canvas)

    d.text((60, 40), '关键数字', fill=(20, 20, 20), font=font_t)

    data = [
        ('27,996', '行 Python 代码', '97 个文件'),
        ('47', 'AI 实现', '9 类能力'),
        ('122', 'REST 端点', '18 个页面'),
        ('81', '测试脚本', '覆盖全接口'),
        ('78% → 0', '写入丢失率', '4 进程并发实测'),
        ('¥168', '一集成本', '40 镜精确到分'),
        ('7.6 秒', '素材包导出', '40 镜 221 MB'),
        ('21 镜', '真实成片', '85 秒 + 81 秒 4K'),
    ]

    cw, ch = 355, 200
    for i, (num, label, sub) in enumerate(data):
        cx = 60 + (i % 4) * (cw + 25)
        cy = 120 + (i // 4) * (ch + 30)
        d.rounded_rectangle([cx, cy, cx + cw, cy + ch],
                            radius=14, fill=(245, 248, 252),
                            outline=(210, 220, 235), width=2)
        d.text((cx + 24, cy + 26), num, fill=(30, 80, 160), font=font_num)
        d.text((cx + 24, cy + 118), label, fill=(40, 45, 55), font=font_lab)
        d.text((cx + 24, cy + 152), sub, fill=(120, 125, 135), font=font_lab)

    out = OUT / '关键数字.jpg'
    canvas.save(out, quality=92)
    print('生成数字卡片: %s  %dx%d' % (out, W, H))


if __name__ == '__main__':
    print('=== 生成作品集 ===')
    print()
    make_poster()
    print()
    make_pipeline_diagram()
    print()
    make_number_card()
    print()
    print('输出目录:', OUT)
    for f in sorted(OUT.glob('*.jpg')):
        print('  %-24s %6.0f KB' % (f.name, f.stat().st_size / 1024))
