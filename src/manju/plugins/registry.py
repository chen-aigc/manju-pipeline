# -*- coding: utf-8 -*-
"""插件注册表与工厂。

用法：
    @register("script", "deepseek")
    class DeepSeekScript(ScriptProvider): ...

    p = make("script")              # 按配置取
    p = make("script", "mock")      # 指定实现
    chain = make_chain("video")     # 主 + 降级链
"""
import importlib
import pkgutil
from pathlib import Path

from . import base

_REGISTRY = {}          # {kind: {name: cls}}
_LOADED = False

# 七类插件对应的基类
KINDS = {
    "script": base.ScriptProvider,
    "image": base.ImageProvider,
    "video": base.VideoProvider,
    "tts": base.TTSProvider,
    "bgm": base.BGMProvider,
    "edit": base.EditProvider,
    "log": base.LogProvider,
    # 扩展能力（可选环节，不影响主流程）
    "scene3d": base.Scene3DProvider,
    "radio": base.RadioProvider,
}

# 配置项 → 类别
CONFIG_KEYS = {
    "script": "SCRIPT_PROVIDER",
    "image": "IMAGE_PROVIDER",
    "video": "VIDEO_PROVIDER",
    "tts": "TTS_PROVIDER",
    "bgm": "BGM_PROVIDER",
    "edit": "EDIT_PROVIDER",
    "log": "LOG_PROVIDER",
    "scene3d": "SCENE3D_PROVIDER",
    "radio": "RADIO_PROVIDER",
}

# 降级链配置项
FALLBACK_KEYS = {
    "script": "SCRIPT_FALLBACK",
    "image": "IMAGE_FALLBACK",
    "video": "VIDEO_FALLBACK",
    "tts": "TTS_FALLBACK",
    "bgm": "BGM_FALLBACK",
    "edit": "EDIT_FALLBACK",
    "log": "LOG_FALLBACK",
    # 扩展能力
    "scene3d": "SCENE3D_FALLBACK",
    "radio": "RADIO_FALLBACK",
}


def register(kind, name):
    """装饰器：把实现注册到指定类别。"""
    def deco(cls):
        if kind not in KINDS:
            raise ValueError("未知插件类别：%s" % kind)
        _REGISTRY.setdefault(kind, {})[name] = cls
        cls.name = name
        return cls
    return deco


def load_all(force=False):
    """自动导入 plugins 下所有子包里的模块，触发注册。"""
    global _LOADED
    if _LOADED and not force:
        return

    pkg_dir = Path(__file__).parent
    skip_top = {"base", "registry"}

    for info in pkgutil.iter_modules([str(pkg_dir)]):
        if info.name in skip_top:
            continue

        if info.ispkg:
            # 子包（script / image / ...）：把里面的模块都导一遍
            sub_dir = pkg_dir / info.name
            try:
                importlib.import_module("%s.%s" % (__package__, info.name))
            except Exception as exc:  # noqa: BLE001
                print("[plugins] 加载包 %s 失败：%r" % (info.name, exc))
                continue

            for sub in pkgutil.iter_modules([str(sub_dir)]):
                if sub.name.startswith("_"):
                    continue
                mod = "%s.%s.%s" % (__package__, info.name, sub.name)
                try:
                    importlib.import_module(mod)
                except Exception as exc:  # noqa: BLE001
                    print("[plugins] 加载 %s 失败：%r" % (mod, exc))
        else:
            try:
                importlib.import_module("%s.%s" % (__package__, info.name))
            except Exception as exc:  # noqa: BLE001
                print("[plugins] 加载 %s 失败：%r" % (info.name, exc))

    _LOADED = True


def list_providers(kind):
    load_all()
    return sorted(_REGISTRY.get(kind, {}).keys())


def get_class(kind, name):
    load_all()
    cls = _REGISTRY.get(kind, {}).get(name)
    if cls is None:
        raise KeyError("未注册的插件：%s/%s（可用：%s）"
                       % (kind, name, ", ".join(list_providers(kind)) or "无"))
    return cls


def make(kind, name=None, config=None):
    """创建一个插件实例。name 为空时按配置取。"""
    if kind not in KINDS:
        raise ValueError("未知插件类别：%s" % kind)

    if name is None:
        from .. import config as cfg
        name = getattr(cfg, CONFIG_KEYS[kind], "") or _default_of(kind)

    cls = get_class(kind, name)

    # 自动套上入口/出口监控（幂等）
    try:
        from . import monitor as _mon
        _mon.wrap_class(cls, kind, name)
    except Exception:  # noqa: BLE001
        pass

    inst = cls(config)
    if not isinstance(inst, KINDS[kind]):
        raise TypeError("%s/%s 未实现 %s 接口"
                        % (kind, name, KINDS[kind].__name__))
    return inst


def make_chain(kind, config=None):
    """按「主 + 降级链」返回实例列表（可用性过滤交给调用方）。"""
    from .. import config as cfg

    names = []
    primary = getattr(cfg, CONFIG_KEYS[kind], "") or _default_of(kind)
    if primary:
        names.append(primary)

    fb = getattr(cfg, FALLBACK_KEYS[kind], "") or ""
    for n in str(fb).split(","):
        n = n.strip()
        if n and n not in names:
            names.append(n)

    chain = []
    for n in names:
        try:
            chain.append(make(kind, n, config))
        except KeyError as exc:
            print("[plugins] 跳过降级项：%s" % exc)
    return chain


def pick(kind, config=None):
    """取第一个 available() 的实例。都不可用则抛错。"""
    chain = make_chain(kind, config)
    for p in chain:
        try:
            if p.available():
                return p
        except Exception as exc:  # noqa: BLE001
            print("[plugins] %s/%s 可用性检查失败：%r" % (kind, p.name, exc))
    raise RuntimeError(
        "类别 %s 没有可用插件（候选：%s）"
        % (kind, ", ".join(p.name for p in chain) or "无"))


def _default_of(kind):
    """配置缺失时的兜底默认。"""
    return {
        "script": "mock",
        "image": "placeholder",
        "video": "null",
        "tts": "mock",
        "bgm": "none",
        "edit": "auto_cut",
        "log": "console",
    }.get(kind, "")


def registry_snapshot():
    """给 Web 设置页用：列出所有可用实现及配置。"""
    load_all()
    out = {}
    for kind in KINDS:
        out[kind] = list_providers(kind)
    return out
