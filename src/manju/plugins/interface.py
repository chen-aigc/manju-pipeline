# -*- coding: utf-8 -*-
"""插件接口规范 v1：所有 provider 必须遵守的统一契约。

设计原则：
    1. 所有 provider 的 generate() 签名完全一致
    2. 可选参数一律带默认值，且默认值语义统一
    3. 返回值类型固定（ImageResult / VideoResult / ...）
    4. 失败一律抛 ProviderError，不返回 None
    5. available() 必须是廉价的（不发起网络请求）

七个环节的统一契约：

    Script  generate(source, memory, chapter, target_shots) -> ScriptResult
    Image   generate(prompt, negative, seed, size,
                     ref_image=None, keep_seed_on_retry=False) -> ImageResult
    Video   generate(image, prompt, duration=3.0,
                     first_frame=None, last_frame=None) -> VideoResult
    TTS     synthesize(text, voice, rate=None, pitch=None) -> TTSResult
    BGM     build(shots, chapter_dir) -> BGMResult
    Edit    build(chapter_dir, shots, audio, bgm, srt) -> EditResult
    Log     log(event) / emit(out_path) -> Path | None

尺寸约定（Image）：
    size 可以是：
        (w, h) 元组           → 直接用
        "1080x1920" 字符串     → 直接用
        预设名 "portrait_1080" → 查表
        None                  → 用 provider 自己的默认值

seed 约定（Image）：
    int >= 0  → 用这个值
    -1 或 None → 随机（provider 自行决定）

ref_image 约定：
    本地路径（Path 或 str）→ provider 负责编码上传
    None                  → 纯文生图

pacing 约定：
    全部 provider 在耗时操作前应检查 self.cancel_event（如果有），
    收到取消请求时抛 ProviderError(retryable=False, message="已取消")
"""
from dataclasses import dataclass
from typing import Optional, Union, Tuple, List, Dict, Any


# ---------------------------------------------------------------- 类型别名
Size = Union[Tuple[int, int], str, None]
PathLike = Union[str, "Path"]  # noqa: F821


@dataclass
class ProviderSpec:
    """单个 provider 的规范声明。"""
    kind: str                       # image / video / tts / bgm / edit / script / log
    name: str                       # 插件名
    generate_sig: str               # generate 的签名（用于自检）
    result_type: str                # 返回类型名
    required_env: List[str]         # 必需的环境变量
    optional_env: List[str]         # 可选的环境变量
    default_price: Optional[float]  # 默认单价（元），用于成本估算
    price_unit: str                 # 单价的单位："per_image" / "per_second" / "per_call"


# ---------------------------------------------------------------- 规范清单
SPECS = {
    # ---------- image ----------
    "image": {
        "generate_sig": "prompt, negative, seed, size, ref_image=None",
        "result_type": "ImageResult",
        "params": {
            "prompt": "正向提示词（str）",
            "negative": "负向提示词（str，可为空）",
            "seed": "随机种子（int，-1 = 随机）",
            "size": "(w,h) / 'WxH' / 预设名 / None",
            "ref_image": "参考图路径或 URL（None = 纯文生图）",
        },
        "optional": {
            "keep_seed_on_retry": "重试时是否固定种子（部分实现支持，可选）",
        },
    },
    # ---------- video ----------
    "video": {
        "generate_sig": ("image, prompt, duration=3.0, "
                         "first_frame=None, last_frame=None"),
        "result_type": "VideoResult",
        "params": {
            "image": "首帧图片（必填）",
            "prompt": "动作描述（str）",
            "duration": "时长秒数（float）",
            "first_frame": "可选的首帧（覆盖 image）",
            "last_frame": "可选的尾帧",
        },
    },
    # ---------- tts ----------
    "tts": {
        "synthesize_sig": "text, voice, rate=None, pitch=None",
        "result_type": "TTSResult",
        "params": {
            "text": "要合成的文本（str）",
            "voice": "音色名（str）",
            "rate": "语速（如 '+0%' / '-10%'）",
            "pitch": "音调（如 '+0Hz'）",
        },
    },
    # ---------- bgm ----------
    "bgm": {
        "build_sig": "shots, chapter_dir",
        "result_type": "BGMResult",
    },
    # ---------- edit ----------
    "edit": {
        "build_sig": "chapter_dir, shots, audio, bgm, srt",
        "result_type": "EditResult",
    },
    # ---------- script ----------
    "script": {
        "generate_sig": "source, memory, chapter, target_shots",
        "result_type": "ScriptResult",
    },
    # ---------- log ----------
    "log": {
        "log_sig": "event",
        "emit_sig": "out_path=None",
    },
}


# ---------------------------------------------------------------- 校验工具
def check_provider(cls, kind):
    """校验一个 provider 类是否符合规范。

    用法（测试用）：
        from manju.plugins.interface import check_provider
        from manju.plugins.image.doubao import DoubaoImage
        errors = check_provider(DoubaoImage, "image")
    """
    import inspect
    errors = []

    # 1. 必须有 available()
    if not hasattr(cls, "available"):
        errors.append("缺少 available() 方法")
    else:
        try:
            sig = inspect.signature(cls.available)
            if len(sig.parameters) != 1:
                errors.append("available() 应只有 self 参数")
        except (ValueError, TypeError):
            pass

    # 2. 必须有 cost_estimate()
    if not hasattr(cls, "cost_estimate"):
        errors.append("缺少 cost_estimate() 方法")

    # 3. 按环节检查主方法
    spec = SPECS.get(kind)
    if not spec:
        errors.append("未知环节：%s" % kind)
        return errors

    method_map = {
        "image": "generate",
        "video": "generate",
        "tts": "synthesize",
        "bgm": "build",
        "edit": "build",
        "script": "generate",
    }
    mname = method_map.get(kind)
    if mname:
        fn = getattr(cls, mname, None)
        if fn is None:
            errors.append("缺少 %s() 方法" % mname)
        else:
            try:
                sig = inspect.signature(fn)
                expect_keys = spec.get("generate_sig") or spec.get("synthesize_sig") \
                    or spec.get("build_sig") or ""
                # 提取期望的参数名
                expect_names = []
                for part in expect_keys.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    pname = part.split("=")[0].split(":")[0].strip()
                    if pname:
                        expect_names.append(pname)
                actual = [p for p in sig.parameters
                          if p not in ("self", "cls") and
                          sig.parameters[p].kind not in (sig.parameters[p].VAR_KEYWORD,)]
                missing = [n for n in expect_names if n not in actual]
                if missing:
                    errors.append("%s() 缺少参数：%s" % (mname, ", ".join(missing)))
                # 额外参数只要带默认值就算兼容
                optional_names = set(spec.get("optional") or {})
                extra = [n for n in actual if n not in expect_names]
                no_default = [n for n in extra
                              if n not in optional_names
                              and sig.parameters[n].default is inspect.Parameter.empty]
                if no_default:
                    errors.append("%s() 多了无默认值的参数：%s"
                                  % (mname, ", ".join(no_default)))
            except (ValueError, TypeError):
                pass

    return errors


def check_all(kinds=None):
    """校验所有已注册的 provider。

    Args:
        kinds: 只校验这些环节（None = 全部）

    注意：不需要实例化，直接检查类，速度快。
    """
    from .registry import list_providers, get_class
    results = {}
    for kind in (kinds or SPECS.keys()):
        if kind not in SPECS:
            continue
        try:
            names = list_providers(kind)
        except Exception:  # noqa: BLE001
            continue
        for name in names:
            key = "%s/%s" % (kind, name)
            try:
                cls = get_class(kind, name)
                results[key] = check_provider(cls, kind)
            except Exception as exc:  # noqa: BLE001
                results[key] = ["加载失败：%r" % exc]
    return results


def report(kinds=None):
    """打印规范校验报告。"""
    results = check_all(kinds)
    lines = ["=" * 62, "插件接口规范校验", "=" * 62]
    ok = 0
    bad = 0
    for key in sorted(results):
        errs = results[key]
        if not errs:
            lines.append("  ✅ %-24s" % key)
            ok += 1
        else:
            lines.append("  ❌ %-24s" % key)
            for e in errs:
                lines.append("       - %s" % e)
            bad += 1
    lines.append("")
    lines.append("  通过 %d / %d" % (ok, ok + bad))
    return "\n".join(lines)
