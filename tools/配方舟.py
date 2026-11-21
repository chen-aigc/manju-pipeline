# -*- coding: utf-8 -*-
"""配置火山方舟为默认引擎（明天开通模型后直接可用）。

做了：
    1. .env 里设 IMAGE_PROVIDER=doubao, VIDEO_PROVIDER=jimeng
    2. 修正图像模型配置
    3. 加一个「一键测试」脚本
"""
import io
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

ENV = r'D:\deepseek文件区\manju\.env'

# 读现有
lines = []
if os.path.exists(ENV):
    lines = io.open(ENV, encoding='utf-8').read().split('\n')

# 要设置的键
WANT = {
    'ARK_API_KEY': None,           # 保留原值
    'ARK_BASE_URL': 'https://ark.cn-beijing.volces.com/api/v3',
    'DOUBAO_IMAGE_MODEL': 'doubao-seedream-5-5-261028',
    'DOUBAO_IMAGE_SIZE': '1080x1920',
    'DOUBAO_IMAGE_PRICE': '0.20',
    'DOUBAO_IMAGE_WATERMARK': '0',
    'JIMENG_BASE_URL': 'https://ark.cn-beijing.volces.com/api/v3',
    'JIMENG_MODEL': 'doubao-seedance-2-0-mini-260615',
    'JIMENG_RESOLUTION': '720p',
    'JIMENG_DURATION': '4',
    'JIMENG_AUDIO': 'off',
    # 切换默认引擎（明天开通后即可用）
    'IMAGE_PROVIDER': 'doubao',
    'VIDEO_PROVIDER': 'jimeng',
}

# 解析现有
existing = {}
for i, line in enumerate(lines):
    s = line.strip()
    if not s or s.startswith('#') or '=' not in s:
        continue
    k, v = s.split('=', 1)
    existing[k.strip()] = (i, v.strip())

changed = []
for k, v in WANT.items():
    if v is None:
        continue
    if k in existing:
        idx, old = existing[k]
        if old != v:
            lines[idx] = '%s=%s' % (k, v)
            changed.append((k, old, v))
    else:
        lines.append('%s=%s' % (k, v))
        changed.append((k, '<无>', v))

io.open(ENV, 'w', encoding='utf-8').write('\n'.join(lines))

print("=" * 70)
print("配置已更新")
print("=" * 70)
print()
for k, old, new in changed:
    print("  %-24s %r → %r" % (k, old, new))

if not changed:
    print("  （无变化）")

print()
print("=" * 70)
print("重要：明天要做的事")
print("=" * 70)
print()
print("  1. 打开 https://console.volcengine.com/ark")
print("  2. 左侧「模型推理」→「开通管理」")
print("  3. 开通这两个模型：")
print("       doubao-seedream-5-5-261028        出图  ¥0.20/张")
print("       doubao-seedance-2-0-mini-260615    出视频 ¥0.26/秒")
print("  4. 开通后重新运行验证脚本：")
print("       .venv\\Scripts\\python tools\\验证方舟API.py")
print()
print("  开通后引擎已切到 doubao/jimeng，直接跑就行。")
