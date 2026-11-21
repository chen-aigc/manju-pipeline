# -*- coding: utf-8 -*-
"""长期记忆库（memory.json）

六类记忆：
    characters  角色：外貌 / seed / 音色 / 处境 / 关系
    props       道具：外观 / 持有者 / 位置 / 历史
    world       世界观：地点 / 势力 / 规则
    visual      视觉资产：参考图 / LoRA / 已验证好图
    production  制作状态：每集进度 / 失败 / 成本
    plot        剧情：时间线 / 摘要 / 伏笔

设计约束：
    1. context() 组装后必须硬截断（字符级），绝不超限
    2. 文件损坏时降级为空记忆，不阻断流程
    3. 保存走原子写，避免写坏
"""
import json
import random
import time
from pathlib import Path

from .core import atomic

# 保留旧结构里的键，向前兼容
_CATEGORIES = ("characters", "props", "world", "visual", "production", "plot")

DEFAULT_MAX_CHARS = 1200


def _empty(project=""):
    return {
        "project": project,
        "characters": {},
        "props": {},
        "world": {"locations": {}, "factions": {}, "rules": []},
        "visual": {"style_anchor": "", "refs": {}, "approved": [], "lora": {}},
        "production": {},
        "plot": {"chapter": 0, "progress": "", "foreshadowing": [],
                 "timeline": []},
        "rolling_summary": [],
        "updated_at": "",
        "corrupt": False,
    }


class Memory:
    def __init__(self, path, project=None):
        self.path = Path(path)
        self.bak = self.path.with_name(self.path.name + ".bak")
        self.project = project or self.path.parent.name
        self.data = _empty(self.project)
        self.corrupt = False
        self._load()

    # ---------------- 读写 ----------------
    def _load(self):
        """主文件 → 备份 → 空。损坏时打标记，不抛异常。"""
        for src in (self.path, self.bak):
            if not src.exists():
                continue
            try:
                raw = json.loads(src.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("顶层不是对象")
                merged = _empty(self.project)
                merged.update(raw)
                # 补齐缺失的类别，避免旧文件缺字段
                for k in _CATEGORIES:
                    if not isinstance(merged.get(k), dict):
                        merged[k] = _empty(self.project)[k]
                self.data = merged
                if src is self.bak:
                    print("[memory] 主文件损坏，已从备份恢复")
                return
            except Exception as exc:  # noqa: BLE001
                print("[memory] %s 解析失败：%r" % (src.name, exc))

        if self.path.exists():
            self.corrupt = True
            self.data["corrupt"] = True
            print("[memory] 记忆文件已损坏，本集使用空记忆继续"
                  "（角色一致性可能受影响）")

    def save(self):
        import json as _json
        self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        text = _json.dumps(self.data, ensure_ascii=False, indent=2)
        atomic.atomic_write_text(self.path, text)
        return self.path

    # ---------------- 角色 ----------------
    def ensure_character(self, name, appearance="", voice=""):
        """确保角色存在，并分配固定 Seed（保证人脸一致）。"""
        chars = self.data["characters"]
        if name not in chars:
            chars[name] = {
                "appearance_prompt": appearance,
                "appearance_en": "",
                "state": "",
                "seed": random.randint(100000, 999999),
                "voice": voice,
                "ref_image": "",
                "relations": {},
                "first_seen": self.data["plot"].get("chapter", 0) or 1,
            }
            return chars[name], True

        ch = chars[name]
        if appearance and not ch.get("appearance_prompt"):
            ch["appearance_prompt"] = appearance
        if voice and not ch.get("voice"):
            ch["voice"] = voice
        if not ch.get("seed"):
            ch["seed"] = random.randint(100000, 999999)
        return ch, False

    def get_seed(self, name):
        return (self.data["characters"].get(name) or {}).get("seed")

    def get_character(self, name):
        return self.data["characters"].get(name) or {}

    def set_state(self, name, state):
        if name in self.data["characters"]:
            self.data["characters"][name]["state"] = state

    def set_relation(self, a, b, relation):
        chars = self.data["characters"]
        if a in chars:
            chars[a].setdefault("relations", {})[b] = relation

    # ---------------- 道具 ----------------
    def ensure_prop(self, name, appearance_en="", holder="", location=""):
        props = self.data["props"]
        if name not in props:
            props[name] = {
                "appearance_en": appearance_en,
                "holder": holder,
                "location": location,
                "history": [],
            }
            return props[name], True
        p = props[name]
        if appearance_en and not p.get("appearance_en"):
            p["appearance_en"] = appearance_en
        if holder:
            p["holder"] = holder
        if location:
            p["location"] = location
        return p, False

    def log_prop_event(self, name, chapter, event):
        p = self.data["props"].get(name)
        if not p:
            return
        p.setdefault("history", []).append({"chapter": chapter, "event": event})

    # ---------------- 世界观 ----------------
    def add_location(self, name, desc=""):
        self.data["world"].setdefault("locations", {})[name] = desc

    def add_rule(self, rule):
        rules = self.data["world"].setdefault("rules", [])
        if rule and rule not in rules:
            rules.append(rule)

    # ---------------- 视觉资产 ----------------
    def set_ref_image(self, name, path):
        self.data["visual"].setdefault("refs", {})[name] = str(path)

    def approve_image(self, shot_index, path):
        item = {"shot": int(shot_index), "path": str(path)}
        self.data["visual"].setdefault("approved", []).append(item)

    def set_lora(self, name, path):
        self.data["visual"].setdefault("lora", {})[name] = str(path)

    # ---------------- 制作状态 ----------------
    def update_production(self, chapter, **fields):
        key = "chapter_%03d" % int(chapter)
        rec = self.data["production"].setdefault(key, {})
        rec.update(fields)

    def get_production(self, chapter):
        return self.data["production"].get("chapter_%03d" % int(chapter)) or {}

    # ---------------- 剧情 ----------------
    def set_plot(self, chapter=None, progress=None, foreshadowing=None):
        plot = self.data["plot"]
        if chapter is not None:
            plot["chapter"] = int(chapter)
        if progress is not None:
            plot["progress"] = progress
        if foreshadowing is not None:
            plot["foreshadowing"] = list(foreshadowing)

    def add_timeline(self, chapter, event):
        self.data["plot"].setdefault("timeline", []).append(
            {"chapter": int(chapter), "event": str(event)})

    def add_summary(self, text, max_items=20):
        """滚动摘要：只保留最近 N 条，防止 prompt 无限膨胀。"""
        text = (text or "").strip()
        if not text:
            return
        self.data["rolling_summary"].append(text)
        self.data["rolling_summary"] = self.data["rolling_summary"][-max_items:]

    # ---------------- 上下文 ----------------
    def context(self, stage="script", shot=None, max_chars=DEFAULT_MAX_CHARS):
        """按阶段组装上下文，并强制截断。

        stage:
            script  写剧本：角色 + 地点 + 上集结尾 + 伏笔
            image   出图：角色外貌 + 道具 + 场景
            tts     配音：角色音色 + 情绪
        """
        builders = {
            "script": self._ctx_script,
            "image": self._ctx_image,
            "tts": self._ctx_tts,
        }
        fn = builders.get(stage, self._ctx_script)
        text = fn(shot)
        return self._truncate(text, max_chars)

    # 兼容旧接口
    def context_for_prompt(self, max_chars=DEFAULT_MAX_CHARS):
        return self.context("script", max_chars=max_chars)

    def _ctx_script(self, shot=None):
        parts = []

        chars = self.data["characters"]
        if chars:
            lines = []
            for name, c in chars.items():
                lines.append("- %s：外貌=%s；状态=%s" % (
                    name, c.get("appearance_prompt") or "未定义",
                    c.get("state") or "正常"))
            parts.append("【角色设定】\n" + "\n".join(lines))

        props = self.data["props"]
        if props:
            lines = ["- %s：%s（在%s）" % (
                n, p.get("appearance_en") or "未定义",
                p.get("location") or p.get("holder") or "未知")
                for n, p in props.items()]
            parts.append("【关键道具】\n" + "\n".join(lines))

        locs = self.data["world"].get("locations") or {}
        if locs:
            parts.append("【地点】" + "、".join(locs.keys()))

        plot = self.data["plot"]
        if plot.get("progress"):
            parts.append("【剧情进度】第 %s 章，%s"
                         % (plot.get("chapter", 0), plot["progress"]))
        if plot.get("foreshadowing"):
            parts.append("【未回收伏笔】" + "、".join(plot["foreshadowing"]))

        summary = self.data["rolling_summary"]
        if summary:
            parts.append("【前情摘要】\n" + "\n".join(summary[-5:]))

        return "\n\n".join(parts)

    def _ctx_image(self, shot=None):
        parts = []
        names = (shot or {}).get("characters") or []

        chars = self.data["characters"]
        picked = [(n, chars[n]) for n in names if n in chars] or list(chars.items())[:2]
        if picked:
            lines = []
            for n, c in picked:
                bits = [c.get("appearance_en") or c.get("appearance_prompt") or ""]
                if c.get("seed"):
                    bits.append("seed=%s" % c["seed"])
                lines.append("- %s：%s" % (n, "，".join(b for b in bits if b)))
            parts.append("【角色】\n" + "\n".join(lines))

        props = self.data["props"]
        if props:
            lines = ["- %s：%s" % (n, p.get("appearance_en") or "")
                     for n, p in list(props.items())[:4]]
            parts.append("【道具】\n" + "\n".join(lines))

        if (shot or {}).get("scene"):
            parts.append("【场景】" + str(shot["scene"]))

        return "\n\n".join(p for p in parts if p.strip())

    def _ctx_tts(self, shot=None):
        parts = []
        chars = self.data["characters"]
        names = (shot or {}).get("characters") or []
        picked = [(n, chars[n]) for n in names if n in chars]
        if picked:
            lines = ["- %s：音色=%s" % (n, c.get("voice") or "默认")
                     for n, c in picked]
            parts.append("【音色】\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def _truncate(self, text, max_chars):
        """硬截断：按字符数，优先在【】块边界切。

        保证返回长度 <= max_chars（后缀也计入预算）。
        """
        text = text or ""
        max_chars = int(max_chars or DEFAULT_MAX_CHARS)
        if len(text) <= max_chars:
            return text

        suffix = "\n……"
        budget = max_chars - len(suffix)
        cut = text[:budget]

        # 优先在块边界切，避免切在半句话
        last = cut.rfind("\n【")
        if last > budget * 0.6:
            cut = cut[:last]

        return cut.rstrip() + suffix

    def __repr__(self):
        return "<Memory %s chars=%d props=%d ch=%s>" % (
            self.path.name, len(self.data["characters"]),
            len(self.data["props"]), self.data["plot"].get("chapter"))
