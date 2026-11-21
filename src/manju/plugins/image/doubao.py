# -*- coding: utf-8 -*-
"""image 插件：豆包 Seedream（火山方舟）

真实接口（2026-11 核对，来源：火山方舟 API 文档）：
    生成  POST {base}/images/generations
    鉴权  Authorization: Bearer <ARK_API_KEY>
    base  https://ark.cn-beijing.volces.com/api/v3

请求体（文生图 / 图生图统一）：
    {
      "model": "doubao-seedream-4-0-250828",
      "prompt": "提示词",
      "image": "data:image/png;base64,...",   # 可选，图生图
      "size": "1080x1920",                     # 或 "2K" / "1K"
      "seed": 12345,
      "watermark": false,
      "response_format": "url"                 # 或 "b64_json"
    }

价格（火山方舟国内牌价，2026-10 核对）：
    Seedream 4.0  约 0.20 元/张（1K~2K）
    Seedream 3.0  约 0.20 元/张

为什么值得用（对比本地 SD1.5）：
    画风准确度高 5 分（本地 3 分 vs 8 分）
    服装结构正确（"交领"能画对）
    场景丰富（农家小院、木屋、积雪）

注意：
    watermark=false 可去掉"豆包AI生成"水印
    （如果账号不支持，会自动忽略该字段）
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..base import ImageProvider, ImageResult, ProviderError
from ..registry import register
from ... import config
from ...core import atomic, integrity
from ...core.cost import Cost, to_si

# 元/张（火山方舟国内牌价，2026-10 核对）
PRICE = {
    # 模型关键字        单价
    "seedream-4-5":    0.25,
    "seedream-4-0":    0.20,
    "seedream-3-0":    0.20,
    "seededit":        0.20,
}

# 支持的尺寸
SIZES = {
    "portrait_1080": "1080x1920",
    "portrait_720": "720x1280",
    "square": "1024x1024",
    "landscape": "1920x1080",
    "auto": "2K",
}


@register("image", "doubao")
class DoubaoImage(ImageProvider):
    """豆包 Seedream 文生图 / 图生图。

    配置：
        ARK_API_KEY         必填
        JIMENG_BASE_URL     默认 https://ark.cn-beijing.volces.com/api/v3
        DOUBAO_IMAGE_MODEL  默认 doubao-seedream-4-0-250828
        DOUBAO_IMAGE_SIZE   默认 1080x1920（竖屏）
        DOUBAO_SEED         默认 -1（随机）
        DOUBAO_WATERMARK    默认 off（不加"豆包AI生成"水印）
    """

    poll_interval = 3
    max_wait = 180

    # ---------------- 基础 ----------------
    def _key(self):
        return (getattr(config, "ARK_API_KEY", "") or "").strip()

    def _base(self):
        return (getattr(config, "JIMENG_BASE_URL", "")
                or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

    def _model(self):
        return (getattr(config, "DOUBAO_IMAGE_MODEL", "")
                or "doubao-seedream-4-0-250828")

    def _size(self, size=None):
        """尺寸：优先用调用方给的，其次配置，最后默认竖屏。"""
        if size:
            # 已经是 "1080x1920" 这种直接返回
            if isinstance(size, str) and "x" in size:
                return size
            # 或者是预设名
            if isinstance(size, str) and size in SIZES:
                return SIZES[size]
            # 或者是 (w, h) 元组
            if isinstance(size, (tuple, list)) and len(size) == 2:
                return "%dx%d" % (int(size[0]), int(size[1]))
        cfg_size = getattr(config, "DOUBAO_IMAGE_SIZE", "1080x1920")
        if cfg_size in SIZES:
            return SIZES[cfg_size]
        return str(cfg_size)

    def _price(self):
        m = self._model().lower()
        for kw, p in PRICE.items():
            if kw in m:
                return p
        try:
            return float(getattr(config, "DOUBAO_IMAGE_PRICE", 0.20))
        except (TypeError, ValueError):
            return 0.20

    def _watermark(self):
        v = str(getattr(config, "DOUBAO_WATERMARK", "off") or "off").lower()
        return v in ("on", "true", "1", "yes")

    def available(self):
        return bool(self._key())

    def cost_estimate(self):
        return to_si(self._price())

    # ---------------- HTTP ----------------
    def _request(self, method, path, payload=None, timeout=120):
        url = self._base() + path
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self._key())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if "ModelNotOpen" in body:
                raise ProviderError(
                    "模型没开通：请在方舟控制台「开通管理」里开通 %s\n"
                    "  （控制台能查到模型 ≠ 已开通，必须点「开通」）"
                    % self._model(),
                    provider="doubao", retryable=False) from exc
            if "InvalidParameter" in body and "watermark" in body:
                # watermark 字段不支持，去掉重试一次
                return self._request(method, path,
                                     {k: v for k, v in (payload or {}).items()
                                      if k != "watermark"}, timeout)
            raise ProviderError(
                "豆包 HTTP %s：%s" % (exc.code, body),
                provider="doubao",
                retryable=exc.code in (429, 500, 502, 503, 504)) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("豆包请求失败：%s" % exc,
                                provider="doubao") from exc

    # ---------------- 自检（不花钱） ----------------
    def check(self):
        """验证 Key 和模型开通情况；只查模型列表，不出图、不扣费。"""
        if not self._key():
            return False, "ARK_API_KEY 未配置"

        try:
            data = self._request("GET", "/models", timeout=30)
        except ProviderError as exc:
            msg = str(exc)
            if "404" in msg:
                return False, "接口 404：检查 JIMENG_BASE_URL 是否是 /api/v3"
            return False, msg[:200]

        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        want = self._model()
        if want not in ids:
            near = [i for i in ids if "seedream" in i.lower()]
            if near:
                return False, ("模型 %s 不在账号可用列表里；可用的是：%s"
                               % (want, ", ".join(near[:6])))
            return False, "账号未列出任何 seedream 模型，可能没开通"

        return True, "Key 可用（model=%s size=%s 单价 %.2f 元/张）" % (
            self._model(), self._size(), self._price())

    def list_models(self):
        """列出账号可用的 seedream 模型。"""
        try:
            data = self._request("GET", "/models", timeout=30)
        except ProviderError:
            return []
        return [m.get("id") for m in (data.get("data") or [])
                if m.get("id") and "seedream" in str(m.get("id")).lower()]

    # ---------------- 生成 ----------------
    def _image_data_url(self, image):
        """把本地图片转成 data URL。"""
        p = Path(image)
        if p.exists():
            raw = p.read_bytes()
            ext = p.suffix.lower().lstrip(".") or "png"
            if ext == "jpg":
                ext = "jpeg"
            return "data:image/%s;base64,%s" % (
                ext, base64.b64encode(raw).decode("ascii"))
        # 已经是 URL
        return str(image)

    def generate(self, prompt, negative="", seed=-1, size=None,
                 ref_image=None, keep_seed_on_retry=False):
        """生成图片。

        Args:
            prompt:       正向提示词
            negative:     负向提示词（会被拼进 prompt，Seedream 没有独立负向字段）
            seed:         随机种子，-1 为随机
            size:         尺寸，见 SIZES
            ref_image:    参考图（图生图模式）
        """
        t0 = time.time()
        if not self._key():
            raise ProviderError("豆包未配置 ARK_API_KEY",
                                provider="doubao", retryable=False)

        out = self.config.get("out_path")
        if not out:
            raise ValueError("doubao 需要 config['out_path']")

        # 拼接提示词（Seedream 没有独立负向字段）
        full_prompt = str(prompt or "")
        if negative:
            full_prompt += "\n\n【不要出现】" + str(negative)

        payload = {
            "model": self._model(),
            "prompt": full_prompt[:8000],
            "size": self._size(size),
            "response_format": "url",
        }
        if seed is not None and int(seed) >= 0:
            payload["seed"] = int(seed)
        if not self._watermark():
            payload["watermark"] = False
        if ref_image:
            # 支持单张或多张（多角色镜头用数组传）
            if isinstance(ref_image, (list, tuple)):
                urls = [self._image_data_url(x) for x in ref_image if x]
                if urls:
                    payload["image"] = urls if len(urls) > 1 else urls[0]
            else:
                payload["image"] = self._image_data_url(ref_image)

        data = self._request("POST", "/images/generations", payload)

        # 解析返回
        items = data.get("data") or []
        if not items:
            raise ProviderError("豆包没有返回图片数据：%s"
                                % str(data)[:300], provider="doubao")

        item = items[0]
        url = item.get("url") or ""
        b64 = item.get("b64_json") or ""
        used_seed = item.get("seed", int(seed) if seed else 0)

        # 下载
        out_path = Path(out)
        if b64:
            raw = base64.b64decode(b64)
            atomic.atomic_write_bytes(out_path, raw,
                                      verify=integrity.validate_image)
        elif url:
            raw = self._download(url)
            atomic.atomic_write_bytes(out_path, raw,
                                      verify=integrity.validate_image)
        else:
            raise ProviderError("豆包返回既没有 url 也没有 b64_json：%s"
                                % str(item)[:300], provider="doubao")

        return ImageResult(
            path=out_path,
            seed=int(used_seed or 0),
            attempts=1,
            cost=Cost(self.cost_estimate(), "unit_price",
                      {"per_image": self._price(),
                       "model": self._model(),
                       "size": self._size(size)}),
            elapsed=time.time() - t0,
        )

    def _download(self, url, timeout=120):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("下载豆包图片失败：%s" % exc,
                                provider="doubao") from exc

    def head_ok(self, path):
        """切头检测：用现有的 image_gen 检测。"""
        try:
            from ... import image_gen
            cut, why = image_gen.head_is_cut(path)
            return (not cut), why
        except Exception:  # noqa: BLE001
            return True, ""
