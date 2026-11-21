# -*- coding: utf-8 -*-
"""按头部参数生成蒙版

关键：蒙版坐标和骨架坐标都来自同一个 profile，
     所以蒙版一定圈在头的正确位置上——不需要人脸检测。
"""
import os
from PIL import Image, ImageDraw, ImageFilter


def head_box(prof, w, h, first=True):
    """返回头部包围盒 (x0, y0, x1, y1) 的像素坐标"""
    if first:
        cx, cy, size = prof["cx"], prof["cy"], prof["size"]
    else:
        cx, cy, size = prof["second"]

    head_h = size * h
    head_w = head_h * 0.78
    # 稍微放大一点，把头发/下巴也包进去
    pad_x = head_w * 0.28
    pad_y = head_h * 0.22

    px, py = cx * w, cy * h
    x0 = px - head_w / 2 - pad_x
    x1 = px + head_w / 2 + pad_x
    y0 = py - head_h / 2 - pad_y
    y1 = py + head_h / 2 + pad_y
    return (max(0, int(x0)), max(0, int(y0)),
            min(w, int(x1)), min(h, int(y1)))


def make_mask(prof, w, h, blur=18, expand=0.15, out_path=None):
    """生成重绘蒙版（白=重绘区域，黑=保留）

    expand：在头部框基础上再向外扩张的比例
            0 = 只重绘头部本身
            0.4 = 连肩颈一起重绘（换发型/服装领口时用）
    """
    m = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(m)

    targets = [True] if prof.get("count", 1) <= 1 else [True, False]
    for first in targets:
        if not first and not prof.get("second"):
            continue
        x0, y0, x1, y1 = head_box(prof, w, h, first=first)
        bw, bh = x1 - x0, y1 - y0
        ex, ey = int(bw * expand), int(bh * expand)
        d.ellipse([x0 - ex, y0 - ey, x1 + ex, y1 + ey], fill=255)

    if blur:
        m = m.filter(ImageFilter.GaussianBlur(blur))

    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        Image.merge('RGB', (m, m, m)).save(out_path)
    return m


def verify(prof, w, h, skeleton_path=None, out_path=None):
    """把骨架和蒙版叠在一张图上看是否对齐（调试用）"""
    if skeleton_path and os.path.exists(skeleton_path):
        im = Image.open(skeleton_path).convert('RGB')
    else:
        im = Image.new('RGB', (w, h), (0, 0, 0))
    mask = make_mask(prof, w, h, blur=0)
    # 蒙版当红色叠层
    red = Image.new('RGB', (w, h), (255, 0, 0))
    blended = Image.composite(red, im, mask.point(lambda v: v // 3))
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        blended.save(out_path)
    return blended


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from manju.headlock.profile import get_profile, describe
    from manju.headlock.skeleton import make_skeleton

    OUT = r'D:\deepseek文件区\manju\_头锁校验'
    os.makedirs(OUT, exist_ok=True)
    SK = r'D:\deepseek文件区\manju\_头锁骨架'

    for st in ("远景", "中景", "近景", "特写"):
        for cnt in (1, 2):
            p = get_profile(st, cnt)
            sk = os.path.join(SK, '%s_%d人.png' % (st, cnt))
            out = os.path.join(OUT, '%s_%d人_校验.png' % (st, cnt))
            verify(p, 832, 1216, sk, out)
            print('  %-6s %s -> %s' % (st, describe(p), os.path.basename(out)))
    print()
    print('输出:', OUT)
