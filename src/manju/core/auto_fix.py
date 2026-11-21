# -*- coding: utf-8 -*-
"""一致性自动修复：发现离群镜头并重做。

流程：
    1. 出完图后跑一致性检查
    2. 找出离群镜头（WARN / FAIL）
    3. 用角色定妆照做参考，重做这些镜头
    4. 重做后不再检查（避免死循环）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .. import config


def audit_and_fix(orch, script: Dict[str, Any],
                  max_rounds: int = 1,
                  threshold: str = "WARN") -> Dict[str, Any]:
    """检查并修复离群镜头。

    orch        Orchestrator 实例
    script      剧本数据
    max_rounds  最多修几轮（默认 1，避免无限重试）
    threshold   触发重做的级别：WARN / FAIL

    返回：{checked, redo, failed, skipped}
    """
    if not getattr(config, "AUTO_FIX_OUTLIERS", True):
        return {"checked": 0, "redo": 0, "failed": 0, "skipped": True}

    try:
        from .consistency import ConsistencyChecker
    except Exception as exc:  # noqa: BLE001
        return {"checked": 0, "redo": 0, "failed": 0,
                "error": repr(exc)[:100]}

    img_dir = orch.chapter_dir / "images"
    if not img_dir.exists():
        return {"checked": 0, "redo": 0, "failed": 0, "error": "没有图"}

    shots = []
    for s in (script.get("shots") or []):
        idx = int(s.get("index") or 0)
        for cand in (img_dir / ("shot_%02d.png" % idx),
                     img_dir / ("shot_%03d.png" % idx)):
            if cand.exists():
                shots.append({"id": "shot_%02d" % idx,
                              "path": str(cand)})
                break

    if len(shots) < 3:
        return {"checked": len(shots), "redo": 0, "failed": 0,
                "skipped": True}

    # 跑检查
    try:
        ck = ConsistencyChecker(None)
        report = ck.audit_batch(shots, mode="relative")
    except Exception as exc:  # noqa: BLE001
        return {"checked": len(shots), "redo": 0, "failed": 0,
                "error": repr(exc)[:100]}

    # 找离群镜头
    levels = {"FAIL": 2, "WARN": 1, "OK": 0}
    need = levels.get(threshold, 1)

    outliers: List[int] = []
    for item in (report.items if hasattr(report, "items") else []):
        lv = (item.get("level") if isinstance(item, dict)
              else getattr(item, "level", "OK"))
        sid = (item.get("id") if isinstance(item, dict)
               else getattr(item, "id", ""))
        if levels.get(lv, 0) >= need:
            try:
                outliers.append(int(str(sid).replace("shot_", "")))
            except ValueError:
                pass

    if not outliers:
        return {"checked": len(shots), "redo": 0, "failed": 0,
                "outliers": []}

    print("[一致] 发现 %d 个离群镜头：%s"
          % (len(outliers), ", ".join("%02d" % i for i in outliers)))

    # 重做
    redone = 0
    failed = 0

    ip = orch.p("image")
    if ip is None:
        return {"checked": len(shots), "redo": 0, "failed": 0,
                "error": "没有图像插件"}

    for idx in outliers:
        shot = None
        for s in (script.get("shots") or []):
            if int(s.get("index") or 0) == idx:
                shot = s
                break
        if not shot:
            continue

        try:
            ok = _redo_one(orch, ip, script, shot)
            if ok:
                redone += 1
                print("  ✅ 镜 %02d 已重做" % idx)
            else:
                failed += 1
                print("  ❌ 镜 %02d 重做失败" % idx)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  ❌ 镜 %02d 重做出错：%s" % (idx, str(exc)[:100]))

    return {"checked": len(shots), "redo": redone, "failed": failed,
            "outliers": outliers}


def _redo_one(orch, ip, script, shot) -> bool:
    """重做单个镜头。"""
    idx = int(shot.get("index") or 0)
    if idx <= 0:
        return False

    # 拼 prompt
    from .. import image_gen
    pos, neg = image_gen.build_prompts(shot, orch.memory)

    style = (getattr(config, "IMAGE_STYLE_SUFFIX", "") or "").strip()
    if style and style not in pos:
        pos = "%s，%s" % (pos.rstrip("，,。. "), style)

    try:
        from .scene_style import build_scene_clause
        clause = build_scene_clause(script, shot)
        if clause and clause not in pos:
            pos = "%s，%s" % (pos.rstrip("，,。. "), clause)
    except Exception:  # noqa: BLE001
        pass

    # 收集角色定妆照
    refs = []
    for rname in (shot.get("characters") or []):
        p = orch._char_ref_path(rname)
        if p and p not in refs:
            refs.append(p)
    refs = refs[:3]

    out = orch.chapter_dir / "images" / ("shot_%02d.png" % idx)

    ip.config.update({
        "out_path": str(out),
        "shot_index": idx,
        "max_retry": 3,
        "use_ref": bool(refs),
    })

    try:
        res = ip.generate(pos, neg, -1, config.image_gen_size(),
                          ref_image=(refs if refs else None))
        return bool(res and Path(res.path).exists())
    except Exception as exc:  # noqa: BLE001
        print("    重做异常：%s" % str(exc)[:120])
        return False
