# -*- coding: utf-8 -*-
"""素材包导出：把一集的全部产物整理成标准化交付包。

目标：交付给外部剪辑团队，对方拿到就能开工，不需要问任何问题。

设计要点：
    1. 目录结构固定且自解释
    2. manifest.json 包含全部元数据（镜号/时长/台词/角色/时间轴）
    3. 提供三种时间轴格式：JSON（程序用）、CSV（Excel 用）、SRT（剪辑软件用）
    4. 附带人可读的说明文档
    5. 可选导入剪映工程文件

产物结构：
    素材包_<项目>_第<XX>集/
    ├── 01_分镜图/         shot_01.png ... （原始高清图）
    ├── 02_视频片段/       shot_01.mp4 ... （生成的视频或图转视频）
    ├── 03_配音/           台词_镜04_月白.mp3 ...
    ├── 04_字幕/           字幕.srt / 字幕.ass
    ├── 05_时间轴/         manifest.json / timeline.csv
    ├── 说明.md            剪辑团队看的操作指南
    └── checklist.txt      自检清单
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config
from . import atomic


@dataclass
class ShotEntry:
    """一个镜头在素材包里的全部信息。"""
    index: int
    shot_type: str = ""            # 远景/中景/特写/过肩/死物/空镜
    scene: str = ""                # 场景名
    characters: List[str] = field(default_factory=list)
    image: str = ""                # 相对路径
    video: str = ""                # 相对路径（可能为空）
    duration: float = 0.0
    narration: str = ""            # 旁白
    dialogues: List[Dict[str, str]] = field(default_factory=list)
    image_prompt: str = ""
    start_time: float = 0.0        # 在整条时间轴上的起始秒
    image_src: Any = None          # 源文件绝对路径
    video_src: Any = None          # 源文件绝对路径


@dataclass
class AudioEntry:
    """一条音频。"""
    file: str = ""                 # 相对路径
    role: str = ""                 # 月白 / 林越 / 旁白
    text: str = ""
    shot: int = 0
    start: float = 0.0
    duration: float = 0.0


class PackBuilder:
    """构建素材包。"""

    def __init__(self, chapter_dir: Path, project: str, chapter: int):
        self.chapter_dir = Path(chapter_dir)
        self.project = project
        self.chapter = int(chapter)

        # 产物目录：按优先级找（不同阶段的产物可能落在不同子目录）
        self.img_dirs = [
            self.chapter_dir / "images_v2",   # 最新一批
            self.chapter_dir / "images",
            self.chapter_dir / "images_new",
        ]
        self.vid_dirs = [
            self.chapter_dir / "videos_v2",
            self.chapter_dir / "videos",
        ]

        # 兼容旧属性的单值形式（供内部使用）
        self.img_dir = self.img_dirs[0]
        self.vid_dir = self.vid_dirs[0]

        # 输出目录
        safe_name = self._safe(project)
        self.pack_name = "素材包_%s_第%02d集" % (safe_name, self.chapter)
        self.pack_dir = self.chapter_dir / self.pack_name

        self.shots: List[ShotEntry] = []
        self.audio: List[AudioEntry] = []
        self.warnings: List[str] = []

    @staticmethod
    def _safe(name: str) -> str:
        """把项目名转成安全的目录名。"""
        bad = set('\\/:*?"<>|')
        return "".join(c if c not in bad else "_" for c in str(name))[:40]

    # ---------------- 构建 ----------------

    def load_script(self) -> Optional[Dict[str, Any]]:
        """读剧本。"""
        p = self.chapter_dir / "script.json"
        if not p.exists():
            self.warnings.append("剧本不存在：%s" % p.name)
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.warnings.append("剧本读取失败：%r" % exc)
            return None

    def scan(self) -> None:
        """扫描现有产物，填充 shots / audio 列表。"""
        script = self.load_script()
        if not script:
            return

        raw_shots = script.get("shots") or []
        t = 0.0

        for s in raw_shots:
            idx = int(s.get("index") or 0)
            if idx <= 0:
                continue

            dur = float(s.get("duration") or 4.0)

            entry = ShotEntry(
                index=idx,
                shot_type=str(s.get("shot_type") or ""),
                scene=str(s.get("scene") or ""),
                characters=list(s.get("characters") or []),
                duration=dur,
                narration=str(s.get("narration") or ""),
                dialogues=list(s.get("dialogues") or []),
                image_prompt=str(s.get("image_prompt") or ""),
                start_time=round(t, 3),
            )

            # 找图（按目录优先级找第一个存在的）
            for d in self.img_dirs:
                if not d.exists():
                    continue
                found = None
                for cand in (d / ("shot_%02d.png" % idx),
                             d / ("shot_%03d.png" % idx)):
                    if cand.exists():
                        found = cand
                        break
                if found:
                    entry.image = "01_分镜图/%s" % found.name
                    entry.image_src = found
                    break
            if not entry.image:
                self.warnings.append("镜 %02d 缺图片" % idx)

            # 找视频
            for d in self.vid_dirs:
                if not d.exists():
                    continue
                found = None
                for cand in (d / ("shot_%02d.mp4" % idx),
                             d / ("shot_%03d.mp4" % idx)):
                    if cand.exists():
                        found = cand
                        break
                if found:
                    entry.video = "02_视频片段/%s" % found.name
                    entry.video_src = found
                    break

            self.shots.append(entry)
            t += dur

    # ---------------- 导出 ----------------

    def build(self, *, zip_it: bool = True,
              with_video: bool = True) -> Dict[str, Any]:
        """构建素材包。

        返回摘要：{ok, pack_dir, zip_path, shots, audio, warnings}
        """
        self.scan()

        if not self.shots:
            return {"ok": False, "error": "没有任何镜头",
                    "warnings": self.warnings}

        # 清空旧包
        if self.pack_dir.exists():
            shutil.rmtree(self.pack_dir, ignore_errors=True)

        dirs = {
            "img": self.pack_dir / "01_分镜图",
            "vid": self.pack_dir / "02_视频片段",
            "aud": self.pack_dir / "03_配音",
            "sub": self.pack_dir / "04_字幕",
            "tl": self.pack_dir / "05_时间轴",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)

        n_img = n_vid = 0

        # 1. 复制图片
        for s in self.shots:
            src = s.image_src
            if src and Path(src).exists():
                shutil.copy2(src, dirs["img"] / Path(src).name)
                n_img += 1

        # 2. 复制视频
        if with_video:
            for s in self.shots:
                src = s.video_src
                if src and Path(src).exists():
                    shutil.copy2(src, dirs["vid"] / Path(src).name)
                    n_vid += 1

        # 3. 收集音频
        self._collect_audio(dirs["aud"])

        # 4. 字幕
        self._write_subtitles(dirs["sub"])

        # 5. 时间轴
        self._write_timeline(dirs["tl"])

        # 6. 说明文档
        self._write_readme()

        # 7. 打包
        zip_path = None
        if zip_it:
            zip_path = self._make_zip()

        return {
            "ok": True,
            "pack_dir": str(self.pack_dir),
            "zip_path": str(zip_path) if zip_path else None,
            "shots": len(self.shots),
            "images": n_img,
            "videos": n_vid,
            "audio": len(self.audio),
            "warnings": self.warnings,
        }

    def _collect_audio(self, dest: Path) -> None:
        """收集配音/旁白音频。"""
        # 1. 优先用 _timeline_full.json（含精确时间点）
        base = Path(getattr(config, "ROOT", self.chapter_dir.parent.parent))
        candidates_tl = [
            base / "_timeline_full.json",
            self.chapter_dir.parent.parent.parent / "_timeline_full.json",
            self.chapter_dir / "_timeline_full.json",
        ]
        for tl_file in candidates_tl:
            if not tl_file.exists():
                continue
            try:
                tl = json.loads(tl_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue

            n = 0
            for item in tl:
                src = Path(item.get("file") or "")
                if not src.exists():
                    continue
                role = str(item.get("type") or "unknown")
                text = str(item.get("text") or "")
                shot = int(item.get("shot") or 0)
                # 文件名：角色_镜04_文本前8字.mp3
                safe_text = self._safe(text[:8]).strip() or "audio"
                name = "%s_镜%02d_%s.mp3" % (role, shot, safe_text)
                shutil.copy2(src, dest / name)
                self.audio.append(AudioEntry(
                    file="03_配音/%s" % name,
                    role=role, text=text, shot=shot,
                    start=float(item.get("start") or 0),
                    duration=float(item.get("dur") or 0),
                ))
                n += 1
            if n:
                return

        # 2. 退而求其次：扫 chapter 下的 audio 目录
        for d in (self.chapter_dir / "audio", self.chapter_dir / "voice"):
            if not d.exists():
                continue
            for f in sorted(d.glob("*.mp3")):
                shutil.copy2(f, dest / f.name)
                self.audio.append(AudioEntry(file="03_配音/%s" % f.name))
            if self.audio:
                return

        # 3. 再退：扫根目录的 _tts_* 目录
        for d in sorted(base.glob("_tts_*")):
            if not d.is_dir():
                continue
            mp3s = sorted(d.glob("*.mp3"))
            if not mp3s:
                continue
            # 优先用「管线验证2」（最新的正式版）
            if "管线验证2" not in d.name and any(
                    "管线验证2" in x.name for x in base.glob("_tts_*")):
                continue
            for f in mp3s:
                shutil.copy2(f, dest / f.name)
                self.audio.append(AudioEntry(file="03_配音/%s" % f.name))
            if self.audio:
                return

    def _write_subtitles(self, dest: Path) -> None:
        """生成 SRT 字幕。"""
        lines = []
        n = 0
        for s in self.shots:
            texts = []
            if s.narration:
                texts.append(s.narration)
            for d in s.dialogues:
                t = str(d.get("text") or "").strip()
                if t:
                    texts.append("%s：%s" % (d.get("role", ""), t)
                                 if d.get("role") else t)
            if not texts:
                continue

            n += 1
            start = self._srt_time(s.start_time)
            end = self._srt_time(s.start_time + s.duration * 0.8)
            lines.append(str(n))
            lines.append("%s --> %s" % (start, end))
            lines.append(" ".join(texts))
            lines.append("")

        if lines:
            atomic.atomic_write_text(
                dest / "字幕.srt", "\n".join(lines), encoding="utf-8-sig")
            atomic.atomic_write_text(
                dest / "字幕_纯台词.srt",
                "\n".join(self._dialogue_only_srt()), encoding="utf-8-sig")

    def _dialogue_only_srt(self) -> List[str]:
        """只含台词的 SRT（不含旁白）。"""
        lines = []
        n = 0
        for s in self.shots:
            for d in s.dialogues:
                t = str(d.get("text") or "").strip()
                if not t:
                    continue
                n += 1
                lines.append(str(n))
                lines.append("%s --> %s" % (
                    self._srt_time(s.start_time),
                    self._srt_time(s.start_time + s.duration * 0.7)))
                lines.append(t)
                lines.append("")
        return lines

    @staticmethod
    def _srt_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

    def _write_timeline(self, dest: Path) -> None:
        """写 manifest.json 和 timeline.csv。"""
        manifest = {
            "project": self.project,
            "chapter": self.chapter,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_duration": round(
                sum(s.duration for s in self.shots), 3),
            "shot_count": len(self.shots),
            "shots": [
                {
                    "index": s.index,
                    "start": s.start_time,
                    "duration": s.duration,
                    "shot_type": s.shot_type,
                    "scene": s.scene,
                    "characters": s.characters,
                    "image": s.image,
                    "video": s.video,
                    "narration": s.narration,
                    "dialogues": s.dialogues,
                }
                for s in self.shots
            ],
            "audio": [
                {
                    "file": a.file, "role": a.role, "text": a.text,
                    "shot": a.shot, "start": a.start,
                    "duration": a.duration,
                }
                for a in self.audio
            ],
        }
        atomic.atomic_write_text(
            dest / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")

        # CSV（Excel 友好）
        rows = ["镜号,起始秒,时长秒,景别,场景,角色,画面文件,视频文件,台词"]
        for s in self.shots:
            dial = " / ".join(
                "%s:%s" % (d.get("role", ""), d.get("text", ""))
                for d in s.dialogues) or ""
            rows.append('%d,%.2f,%.2f,%s,%s,%s,%s,%s,"%s"' % (
                s.index, s.start_time, s.duration,
                s.shot_type, s.scene,
                "/".join(s.characters),
                Path(s.image).name if s.image else "",
                Path(s.video).name if s.video else "",
                dial.replace('"', '"')))
        atomic.atomic_write_text(
            dest / "timeline.csv", "\n".join(rows), encoding="utf-8-sig")

    def _write_readme(self) -> None:
        total = sum(s.duration for s in self.shots)
        n_vid = len([s for s in self.shots if s.video])

        text = """# 素材包 · {project} 第 {ch} 集

**生成时间**：{ts}
**镜头数**：{n} 个
**总时长**：{dur:.1f} 秒
**有视频的镜头**：{nv} 个（其余为静态图）

---

## 目录内容

```
01_分镜图/     每镜一张 PNG（1080×1920 竖版高清）
02_视频片段/   每镜一段 MP4（4 秒，720p，24fps）
03_配音/       台词和旁白（MP3）
04_字幕/       字幕.srt（全部）/ 字幕_纯台词.srt（只要台词）
05_时间轴/     manifest.json（程序用）/ timeline.csv（Excel 用）
```

---

## 在剪映里怎么用

1. **新建项目** → 比例选 **9:16**（1080×1920）
2. **导入视频**：把 `02_视频片段/` 里所有 MP4 拖进轨道，按文件名顺序
3. **导入字幕**：右键 → 导入字幕 → 选 `04_字幕/字幕.srt`
4. **导入配音**：`03_配音/` 里的 MP3 拖到音频轨
5. **对齐节奏**：参考 `05_时间轴/timeline.csv` 里的起始秒
6. **精剪**：加音效、调色、转场、变速

**注意**：如果某个镜头没有视频，用 `01_分镜图/` 里对应的图，
在剪映里加个「轻微放大」动效就行。

---

## 在 Premiere / 达芬奇里怎么用

导入 `02_视频片段/` 全部文件，按文件名排序即可。
`manifest.json` 可以用脚本解析出精确的时间轴。

---

## 时间轴格式

`timeline.csv` 每行一个镜头：

| 列 | 含义 |
|---|---|
| 镜号 | 1 到 40 |
| 起始秒 | 在整集里的开始时间 |
| 时长秒 | 该镜头持续多久 |
| 景别 | 远景/中景/特写/过肩/死物/空镜 |
| 场景 | 院门外/破屋屋内 等 |
| 角色 | 该镜头出现的人物 |
| 画面文件 | 对应的 PNG 文件名 |
| 视频文件 | 对应的 MP4 文件名（可能为空） |
| 台词 | 该镜头的对白 |

---

## 素材规格

| 项 | 规格 |
|---|---|
| 图片 | 1080×1920 PNG，竖版 |
| 视频 | 720×1280 MP4，24fps，H.264 |
| 音频 | MP3，24kHz |
| 字幕 | SRT（UTF-8 with BOM，剪映可直接导入） |

---

## 交付前自检

- [ ] 每个镜头都有对应的图
- [ ] 视频文件名和镜号对得上
- [ ] 字幕时间轴和画面同步
- [ ] 配音没有缺句
- [ ] 总时长和预期一致

---

## 需要重新生成某个镜头

联系制作方，提供镜号即可。
""".format(
            project=self.project, ch=self.chapter,
            ts=time.strftime("%Y-%m-%d %H:%M:%S"),
            n=len(self.shots), dur=total, nv=n_vid)

        atomic.atomic_write_text(
            self.pack_dir / "说明.md", text, encoding="utf-8")

        # 自检清单
        check = ["素材包自检", "=" * 40, ""]
        miss_img = [s.index for s in self.shots if not s.image]
        miss_vid = [s.index for s in self.shots if not s.video]
        check.append("镜头总数: %d" % len(self.shots))
        check.append("有图片: %d" % (len(self.shots) - len(miss_img)))
        check.append("有视频: %d" % (len(self.shots) - len(miss_vid)))
        check.append("音频条数: %d" % len(self.audio))
        check.append("")
        if miss_img:
            check.append("缺图片的镜头: %s"
                         % ", ".join("%02d" % i for i in miss_img))
        if miss_vid:
            check.append("缺视频的镜头（用静态图代替）: %s"
                         % ", ".join("%02d" % i for i in miss_vid))
        if self.warnings:
            check.append("")
            check.append("警告:")
            check.extend("  - " + w for w in self.warnings)

        atomic.atomic_write_text(
            self.pack_dir / "checklist.txt", "\n".join(check),
            encoding="utf-8")

    def _make_zip(self) -> Path:
        """打包成 zip。"""
        zpath = self.chapter_dir / (self.pack_name + ".zip")
        if zpath.exists():
            zpath.unlink()

        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            for f in self.pack_dir.rglob("*"):
                if f.is_file():
                    arc = f.relative_to(self.pack_dir)
                    z.write(f, str(arc).replace("\\", "/"))

        return zpath


def export_pack(project: str, chapter: int,
                chapter_dir: Optional[Path] = None,
                *, zip_it: bool = True,
                with_video: bool = True) -> Dict[str, Any]:
    """对外接口：导出素材包。"""
    if chapter_dir is None:
        base = Path(getattr(config, "PROJECTS_DIR",
                            Path(config.ROOT) / "projects"))
        chapter_dir = base / project / ("chapter_%03d" % int(chapter))

    chapter_dir = Path(chapter_dir)
    if not chapter_dir.exists():
        return {"ok": False, "error": "章节目录不存在：%s" % chapter_dir}

    b = PackBuilder(chapter_dir, project, chapter)
    return b.build(zip_it=zip_it, with_video=with_video)
