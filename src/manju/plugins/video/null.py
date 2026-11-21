# -*- coding: utf-8 -*-
"""video 插件：null（不做视频）、manual（人工补做）。"""
import json
import time
from pathlib import Path

from ..base import VideoProvider, VideoResult
from ..registry import register
from ...core.cost import Cost


@register("video", "null")
class NullVideo(VideoProvider):
    """声明式降级：不生成视频，由初剪层用静态图推拉代替。

    返回 pending=False + path=None，编排层据此把状态标为 degraded。
    """

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None):
        return VideoResult(path=None, task_id="", pending=False,
                           duration=duration, cost=Cost.zero(), elapsed=0.0)


@register("video", "manual")
class ManualVideo(VideoProvider):
    """人工视频：把待做清单写进 manual_todo/，人工补完放回 manual/videos_ok/。

    generate() 时：
        - manual/videos_ok/shot_XX.mp4 存在 → 直接用（adopt）
        - 否则写入待办，返回 pending
    导入结果后再次运行即可自动接上。
    """

    def __init__(self, config=None):
        super().__init__(config)
        self.videos_dir = None
        self.todo_path = None
        self.fallback = None

    def bind(self, videos_dir, todo_path=None, fallback=None):
        self.videos_dir = Path(videos_dir)
        self.todo_path = Path(todo_path) if todo_path else \
            self.videos_dir.parent.parent / "manual_todo" / "videos.json"
        self.fallback = fallback
        return self

    def available(self):
        return self.videos_dir is not None

    def _todo_file(self, shot_index):
        if not self.todo_path:
            return None
        for ext in (".mp4", ".mov"):
            cand = self.videos_dir / ("shot_%02d%s" % (int(shot_index), ext))
            if cand.exists():
                return cand
        return None

    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None):
        t0 = time.time()
        shot = self.config.get("shot_index")
        out = self.config.get("out_path")

        hit = self._todo_file(shot) if shot is not None else None
        if hit is not None:
            from ...core import atomic
            target = Path(out) if out else hit
            if Path(out) != hit:
                atomic.atomic_copy(hit, target)
            return VideoResult(path=target, task_id="", pending=False,
                               duration=duration, cost=Cost.zero(),
                               elapsed=time.time() - t0)

        # 记待办
        self._append_todo({
            "shot": shot,
            "prompt": prompt,
            "image": str(image) if image else "",
            "duration": duration,
            "out_path": str(out) if out else "",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

        if self.fallback is not None:
            return self.fallback.generate(image, prompt, duration,
                                          first_frame, last_frame)

        return VideoResult(path=None, task_id="", pending=True,
                           duration=duration, cost=Cost.zero(),
                           elapsed=time.time() - t0)

    def _append_todo(self, item):
        if not self.todo_path:
            return
        self.todo_path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        if self.todo_path.exists():
            try:
                data = json.loads(self.todo_path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:  # noqa: BLE001
                data = []
        data.append(item)
        from ...core import atomic
        atomic.atomic_write_text(
            self.todo_path,
            json.dumps(data, ensure_ascii=False, indent=2))

    def query(self, task_id):
        return VideoResult(pending=False)
