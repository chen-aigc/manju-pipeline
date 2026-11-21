# -*- coding: utf-8 -*-
"""镜头类型 → 头部参数表

改这里就能调所有镜头的取景，不用改代码。

参数含义（全部是 0~1 的归一化比例）：
    cx      头部中心 x
    cy      头部中心 y
    size    头部高度占画面高度的比例
    count   该镜头里要画几个人
    second  （可选）第二个人（配角）的 (cx, cy, size)

为什么按头部而不是按全身来定：
    头部位置决定了整个取景——头在上方=远景，头占画面大=特写。
    用头部一个锚点就能控制全片统一的取景语言。
"""

# 单人镜头
PROFILES = {
    # 远景：人物小，头在上半部分，留大量环境
    "远景": {"cx": 0.50, "cy": 0.20, "size": 0.085, "count": 1},
    # 中景：腰以上，标准对话镜头
    "中景": {"cx": 0.50, "cy": 0.26, "size": 0.155, "count": 1},
    # 近景：胸以上
    "近景": {"cx": 0.50, "cy": 0.30, "size": 0.225, "count": 1},
    # 特写：脸占主导
    "特写": {"cx": 0.50, "cy": 0.38, "size": 0.34, "count": 1},
    # 过肩：前景人物在右，主体在左
    "过肩": {"cx": 0.38, "cy": 0.28, "size": 0.155, "count": 1},
    # 空镜/死物：没有人
    "空镜": {"cx": 0.50, "cy": 0.50, "size": 0.0, "count": 0},
    "死物": {"cx": 0.50, "cy": 0.50, "size": 0.0, "count": 0},
}

# 双人镜头：主角在左，配角在右
PAIR_PROFILES = {
    "远景": {"cx": 0.34, "cy": 0.22, "size": 0.075,
             "second": (0.66, 0.22, 0.075), "count": 2},
    "中景": {"cx": 0.33, "cy": 0.27, "size": 0.145,
             "second": (0.67, 0.27, 0.145), "count": 2},
    "近景": {"cx": 0.31, "cy": 0.31, "size": 0.21,
             "second": (0.69, 0.31, 0.21), "count": 2},
    "过肩": {"cx": 0.34, "cy": 0.28, "size": 0.16,
             "second": (0.68, 0.30, 0.20), "count": 2},
}

# 表情/朝向微调（在基础参数上叠加）
EXPRESSION_OFFSETS = {
    "低头": {"cy": +0.02},
    "抬头": {"cy": -0.02},
    "仰视": {"cy": -0.03},
    "俯视": {"cy": +0.03},
}


def get_profile(shot_type, npc_count=1, expression=None):
    """取某个镜头的头部参数。

    shot_type  ：剧本里的 shot_type 字段
    npc_count  ：画面里的人数（1 或 2）
    expression ：可选，低头/抬头/仰视/俯视
    """
    if npc_count >= 2:
        prof = dict(PAIR_PROFILES.get(shot_type, PAIR_PROFILES["中景"]))
    else:
        prof = dict(PROFILES.get(shot_type, PROFILES["中景"]))

    if expression:
        for k, v in EXPRESSION_OFFSETS.get(expression, {}).items():
            prof[k] = prof.get(k, 0.0) + v
    return prof


def describe(prof):
    """给日志用的简短描述"""
    base = "头 (%.2f, %.2f) 大 %.3f 人数 %d" % (
        prof["cx"], prof["cy"], prof["size"], prof["count"])
    if prof.get("second"):
        s = prof["second"]
        base += " | 配角 (%.2f, %.2f) 大 %.3f" % (s[0], s[1], s[2])
    return base


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    for st in ("远景", "中景", "近景", "特写", "过肩"):
        print('%-4s 单人: %s' % (st, describe(get_profile(st, 1))))
        print('%-4s 双人: %s' % (st, describe(get_profile(st, 2))))
    print()
    print('低头中景:', describe(get_profile("中景", 1, "低头")))
