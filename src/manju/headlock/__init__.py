# -*- coding: utf-8 -*-
"""manju_headlock —— 头部位置锁定插件

核心思想：
    头部位置/大小由同一套参数决定，
    骨架图（约束生成）和蒙版（局部重绘）共用这套坐标，
    因此永远不会错位。

目录结构：
    headlock/
        __init__.py
        profile.py     镜头类型 → 头部参数表（可调）
        skeleton.py    生成"只含头部"的骨架图
        masker.py      按同一坐标生成蒙版
        pipeline.py    出图 → 蒙版 → 重绘 一条龙
"""
