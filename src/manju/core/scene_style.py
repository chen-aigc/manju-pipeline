# -*- coding: utf-8 -*-
"""场景色调统一：消除同场景不同镜的色温突变。

问题：
    同一个「山巅」场景，01/03 是冷蓝，02 突然暖金。
    因为每个镜头独立写 prompt，没有共用的色调约束。

方案：
    剧本里为每个场景声明色调，出图时自动拼接。

剧本写法：
    {
      "scenes": {
        "山巅": {"palette": "冷蓝调", "light": "黎明逆光",
                 "weather": "薄雾"},
        "破屋屋内": {"palette": "暖黄调", "light": "油灯侧光"}
      }
    }

    或者简单版（只有字符串）：
    {
      "scenes": {
        "山巅": "冷蓝调，黎明逆光，薄雾"
      }
    }
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def scene_style(script: Dict[str, Any], scene_name: str) -> str:
    """取某场景的色调描述。

    返回空字符串表示没有约束。
    """
    if not script or not scene_name:
        return ""

    scenes = script.get("scenes")
    if not isinstance(scenes, dict):
        return ""

    info = scenes.get(scene_name)
    if info is None:
        # 模糊匹配：场景名可能带前后缀
        for k, v in scenes.items():
            if str(k) and (str(k) in str(scene_name)
                           or str(scene_name) in str(k)):
                info = v
                break

    if info is None:
        return ""

    if isinstance(info, str):
        return info.strip()

    if isinstance(info, dict):
        keys = ("palette", "color", "tone", "light", "lighting",
                "weather", "mood", "atmosphere")
        parts = []
        for k in keys:
            v = info.get(k)
            if v:
                parts.append(str(v))
        return "，".join(parts)

    return ""


def build_scene_clause(script: Dict[str, Any], shot: Dict[str, Any]) -> str:
    """为一个镜头拼出场景色调约束句。

    例：「场景色调：冷蓝调，黎明逆光，薄雾」
    """
    scene = str(shot.get("scene") or "")
    style = scene_style(script, scene)
    if not style:
        return ""
    return "场景色调统一为：%s" % style


def normalize_scenes(script: Dict[str, Any]) -> Dict[str, Any]:
    """从镜头列表里推断场景，补全 scenes 字段（可选，供参考）。

    如果没有显式声明，返回空 dict。
    """
    scenes = script.get("scenes")
    if isinstance(scenes, dict):
        return scenes

    # 从镜头里收集场景名
    names = []
    for s in (script.get("shots") or []):
        n = str(s.get("scene") or "").strip()
        if n and n not in names:
            names.append(n)

    # 没有色调信息，只能列名字
    return {n: "" for n in names}
