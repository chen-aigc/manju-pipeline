# -*- coding: utf-8 -*-
"""tts 插件：火山引擎（需要 appid + token）。

edge-tts 免费就够用；火山的音色更自然，按字符计费。
"""
import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..base import ProviderError, TTSProvider, TTSResult
from ..registry import register
from ... import config
from ...core import atomic, integrity
from ...core.cost import Cost, to_si


@register("tts", "volc")
class VolcTTS(TTSProvider):
    """火山引擎语音合成（HTTP 一次性接口）。

    需要 VOLC_TTS_APPID + VOLC_TTS_TOKEN。
    config 可覆盖 voice / cluster / speed。
    """

    DEFAULT_VOICE = "zh_female_shuangkuaisisi_moon_bigtts"

    def available(self):
        return bool(config.VOLC_TTS_APPID and config.VOLC_TTS_TOKEN)

    def cost_estimate(self):
        # 4.5 元/万字符 → 一句 20 字约 0.006 元
        return to_si("0.01125")

    def _voice(self, voice):
        if voice and not voice.startswith("zh-CN-"):
            return voice
        return self.config.get("voice") or self.DEFAULT_VOICE

    def synthesize(self, text, voice, rate=None, pitch=None):
        t0 = time.time()
        out = self.config.get("out_path")
        if not out:
            raise ValueError("volc tts 需要 config['out_path']")

        text = str(text or "").strip()
        if not text:
            raise ProviderError("空文本", provider="volc", retryable=False)

        payload = {
            "app": {"appid": config.VOLC_TTS_APPID,
                    "token": config.VOLC_TTS_TOKEN,
                    "cluster": self.config.get("cluster", "volcano_tts")},
            "user": {"uid": "manju"},
            "audio": {
                "voice_type": self._voice(voice),
                "encoding": "mp3",
                "speed_ratio": self._ratio(rate),
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {"reqid": uuid.uuid4().hex, "text": text,
                        "text_type": "plain", "operation": "query"},
        }

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://openspeech.bytedance.com/api/v1/tts",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer;" + config.VOLC_TTS_TOKEN)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ProviderError("火山 TTS HTTP %s" % exc.code,
                                provider="volc") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("火山 TTS 失败：%s" % exc,
                                provider="volc") from exc

        if data.get("code") not in (3000, 0):
            raise ProviderError("火山 TTS 返回错误：%s %s"
                                % (data.get("code"), data.get("message")),
                                provider="volc")

        raw = base64.b64decode(data.get("data") or "")
        if not raw:
            raise ProviderError("火山 TTS 没有返回音频", provider="volc")

        atomic.atomic_write_bytes(out, raw, verify=integrity.validate_audio)

        from ...tts import probe_duration
        dur = probe_duration(out)
        chars = len(text)
        # 火山官方：超自然大模型音色 6.5 元/万字符（量大可降到 4.9）
        from decimal import Decimal
        price = Decimal(str(self.config.get("price_per_wan", "6.5")))
        amount = (price * Decimal(chars) / Decimal("10000")).quantize(
            Decimal("0.0001"))
        return TTSResult(Path(out), duration=dur,
                         cost=Cost(to_si(amount), "unit_price",
                                   {"chars": chars, "price_per_wan": str(price)}),
                         elapsed=time.time() - t0)

    @staticmethod
    def _ratio(rate):
        """把 '+10%' 转成 1.1 这种倍率。"""
        try:
            s = str(rate or "+0%").replace("%", "").replace("+", "")
            return max(0.5, min(2.0, 1.0 + float(s) / 100.0))
        except Exception:  # noqa: BLE001
            return 1.0

    def voices(self):
        return ["zh_female_shuangkuaisisi_moon_bigtts",
                "zh_male_yangguangqingnian_moon_bigtts",
                "zh_male_ahu_conversation_wvae_bigtts"]
