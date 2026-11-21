# -*- coding: utf-8 -*-
"""tts 插件：火山方舟语音合成（v3 单向 SSE 接口）。

认证：
    X-Api-Key: 语音控制台的 API Key / Access Token
    X-Api-Resource-Id: seed-tts-1.0 或 seed-tts-2.0

音色与资源必须匹配：
    *_moon_bigtts / *_mars_bigtts  → seed-tts-1.0
    *_uranus_bigtts                → seed-tts-2.0

接口：
    POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
    SSE 流，每行 data: {"code":0,"data":"<base64 mp3>"}
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


# 已验证可用的 2.0 音色（_uranus_bigtts）
VOICES_2_0 = {
    "zh_female_gaolengyujie_uranus_bigtts": "高冷御姐（女·冷漠）",
    "zh_male_shaonianzixin_uranus_bigtts": "少年梓辛（男·少年）",
    "zh_female_wanwanxiaohe_uranus_bigtts": "湾湾小何（女·温柔）",
    "zh_female_shuangkuaisisi_uranus_bigtts": "爽快思思（女·活泼）",
    "zh_male_yuanboxiaoshu_uranus_bigtts": "渊博小叔（男·沉稳）",
    "zh_male_yangguangqingnian_uranus_bigtts": "阳光青年（男·清亮）",
    "zh_female_tianmeixiaoyuan_uranus_bigtts": "甜美小源（女·少女）",
}

# 1.0 音色（需要 seed-tts-1.0，但你账号没开通）
VOICES_1_0 = {
    "zh_female_shuangkuaisisi_moon_bigtts": "爽快思思 1.0",
    "zh_male_yangguangqingnian_moon_bigtts": "阳光青年 1.0",
    "zh_male_yuanboxiaoshu_moon_bigtts": "渊博小叔 1.0",
    "zh_female_cancan_mars_bigtts": "灿灿 1.0",
}

VOICES = {**VOICES_2_0, **VOICES_1_0}

# 实测的情绪支持矩阵（哪些音色对哪些情绪有反应）
# 数据来自 2026-11-19 实测
EMOTION_SUPPORT = {
    'zh_male_yangguangqingnian_uranus_bigtts': {
        'tender', 'happy', 'sad', 'angry',
        'serious', 'comfort', 'neutral', 'surprised',
    },
    'zh_male_shaonianzixin_uranus_bigtts': {
        'tender', 'sad', 'angry', 'comfort', 'neutral', 'surprised',
    },
    'zh_female_shuangkuaisisi_uranus_bigtts': {
        'happy', 'sad', 'comfort', 'neutral', 'surprised',
    },
    'zh_female_tianmeixiaoyuan_uranus_bigtts': {
        'serious', 'surprised',
    },
    'zh_male_yuanboxiaoshu_uranus_bigtts': {
        'serious', 'comfort',
    },
    'zh_female_gaolengyujie_uranus_bigtts': {
        'serious',
    },
}

# 支持的情绪（部分音色支持）
EMOTIONS = {
    'tender', 'sad', 'angry', 'happy',
    'shocked', 'surprised', 'neutral', 'fearful',
    'disgusted', 'amazed', 'serious', 'comfort',
}

# edge 音色 → 豆包 2.0 音色
EDGE_MAP = {
    'zh-CN-XiaoxiaoNeural': 'zh_female_shuangkuaisisi_uranus_bigtts',
    'zh-CN-YunxiNeural': 'zh_male_yangguangqingnian_uranus_bigtts',
    'zh-CN-YunyangNeural': 'zh_male_yuanboxiaoshu_uranus_bigtts',
    'zh-CN-XiaoyiNeural': 'zh_female_tianmeixiaoyuan_uranus_bigtts',
    'zh-CN-YunjianNeural': 'zh_male_shaonianzixin_uranus_bigtts',
}


@register("tts", "ark")
class ArkTTS(TTSProvider):
    """火山方舟语音合成。

    用语音控制台的 API Key（不是方舟的 ark- Key）。
    成本 4.5 元/万字符。
    """

    DEFAULT_VOICE = "zh_female_gaolengyujie_uranus_bigtts"
    DEFAULT_ENDPOINT = ("https://openspeech.bytedance.com"
                        "/api/v3/tts/unidirectional")

    def available(self):
        return bool(self._key())

    def _key(self):
        """取语音 API Key。优先 VOLC_TTS_TOKEN，其次 VOLC_TTS_API_KEY。"""
        return (getattr(config, 'VOLC_TTS_TOKEN', '')
                or getattr(config, 'VOLC_TTS_API_KEY', '')
                or self.config.get('api_key')
                or '')

    def cost_estimate(self):
        # 4.5 元/万字符 → 一句 20 字约 0.009 元
        return to_si("0.009")

    def voices(self):
        return list(VOICES.items())

    def _endpoint(self):
        e = getattr(config, 'VOLC_TTS_ENDPOINT', '') or ''
        return (e or self.DEFAULT_ENDPOINT).rstrip('/')

    def _resource_for(self, speaker):
        """按音色名推断 Resource ID。"""
        if 'uranus' in speaker:
            return 'seed-tts-2.0'
        if 'moon' in speaker or 'mars' in speaker:
            return 'seed-tts-1.0'
        return 'seed-tts-2.0'

    def _voice(self, voice):
        """解析音色。"""
        if voice and (voice in VOICES or '_bigtts' in str(voice)):
            return voice
        if voice and voice in EDGE_MAP:
            return EDGE_MAP[voice]
        if voice and str(voice).startswith('zh-CN-'):
            return self.DEFAULT_VOICE
        return (self.config.get('voice')
                or getattr(config, 'VOLC_TTS_VOICE', '')
                or self.DEFAULT_VOICE)

    def synthesize(self, text, voice, rate=None, pitch=None):
        t0 = time.time()

        out = self.config.get('out_path')
        if not out:
            raise ValueError("ark tts 需要 config['out_path']")

        text = str(text or '').strip()
        if not text:
            raise ProviderError("空文本", provider="ark", retryable=False)

        key = self._key()
        if not key:
            raise ProviderError(
                "未配置 VOLC_TTS_TOKEN（语音控制台的 API Key，"
                "不是方舟的 ARK_API_KEY）",
                provider="ark", retryable=False)

        speaker = self._voice(voice)
        resource = self._resource_for(speaker)

        audio_params = {
            "format": "mp3",
            "sample_rate": 24000,
        }

        # 语速：-50 ~ 100（0 是正常）
        if rate is not None:
            try:
                r = float(rate)
                # 传入 0.5~2.0 的倍率 → -50~100
                audio_params["speech_rate"] = max(
                    -50, min(100, int((r - 1.0) * 100)))
            except (TypeError, ValueError):
                pass

        # 情绪参数
        #
        # 实测规律（2026-11-19）：
        #   emotion_scale 不传       → 情绪完全无效果
        #   emotion_scale = 0.5      → 返回空音频（报错）
        #   emotion_scale = 1.0      → ✅ 唯一稳定生效
        #   emotion_scale = 1.5      → 部分情绪生效
        #   emotion_scale = 2.0      → 无效果
        #
        # 所以：只要指定了 emotion，就固定传 scale=1.0。
        emotion = self.config.get('emotion')
        if isinstance(rate, str) and rate in EMOTIONS:
            emotion = rate
        if emotion and emotion in EMOTIONS:
            audio_params["emotion"] = emotion
            audio_params["emotion_scale"] = 1.0

        payload = {
            "user": {"uid": "manju"},
            "req_params": {
                "text": text,
                "speaker": speaker,
                "audio_params": audio_params,
            },
        }

        headers = {
            'X-Api-Key': key,
            'X-Api-Resource-Id': resource,
            'X-Api-Request-Id': str(uuid.uuid4()),
            'Content-Type': 'application/json',
        }

        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers=headers,
            method='POST')

        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:400]
            if 'not granted' in body:
                raise ProviderError(
                    "该音色的资源未开通（%s → %s）：%s"
                    % (speaker, resource, body[:200]),
                    provider="ark", retryable=False)
            if 'Invalid X-Api-Key' in body:
                raise ProviderError(
                    "API Key 无效：请用语音控制台的 Key，"
                    "不是方舟的 ARK_API_KEY", provider="ark",
                    retryable=False)
            raise ProviderError(
                "火山 TTS 返回 %d：%s" % (e.code, body),
                provider="ark") from e
        except Exception as e:
            raise ProviderError("火山 TTS 请求失败：%r" % e,
                                provider="ark") from e

        audio_bytes = self._parse_sse(raw)
        if not audio_bytes:
            raise ProviderError(
                "火山 TTS 没有返回音频（响应：%s）"
                % raw[:300].decode('utf-8', 'replace'),
                provider="ark")

        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic.atomic_write_bytes(out_path, audio_bytes)

        ok, why = integrity.validate_audio(out_path)
        if not ok:
            raise ProviderError("音频校验失败：%s" % why, provider="ark")

        dur = _probe_duration(out_path)

        # 4.5 元/万字符
        cost = Cost.from_price(4.5, len(text) / 10000.0,
                               detail={'chars': len(text),
                                       'unit': '元/万字符'})

        return TTSResult(
            Path(out_path),
            duration=dur,
            cost=cost,
            elapsed=time.time() - t0,
        )

    @staticmethod
    def _parse_sse(raw):
        """解析 SSE 流，提取音频。"""
        chunks = []
        text = raw.decode('utf-8', 'replace')

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('data:'):
                line = line[5:].strip()
            if not line or line == '[DONE]':
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(obj, dict):
                continue

            b64 = obj.get('data')
            if isinstance(b64, str) and b64:
                try:
                    decoded = base64.b64decode(b64)
                    if decoded:
                        chunks.append(decoded)
                except Exception:  # noqa: BLE001
                    pass

        # 如果整个响应就是 mp3
        if not chunks and raw[:3] == b'ID3':
            return raw

        return b''.join(chunks)


def _probe_duration(path):
    """探测音频时长。"""
    import shutil
    import subprocess
    import sys as _sys

    ff = shutil.which('ffprobe')
    if not ff:
        ffm = shutil.which('ffmpeg')
        if ffm:
            p = Path(ffm).parent / (
                'ffprobe.exe' if _sys.platform == 'win32' else 'ffprobe')
            if p.exists():
                ff = str(p)

    if ff:
        try:
            proc = subprocess.run(
                [ff, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=nw=1:nk=1', str(path)],
                capture_output=True, text=True, timeout=20)
            if proc.returncode == 0:
                return float(proc.stdout.strip() or 0)
        except Exception:  # noqa: BLE001
            pass

    try:
        return Path(path).stat().st_size * 8 / 128000
    except OSError:
        return 0.0
