# -*- coding: utf-8 -*-
"""script 插件：所有 OpenAI 兼容接口的大模型。

同一个类，注册成多家：
    qwen        通义千问（阿里云 DashScope 兼容模式）
    kimi        Moonshot
    glm         智谱 GLM
    doubao      火山方舟（豆包）
    siliconflow 硅基流动（聚合多家）
    openai      OpenAI
    ollama      本地 Ollama
"""
import json
import time
import urllib.error
import urllib.request

from ..base import ProviderError, ScriptProvider, ScriptResult
from ..registry import register
from ... import config, llm
from ...core.cost import Cost, to_si


class OpenAICompatScript(ScriptProvider):
    """通用 OpenAI 兼容聊天接口。

    子类只需设置 profile：环境变量名、base_url、默认模型、单价。
    """

    profile = {
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "price_in": 1.0,        # 元/百万 token
        "price_out": 3.0,
    }

    # ---------------- 基础 ----------------
    def _key(self):
        return getattr(config, self.profile["env_key"], "") or ""

    def _base(self):
        return (self.config.get("base_url")
                or getattr(config, self.profile.get("env_url", ""), "")
                or self.profile["base_url"]).rstrip("/")

    def _model(self):
        return self.config.get("model") or self.profile["model"]

    def available(self):
        return bool(self._key())

    def cost_estimate(self):
        return to_si("0.5")

    # ---------------- 调用 ----------------
    def _chat(self, messages, temperature=0.8, max_tokens=8000):
        payload = {
            "model": self._model(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._base() + "/chat/completions", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self._key())

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ProviderError("%s 返回 %s：%s"
                                % (self.name, exc.code, detail),
                                provider=self.name) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("%s 调用失败：%s" % (self.name, exc),
                                provider=self.name) from exc

        try:
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("%s 返回格式异常：%s"
                                % (self.name, str(data)[:200]),
                                provider=self.name) from exc

        return text, data.get("usage") or {}

    def _cost(self, usage):
        if not usage:
            return Cost.unknown(0, to_si("1.0"), {"note": "无 usage"})
        from decimal import Decimal
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        amount = (Decimal(str(self.profile["price_in"])) * pt
                  + Decimal(str(self.profile["price_out"])) * ct) / Decimal("1000000")
        return Cost(to_si(amount), "api_return",
                    {"prompt_tokens": pt, "completion_tokens": ct,
                     "model": self._model()})

    def generate(self, source, memory, chapter, target_shots):
        t0 = time.time()
        ctx = ""
        if memory is not None:
            try:
                ctx = memory.context("script")
            except Exception:  # noqa: BLE001
                ctx = ""

        system = llm.SYSTEM_PROMPT
        user = []
        if ctx:
            user.append(ctx)
        user.append("【本章要求】第 %d 章，拆成约 %d 个镜头。\n【素材】%s"
                    % (chapter, int(target_shots or 40), source or "自由发挥"))
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": "\n\n".join(user)}]

        text, usage = self._chat(messages)
        data = llm._extract_json(text)
        if not data or not data.get("shots"):
            raise ProviderError("%s 返回的 JSON 里没有 shots，模型：%s"
                                % (self.name, self._model()), provider=self.name)

        data.setdefault("chapter", chapter)
        for i, s in enumerate(data["shots"], 1):
            s.setdefault("index", i)

        if memory is not None:
            try:
                memory.set_plot(chapter=chapter,
                                progress=(data.get("summary") or "")[:200])
                memory.add_summary("第%d章：%s" % (chapter,
                                                  (data.get("summary") or "")[:120]))
            except Exception:  # noqa: BLE001
                pass

        return ScriptResult(data, self._cost(usage), time.time() - t0)

    def summarize(self, text):
        try:
            out, _ = self._chat(
                [{"role": "user",
                  "content": "用 150 字总结这段剧情：\n" + str(text)[:4000]}],
                temperature=0.3, max_tokens=600)
            return out.strip()
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------- 注册各家
@register("script", "qwen")
class QwenScript(OpenAICompatScript):
    profile = {
        "env_key": "DASHSCOPE_API_KEY", "env_url": "DASHSCOPE_BASE_URL",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus", "price_in": 0.8, "price_out": 2.0,
    }


@register("script", "kimi")
class KimiScript(OpenAICompatScript):
    profile = {
        "env_key": "MOONSHOT_API_KEY", "env_url": "MOONSHOT_BASE_URL",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k", "price_in": 12.0, "price_out": 12.0,
    }


@register("script", "glm")
class GlmScript(OpenAICompatScript):
    profile = {
        "env_key": "ZHIPU_API_KEY", "env_url": "ZHIPU_BASE_URL",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash", "price_in": 0.1, "price_out": 0.1,
    }


@register("script", "doubao")
class DoubaoScript(OpenAICompatScript):
    profile = {
        "env_key": "ARK_API_KEY", "env_url": "ARK_BASE_URL",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k", "price_in": 0.8, "price_out": 2.0,
    }


@register("script", "siliconflow")
class SiliconFlowScript(OpenAICompatScript):
    profile = {
        "env_key": "SILICONFLOW_API_KEY", "env_url": "SILICONFLOW_BASE_URL",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-72B-Instruct", "price_in": 4.0, "price_out": 4.0,
    }


@register("script", "openai")
class OpenAIScript(OpenAICompatScript):
    profile = {
        "env_key": "OPENAI_API_KEY", "env_url": "OPENAI_BASE_URL",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "price_in": 1.0, "price_out": 3.0,
    }


@register("script", "ollama")
class OllamaScript(OpenAICompatScript):
    """本地 Ollama，不花钱。"""

    profile = {
        "env_key": "OLLAMA_HOST", "env_url": "OLLAMA_BASE_URL",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b", "price_in": 0.0, "price_out": 0.0,
    }

    def _key(self):
        return "ollama"          # Ollama 不校验 key

    def available(self):
        try:
            import urllib.request as u
            base = self._base().replace("/v1", "")
            with u.urlopen(base + "/api/tags", timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _cost(self, usage):
        return Cost.zero({"note": "本地推理"})
