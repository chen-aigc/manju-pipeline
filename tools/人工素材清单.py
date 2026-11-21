# -*- coding: utf-8 -*-
"""生成人工素材清单：告诉你哪个文件该放哪里、叫什么名字。

用法：
    python 人工素材清单.py <项目名> [集数]
    python 人工素材清单.py 断剑月白 1
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

BASE = Path(r'D:\deepseek文件区\manju')
sys.path.insert(0, str(BASE / 'src'))

from manju import config  # noqa: E402
import manju.shots as shots_mod  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: python 人工素材清单.py <项目名> [集数]")
        print("例:   python 人工素材清单.py 断剑月白 1")
        return 1

    project = sys.argv[1]
    chapter = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    ch_dir = Path(config.PROJECTS_DIR) / project / ("chapter_%03d" % chapter)
    script_file = ch_dir / "script.json"
    if not script_file.exists():
        print("❌ 剧本不存在:", script_file)
        return 1

    script = json.loads(script_file.read_text(encoding='utf-8'))
    shots = script.get('shots') or []

    # 输出目录
    out_dir = ch_dir / "manual" / "_清单"
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# 人工素材清单 · %s 第 %d 集" % (project, chapter))
    lines.append("")
    lines.append("把文件按下面的名字放进去，系统会自动用你的版本。")
    lines.append("")
    lines.append("## 目录说明")
    lines.append("")
    lines.append("```")
    lines.append("manual/")
    lines.append("├── images/        分镜图（.png / .jpg）")
    lines.append("├── videos_ok/     视频（.mp4 / .mov）")
    lines.append("└── audio/         配音（.mp3 / .wav / .m4a）")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 分镜图")
    lines.append("")
    lines.append("| 放这里 | 镜号 | 景别 | 场景 | 内容 |")
    lines.append("|---|---|---|---|---|")
    for s in shots:
        idx = s.get('index')
        lines.append("| `manual/images/shot_%02d.png` | %d | %s | %s | %s |"
                     % (idx, idx,
                        s.get('shot_type') or '',
                        s.get('scene') or '',
                        (s.get('image_prompt') or '')[:30]))
    lines.append("")
    lines.append("## 视频")
    lines.append("")
    lines.append("| 放这里 | 镜号 | 景别 |")
    lines.append("|---|---|---|")
    for s in shots:
        idx = s.get('index')
        lines.append("| `manual/videos_ok/shot_%02d.mp4` | %d | %s |"
                     % (idx, idx, s.get('shot_type') or ''))
    lines.append("")
    lines.append("## 配音")
    lines.append("")
    lines.append("| 放这里 | 镜号 | 句 | 角色 | 台词 |")
    lines.append("|---|---|---|---|---|")

    n_audio_total = 0
    for s in shots:
        idx = s.get('index')
        try:
            ls = shots_mod.shot_lines(s)
        except Exception:  # noqa: BLE001
            ls = []

        # shot_lines 已经包含旁白，这里不再手动加
        # 句号按管线逻辑：每镜从 1 开始
        for k, (role, text, _emo) in enumerate(ls, 1):
            n_audio_total += 1
            lines.append("| `manual/audio/s%02d_%02d.mp3` | %d | %d | %s | %s |"
                         % (idx, k, idx, k,
                            role or '?', (text or '')[:40]))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 命名规则")
    lines.append("")
    lines.append("- **分镜图**：`shot_<两位镜号>.png`，如 `shot_01.png`")
    lines.append("- **视频**：`shot_<两位镜号>.mp4`")
    lines.append("- **配音**：`s<两位镜号>_<两位句号>.mp3`，如 `s01_02.mp3`")
    lines.append("")
    lines.append("**句号在每一镜内部重新计数**，不是全片连续编号。")
    lines.append("")
    lines.append("## 放好之后")
    lines.append("")
    lines.append("重新跑一次任务（Web 或命令行），系统会：")
    lines.append("1. 优先用你放的文件")
    lines.append("2. 校验文件是否完好")
    lines.append("3. 无效的文件会写进 `manual_todo/` 待办")
    lines.append("4. 其余镜头照常用 AI 生成")

    out_md = out_dir / "清单.md"
    out_md.write_text("\n".join(lines), encoding='utf-8')

    # 顺便建空白目录
    for sub in ('images', 'videos_ok', 'audio'):
        (ch_dir / 'manual' / sub).mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print("人工素材清单")
    print("=" * 66)
    print()
    print("项目: %s  第 %d 集" % (project, chapter))
    print("镜头: %d 个" % len(shots))
    print("音频: %d 条" % n_audio_total)
    print()
    print("清单已生成:")
    print(" ", out_md)
    print()

    # 生成空占位文件（方便用户看文件名）
    print("目录已建好:")
    for sub in ('images', 'videos_ok', 'audio'):
        d = ch_dir / 'manual' / sub
        print("  %s" % d)

    print()
    print("=" * 66)
    print("前 5 条音频的放法:")
    print("=" * 66)
    print()

    cnt = 0
    for s in shots:
        if cnt >= 5:
            break
        idx = s.get('index')
        try:
            ls = shots_mod.shot_lines(s)
        except Exception:  # noqa: BLE001
            ls = []
        for k, (role, text, _emo) in enumerate(ls, 1):
            if cnt >= 5:
                break
            cnt += 1
            print("  s%02d_%02d.mp3   ← [%s] %s"
                  % (idx, k, role or '?', (text or '')[:34]))

    return 0


if __name__ == '__main__':
    sys.exit(main())
