# -*- coding: utf-8 -*-
"""script 插件：mock 与 manual。"""
import json
import time
from pathlib import Path

from ..base import ProviderError, ScriptProvider, ScriptResult
from ..registry import register
from ...core.cost import Cost


def _mock_shots(count=8):
    """构造一段哑分镜，用于空跑测试。"""
    types = ["远景", "中景", "特写", "空镜", "死物", "过肩"]
    shots = []
    for i in range(1, count + 1):
        kind = types[(i - 1) % len(types)]
        shots.append({
            "index": i,
            "shot_type": kind,
            "scene": "测试场景",
            "narration": "第 %d 个镜头的旁白。" % i,
            "dialogues": [{"role": "林越", "text": "第 %d 句台词。" % i,
                           "emotion": "平静"}],
            "image_prompt": "测试画面 %d：少年站在空旷处" % i,
            "image_prompt_en": "young man standing in empty place, shot %d" % i,
            "characters": ["林越"],
            "act": "站立",
        })
    return shots


@register("script", "mock")
class MockScript(ScriptProvider):
    """不花一分钱、不联网，生成结构完整的分镜表。"""

    def available(self):
        return True

    def cost_estimate(self):
        return 0

    def generate(self, source, memory, chapter, target_shots):
        t0 = time.time()
        data = {
            "chapter": chapter,
            "title": "第%d章 空跑测试" % chapter,
            "shots": _mock_shots(min(int(target_shots or 8), 8)),
            "summary": "空跑模式生成的测试摘要。",
            "new_foreshadowing": ["测试伏笔"],
            "character_updates": [],
        }
        if memory is not None:
            try:
                memory.ensure_character("林越", "黑发少年，青绿汉服",
                                        "zh-CN-YunxiNeural")
                memory.set_plot(chapter=chapter, progress="空跑测试")
                memory.add_summary("第%d章：空跑测试" % chapter)
            except Exception:  # noqa: BLE001
                pass
        return ScriptResult(data, Cost.zero(), time.time() - t0)


@register("script", "manual")
class ManualScript(ScriptProvider):
    """人工脚本：读 manual/script.json；没有就让 AI 补。

    fallback_provider 用于"人给结构、AI 填细节"。
    """

    def __init__(self, config=None):
        super().__init__(config)
        self.chapter_dir = None
        self.fallback = None

    def bind(self, chapter_dir):
        self.chapter_dir = Path(chapter_dir)
        return self

    def _manual_path(self):
        if not self.chapter_dir:
            return None
        for name in ("manual/script.json", "manual_script.json"):
            p = self.chapter_dir / name
            if p.exists():
                return p
        return None

    def available(self):
        return True

    def generate(self, source, memory, chapter, target_shots):
        t0 = time.time()
        p = self._manual_path()
        if p is not None:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("shots"):
                    return ScriptResult(data, Cost.zero(), time.time() - t0)
                print("[script/manual] %s 没有 shots，回退到 AI" % p.name)
            except Exception as exc:  # noqa: BLE001
                print("[script/manual] 解析失败：%r，回退到 AI" % (exc,))

        if self.fallback is not None:
            return self.fallback.generate(source, memory, chapter, target_shots)

        raise ProviderError("没有人工脚本，也没有可用的 AI 兜底",
                            provider="manual", retryable=False)
