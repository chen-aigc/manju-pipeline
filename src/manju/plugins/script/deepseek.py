# -*- coding: utf-8 -*-
"""script 插件：DeepSeek（复用现有 llm 模块）。"""
import time

from ..base import ProviderError, ScriptProvider, ScriptResult
from ..registry import register
from ...core.cost import Cost, to_si
from ... import llm, config


@register("script", "deepseek")
class DeepSeekScript(ScriptProvider):
    """调用现有 llm.generate_script，并把 usage 换算成真实成本。"""

    def available(self):
        return bool(config.LLM_API_KEY)

    def cost_estimate(self):
        # 一集剧本约 8000 输出 token，粗估 1 元以内
        return to_si("0.5")

    def generate(self, source, memory, chapter, target_shots):
        if not self.available():
            raise ProviderError("未配置 LLM_API_KEY", provider="deepseek",
                                retryable=False)

        t0 = time.time()
        try:
            data, usage = llm.generate_script(
                source, memory, chapter=chapter,
                target_shots=int(target_shots or 40))
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("剧本生成失败：%s" % exc,
                                provider="deepseek") from exc

        return ScriptResult(data, self._cost(usage), time.time() - t0)

    def summarize(self, text):
        try:
            return llm.summarize(text)
        except Exception:  # noqa: BLE001
            return ""

    def _cost(self, usage):
        """按 DeepSeek 官方价换算（优先使用返回的 usage）。"""
        if not isinstance(usage, dict):
            return Cost.unknown(0, to_si("1.0"), {"note": "无 usage 返回"})

        pin = float(self.config.get("price_input", 2.0))    # 元/百万 token
        pout = float(self.config.get("price_output", 8.0))
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)

        from decimal import Decimal
        amount = (Decimal(str(pin)) * pt + Decimal(str(pout)) * ct) \
            / Decimal("1000000")
        return Cost(to_si(amount), "api_return",
                    {"prompt_tokens": pt, "completion_tokens": ct,
                     "price_input": pin, "price_output": pout})
