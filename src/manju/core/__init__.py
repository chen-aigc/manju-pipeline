# -*- coding: utf-8 -*-
"""core：原子写入、完整性校验、状态恢复、成本、锁、编排。

注意：Orchestrator / JobResult 用懒加载导出，
因为编排器依赖 plugins，而 plugins 又依赖 core.cost，
顶层硬导入会形成循环引用。
"""
from . import atomic, cost, integrity, lock, state  # noqa: F401

__all__ = ["atomic", "integrity", "state", "cost", "lock",
           "Orchestrator", "JobResult"]


def __getattr__(name):
    if name in ("Orchestrator", "JobResult"):
        from . import orchestrator
        return getattr(orchestrator, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
