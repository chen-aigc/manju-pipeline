# -*- coding: utf-8 -*-
"""成本核算（管理会计版）。

精度：0.0001 元（1 丝）
内部单位：整数"丝"（1 元 = 10000 丝）
运算：全程 Decimal，只在写入那一刻取整

铁律：
    1. 绝不写死 0 —— 查不到就记 unknown
    2. 极小单价不能先取整（会归零）
    3. 失败也要计费
"""
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

SI_PER_YUAN = 10000          # 1 元 = 10000 丝
_Q = Decimal("0.0001")       # 取整精度


class CostSource(str, Enum):
    API_RETURN = "api_return"        # 接口返回实测（积分差 / usage）
    UNIT_PRICE = "unit_price"        # 单价 × 数量
    LOCAL_ESTIMATE = "local_estimate"  # 本地功耗/折旧估算
    UNKNOWN = "unknown"              # 查不到


def to_si(amount) -> int:
    """元 → 丝（四舍五入）。接受 Decimal / str / int / float。

    注意：调用前应先乘完数量，不要先对单价取整。
    """
    if isinstance(amount, int):
        return amount * SI_PER_YUAN
    d = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int((d * SI_PER_YUAN).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_yuan(si) -> Decimal:
    """丝 → 元（Decimal，4 位小数）。"""
    if si is None:
        return Decimal("0")
    return (Decimal(int(si)) / SI_PER_YUAN).quantize(_Q, rounding=ROUND_HALF_UP)


def fmt_yuan(si, digits=4) -> str:
    """丝 → 显示字符串。"""
    if si is None:
        return "未知"
    return ("%." + str(digits) + "f") % float(to_yuan(si))


class Cost:
    """一次调用的成本。

    amount_si 为 None 表示未知；此时 est_min_si / est_max_si 给区间。
    """

    __slots__ = ("amount_si", "source", "detail", "est_min_si", "est_max_si")

    def __init__(self, amount_si=None, source=CostSource.UNIT_PRICE,
                 detail=None, est_min_si=None, est_max_si=None):
        if isinstance(source, str):
            source = CostSource(source)
        self.source = source
        self.amount_si = None if amount_si is None else int(amount_si)
        self.detail = dict(detail or {})
        self.est_min_si = est_min_si
        self.est_max_si = est_max_si

        if source == CostSource.UNKNOWN and self.amount_si is None:
            # 未知成本必须给区间，否则无法做预算保护
            if self.est_min_si is None:
                self.est_min_si = 0
            if self.est_max_si is None:
                self.est_max_si = self.est_min_si

    # ---------------- 构造快捷方式 ----------------
    @classmethod
    def zero(cls, detail=None):
        """确实不花钱（本地免费操作）。"""
        return cls(0, CostSource.UNIT_PRICE, detail)

    @classmethod
    def unknown(cls, est_min_si=0, est_max_si=0, detail=None):
        return cls(None, CostSource.UNKNOWN, detail, est_min_si, est_max_si)

    @classmethod
    def from_api(cls, amount_si, detail=None):
        return cls(amount_si, CostSource.API_RETURN, detail)

    @classmethod
    def from_price(cls, unit_price_yuan, quantity, detail=None):
        """单价 × 数量。全程 Decimal，最后取整。"""
        up = unit_price_yuan if isinstance(unit_price_yuan, Decimal) \
            else Decimal(str(unit_price_yuan))
        q = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
        return cls(to_si(up * q), CostSource.UNIT_PRICE, detail)

    # ---------------- 属性 ----------------
    @property
    def is_known(self):
        return self.amount_si is not None

    @property
    def yuan(self):
        return to_yuan(self.amount_si)

    def __add__(self, other):
        if not isinstance(other, Cost):
            return NotImplemented
        if self.amount_si is not None and other.amount_si is not None:
            return Cost(self.amount_si + other.amount_si, self.source,
                        {**self.detail, **other.detail})
        return Cost.unknown(
            (self.est_min_si or 0) + (other.est_min_si or 0),
            (self.est_max_si or 0) + (other.est_max_si or 0))

    def __repr__(self):
        if self.amount_si is None:
            return "<Cost unknown %s~%s丝>" % (self.est_min_si, self.est_max_si)
        return "<Cost %s元 %s>" % (fmt_yuan(self.amount_si), self.source.value)


class CostLedger:
    """三向汇总台账。全程整数，无浮点误差。"""

    def __init__(self):
        self.by_stage = {}
        self.by_shot = {}
        self.by_provider = {}
        self.records = []
        self.unknown_count = 0
        self.unknown_min_si = 0
        self.unknown_max_si = 0

    def add(self, stage, provider, cost, shot=None, action="", status="ok"):
        rec = {
            "stage": stage,
            "provider": provider,
            "shot": shot,
            "action": action,
            "status": status,
            "amount_si": cost.amount_si,
            "source": cost.source.value,
            "detail": cost.detail,
        }
        self.records.append(rec)

        if cost.amount_si is None:
            self.unknown_count += 1
            self.unknown_min_si += cost.est_min_si or 0
            self.unknown_max_si += cost.est_max_si or 0
        else:
            self.by_stage[stage] = self.by_stage.get(stage, 0) + cost.amount_si
            self.by_provider[provider] = (
                self.by_provider.get(provider, 0) + cost.amount_si)
            if shot is not None:
                self.by_shot[shot] = self.by_shot.get(shot, 0) + cost.amount_si

    @property
    def known_si(self):
        return sum(self.by_stage.values())

    @property
    def total_min_si(self):
        return self.known_si + self.unknown_min_si

    @property
    def total_max_si(self):
        return self.known_si + self.unknown_max_si

    @property
    def has_unknown(self):
        return self.unknown_count > 0

    def summary(self):
        return {
            "known_si": self.known_si,
            "known_yuan": fmt_yuan(self.known_si),
            "unknown_count": self.unknown_count,
            "unknown_min_si": self.unknown_min_si,
            "unknown_max_si": self.unknown_max_si,
            "total_min_yuan": fmt_yuan(self.total_min_si),
            "total_max_yuan": fmt_yuan(self.total_max_si),
            "by_stage": {k: fmt_yuan(v) for k, v in self.by_stage.items()},
            "by_provider": {k: fmt_yuan(v) for k, v in self.by_provider.items()},
        }

    def text(self):
        lines = ["本集成本"]
        for stage, si in sorted(self.by_stage.items()):
            lines.append("  %-6s %s 元" % (stage, fmt_yuan(si)))
        if self.has_unknown:
            lines.append("  未知   %d 次调用  可能 %s ~ %s 元" % (
                self.unknown_count,
                fmt_yuan(self.unknown_min_si), fmt_yuan(self.unknown_max_si)))
            lines.append("  ─────────────────────")
            lines.append("  合计   %s ~ %s 元" % (
                fmt_yuan(self.total_min_si), fmt_yuan(self.total_max_si)))
        else:
            lines.append("  ─────────────────────")
            lines.append("  合计   %s 元" % fmt_yuan(self.known_si))
        return "\n".join(lines)


class LocalCostAccumulator:
    """本地成本累加器：低于 1 丝的余数保留，不丢精度。"""

    def __init__(self):
        self.residual = Decimal("0")

    def add(self, amount_yuan):
        """累加，返回本次应记账的丝数。"""
        d = amount_yuan if isinstance(amount_yuan, Decimal) \
            else Decimal(str(amount_yuan))
        self.residual += d
        si = int((self.residual * SI_PER_YUAN).quantize(
            Decimal("1"), rounding="ROUND_FLOOR"))
        if si:
            self.residual -= Decimal(si) / SI_PER_YUAN
        return si
