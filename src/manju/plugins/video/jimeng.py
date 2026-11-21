# -*- coding: utf-8 -*-
"""video 插件：即梦 / 豆包 Seedance（火山方舟）

真实接口（2026-11 核对，来源：火山方舟 API 文档 + 社区 curl 参考）：
    创建  POST {base}/contents/generations/tasks
    查询  GET  {base}/tasks/{id}
    列表  GET  {base}/tasks?page_num=1&page_size=1   ← 自检用，不花钱
    鉴权  Authorization: Bearer <ARK_API_KEY>
    base  https://ark.cn-beijing.volces.com/api/v3

请求体：
    {
      "model": "doubao-seedance-2-0-mini-260615",
      "content": [
        {"type": "text", "text": "提示词"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."},
         "role": "first_frame"}
      ],
      "ratio": "adaptive",
      "duration": 4,
      "resolution": "720p",
      "generate_audio": false
    }

为什么值得用（对比可灵）：
    单价更低（Mini 720p 0.26 元/秒 vs 可灵 0.60）
    最短可 4 秒（可灵 5 秒起售，3 秒镜头会浪费 2 秒）
    单段最长 15~30 秒（可灵 5/10 秒）

注意：
    1080p 反而比可灵贵（2.0 的 1080p 是 2.48 元/秒 vs 可灵 0.80）
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..base import ProviderError, VideoProvider, VideoResult
from ..registry import register
from ... import config
from ...core import atomic, integrity
from ...core.cost import Cost, to_si

# 元/秒（火山方舟国内牌价，美元 × 7.1，2026-10 核对）
PRICE = {
    # 模型关键字       720p   1080p
    "mini":          (0.26, None),
    "fast":          (0.62, None),
    "seedance-2-5":  (1.51, 3.74),
    "seedance-2-0":  (0.99, 2.48),
    "seedance-1-5":  (0.99, 2.48),   # 未在比价表内，按 2.0 牌价估
    "lite-i2v":      (0.60, None),
}

# 单段时长上限
MAX_SEC = {
    "seedance-2-5": 30,
    "seedance-2-0": 15,
    "seedance-1-5": 12,
    "lite-i2v": 10,
}


@register("video", "jimeng")
class JimengVideo(VideoProvider):
    """即梦 / 豆包 Seedance 图生视频。

    配置：
        ARK_API_KEY        必填
        JIMENG_BASE_URL    默认 https://ark.cn-beijing.volces.com/api/v3
        JIMENG_MODEL       默认 doubao-seedance-2-0-mini-260615
        JIMENG_RESOLUTION  默认 720p（1080p 只在 2.0/2.5 上支持）
        JIMENG_DURATION    默认 4（秒，最短 4，比可灵的 5 更省）
        JIMENG_AUDIO       默认 off；native/true 时让模型自带音
    """

    poll_interval = 5
    max_wait = 900

    # ---------------- 基础 ----------------
    def _key(self):
        return (getattr(config, "ARK_API_KEY", "") or "").strip()

    def _base(self):
        return (getattr(config, "JIMENG_BASE_URL", "")
                or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

    def _model(self):
        return (getattr(config, "JIMENG_MODEL", "")
                or "doubao-seedance-2-0-mini-260615")

    def _resolution(self):
        r = str(getattr(config, "JIMENG_RESOLUTION", "720p") or "720p").lower()
        return "1080p" if r in ("1080p", "1080") else "720p"

    def _price(self):
        m = self._model().lower()
        res = self._resolution()
        for kw, (p720, p1080) in PRICE.items():
            if kw in m:
                if res == "1080p":
                    return p1080 or p720
                return p720
        try:
            return float(getattr(config, "JIMENG_PRICE_PER_SEC", 0.60))
        except (TypeError, ValueError):
            return 0.60

    def _duration(self, want=3.0):
        m = self._model().lower()
        cap = 15
        for kw, mx in MAX_SEC.items():
            if kw in m:
                cap = mx
                break
        cfg_d = int(getattr(config, "JIMENG_DURATION", 4) or 4)
        d = cfg_d if cfg_d else int(round(float(want or 4)))
        return max(4, min(cap, d))

    def available(self):
        return bool(self._key())

    def cost_estimate(self, duration=4.0):
        return to_si(self._price() * float(self._duration(duration)))

    def takes_seconds_async(self):
        return True

    def supports_keyframes(self):
        return True

    # ---------------- HTTP ----------------
    def _request(self, method, path, payload=None, timeout=60):
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
            body = exc.read().decode("utf-8", "replace")[:400]
            if "ModelNotOpen" in body:
                raise ProviderError(
                    "模型没开通：请在方舟控制台「开通管理」里开通 %s\n"
                    "  （控制台能查到模型 ≠ 已开通，必须点「开通」）"
                    % self._model(),
                    provider="jimeng", retryable=False) from exc
            raise ProviderError(
                "即梦 HTTP %s：%s" % (exc.code, body),
                provider="jimeng",
                retryable=exc.code in (429, 500, 502, 503, 504)) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("即梦请求失败：%s" % exc,
                                provider="jimeng") from exc

    # ---------------- 自检（不花钱） ----------------
    def check(self):
        """验证 Key 和服务开通情况；只查列表/模型，不创建任务、不扣费。"""
        if not self._key():
            return False, "ARK_API_KEY 未配置"

        # 1) 鉴权 + 服务开通：列任务列表（正确路径带 /contents/generations/）
        try:
            self._request("GET",
                          "/contents/generations/tasks?page_num=1&page_size=1")
        except ProviderError as exc:
            msg = str(exc)
            if "404" in msg:
                return False, "接口 404：检查 JIMENG_BASE_URL 是否是 /api/v3"
            return False, msg[:200]

        # 2) 看这个账号能不能用配置里的模型
        note = ""
        try:
            data = self._request("GET", "/models")
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            want = self._model()
            if want not in ids:
                near = [i for i in ids if "seedance" in i.lower()]
                if near:
                    return False, ("模型 %s 不在账号可用列表里；可用的是：%s"
                                   % (want, ", ".join(near[:6])))
                note = "（账号未列出任何 seedance 模型，可能没开通）"
        except ProviderError:
            pass

        return True, "Key 可用（model=%s res=%s 单价 %.2f 元/秒 最短 %d 秒）%s" % (
            self._model(), self._resolution(), self._price(),
            self._duration(99), note)

    def list_models(self):
        """列出账号可用的 seedance 模型（给排查用）。"""
        try:
            data = self._request("GET", "/models")
        except ProviderError:
            return []
        return [m.get("id") for m in (data.get("data") or [])
                if m.get("id") and "seedance" in str(m.get("id")).lower()]

    # ---------------- 生成 ----------------
    def _image_url(self, image):
        p = Path(image)
        if p.exists():
            raw = p.read_bytes()
            ext = p.suffix.lower().lstrip(".") or "png"
            if ext == "jpg":
                ext = "jpeg"
            return "data:image/%s;base64,%s" % (
                ext, base64.b64encode(raw).decode("ascii"))
        return str(image)

    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None):
        t0 = time.time()
        if not self._key():
            raise ProviderError("即梦未配置 ARK_API_KEY",
                                provider="jimeng", retryable=False)

        res = self._resolution()
        secs = self._duration(duration)
        audio = bool(getattr(config, "JIMENG_AUDIO", "")) and \
            str(getattr(config, "JIMENG_AUDIO", "")).lower() not in ("off", "0", "false")

        content = [{"type": "text", "text": str(prompt or "")[:2500]}]
        content.append({"type": "image_url",
                        "image_url": {"url": self._image_url(image)},
                        "role": "first_frame"})
        if last_frame:
            content.append({"type": "image_url",
                            "image_url": {"url": self._image_url(last_frame)},
                            "role": "last_frame"})

        payload = {
            "model": self._model(),
            "content": content,
            "ratio": "adaptive",        # 图生视频保持原图比例
            "duration": secs,
            "resolution": res,
            "generate_audio": bool(audio),
        }

        created = self._request("POST", "/contents/generations/tasks", payload)
        task_id = created.get("id")
        if not task_id:
            raise ProviderError("即梦没有返回任务 id：%s"
                                % str(created)[:200], provider="jimeng")

        deadline = time.time() + self.max_wait
        out = self.config.get("out_path")

        while time.time() < deadline:
            time.sleep(self.poll_interval)
            item = self._request("GET", "/contents/generations/tasks/" + str(task_id))
            status = str(item.get("status") or "").lower()

            if status in ("failed", "expired", "cancelled"):
                raise ProviderError(
                    "即梦生成失败：%s" % (item.get("error") or status),
                    provider="jimeng", cost=self.cost_estimate(secs))
            if status != "succeeded":
                continue

            url = ""
            body = item.get("content") or {}
            if isinstance(body, dict):
                url = body.get("video_url") or body.get("url") or ""
            if not url:
                for k in ("video_url", "url", "file_url"):
                    if isinstance(item.get(k), str) and item[k]:
                        url = item[k]
                        break
            if not url:
                raise ProviderError("即梦任务成功但没找到视频地址：%s"
                                    % str(item)[:300], provider="jimeng")

            if not out:
                return VideoResult(path=None, task_id=str(task_id),
                                   pending=False, duration=secs,
                                   cost=Cost(self.cost_estimate(secs),
                                             "unit_price",
                                             {"per_second": self._price(),
                                              "seconds": secs}),
                                   elapsed=time.time() - t0)

            self._download(url, out)
            return VideoResult(path=Path(out), task_id=str(task_id),
                               pending=False, duration=secs,
                               cost=Cost(self.cost_estimate(secs), "unit_price",
                                         {"per_second": self._price(),
                                          "seconds": secs,
                                          "model": self._model(),
                                          "resolution": res}),
                               elapsed=time.time() - t0)

        raise ProviderError("即梦等待超时（%d 秒）" % self.max_wait,
                            provider="jimeng",
                            cost=self.cost_estimate(secs))

    def _download(self, url, out_path):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("下载即梦视频失败：%s" % exc,
                                provider="jimeng") from exc
        atomic.atomic_write_bytes(out_path, raw,
                                  verify=integrity.validate_video)
        return Path(out_path)
