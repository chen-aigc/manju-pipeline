# -*- coding: utf-8 -*-
"""从现有高质量图裁出特写素材

思路：整图生成特写不可靠（SD 默认画完整的人），
但从已生成的图里裁局部，画风 100% 一致、零成本。

裁完统一放大到 512×768 或原比例，保证够用。
"""
import sys
import os
import json

sys.stdout.reconfigure(encoding='utf-8')

from PIL import Image, ImageFilter, ImageDraw, ImageFont

HERE = r'D:\deepseek文件区\manju'
SRC40 = os.path.join(HERE, '_SD15_40镜')
SRC_SNOW = os.path.join(HERE, '_雪夜入门_素材')
OUT = os.path.join(HERE, '_裁切特写')
os.makedirs(OUT, exist_ok=True)

# (输出名, 源文件, 裁切框比例 x0,y0,x1,y1, 说明)
# 比例按原图 512x768 的百分比
CROPS = [
    # === 手部 ===
    ("T01_提灯的手", "s01.png", SRC40, (0.28, 0.52, 0.62, 0.78), "林越提灯的手"),
    ("T02_扶墙的手", "s02.png", SRC40, (0.30, 0.55, 0.68, 0.82), "站立时垂下的手"),
    ("T03_剑柄", "s20.png", SRC40, (0.25, 0.45, 0.75, 0.80), "握剑区域"),

    # === 眼睛/脸部 ===
    ("T04_垂眸", "s34.png", SRC40, (0.28, 0.10, 0.72, 0.38), "林越低垂的眼睛"),
    ("T05_抬眼", "s30.png", SRC40, (0.26, 0.12, 0.74, 0.42), "月白抬眼看"),
    ("T06_侧脸", "s13.png", SRC40, (0.25, 0.10, 0.75, 0.40), "林越侧脸"),

    # === 道具/环境 ===
    ("T07_灯笼", "s01.png", SRC40, (0.50, 0.40, 0.85, 0.68), "提着的油灯"),
    ("T08_窗棂", "s09.png", SRC40, (0.00, 0.05, 0.45, 0.55), "纸窗与光"),
    ("T09_木梁", "s14.png", SRC40, (0.00, 0.00, 0.55, 0.40), "屋檐与木梁"),
    ("T10_雪地", "s14.png", SRC40, (0.10, 0.55, 0.75, 0.95), "积雪地面"),
    ("T11_台阶", "s22.png", SRC40, (0.05, 0.45, 0.70, 0.90), "石阶"),
    ("T12_灯下桌面", "s34.png", SRC40, (0.45, 0.45, 0.95, 0.85), "灯下的木桌"),

    # === 从新素材里取 ===
    ("T13_院门缝光", "S02_雪夜院门_近.png", SRC_SNOW, (0.30, 0.25, 0.75, 0.70), "门缝漏出的光"),
    ("T14_屋内一角", "S04_屋内灯下_广.png", SRC_SNOW, (0.55, 0.20, 1.00, 0.65), "房间一角"),
    ("T15_两人距离", "S08_两人灯下.png", SRC_SNOW, (0.15, 0.25, 0.85, 0.70), "两人互动的区域"),
]


def crop_and_scale(src_path, box_ratio, out_path, target_long=768):
    im = Image.open(src_path).convert('RGB')
    w, h = im.size
    x0, y0, x1, y1 = box_ratio
    box = (int(w*x0), int(h*y0), int(w*x1), int(h*y1))
    c = im.crop(box)
    # 放大到长边 target_long（用 LANCZOS 保质量）
    cw, ch = c.size
    scale = target_long / max(cw, ch)
    if scale > 1:
        c = c.resize((int(cw*scale), int(ch*scale)), Image.LANCZOS)
        # 轻微锐化，弥补放大损失
        c = c.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
    c.save(out_path, quality=95)
    return c.size


if __name__ == '__main__':
    print('=' * 74)
    print('裁切特写素材')
    print('=' * 74)

    ok, skip = 0, []
    for name, fname, srcdir, box, desc in CROPS:
        src = os.path.join(srcdir, fname)
        if not os.path.exists(src):
            skip.append('%s（缺 %s）' % (name, fname))
            continue
        dst = os.path.join(OUT, name + '.png')
        size = crop_and_scale(src, box, dst)
        print('  %-16s <- %-22s %dx%d  [%s]' % (name, fname, size[0], size[1], desc))
        ok += 1

    print()
    print('成功 %d 张' % ok)
    for s in skip:
        print('  跳过:', s)
    print('输出:', OUT)
