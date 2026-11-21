# -*- coding: utf-8 -*-
"""剧情状态追踪：解决"连续剧情不崩"。

问题：
    AI 每镜独立生成，不知道上一镜发生了什么
    → 镜 03 脸上有血，镜 15 脸上干干净净
    → 服装、伤痕、光线全断

方案：
    每镜生成后记录角色状态快照，
    下一镜的提示词自动带上上一镜的状态。

状态字段：
    clothing      服装描述
    wounds        伤痕列表
    dirt          污迹列表（雪、泥、血渍）
    emotion       情绪
    hair_state    头发状态（束起/散开/湿）
    light         所处光线
    time_of_day   时间
    location      地点

持久规则（config/state_rules.yaml）：
    某些状态必须持续，直到特定事件发生才改变
    例：wounds_persist - 伤痕必须持续到"治愈"事件
"""
import json
import time
from pathlib import Path

from . import atomic, integrity


# ---------------------------------------------------------------- 默认规则
DEFAULT_RULES = {
    "rules": [
        {
            "name": "wounds_persist",
            "description": "伤痕必须持续，直到剧情节点明确治愈",
            "field": "wounds",
            "persist": True,
            "cleared_by": ["治疗", "包扎", "时间跳跃", "换装"],
        },
        {
            "name": "dirt_persist",
            "description": "污迹（雪、泥）必须持续到明确清理",
            "field": "dirt",
            "persist": True,
            "cleared_by": ["换装", "进室内取暖", "时间跳跃"],
        },
        {
            "name": "clothing_persist",
            "description": "服装不变，直到明确换装",
            "field": "clothing",
            "persist": True,
            "cleared_by": ["换装", "时间跳跃"],
        },
        {
            "name": "hair_persist",
            "description": "发型不变，直到明确改变",
            "field": "hair_state",
            "persist": True,
            "cleared_by": ["梳洗", "时间跳跃"],
        },
    ]
}


class StoryState:
    """一集的剧情状态。

    存储结构：
        {
          "shots": {
            "shot_003": {
              "characters": {
                "林越": {"clothing": "...", "wounds": [...], ...}
              },
              "scene": "院门外",
              "light": "冷蓝夜色",
              "time_of_day": "night"
            }
          }
        }
    """

    def __init__(self, path, rules_path=None):
        self.path = Path(path)
        self.bak = self.path.with_name(self.path.name + ".bak")
        self.new = self.path.with_name(self.path.name + ".new")
        self.rules = self._load_rules(rules_path)
        self.data = self._empty()
        self.load()

    def _empty(self):
        return {
            "chapter": 0,
            "shots": {},
            "updated_at": "",
        }

    def _load_rules(self, rules_path):
        if not rules_path:
            return DEFAULT_RULES
        p = Path(rules_path)
        if not p.exists():
            return DEFAULT_RULES
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return DEFAULT_RULES

    def load(self):
        for src in (self.path, self.bak):
            if not src.exists():
                continue
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.data = dict(self._empty(), **data)
                    self.data.setdefault("shots", {})
                    return
            except Exception:  # noqa: BLE001
                pass
        self.data = self._empty()

    def save(self):
        self.data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        self.new.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_text(self.new, text, verify=integrity.validate_json)
        if self.path.exists():
            try:
                self.path.replace(self.bak)
            except OSError:
                pass
        self.new.replace(self.path)
        return self.path

    # ---------------- 记录 ----------------
    def record_shot(self, shot_id, characters, scene="", light="",
                    time_of_day="", location="", events=None):
        """记录一镜的状态。

        Args:
            shot_id:     "shot_003"
            characters:  {"林越": {"clothing": "...", "wounds": [...]}}
            scene:       场景描述
            light:       光线
            time_of_day: 时间
            location:    地点
            events:      本镜发生的事件（如 ["包扎伤口"]），用于清除状态
        """
        self.data["shots"][shot_id] = {
            "characters": characters,
            "scene": scene,
            "light": light,
            "time_of_day": time_of_day,
            "location": location,
            "events": events or [],
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.save()

    def get_shot(self, shot_id):
        return self.data["shots"].get(shot_id) or {}

    def prev_shot(self, shot_id):
        """找上一镜（按 shot 编号排序）。"""
        ids = sorted(self.data["shots"].keys())
        try:
            idx = ids.index(shot_id)
        except ValueError:
            # 没找到，取最后一个
            return ids[-1] if ids else None
        return ids[idx - 1] if idx > 0 else None

    # ---------------- 核心：生成下一镜的提示词 ----------------
    def build_prompt(self, shot_id, prompt_characters, extra_events=None):
        """根据上一镜的状态，生成这一镜的提示词。

        Args:
            shot_id:            当前镜 id
            prompt_characters:  这一镜出现的角色列表 ["林越", "月白"]
            extra_events:       这一镜发生的事件（会清除对应状态）

        Returns:
            (prompt_addon, warnings)
                prompt_addon: 要追加到提示词的字符串
                warnings:     状态冲突警告列表
        """
        prev_id = self.prev_shot(shot_id)
        if not prev_id:
            return "", []

        prev = self.get_shot(prev_id)
        prev_chars = prev.get("characters") or {}
        events = list(prev.get("events") or []) + list(extra_events or [])

        parts = []
        warnings = []

        for name in prompt_characters:
            st = prev_chars.get(name)
            if not st:
                warnings.append("角色 %s 在上一镜没有记录，无法传递状态" % name)
                continue

            # 逐字段检查持久规则
            for rule in self.rules.get("rules", []):
                if not rule.get("persist"):
                    continue
                field = rule.get("field")
                val = st.get(field)
                if not val:
                    continue

                # 检查是否被事件清除
                cleared = rule.get("cleared_by") or []
                hit = [e for e in events if e in cleared]
                if hit:
                    continue

                # 加到提示词
                if isinstance(val, list):
                    if val:
                        parts.append("%s 保持 %s：%s" % (name, field, "、".join(val)))
                else:
                    parts.append("%s 保持 %s：%s" % (name, field, val))

        # 光线/时间也传递
        if prev.get("light"):
            parts.append("光线保持：%s" % prev["light"])
        if prev.get("location"):
            parts.append("地点：%s" % prev["location"])

        return "；".join(parts), warnings

    def check_conflicts(self, shot_id, new_state):
        """检查新状态与上一镜是否冲突（用于生成后校验）。

        Returns:
            冲突列表 [{"field": ..., "prev": ..., "new": ..., "reason": ...}]
        """
        prev_id = self.prev_shot(shot_id)
        if not prev_id:
            return []

        prev = self.get_shot(prev_id)
        prev_chars = prev.get("characters") or {}
        conflicts = []

        for name, new_st in (new_state.get("characters") or {}).items():
            prev_st = prev_chars.get(name)
            if not prev_st:
                continue

            for rule in self.rules.get("rules", []):
                if not rule.get("persist"):
                    continue
                field = rule.get("field")
                old_v = prev_st.get(field)
                new_v = new_st.get(field)
                if old_v == new_v:
                    continue
                if not old_v and not new_v:
                    continue

                # 有变化：检查是否允许
                cleared = rule.get("cleared_by") or []
                events = new_state.get("events") or []
                if any(e in cleared for e in events):
                    continue

                conflicts.append({
                    "character": name,
                    "field": field,
                    "prev": old_v,
                    "new": new_v,
                    "reason": rule.get("description", ""),
                })

        return conflicts

    def summary(self):
        """给人工看的摘要。"""
        lines = []
        ids = sorted(self.data["shots"].keys())
        for sid in ids:
            s = self.data["shots"][sid]
            chars = s.get("characters") or {}
            desc = []
            for name, st in chars.items():
                bits = []
                if st.get("clothing"):
                    bits.append(st["clothing"])
                if st.get("wounds"):
                    bits.append("伤:" + "/".join(st["wounds"]))
                if bits:
                    desc.append("%s(%s)" % (name, ",".join(bits)))
            lines.append("%s | %s | %s" % (sid, s.get("scene", ""), " ".join(desc)))
        return "\n".join(lines)

    def __repr__(self):
        return "<StoryState %s shots=%d>" % (
            self.path.parent.name, len(self.data.get("shots", {})))
