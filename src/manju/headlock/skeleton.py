# -*- coding: utf-8 -*-
"""生成"只含头部"的 OpenPose 骨架图

为什么只画头部：
    全身骨架会僵化整个姿势；
    只画头部锚点，ControlNet 只约束头的位置和大小，
    身体/服饰/姿态仍由提示词自由决定。

OpenPose 头部相关点：
    0  鼻子   1  脖子
    14 右眼   15 左眼   16 右耳   17 左耳
    2  右肩   5  左肩   （用来暗示头的朝向和肩宽比例）
"""
import os
import sys

from PIL import Image, ImageDraw

# OpenPose 标准配色（只用到这些点）
C = {
    0: (255, 0, 0),       # 鼻子
    1: (255, 85, 0),      # 脖子
    2: (255, 170, 0),     # 右肩
    5: (85, 255, 0),      # 左肩
    14: (170, 0, 255),    # 右眼
    15: (255, 0, 255),    # 左眼
    16: (255, 0, 170),    # 右耳
    17: (255, 0, 85),     # 左耳
}


def head_points(cx, cy, size, w, h, facing="front", shoulder_w=2.6):
    """按归一化参数算头部各点的像素坐标。

    size 表示头部高度占画面高度的比例；
    shoulder_w 是肩宽相对于头宽的倍数。
    """
    head_h = size * h
    head_w = head_h * 0.78          # 动漫头偏窄长
    px, py = cx * w, cy * h

    # 各点相对头部中心的偏移（以头高为单位）
    nose = (px, py + head_h * 0.10)
    neck = (px, py + head_h * 0.52)
    eye_dy = py - head_h * 0.06
    ear_dy = py + head_h * 0.02

    if facing == "front":
        r_eye = (px - head_w * 0.20, eye_dy)
        l_eye = (px + head_w * 0.20, eye_dy)
        r_ear = (px - head_w * 0.52, ear_dy)
        l_ear = (px + head_w * 0.52, ear_dy)
    elif facing == "left":          # 脸朝画面左侧
        r_eye = (px - head_w * 0.34, eye_dy)
        l_eye = (px - head_w * 0.06, eye_dy)
        r_ear = (px - head_w * 0.44, ear_dy)
        l_ear = None
    elif facing == "right":
        r_eye = (px + head_w * 0.06, eye_dy)
        l_eye = (px + head_w * 0.34, eye_dy)
        r_ear = None
        l_ear = (px + head_w * 0.44, ear_dy)
    else:                            # back 背面
        r_eye = l_eye = None
        r_ear = (px - head_w * 0.40, ear_dy)
        l_ear = (px + head_w * 0.40, ear_dy)

    sh_dy = py + head_h * 0.78
    sh_dx = head_w * shoulder_w / 2
    r_sh = (px - sh_dx, sh_dy)
    l_sh = (px + sh_dx, sh_dy)

    return {
        0: nose, 1: neck,
        2: r_sh, 5: l_sh,
        14: r_eye, 15: l_eye, 16: r_ear, 17: l_ear,
    }


LIMBS = [
    (1, 0), (1, 2), (1, 5),
    (0, 14), (0, 15), (14, 16), (15, 17),
]


def draw_head_skeleton(img, pts, stick=10, radius=7):
    """在给定画布上画头部骨架"""
    d = ImageDraw.Draw(img)
    for a, b in LIMBS:
        pa, pb = pts.get(a), pts.get(b)
        if pa is None or pb is None:
            continue
        d.line([tuple(int(v) for v in pa), tuple(int(v) for v in pb)],
               fill=C[b], width=stick)
    for i, p in pts.items():
        if p is None:
            continue
        x, y = int(p[0]), int(p[1])
        d.ellipse([x - radius, y - radius, x + radius, y + radius], fill=C[i])
    return img


def make_skeleton(prof, w=832, h=1216, facing="front", out_path=None):
    """按 profile 生成头部骨架图

    prof 来自 profile.get_profile()：
        count=0 → 全黑（不约束）
        count=1 → 一个头
        count=2 → 两个头（用 second 参数）
    """
    img = Image.new('RGB', (w, h), (0, 0, 0))
    if prof.get("count", 1) == 0:
        if out_path:
            img.save(out_path)
        return img

    # 主角
    pts = head_points(prof["cx"], prof["cy"], prof["size"], w, h, facing)
    draw_head_skeleton(img, pts)

    # 配角
    if prof.get("second"):
        sx, sy, ss = prof["second"]
        # 配角脸朝主角方向
        sfacing = "right" if prof["cx"] < sx else "left"
        draw_head_skeleton(img, head_points(sx, sy, ss, w, h, sfacing))

    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        img.save(out_path)
    return img


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from manju.headlock.profile import get_profile, describe

    OUT = r'D:\deepseek文件区\manju\_头锁骨架'
    os.makedirs(OUT, exist_ok=True)
    for st in ("远景", "中景", "近景", "特写"):
        for cnt in (1, 2):
            p = get_profile(st, cnt)
            name = '%s_%d人.png' % (st, cnt)
            make_skeleton(p, out_path=os.path.join(OUT, name))
            print('  %-6s %s' % (name, describe(p)))
    print()
    print('输出:', OUT)
