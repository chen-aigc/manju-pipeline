# -*- coding: utf-8 -*-
"""一键安装：建虚拟环境 + 装依赖 + 检查外部工具。

用法：
    python 安装.py           正常安装
    python 安装.py --force   强制重装依赖

不修改任何现有素材和配置；只创建 .venv 并安装依赖。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.venv'
PY = VENV / 'Scripts' / 'python.exe' if sys.platform == 'win32' \
    else VENV / 'bin' / 'python'

MIRROR = 'https://pypi.tuna.tsinghua.edu.cn/simple'


def run(cmd, **kw):
    """跑命令，返回 (成功?, 输出)。"""
    print('  $', ' '.join(str(c) for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=kw.pop('timeout', 1800), **kw)
        ok = r.returncode == 0
        out = (r.stdout or '') + (r.stderr or '')
        return ok, out
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def main():
    force = '--force' in sys.argv

    print('=' * 70)
    print('  AI 漫剧管线 · 一键安装')
    print('=' * 70)
    print()

    # ---------- 1. 检查 Python ----------
    print('[1/5] 检查 Python 版本')
    v = sys.version_info
    print('  当前: %d.%d.%d' % (v.major, v.minor, v.micro))
    if v < (3, 11):
        print('  ❌ 需要 Python 3.11 及以上')
        print('     去 https://www.python.org/downloads/ 下载新版')
        input('\n按回车退出...')
        return 1
    print('  ✅ 版本满足要求')
    print()

    # ---------- 2. 建虚拟环境 ----------
    print('[2/5] 虚拟环境')
    if VENV.exists() and not force:
        print('  ✅ 已存在: %s' % VENV)
    else:
        if VENV.exists() and force:
            print('  删除旧环境...')
            shutil.rmtree(VENV)
            print('  创建新环境...')
        ok, out = run([sys.executable, '-m', 'venv', str(VENV)])
        if not ok:
            print('  ❌ 创建失败:')
            print(out[-800:])
            input('\n按回车退出...')
            return 1
        print('  ✅ 已创建: %s' % VENV)
    print()

    # ---------- 3. 装依赖 ----------
    print('[3/5] 安装依赖')
    req = ROOT / 'requirements.txt'
    if not req.exists():
        print('  ❌ 找不到 requirements.txt')
        input('\n按回车退出...')
        return 1

    print('  升级 pip...')
    run([str(PY), '-m', 'pip', 'install', '--upgrade', 'pip', '-q'])

    print('  安装主依赖（用清华源，国内更快）...')
    print('  （首次安装约 1-3 分钟，请耐心等待）')
    ok, out = run([str(PY), '-m', 'pip', 'install', '-r', str(req),
                   '-i', MIRROR, '-q'])

    if not ok:
        print('  ⚠️ 清华源失败，改用官方源重试...')
        ok, out = run([str(PY), '-m', 'pip', 'install', '-r', str(req),
                       '-q'])

    if not ok:
        print('  ❌ 安装失败:')
        print(out[-1500:])
        input('\n按回车退出...')
        return 1

    print('  ✅ 主依赖装好了')

    opt = ROOT / 'requirements-optional.txt'
    if opt.exists():
        print()
        print('  可选依赖（字幕自动对齐，约 90 MB）')
        ans = input('  要装吗？[y/N] ').strip().lower()
        if ans == 'y':
            print('  安装中...')
            ok2, out2 = run([str(PY), '-m', 'pip', 'install', '-r',
                             str(opt), '-i', MIRROR, '-q'])
            print('  %s 可选依赖' % ('✅' if ok2 else '⚠️'))
            if not ok2:
                print(out2[-600:])
        else:
            print('  跳过')
    print()

    # ---------- 4. 检查外部工具 ----------
    print('[4/5] 检查外部工具')

    ff = shutil.which('ffmpeg')
    if ff:
        ok, out = run([ff, '-version'])
        ver = out.split('\n')[0] if out else ''
        print('  ✅ ffmpeg  %s' % ver[:60])
    else:
        print('  ❌ ffmpeg 没找到')
        print('     装法: winget install Gyan.FFmpeg')
        print('     或者去 https://ffmpeg.org/download.html 下载解压加 PATH')

    # Blender
    blender = None
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8').split('\n'):
            if line.strip().startswith('BLENDER_EXE='):
                blender = line.split('=', 1)[1].strip().strip('"')
                break

    if blender and Path(blender).exists():
        print('  ✅ blender %s' % blender[:60])
    else:
        cands = []
        for pat in ('C:/Program Files/Blender Foundation/Blender*/blender.exe',
                    'D:/Blender*/blender.exe',
                    str(ROOT / '_tools' / 'blender' / '**' / 'blender.exe')):
            import glob
            cands.extend(glob.glob(pat, recursive=True))
        if cands:
            print('  ✅ blender %s' % cands[0][:60])
            print('     （.env 里没配，但会自动找到）')
        else:
            print('  ⚠️ blender 没找到（只有 3D 场景功能需要）')
            print('     可不装；要用的话去 https://www.blender.org/download/')
    print()

    # ---------- 5. 检查配置 ----------
    print('[5/5] 检查 .env 配置')
    if not env.exists():
        print('  ❌ .env 不存在')
        print('     （应该随项目一起拷过来，或者手动创建）')
    else:
        text = env.read_text(encoding='utf-8')
        issues = []

        # 绝对路径检查
        import re
        for line in text.split('\n'):
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            m = re.match(r'(COMFY_WORKFLOW|BLENDER_EXE)=(.+)', s)
            if m:
                p = m.group(2).strip().strip('"')
                if Path(p).is_absolute() and not Path(p).exists():
                    issues.append('%s 指向的路径不存在: %s'
                                  % (m.group(1), p))

        # API key 检查
        for key in ('ARK_API_KEY', 'VOLC_TTS_TOKEN', 'LLM_API_KEY'):
            m = re.search(r'^%s=(.+)$' % key, text, re.M)
            if not m or not m.group(1).strip():
                issues.append('%s 没填' % key)

        if issues:
            print('  ⚠️ 有 %d 项需要注意：' % len(issues))
            for x in issues:
                print('     - %s' % x)
            print()
            print('     编辑 .env 修改，详见 安装说明.md')
        else:
            print('  ✅ 配置没问题')
    print()

    # ---------- 完成 ----------
    print('=' * 70)
    print('  安装完成')
    print('=' * 70)
    print()
    print('  启动方式：')
    print('    %s 启动.py' % PY)
    print('    或者双击 启动.py')
    print()
    print('  浏览器打开 http://127.0.0.1:8501')
    print()

    ans = input('现在就启动吗？[Y/n] ').strip().lower()
    if ans != 'n':
        os.chdir(str(ROOT))
        env_vars = os.environ.copy()
        env_vars['PYTHONPATH'] = str(ROOT / 'src')
        try:
            subprocess.run([str(PY), str(ROOT / '启动.py')],
                           cwd=str(ROOT), env=env_vars)
        except KeyboardInterrupt:
            pass

    return 0


if __name__ == '__main__':
    sys.exit(main())
