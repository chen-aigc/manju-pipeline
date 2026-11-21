# -*- coding: utf-8 -*-
"""配置火山语音并测试完整插件。"""
import io
import sys

sys.stdout.reconfigure(encoding='utf-8')

ENV = r'D:\deepseek文件区\manju\.env'
TOKEN = 'b7d193a4-a48d-459c-82e3-63403bc40c08'

lines = io.open(ENV, encoding='utf-8').read().split('\n')

WANT = {
    'VOLC_TTS_TOKEN': TOKEN,
    'VOLC_TTS_VOICE': 'zh_female_gaolengyujie_uranus_bigtts',
}

existing = {}
for i, line in enumerate(lines):
    s = line.strip()
    if not s or s.startswith('#') or '=' not in s:
        continue
    k = s.split('=', 1)[0].strip()
    existing[k] = i

changed = []
for k, v in WANT.items():
    if k in existing:
        old = lines[existing[k]].split('=', 1)[1].strip()
        if old != v:
            lines[existing[k]] = '%s=%s' % (k, v)
            changed.append((k, old, v))
    else:
        lines.append('%s=%s' % (k, v))
        changed.append((k, '<无>', v))

io.open(ENV, 'w', encoding='utf-8').write('\n'.join(lines))

print("配置更新:")
for k, o, n in changed:
    if 'TOKEN' in k:
        print("  %-20s %r → %s..." % (k, o[:8] if o != '<无>' else o, n[:12]))
    else:
        print("  %-20s %r → %r" % (k, o, n))
print()

# 重新加载配置
import importlib

sys.path.insert(0, r'D:\deepseek文件区\manju\src')

# 强制重新读 .env
import os
os.environ['VOLC_TTS_TOKEN'] = TOKEN
os.environ['VOLC_TTS_VOICE'] = 'zh_female_gaolengyujie_uranus_bigtts'

import manju.config as cfg
importlib.reload(cfg)

print("=" * 70)
print("插件测试")
print("=" * 70)
print()

from pathlib import Path
import tempfile

from manju.plugins.tts.ark import ArkTTS, VOICES

p = ArkTTS()
print("available:", p.available())
print("可用音色:", len(VOICES), "个")
print()

if not p.available():
    print("❌ 还是不可用")
    sys.exit(1)

OUT = Path(tempfile.mkdtemp(prefix='volc_tts_'))

# 测试剧本里的台词
TESTS = [
    ("疼就说。", "zh_female_gaolengyujie_uranus_bigtts", "月白·高冷御姐"),
    ("不疼。", "zh_male_shaonianzixin_uranus_bigtts", "林越·少年梓辛"),
    ("骗人。", "zh_female_gaolengyujie_uranus_bigtts", "月白·高冷御姐"),
]

for i, (text, voice, label) in enumerate(TESTS, 1):
    print("-" * 70)
    print("[%d] %s  —  %s" % (i, text, label))
    print("-" * 70)

    out = OUT / ('test_%d.mp3' % i)
    try:
        inst = ArkTTS(config={'out_path': str(out), 'voice': voice})
        # 直接指定 voice 参数
        res = inst.synthesize(text, voice=voice)

        print("  ✅ 成功")
        print("     文件:", res.path.name)
        print("     大小: %.1f KB" % (
            Path(res.path).stat().st_size / 1024))
        print("     时长: %.2f 秒" % res.duration)
        print("     成本: %s 元" % getattr(res.cost, 'yuan', '?'))
        print("     耗时: %.1f 秒" % res.elapsed)
    except Exception as e:
        print("  ❌ 失败:", type(e).__name__)
        print("  ", str(e)[:300])

    print()

print("=" * 70)
print("输出目录:", OUT)
print("=" * 70)
