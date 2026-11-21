# -*- coding: utf-8 -*-
"""video 插件：可灵 / 即梦 / Vidu / 海螺（云端图生视频）。

说明：
    这些平台的鉴权和参数各不相同，代码按各自公开接口风格实现。
    填了对应 key 才会显示可用；没填时下拉里仍能选，但会提示未配置。

    成本按秒计费，单价从配置读（元/秒），可在 .env 调。
"""
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


class CloudVideoBase(VideoProvider):
    """云端图生视频的公共骨架：提交 → 轮询 → 下载。"""

    env_key = ""
    price_per_sec = 0.4
    poll_interval = 5
    max_wait = 900

    # ---------------- 子类实现 ----------------
    def _submit(self, image_path, prompt, duration):
        """提交任务，返回 task_id。"""
        raise NotImplementedError

    def _poll(self, task_id):
        """查询任务，返回 (done, video_url_or_path, err)。"""
        raise NotImplementedError

    # ---------------- 公共 ----------------
    def _key(self):
        return getattr(config, self.env_key, "") or ""

    def available(self):
        return bool(self._key())

    def cost_estimate(self, duration=3.0):
        return to_si(float(self.price_per_sec) * float(duration))

    def takes_seconds_async(self):
        return True

    def _download(self, url, out_path):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                raw = r.read()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("下载视频失败：%s" % exc,
                                provider=self.name) from exc
        atomic.atomic_write_bytes(out_path, raw,
                                  verify=integrity.validate_video)
        return Path(out_path)

    def generate(self, image, prompt, duration=3.0,
                 first_frame=None, last_frame=None):
        t0 = time.time()
        if not self.available():
            raise ProviderError("%s 未配置 API Key（%s）"
                                % (self.name, self.env_key),
                                provider=self.name, retryable=False)

        task_id = self._submit(image, prompt, duration)
        deadline = time.time() + self.max_wait
        out = self.config.get("out_path")

        while time.time() < deadline:
            time.sleep(self.poll_interval)
            done, url, err = self._poll(task_id)
            if err:
                raise ProviderError("%s 生成失败：%s" % (self.name, err),
                                    provider=self.name,
                                    cost=self.cost_estimate(duration))
            if not done:
                continue
            if not out:
                return VideoResult(path=None, task_id=task_id, pending=False,
                                   duration=duration,
                                   cost=self.cost_estimate(duration),
                                   elapsed=time.time() - t0)
            path = self._download(url, out)
            return VideoResult(path=path, task_id=task_id, pending=False,
                               duration=duration,
                               cost=Cost(self.cost_estimate(duration),
                                         "unit_price",
                                         {"per_sec": self.price_per_sec,
                                          "seconds": duration}),
                               elapsed=time.time() - t0)

        raise ProviderError("%s 等待超时（%d 秒）" % (self.name, self.max_wait),
                            provider=self.name,
                            cost=self.cost_estimate(duration))

    def _post(self, url, payload, headers=None, timeout=60):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ProviderError("%s HTTP %s：%s"
                                % (self.name, exc.code,
                                   exc.read().decode("utf-8", "replace")[:200]),
                                provider=self.name) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("%s 请求失败：%s" % (self.name, exc),
                                provider=self.name) from exc

    def _get(self, url, headers=None, timeout=60):
        req = urllib.request.Request(url, method="GET")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ProviderError("%s HTTP %s：%s"
                                % (self.name, exc.code,
                                   exc.read().decode("utf-8", "replace")[:200]),
                                provider=self.name) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("%s 请求失败：%s" % (self.name, exc),
                                provider=self.name) from exc

    def _image_b64(self, image):
        import base64
        raw = Path(image).read_bytes()
        return base64.b64encode(raw).decode("ascii")


@register("video", "vidu")
class ViduVideo(CloudVideoBase):
    """Vidu：需要 VIDU_API_KEY。"""

    env_key = "VIDU_API_KEY"

    def _headers(self):
        return {"Authorization": "Token " + self._key()}

    def _submit(self, image_path, prompt, duration):
        data = self._post(
            "https://api.vidu.cn/ent/v2/img2video",
            {"model": "vidu2.0",
             "images": ["data:image/png;base64," + self._image_b64(image_path)],
             "prompt": prompt or "",
             "duration": int(min(8, max(4, round(float(duration))))),
             "resolution": "720p"},
            self._headers())
        return data.get("task_id") or data.get("id", "")

    def _poll(self, task_id):
        d = self._get("https://api.vidu.cn/ent/v2/tasks/" + str(task_id),
                      self._headers())
        state = d.get("state")
        if state == "success":
            url = (d.get("creations") or [{}])[0].get("url")
            return (True, url, None) if url else (False, None, "没有视频地址")
        if state == "failed":
            return False, None, str(d.get("err_code") or "失败")
        return False, None, None


@register("video", "hailuo")
class HailuoVideo(CloudVideoBase):
    """海螺（MiniMax）：需要 HAILUO_API_KEY + HAILUO_GROUP_ID。"""

    env_key = "HAILUO_API_KEY"
    price_per_sec = 0.3

    def available(self):
        return bool(config.HAILUO_API_KEY and config.HAILUO_GROUP_ID)

    def _headers(self):
        return {"Authorization": "Bearer " + self._key()}

    def _submit(self, image_path, prompt, duration):
        data = self._post(
            "https://api.minimax.chat/v1/video_generation"
            "?GroupId=" + str(config.HAILUO_GROUP_ID),
            {"model": "video-01",
             "first_frame_image": "data:image/png;base64,"
                                  + self._image_b64(image_path),
             "prompt": prompt or ""},
            self._headers())
        return (data.get("task_id") or "")

    def _poll(self, task_id):
        d = self._get(
            "https://api.minimax.chat/v1/query/video_generation"
            "?task_id=" + str(task_id), self._headers())
        status = d.get("status")
        if status == "Success":
            fid = d.get("file_id")
            if not fid:
                return False, None, "没有 file_id"
            f = self._get("https://api.minimax.chat/v1/files/retrieve"
                          "?GroupId=%s&file_id=%s"
                          % (config.HAILUO_GROUP_ID, fid), self._headers())
            url = ((f.get("file") or {}).get("download_url"))
            return (True, url, None) if url else (False, None, "没有下载地址")
        if status == "Fail":
            return False, None, "生成失败"
        return False, None, None
