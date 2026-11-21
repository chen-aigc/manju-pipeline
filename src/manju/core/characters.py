# -*- coding: utf-8 -*-
"""角色定妆照管理：保证同一角色跨镜一致。

问题：
    每个镜头独立生成，模型自由发挥，
    同一角色在不同镜头里穿不同衣服。

方案：
    1. 每个角色先生成一张「定妆照」
    2. 之后所有镜头都把它当 ref_image 传给图像模型
    3. 定妆照缓存在 chapter_dir/characters/ 下，只生成一次

用法：
    book = CharacterBook(chapter_dir, image_provider)
    book.ensure_all(script)          # 确保所有角色有定妆照
    ref = book.get("林越")            # 取参考图路径
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from . import atomic


# 定妆照的通用要求
REF_PROMPT_TAIL = (
    '，正面半身像，人物居中，五官清晰可辨，'
    '服装完整可见，纯色浅灰背景，柔和的均匀光线，'
    '不要任何文字，不要水印，不要多余人物'
)


class CharacterBook:
    """角色定妆照库。"""

    def __init__(self, chapter_dir: Path, image_provider=None,
                 style_suffix: str = ""):
        self.chapter_dir = Path(chapter_dir)
        self.dir = self.chapter_dir / "characters"
        self.dir.mkdir(parents=True, exist_ok=True)

        self.ip = image_provider
        self.style = (style_suffix or "").strip()
        self.cache: Dict[str, Path] = {}
        self.failed: Dict[str, str] = {}

    # ---------------- 查询 ----------------

    def get(self, name: str) -> Optional[Path]:
        """取某角色的定妆照路径（没有则 None）。"""
        name = str(name or "").strip()
        if not name:
            return None

        if name in self.cache:
            return self.cache[name]

        safe = self._safe(name)
        for ext in (".png", ".jpg", ".jpeg"):
            p = self.dir / (safe + ext)
            if p.exists() and p.stat().st_size > 1024:
                self.cache[name] = p
                return p
        return None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    # ---------------- 生成 ----------------

    def ensure(self, name: str,
               description: str = "",
               extra: str = "") -> Optional[Path]:
        """确保某角色有定妆照，没有就生成一张。

        name        角色名（用作文件名）
        description 角色外形描述（从剧本里取）
        extra       额外约束（比如服装统一要求）
        """
        name = str(name or "").strip()
        if not name:
            return None

        existing = self.get(name)
        if existing:
            return existing

        if name in self.failed:
            return None          # 之前失败过，本次不重试

        if self.ip is None:
            self.failed[name] = "没有图像插件"
            return None

        # 拼 prompt
        parts = [name]
        if description:
            parts.append(description)
        if extra:
            parts.append(extra)
        parts.append("同一部作品的角色设定图")

        prompt = "，".join(parts) + REF_PROMPT_TAIL
        if self.style and self.style not in prompt:
            prompt = prompt.rstrip("。") + "，" + self.style

        try:
            res = self.ip.generate(prompt, "", -1, "1080x1920")
            src = Path(res.path)
            if not src.exists() or src.stat().st_size < 1024:
                self.failed[name] = "生成的图无效"
                return None

            dst = self.dir / (self._safe(name) + ".png")

            # 用「读字节再写」而不是 shutil.copy2：
            # Windows 下生成的临时文件可能还被进程持有，
            # copy2 会撞上 WinError 32（文件被占用）
            ok = False
            for _ in range(5):
                try:
                    data = src.read_bytes()
                    dst.write_bytes(data)
                    ok = True
                    break
                except (PermissionError, OSError):
                    import time as _t
                    _t.sleep(0.5)

            if not ok:
                self.failed[name] = "拷贝定妆照失败（文件被占用）"
                return None

            self.cache[name] = dst
            return dst

        except Exception as exc:  # noqa: BLE001
            self.failed[name] = str(exc)[:200]
            return None

    def ensure_all(self, script: Dict[str, Any],
                   default_extra: str = "") -> Dict[str, Any]:
        """为剧本里所有角色确保定妆照。

        返回：{ok: [...], failed: {name: reason}}
        """
        roles = self._collect_roles(script)
        ok = []

        for name, desc in roles.items():
            p = self.ensure(name, desc, default_extra)
            if p:
                ok.append(name)

        return {"ok": ok, "failed": dict(self.failed),
                "skipped": [n for n in roles if n not in ok
                            and n not in self.failed]}

    # ---------------- 内部 ----------------

    @staticmethod
    def _collect_roles(script: Dict[str, Any]) -> Dict[str, str]:
        """从剧本里提取角色名 → 描述。

        支持两种写法：
            characters: ["林越", "月白"]              （只有名字）
            characters: [{"name": "林越", "outfit": "..."}]  （带描述）
        """
        roles: Dict[str, str] = {}

        # 1. 从 shots 里收集出现过的角色
        for shot in (script.get("shots") or []):
            for c in (shot.get("characters") or []):
                if isinstance(c, dict):
                    nm = str(c.get("name") or "").strip()
                    if nm:
                        roles.setdefault(nm, _desc_of(c))
                else:
                    nm = str(c or "").strip()
                    if nm:
                        roles.setdefault(nm, "")

        # 2. 如果剧本顶层有角色表，优先用它的描述覆盖
        top = script.get("character_profiles") or script.get("roles")
        if isinstance(top, dict):
            for nm, info in top.items():
                nm = str(nm).strip()
                if not nm:
                    continue
                if isinstance(info, dict):
                    roles[nm] = _desc_of(info)
                elif isinstance(info, str):
                    roles[nm] = info

        return roles

    @staticmethod
    def _safe(name: str) -> str:
        bad = set('\\/:*?"<>|')
        return "".join(c if c not in bad else "_" for c in str(name))[:40]

    def report(self) -> Dict[str, Any]:
        return {
            "dir": str(self.dir),
            "cached": {k: str(v) for k, v in self.cache.items()},
            "failed": dict(self.failed),
        }


def _desc_of(obj: Dict[str, Any]) -> str:
    """把一个角色描述对象拼成一句话。"""
    keys = ("age", "gender", "hair", "face", "outfit", "outfit_color",
            "weapon", "build", "desc", "description")
    parts = []
    for k in keys:
        v = obj.get(k)
        if v:
            parts.append(str(v))
    return "，".join(parts)
