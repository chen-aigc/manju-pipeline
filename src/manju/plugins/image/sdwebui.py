# -*- coding: utf-8 -*-
"""image 插件：SD WebUI、OpenAI 兼容图像接口（硅基流动/通义万相等）。"""
import base64
import binascii
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


def _save_b64(b64, out_path):
    raw = base64.b64decode(b64)
    atomic.atomic_write_bytes(out_path, raw, verify=integrity.validate_image)
    return Path(out_path)


@register("image", "sdwebui")
class SDWebUIImage(ImageProvider):
    """Stable Diffusion WebUI（A1111）的 /sdapi/v1/txt2img。

    需要开 --api 参数；本地或局域网都可用。
    """

    def _url(self):
        return (self.config.get("url") or config.SD_WEBUI_URL
                or "http://127.0.0.1:7860").rstrip("/")

    def available(self):
        try:
            with urllib.request.urlopen(self._url() + "/sdapi/v1/options",
                                        timeout=3):
                return True
        except Exception:  # noqa: BLE001
            return False

    def cost_estimate(self):
        return to_si("0.03")

    def generate(self, prompt, negative, seed, size, ref_image=None, **kwargs):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("sdwebui 需要 config['out_path']")

        w, h = size or (config.IMAGE_DRAFT_W, config.IMAGE_DRAFT_H)
        payload = {
            "prompt": prompt,
            "negative_prompt": negative or "",
            "width": int(w), "height": int(h),
            "steps": int(self.config.get("steps", 28)),
            "cfg_scale": float(self.config.get("cfg", 7.0)),
            "sampler_name": self.config.get("sampler", "DPM++ 2M Karras"),
            "seed": int(seed or -1),
            "batch_size": 1,
            "send_images": True,
            "save_images": False,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._url() + "/sdapi/v1/txt2img",
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ProviderError("SD WebUI 返回 %s：%s"
                                % (exc.code, exc.read().decode("utf-8", "replace")[:200]),
                                provider="sdwebui") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("SD WebUI 调用失败：%s" % exc,
                                provider="sdwebui") from exc

        imgs = data.get("images") or []
        if not imgs:
            raise ProviderError("SD WebUI 没有返回图片", provider="sdwebui")

        b64 = imgs[0].split(",", 1)[-1] if "," in imgs[0][:80] else imgs[0]
        try:
            _save_b64(b64, out)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError("图片解码失败：%s" % exc,
                                provider="sdwebui") from exc

        info = {}
        try:
            info = json.loads(data.get("info") or "{}")
        except Exception:  # noqa: BLE001
            pass
        used_seed = int(info.get("seed") or seed or 0)

        return ImageResult(Path(out), seed=used_seed, attempts=1,
                           cost=Cost(to_si("0.03"), "local_estimate",
                                     {"note": "本地显存耗时"}),
                           elapsed=time.time() - t0)

    def head_ok(self, path):
        try:
            from ... import image_gen
            cut, why = image_gen.head_is_cut(path)
            return (not cut), why
        except Exception:  # noqa: BLE001
            return True, ""


@register("image", "openai_image")
class OpenAIImage(ImageProvider):
    """OpenAI 兼容的图像生成接口（硅基流动 / 通义万相 等）。

    走 /v1/images/generations，返回 b64 或 URL。
    """

    def _key(self):
        return config.OPENAI_IMAGE_API_KEY or config.SILICONFLOW_API_KEY or ""

    def _base(self):
        return (self.config.get("base_url") or config.OPENAI_IMAGE_BASE_URL
                or "https://api.siliconflow.cn/v1").rstrip("/")

    def _model(self):
        return self.config.get("model") or config.OPENAI_IMAGE_MODEL \
            or "Kwai-Kolors/Kolors"

    def available(self):
        return bool(self._key())

    def cost_estimate(self):
        return to_si(config.OPENAI_IMAGE_PRICE or "0.05")

    def generate(self, prompt, negative, seed, size, ref_image=None, **kwargs):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("openai_image 需要 config['out_path']")

        w, h = size or (config.IMAGE_DRAFT_W, config.IMAGE_DRAFT_H)
        full_prompt = prompt
        if negative:
            full_prompt += "\n（不要出现：%s）" % negative

        payload = {
            "model": self._model(),
            "prompt": full_prompt,
            "n": 1,
            "size": "%dx%d" % (int(w), int(h)),
        }
        if seed:
            payload["seed"] = int(seed)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._base() + "/images/generations", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self._key())

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ProviderError("图像接口 %s：%s"
                                % (exc.code, exc.read().decode("utf-8", "replace")[:200]),
                                provider="openai_image") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("图像接口调用失败：%s" % exc,
                                provider="openai_image") from exc

        items = data.get("data") or []
        if not items:
            raise ProviderError("接口没有返回图片", provider="openai_image")

        item = items[0]
        if item.get("b64_json"):
            _save_b64(item["b64_json"], out)
        elif item.get("url"):
            try:
                with urllib.request.urlopen(item["url"], timeout=120) as r:
                    raw = r.read()
                atomic.atomic_write_bytes(out, raw,
                                          verify=integrity.validate_image)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError("下载生成图失败：%s" % exc,
                                    provider="openai_image") from exc
        else:
            raise ProviderError("返回里既没有 b64 也没有 url",
                                provider="openai_image")

        return ImageResult(Path(out), seed=int(seed or 0), attempts=1,
                           cost=Cost(self.cost_estimate(), "unit_price",
                                     {"model": self._model()}),
                           elapsed=time.time() - t0)

    def head_ok(self, path):
        try:
            from ... import image_gen
            cut, why = image_gen.head_is_cut(path)
            return (not cut), why
        except Exception:  # noqa: BLE001
            return True, ""
