# -*- coding: utf-8 -*-
"""插件调用监控：在入口和出口打日志。

用法（在 registry 加载后自动套用）：
    from manju.plugins.monitor import wrap_all
    wrap_all()      # 给所有已注册插件套上监控

日志格式：
    入口：  [插件] image/doubao → prompt="..." size=(1080,1920) seed=42
    出口：  [插件] image/doubao ← 12.3s ¥0.20 seed=42
    失败：  [插件] image/doubao ✗ 8.5s ProviderError: 模型未开通

控制：
    PLUGIN_TRACE=off       关闭全部日志
    PLUGIN_TRACE=entry     只打入口
    PLUGIN_TRACE=exit      只打出口（默认）
    PLUGIN_TRACE=all       入口 + 出口
    PLUGIN_TRACE=verbose   额外打印返回值和异常堆栈
"""
import functools
import inspect
import time
import traceback
from pathlib import Path

from .. import config


# ---------------------------------------------------------------- 配置
def _mode():
    return str(getattr(config, "PLUGIN_TRACE", "exit") or "exit").lower()


def _enabled(phase):
    m = _mode()
    if m in ("off", "0", "false", "none"):
        return False
    if m in ("all", "both"):
        return True
    if m == "verbose":
        return True
    return m == phase


def _verbose():
    return _mode() == "verbose"


# ---------------------------------------------------------------- 格式化
def _short(v, limit=60):
    """把参数值压成一行短文本。"""
    if isinstance(v, str):
        s = v.replace("\n", " ")
        return s if len(s) <= limit else s[:limit] + "…"
    if isinstance(v, (tuple, list)) and len(v) == 2 and all(
            isinstance(x, int) for x in v):
        return "(%d,%d)" % (v[0], v[1])
    if isinstance(v, Path):
        return v.name
    if v is None:
        return "None"
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


def _sig_args(sig, args, kwargs):
    """把 args/kwargs 整理成 key=value 列表（去掉 self）。"""
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return []
    bound.apply_defaults()
    out = []
    for name, val in bound.arguments.items():
        if name in ("self", "cls"):
            continue
        # 跳过一些噪音参数
        if name in ("keep_seed_on_retry",):
            continue
        out.append("%s=%s" % (name, _short(val)))
    return out


def _cost_of(result):
    """从结果里提取成本（元）。"""
    cost = getattr(result, "cost", None)
    if cost is None:
        return None
    # Cost 对象：yuan 是 Decimal（元），amount_si 是 0.0001 元
    yuan = getattr(cost, "yuan", None)
    if yuan is not None:
        try:
            return float(yuan)
        except (TypeError, ValueError):
            pass
    si = getattr(cost, "amount_si", None)
    if si is not None:
        try:
            return float(si) / 10000.0
        except (TypeError, ValueError):
            return None
    return None


def _elapsed_of(result):
    t = getattr(result, "elapsed", None)
    if t is not None:
        try:
            return float(t)
        except (TypeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------- 当前调用栈
class _CallContext:
    """记录当前正在执行的插件调用（给嵌套调用用）。"""
    stack = []

    @classmethod
    def current(cls):
        return cls.stack[-1] if cls.stack else None


# ---------------------------------------------------------------- 装饰器
def monitored(kind, name, method_name):
    """给一个方法套上入口/出口日志 + 限速。"""
    def deco(fn):
        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            sig = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tag = "%s/%s" % (kind, name)
            call = {"tag": tag, "method": method_name}
            _CallContext.stack.append(call)

            if _enabled("entry"):
                parts = _sig_args(sig, args, kwargs) if sig else []
                print("[插件] %-22s → %s" % (tag, " ".join(parts)))

            # 限速：阻塞直到拿到槽位
            _lh = None
            try:
                from ..core.ratelimit import limiter as _lh
                _lh.acquire(tag)
            except RuntimeError as exc:
                # 超出日配额：直接失败，不调用
                _CallContext.stack.pop()
                if _enabled("exit"):
                    print("[插件] %-22s ✗ 限速拒绝: %s" % (tag, exc))
                raise
            except TimeoutError as exc:
                _CallContext.stack.pop()
                if _enabled("exit"):
                    print("[插件] %-22s ✗ 限速超时: %s" % (tag, exc))
                raise

            t0 = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                dt = time.time() - t0
                if _lh is not None:
                    try:
                        _lh.release(tag)
                    except Exception:  # noqa: BLE001
                        pass
                _CallContext.stack.pop()
                if _enabled("exit"):
                    print("[插件] %-22s ✗ %.1fs %s: %s" % (
                        tag, dt, type(exc).__name__, _short(str(exc), 80)))
                    if _verbose():
                        traceback.print_exc()
                raise

            dt = time.time() - t0
            if _lh is not None:
                try:
                    _lh.release(tag)
                except Exception:  # noqa: BLE001
                    pass
            _CallContext.stack.pop()

            if _enabled("exit"):
                parts = ["%.1fs" % dt]
                c = _cost_of(result)
                if c is not None:
                    parts.append("¥%.4f" % c)
                el = _elapsed_of(result)
                if el is not None and abs(el - dt) > 0.5:
                    parts.append("内部%.1fs" % el)
                seed = getattr(result, "seed", None)
                if seed:
                    parts.append("seed=%s" % seed)

                print("[插件] %-22s ← %s" % (tag, " ".join(parts)))

                if _verbose():
                    print("         返回: %r" % (result,))

            return result

        wrapper._monitored = True
        wrapper._orig = fn
        return wrapper
    return deco


# ---------------------------------------------------------------- 包装一个类
MAIN_METHODS = {
    "script": ["generate"],
    "image": ["generate"],
    "video": ["generate", "query"],
    "tts": ["synthesize"],
    "bgm": ["build"],
    "edit": ["build"],
    "log": ["log", "emit"],
}


def wrap_class(cls, kind, name):
    """给一个 provider 类的主方法套上监控（幂等）。"""
    for mname in MAIN_METHODS.get(kind, []):
        fn = getattr(cls, mname, None)
        if fn is None or not callable(fn):
            continue
        if getattr(fn, "_monitored", False):
            continue
        setattr(cls, mname, monitored(kind, name, mname)(fn))
    return cls


# ---------------------------------------------------------------- 全量包装
_wrapped = set()


def wrap_all(registry=None):
    """给 registry 里所有 provider 套上监控。

    在 registry.load_all() 之后调用一次即可（幂等）。
    """
    if registry is None:
        from . import registry as _r
        registry = _r

    count = 0
    for kind in ("script", "image", "video", "tts", "bgm", "edit", "log"):
        try:
            names = registry.list_providers(kind)
        except Exception:  # noqa: BLE001
            continue
        for name in names:
            key = "%s/%s" % (kind, name)
            if key in _wrapped:
                continue
            try:
                p = registry.pick(kind, name)
                cls = type(p)
                wrap_class(cls, kind, name)
                _wrapped.add(key)
                count += 1
            except Exception:  # noqa: BLE001
                continue
    return count


# ---------------------------------------------------------------- 手动使用
def monitor_call(kind, name, method, fn, *args, **kwargs):
    """不依赖类包装，直接调用一次带监控。"""
    wrapped = monitored(kind, name, method)(fn)
    return wrapped(*args, **kwargs)
