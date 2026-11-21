# -*- coding: utf-8 -*-
"""打包成可迁移的压缩包。

用法：
    python 打包迁移.py                打包核心代码（不含素材）
    python 打包迁移.py --with-assets  连素材一起打

规则：
    排除所有 "_" 开头的目录和文件（历史测试产物）
    排除 .venv / __pycache__ / _tools / dist
    排除大体积媒体（除非 --with-assets）
"""
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'

# 明确排除的目录
EXCLUDE_TOP_DIRS = {
    '.venv', '__pycache__', 'dist', '_tools', '.git', '.idea',
    '.vscode', 'logs', 'scripts', 'web', '.cache',
    'projects/_trash', 'projects/_d7人工剪辑', 'projects/_试水',
}

# 排除媒体后缀（--with-assets 时不排除）
MEDIA_EXT = {'.mp4', '.png', '.jpg', '.jpeg', '.mp3', '.wav', '.webp',
             '.m4a', '.aac', '.flac', '.ogg', '.gif', '.bmp', '.tiff'}

# 排除的文件后缀
EXCLUDE_EXT = {'.wav', '.pyc', '.log', '.log.err'}

# 排除的根目录文件（API Key、临时文件、私人文档）
EXCLUDE_ROOT_FILES = {
    '.env',
    '断剑月白_配音.zip',
    '银狐入侵事件_汇总报告.docx',
    '银狐入侵事件_汇总报告.md',
}

# 排除的目录（相对路径，私有运行数据）
EXCLUDE_REL_DIRS = {
    '配音导出',
    '字幕输出',
    '成品',
    '共享素材',
    'projects/断剑月白',
}

# 排除的文件名后缀（私有运行数据）
EXCLUDE_NAME_SUFFIX = {
    '.usage.db',
    '.usage.db-shm',
    '.usage.db-wal',
    '.usage.db-journal',
}


def should_skip(rel: Path, with_assets: bool) -> bool:
    """判断是否排除。"""
    name = rel.name
    s = str(rel).replace('\\', '/')
    parts = s.split('/')

    # _ 开头的目录或文件（历史测试产物）
    for part in parts:
        if part.startswith('_') and part not in ('_',):
            # 但 _tools 已经在 EXCLUDE_TOP_DIRS 里处理了
            return True

    # 点开头的目录（.venv/.cache/.git 等）
    for part in parts[:-1]:
        if part.startswith('.'):
            return True

    # 明确排除的目录
    for d in EXCLUDE_TOP_DIRS:
        if s == d or s.startswith(d + '/'):
            return True

    # 私有运行数据目录
    for d in EXCLUDE_REL_DIRS:
        if s == d or s.startswith(d + '/'):
            return True

    # 私有运行数据文件（用量库）
    for suf in EXCLUDE_NAME_SUFFIX:
        if name.endswith(suf):
            return True

    # 根目录的临时文件 / 私人文档
    if len(parts) == 1 and name in EXCLUDE_ROOT_FILES:
        return True

    # 后缀
    if rel.suffix.lower() in EXCLUDE_EXT:
        return True

    if not with_assets and rel.suffix.lower() in MEDIA_EXT:
        return True

    # 大于 200MB 的单文件跳过
    try:
        if rel.is_file() and rel.stat().st_size > 200 * 1024 * 1024:
            return True
    except OSError:
        pass

    return False


def collect(with_assets: bool):
    files = []
    total = 0
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if should_skip(rel, with_assets):
            continue
        files.append(p)
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return files, total


def main():
    with_assets = '--with-assets' in sys.argv

    print('=' * 70)
    print('  打包迁移包')
    print('=' * 70)
    print()

    DIST.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M')
    suffix = '_含素材' if with_assets else ''
    out = DIST / ('manju_迁移包%s_%s.zip' % (suffix, stamp))

    print('收集文件...')
    files, total = collect(with_assets)
    print('  %d 个文件，%.1f MB（压缩前）' % (len(files), total / 1e6))
    print()

    # 按类型统计
    from collections import Counter
    ext = Counter(f.suffix or '(无)' for f in files)
    print('  类型分布:')
    for e, c in ext.most_common(8):
        print('    %-10s %d' % (e, c))
    print()

    print('压缩中...')
    t0 = time.time()
    n = 0
    try:
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            for f in files:
                rel = f.relative_to(ROOT)
                try:
                    z.write(f, str(rel).replace('\\', '/'))
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    print('  ⚠️ 跳过 %s: %s' % (rel, str(exc)[:50]))
    except KeyboardInterrupt:
        print('\n已取消')
        if out.exists():
            out.unlink()
        return 1

    dt = time.time() - t0
    size = out.stat().st_size

    print('  ✅ %d 个文件，用时 %.0f 秒' % (n, dt))
    print()
    print('=' * 70)
    print('  打包完成')
    print('=' * 70)
    print()
    print('  文件: %s' % out.name)
    print('  路径: %s' % out)
    print('  大小: %.1f MB' % (size / 1e6))
    print()

    if not with_assets:
        print('  ⚠️ 不含图片/视频/音频。要连素材一起，加 --with-assets')
        print()

    print('  对方拿到后：')
    print('    1. 解压')
    print('    2. python 安装.py')
    print('    3. 编辑 .env（填自己的 API Key）')
    print('    4. 详见 安装说明.md')
    print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
