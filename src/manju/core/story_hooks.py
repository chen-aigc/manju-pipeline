# -*- coding: utf-8 -*-
"""把状态追踪和一致性检查接进管线。

提供两个钩子：
    before_shot(shot, story_state)  → 生成前，把上一镜状态拼进提示词
    after_shot(shot, story_state)   → 生成后，记录状态 + 检查一致性

用法（在 orchestrator 或 pipeline 里调用）：
    from manju.core.story_hooks import StoryHooks

    hooks = StoryHooks(chapter_dir, chars_dir)
    prompt = hooks.enrich_prompt(shot, base_prompt)   # 生成前
    result = hooks.record_and_check(shot, img_path, state)  # 生成后
"""
import json
import os
from pathlib import Path

from .story_state import StoryState
from .consistency import ConsistencyChecker, THRESHOLD_OK, THRESHOLD_WARN


class StoryHooks:
    """剧情连贯的挂钩。

    Args:
        chapter_dir:  chapter_001 目录
        chars_dir:    角色定妆照目录（用于一致性检查的基准）
    """

    def __init__(self, chapter_dir, chars_dir=None, enabled=True):
        self.chapter_dir = Path(chapter_dir)
        self.chars_dir = Path(chars_dir) if chars_dir else None
        self.enabled = enabled

        self.state_path = self.chapter_dir / "story_state.json"
        self.rules_path = self.chapter_dir / "state_rules.json"
        self.report_path = self.chapter_dir / "consistency_report.json"

        self.state = StoryState(self.state_path, self.rules_path)
        self._checkers = {}    # 角色名 → ConsistencyChecker

    # ---------------- 基准图 ----------------
    def _checker_for(self, char_name):
        """按角色取（缓存的）检查器。"""
        if char_name in self._checkers:
            return self._checkers[char_name]

        ref = None
        if self.chars_dir and self.chars_dir.exists():
            # 找 <角色名>_*.png 或 <角色名>/*.png
            candidates = []
            for pat in ("%s_*.png" % char_name, "%s_*.jpg" % char_name):
                candidates.extend(sorted(self.chars_dir.glob(pat)))
            sub = self.chars_dir / char_name
            if sub.exists():
                candidates.extend(sorted(sub.glob("*.png")))
                candidates.extend(sorted(sub.glob("*.jpg")))
            if candidates:
                ref = candidates[0]

        ck = ConsistencyChecker(ref if ref and ref.exists() else None)
        self._checkers[char_name] = ck
        return ck

    # ---------------- 生成前：拼提示词 ----------------
    def enrich_prompt(self, shot, base_prompt, extra_events=None):
        """把上一镜的状态拼进这一镜的提示词。

        Args:
            shot:         分镜 dict，含 index / shot_type / characters
            base_prompt:  原本的提示词
            extra_events: 这一镜发生的事件

        Returns:
            (enriched_prompt, warnings)
        """
        if not self.enabled:
            return base_prompt, []

        sid = shot.get("id") or "shot_%03d" % int(shot.get("index", 0))
        chars = shot.get("characters") or []
        if isinstance(chars, str):
            chars = [c.strip() for c in chars.split(",") if c.strip()]

        addon, warns = self.state.build_prompt(
            sid, chars, extra_events=extra_events)

        if not addon:
            return base_prompt, warns

        enriched = "%s\n\n【延续上一镜的状态】%s" % (base_prompt, addon)
        return enriched, warns

    # ---------------- 生成后：记录 + 检查 ----------------
    def record_and_check(self, shot, img_path, state=None, events=None):
        """记录这一镜的状态，并做一致性检查。

        Args:
            shot:     分镜 dict
            img_path: 生成的图片路径
            state:    这一镜的状态（dict），不传则自动从 shot 推断
            events:   这一镜发生的事件

        Returns:
            dict: {"consistency": [...], "recorded": bool, "level": "OK"/...}
        """
        if not self.enabled:
            return {"consistency": [], "recorded": False, "level": "OK"}

        sid = shot.get("id") or "shot_%03d" % int(shot.get("index", 0))
        chars = shot.get("characters") or []
        if isinstance(chars, str):
            chars = [c.strip() for c in chars.split(",") if c.strip()]

        # 1. 一致性检查
        results = []
        worst = "OK"
        for name in chars:
            ck = self._checker_for(name)
            if ck.ref_fp is None:
                continue
            r = ck.check_shot(sid, img_path)
            results.append(r.to_dict())
            if r.level == "FAIL":
                worst = "FAIL"
            elif r.level == "WARN" and worst == "OK":
                worst = "WARN"

        # 2. 记录状态
        recorded = False
        if state is not None:
            prev_id = self.state.prev_shot(sid)
            if prev_id:
                conflicts = self.state.check_conflicts(sid, state)
                if conflicts:
                    for c in conflicts:
                        results.append({
                            "level": "WARN",
                            "shot_id": sid,
                            "notes": ["剧情冲突：%s 的 %s 从 %r 变为 %r（%s）"
                                      % (c["character"], c["field"],
                                         c["prev"], c["new"], c["reason"])],
                        })
                    if worst == "OK":
                        worst = "WARN"

            self.state.record_shot(
                sid,
                characters=state.get("characters", {}),
                scene=state.get("scene", ""),
                light=state.get("light", ""),
                location=state.get("location", ""),
                time_of_day=state.get("time_of_day", ""),
                events=events or state.get("events"),
            )
            recorded = True

        return {
            "consistency": results,
            "recorded": recorded,
            "level": worst,
        }

    # ---------------- 整批审核 ----------------
    def audit_episode(self, shots, write_report=True):
        """整集一致性审核（不需要基准，批内找异类）。

        Args:
            shots: [{"id": ..., "path": ...}, ...]

        Returns:
            BatchReport
        """
        # 用一个无基准的检查器做批内审计
        ck = ConsistencyChecker(None)
        report = ck.audit_batch(shots)

        if write_report:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        return report

    def summary(self):
        """给 CLI 输出用。"""
        return self.state.summary()
