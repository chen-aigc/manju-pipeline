# -*- coding: utf-8 -*-
"""明天用的：一键测试火山方舟出图 + 出视频。

用法：
    .venv\\Scripts\\python 测方舟.py          测试出图 + 出视频
    .venv\\Scripts\\python 测方舟.py --image  只测出图
    .venv\\Scripts\\python 测方舟.py --video  只测出视频

成本：
    出图 1 张   ¥0.20
    出视频 4 秒 ¥1.04
    合计        ¥1.24
"""
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\deepseek文件区\manju\src')

import shutil
import tempfile
from pathlib import Path

do_image = '--video' not in sys.argv
do_video = '--image' not in sys.argv

OUT = Path(tempfile.mkdtemp(prefix='ark_test_'))
print("=" * 72)
print("火山方舟测试")
print("=" * 72)
print()
print("输出目录:", OUT)
print()
print("成本预估:")
if do_image:
    print("  出图 1 张    ¥0.20")
if do_video:
    print("  出视频 4 秒  ¥1.04")
print()

PROMPT = ('古风武侠，少年身着灰绿长衫站在庭院门口，'
          '手持一柄断剑，逆光，水墨风格，电影感构图')


# ================================================================
# 1. 出图
# ================================================================
image_file = None

if do_image:
    print("=" * 72)
    print("1. 出图（Seedream）")
    print("=" * 72)
    print()
    print("Prompt:", PROMPT)
    print()

    try:
        from manju.plugins.image.doubao import DoubaoImage

        out_path = OUT / 'test_image.png'
        # 插件通过 config['out_path'] 拿输出路径
        p = DoubaoImage(config={'out_path': str(out_path)})
        print("available:", p.available())

        t0 = time.time()
        res = p.generate(
            prompt=PROMPT,
            negative='',
            seed=-1,
            size='1080x1920',
        )
        dt = time.time() - t0

        print()
        print("✅ 出图成功  耗时 %.1f 秒" % dt)
        print("   文件:", res.path)
        if res.path and Path(res.path).exists():
            sz = Path(res.path).stat().st_size
            print("   大小: %.0f KB" % (sz / 1024))
            image_file = Path(res.path)
            if image_file != out_path and not out_path.exists():
                shutil.copy2(image_file, out_path)
            print("   已保存到:", out_path)
        print("   成本:", getattr(res.cost, 'yuan', '?'), "元")

    except Exception as e:
        print()
        print("❌ 出图失败:", type(e).__name__)
        print("  ", str(e)[:400])
        if 'ModelNotOpen' in str(e) or 'NotOpen' in str(e):
            print()
            print("   → 模型还没开通！去控制台开通后重试：")
            print("     https://console.volcengine.com/ark")
            print("     左侧「模型推理」→「开通管理」")
            print("     开通 doubao-seedream-5-5-261028")


# ================================================================
# 2. 出视频
# ================================================================
if do_video:
    print()
    print("=" * 72)
    print("2. 出视频（Seedance 图生视频）")
    print("=" * 72)
    print()

    if not image_file or not image_file.exists():
        print("  ⚠️ 需要一张图作为输入（插件只支持图生视频）")
        print("     先跑出图（不加 --video），或手动指定一张图")
        print()
        # 尝试用项目里现有的图
        from pathlib import Path as _P
        candidates = sorted(
            _P(r'D:\deepseek文件区\manju\projects\断剑月白\chapter_001\images')
            .glob('shot_0[2-8].png'))
        if candidates:
            image_file = candidates[0]
            print("   → 使用现有素材:", image_file.name)
        else:
            print("   → 没有可用素材，跳过视频测试")
            do_video = False

if do_video:
    try:
        from manju.plugins.video.jimeng import JimengVideo

        out_path = OUT / 'test_video.mp4'
        v = JimengVideo(config={'out_path': str(out_path)})
        print("available:", v.available())
        print("   输入图:", image_file.name)
        print("   模型:", getattr(v, '_model', lambda: '?')())

        t0 = time.time()
        res = v.generate(
            image=str(image_file),
            prompt='镜头缓慢推进，风吹动衣角',
            duration=4.0,
        )
        dt = time.time() - t0

        print()
        print("✅ 视频生成完成  耗时 %.1f 秒" % dt)
        print("   文件:", getattr(res, 'video', None))
        if getattr(res, 'video', None) and Path(res.video).exists():
            sz = Path(res.video).stat().st_size
            print("   大小: %.1f MB" % (sz / 1024 / 1024))
            if Path(res.video) != out_path:
                shutil.copy2(res.video, out_path)
            print("   已保存到:", out_path)
        print("   成本:", getattr(res.cost, 'yuan', '?'), "元")

    except Exception as e:
        print()
        print("❌ 出视频失败:", type(e).__name__)
        print("  ", str(e)[:400])
        if 'ModelNotOpen' in str(e) or 'NotOpen' in str(e):
            print()
            print("   → 模型还没开通！去控制台开通后重试：")
            print("     https://console.volcengine.com/ark")
            print("     开通 doubao-seedance-2-0-mini-260615")


print()
print("=" * 72)
print("完成，输出目录:", OUT)
print("=" * 72)
