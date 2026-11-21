# -*- coding: utf-8 -*-
"""video 插件：可灵 AI（当前 v2.6 / v3 接口）

官方文档（2026-11 核对）：
    创建任务  POST {base}/image-to-video/{model}
    鉴权      Authorization: Bearer <API_KEY>      ← 单个 Key，不是 JWT
    查询任务  GET  {base}/tasks?task_ids=xxx
    状态      submitted / processing / succeeded / failed
    结果      data[].outputs[] 里 type=video 的 url

请求体结构：
    {
      "contents": [
        {"type": "prompt", "text": "..."},
        {"type": "first_frame", "url": "data:image/png;base64,..."}
      ],
      "settings": {"resolution": "1080p", "duration": 5, "audio": "off"},
      "options":  {"watermark_info": {"enabled": false}}
    }

重要限制：
    duration 只能 5 或 10 秒 —— 我们的镜头通常 3 秒，会有浪费（必须知道）
    resolution 只有 720p / 1080p —— 4K 要走 kling-3.0 另一个路径
    settings.audio="native" 时只能 1080p，且不能指定尾帧
    audio=native 表示视频自带对白音，对真人剧很有用（省掉对口型）
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

# 官方计费：单位「积分」（1 积分 = 0.9 元），按秒计
# 来源：kling.ai/document-api/pricing/base/video.md（2026-11 核对）
UNIT_YUAN = 0.9
PRICE_UNITS = {
    # 模型关键字        {分辨率: 积分/秒}
    "kling-4.0-flash": {"720p": 0.6},
    "kling-4.0":       {"720p": 1.0, "1080p": 1.5},
    "kling-3.0":       {"720p": 0.6, "1080p": 0.8, "4k": 3.0},
    "kling-2.6":       {"720p": 0.3, "1080p": 0.5},
    "kling-2.5":       {"720p": 0.3, "1080p": 0.5},
}

# 单段时长限制：4.0 系可 3~20 秒；老的只能 5/10
DURATION_RANGE = {
    "kling-4.0-flash": (3, 20),
    "kling-4.0": (3, 20),
    "kling-3.0": (5, 10),
    "kling-2.6": (5, 10),
    "kling-2.5": (5, 10),
}


@register("video", "kling")
class KlingVideo(VideoProvider):
    """可灵：图生视频（需要 KLING_API_KEY）。

    配置：
        KLING_API_KEY      必填，开放平台拿的 API Key
        KLING_BASE_URL     默认 https://api-singapore.klingai.com
                           国内节点可用 https://api-beijing.klingai.com
        KLING_MODEL        默认 kling-2.6（决定路径 /image-to-video/{model}）
        KLING_DURATION     5 或 10（默认 5，我们镜头一般 3 秒，会浪费 2 秒）
        KLING_AUDIO        off（默认）| native（自带对白音，只能 1080p）
    """

    poll_interval = 5
    max_wait = 900

    # ---------------- 基础 ----------------
    def _key(self):
        return (getattr(config, "KLING_API_KEY", "") or "").strip()

    def _base(self):
        return (getattr(config, "KLING_BASE_URL", "")
                or "https://api-singapore.klingai.com").rstrip("/")

    def _model(self):
        return getattr(config, "KLING_MODEL", "") or "kling-2.6"

    def _range(self):
        m = self._model().lower()
        for kw, rng in DURATION_RANGE.items():
            if kw in m:
                return rng
        return (5, 10)

    def _duration(self, want=3.0):
        """按模型允许范围取整秒。4.0 系可 3 秒，老的只能 5/10。"""
        lo, hi = self._range()
        cfg_d = int(getattr(config, "KLING_DURATION", 0) or 0)
        d = cfg_d if cfg_d else int(round(float(want or lo)))
        d = max(lo, min(hi, d))
        if lo == 5 and d not in (5, 10):      # 老模型只接受 5/10
            d = 10 if d > 5 else 5
        return d

    def _resolution(self, res=None):
        m = self._model().lower()
        r = str(res or getattr(config, "KLING_RESOLUTION", "") or "720p").lower()
        if "4.0-flash" in m:
            return "720p"                      # 官方只支持 720p
        return "1080p" if r in ("1080p", "1080", "full") else "720p"

    def _audio(self):
        return "native" if str(getattr(config, "KLING_AUDIO", "off")).lower() \
            == "native" else "off"

    def available(self):
        return bool(self._key())

    def _units_per_sec(self):
        m = self._model().lower()
        res = self._resolution()
        for kw, table in PRICE_UNITS.items():
            if kw in m:
                return table.get(res) or list(table.values())[0]
        return 1.0

    def cost_estimate(self, duration=5.0):
        secs = self._duration(duration)
        return to_si(self._units_per_sec() * UNIT_YUAN * secs)

    def takes_seconds_async(self):
        return True

    def supports_keyframes(self):
        return True

    # ---------------- HTTP ----------------
    def _headers(self):
        return {"Authorization": "Bearer " + self._key(),
                "Content-Type": "application/json"}

    def _request(self, method, path, payload=None, timeout=60):
        url = self._base() + path
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise ProviderError(
                "可灵 HTTP %s：%s" % (exc.code, body),
                provider="kling",
                retryable=exc.code in (429, 500, 502, 503, 504)) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("可灵请求失败：%s" % exc,
                                provider="kling") from exc

    @staticmethod
    def _unwrap(data):
        """统一处理 {code,message,data} 结构。"""
        code = data.get("code")
        if code not in (0, None):
            raise ProviderError("可灵返回错误 code=%s：%s"
                                % (code, data.get("message")),
                                provider="kling")
        return data.get("data")

    # ---------------- 自检（不花钱） ----------------
    def _bases(self):
        """候选域名：配置的排第一，再试另外两个官方节点。"""
        out = [self._base()]
        for b in ("https://api-beijing.klingai.com",
                  "https://api-singapore.klingai.com"):
            if b not in out:
                out.append(b)
        return out

    def check(self):
        """验证 Key 能不能用：只查任务，不创建任务、不扣费。

        可灵没有「列任务」接口，所以直接 GET /tasks 观察状态码：
            200 / 400 / 422  → 鉴权通过（只是参数不全）
            401 / 403        → Key 无效或没权限
        """
        if not self._key():
            return False, "KLING_API_KEY 未配置"

        last = ""
        for base in self._bases():
            try:
                import urllib.error
                req = urllib.request.Request(base + "/tasks", method="GET")
                req.add_header("Authorization", "Bearer " + self._key())
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=20) as r:
                    r.read()
                return True, "Key 可用（base=%s model=%s）" % (base, self._model())
            except urllib.error.HTTPError as exc:
                code = exc.code
                body = exc.read().decode("utf-8", "replace")[:200]
                if code in (200, 400, 404, 422):
                    # 鉴权过了，只是这个接口参数不全
                    note = ""
                    if base != self._base():
                        note = "；建议 .env 设 KLING_BASE_URL=%s" % base
                    return True, "Key 可用（base=%s）%s" % (base, note)
                if code in (401, 403):
                    last = "HTTP %s：%s" % (code, body)
                    continue
                last = "HTTP %s：%s" % (code, body)
            except Exception as exc:  # noqa: BLE001
                last = "连接失败：%s" % exc

        return False, last[:200] or "无法连接任一节点"

    # ---------------- 生成 ----------------
    def _image_payload(self, image):
        """把图片转成接口要的格式（base64 data URI）。"""
        p = Path(image)
        if p.exists():
            raw = p.read_bytes()
            ext = p.suffix.lower().lstrip(".") or "png"
            if ext == "jpg":
                ext = "jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            fmt = str(getattr(config, "KLING_IMAGE_FORMAT", "data_uri"))
            if fmt == "base64":
                return b64
            return "data:image/%s;base64,%s" % (ext, b64)
        return str(image)          # 已经是 URL

    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None):
        t0 = time.time()
        if not self._key():
            raise ProviderError("可灵未配置 KLING_API_KEY",
                                provider="kling", retryable=False)

        res = self._resolution()
        secs = self._duration(duration)
        audio = self._audio()
        if audio == "native":
            res = "1080p"           # 官方限制：原生音频只能 1080p

        contents = []
        if prompt:
            contents.append({"type": "prompt",
                             "text": str(prompt)[:2500]})
        contents.append({"type": "first_frame",
                         "url": self._image_payload(image)})
        if last_frame and audio != "native":
            contents.append({"type": "last_frame",
                             "url": self._image_payload(last_frame)})

        payload = {
            "contents": contents,
            "settings": {"resolution": res, "duration": secs, "audio": audio},
            "options": {"watermark_info": {"enabled": False}},
        }

        created = self._request("POST", "/image-to-video/" + self._model(),
                                payload)
        info = self._unwrap(created) or {}
        task_id = info.get("id") or info.get("task_id")
        if not task_id:
            raise ProviderError("可灵没有返回 task id：%s"
                                % str(created)[:200], provider="kling")

        deadline = time.time() + self.max_wait
        out = self.config.get("out_path")

        while time.time() < deadline:
            time.sleep(self.poll_interval)
            res_data = self._request("GET", "/tasks?task_ids=" + str(task_id))
            items = res_data.get("data")
            if isinstance(items, dict):
                items = [items]
            if not items:
                continue
            item = items[0]
            status = str(item.get("status") or "").lower()

            if status == "failed":
                raise ProviderError("可灵生成失败：%s"
                                    % (item.get("message") or "未知原因"),
                                    provider="kling",
                                    cost=self.cost_estimate(secs))
            if status != "succeeded":
                continue

            url = ""
            for o in item.get("outputs") or []:
                if o.get("type") == "video" and o.get("url"):
                    url = o["url"]
                    break
            if not url:
                raise ProviderError("可灵任务成功但没有视频地址",
                                    provider="kling")

            # 用真实账单（如果返回了）
            cost = Cost(self.cost_estimate(secs), "unit_price",
                        {"resolution": res, "seconds": secs,
                         "units_per_sec": self._units_per_sec(),
                         "unit_yuan": UNIT_YUAN})
            for b in item.get("billing") or []:
                if b.get("charge_type") == "cash" and b.get("amount"):
                    try:
                        from decimal import Decimal
                        amt = Decimal(str(b["amount"]))
                        cur = (b.get("currency") or "CNY").upper()
                        if cur == "USD":
                            amt = amt * Decimal("7.15")
                        cost = Cost(to_si(amt), "api_return",
                                    {"billing": b})
                    except Exception:  # noqa: BLE001
                        pass
                    break

            if not out:
                return VideoResult(path=None, task_id=str(task_id),
                                   pending=False, duration=secs,
                                   cost=cost,
                                   elapsed=time.time() - t0)

            self._download(url, out)
            return VideoResult(path=Path(out), task_id=str(task_id),
                               pending=False, duration=secs, cost=cost,
                               elapsed=time.time() - t0)

        raise ProviderError("可灵等待超时（%d 秒）" % self.max_wait,
                            provider="kling",
                            cost=self.cost_estimate(secs))

    def _download(self, url, out_path):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("下载可灵视频失败：%s" % exc,
                                provider="kling") from exc
        atomic.atomic_write_bytes(out_path, raw,
                                  verify=integrity.validate_video)
        return Path(out_path)
