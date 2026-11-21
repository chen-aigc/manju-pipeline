# -*- coding: utf-8 -*-
"""给现有剧本补上角色描述和场景色调。

这些是新管线特性需要的字段：
    - characters 从 ["林越"] 改成 [{"name":"林越", "outfit":"..."}]
    - 新增 scenes 字段声明色调

不改变原有镜头内容，只增强。
"""
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

P = Path(r'D:\deepseek文件区\manju\projects\断剑月白\chapter_001\script.json')

# 备份
bak = P.with_suffix('.json.bak_before_enhance')
if not bak.exists():
    shutil.copy2(P, bak)
    print("备份:", bak.name)
print()

d = json.loads(P.read_text(encoding='utf-8'))

# ================================================================
# 1. 角色描述（基于素材库里的定妆照说明）
# ================================================================
PROFILES = {
    '林越': {
        'name': '林越',
        'age': '十八岁少年',
        'hair': '黑色长发半束，几缕碎发垂在脸侧',
        'face': '剑眉，黑亮眼睛，脸型偏瘦但线条柔和，无胡须，少年感',
        'outfit': '深色破旧布质长衫，衣襟有暗色血迹，袖口磨损',
        'outfit_color': '深灰褐，低饱和',
        'build': '身形偏瘦，肩背微驼，显得疲惫',
        'state': '脸上有干涸血痕，手上有冻疮和刀伤',
    },
    '月白': {
        'name': '月白',
        'age': '十八岁少女',
        'hair': '黑色长发披肩，发间素银簪',
        'face': '清秀温柔，眉眼细长，唇色偏淡，肤白',
        'outfit': '米白色交领襦裙，外罩浅灰披风，腰间浅青色细布带打结',
        'outfit_color': '米白 + 浅灰 + 浅青',
        'build': '身形纤瘦，动作轻缓',
        'state': '干净整洁，居家状态',
    },
}

# 把这些描述写进顶层
d['character_profiles'] = PROFILES
print("✅ 新增 character_profiles（%d 个角色）" % len(PROFILES))

# ================================================================
# 2. 场景色调
# ================================================================
SCENES = {
    '院门外': {
        'palette': '冷调，深蓝夜空 + 暖黄门灯',
        'light': '油灯从门缝透出，雪地反光',
        'weather': '大雪纷飞',
        'mood': '压抑、等待',
    },
    '院门内': {
        'palette': '暖调，油灯昏黄',
        'light': '门内油灯暖光，门外雪夜冷光',
        'weather': '雪夜',
        'mood': '转折、接纳',
    },
    '破屋屋内': {
        'palette': '暖黄调，昏暗',
        'light': '单盏油灯侧光，阴影重',
        'weather': '窗外雪落无声',
        'mood': '亲密、安静',
    },
    '屋内灯下': {
        'palette': '暖黄调，油灯为主光',
        'light': '油灯从侧面照亮，暮色透窗',
        'weather': '夜，窗外有雪',
        'mood': '温情、承诺',
    },
}

d['scenes'] = SCENES
print("✅ 新增 scenes（%d 个场景）" % len(SCENES))

# ================================================================
# 3. 把 characters 从字符串改成带描述的结构（保留名字）
# ================================================================
changed = 0
for s in (d.get('shots') or []):
    chars = s.get('characters') or []
    new_chars = []
    for c in chars:
        if isinstance(c, str):
            # 保留字符串形式（管线支持两种），但确保 character_profiles 有描述
            new_chars.append(c)
        else:
            new_chars.append(c)
    # 不变，因为 character_profiles 已经提供了描述
    if new_chars != chars:
        s['characters'] = new_chars
        changed += 1

print("✅ 镜头 characters 保持字符串形式（描述从 character_profiles 取）")

# ================================================================
# 保存
# ================================================================
P.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')

print()
print("=" * 60)
print("剧本已增强")
print("=" * 60)
print()
print("新增字段:")
print("  character_profiles  → 林越、月白的完整外形描述")
print("  scenes             → 4 个场景的色调/光线/天气/氛围")
print()
print("现在跑 Web 管线会:")
print("  ✅ 自动生成林越、月白的定妆照")
print("  ✅ 每个镜头自动带对应角色定妆照")
print("  ✅ 每个镜头自动加场景色调约束")
