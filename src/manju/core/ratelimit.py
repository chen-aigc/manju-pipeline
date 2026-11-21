# -*- coding: utf-8 -*-
"""限速器：按 provider/环节限制调用频率。

三个维度：
    1. QPS 限速    每秒最多 N 次调用（防 API 429）
    2. 并发上限    同时最多 N 个在飞（防打爆显存/账号）
    3. 日配额      每天最多 N 次（防超预算）

用法：
    from manju.core.ratelimit import limiter

    # 在插件调用前
    limiter.acquire("image/doubao")     # 阻塞直到允许
    ...
    limiter.release("image/doubao")     # 调用结束

    # 或者用上下文管理器
    with limiter.slot("image/doubao"):
        do_call()

配置（config.py）：
    RATE_QPS = "image/doubao=2,video/jimeng=0.5"     # 每秒次数
    RATE_CONCURRENCY = "image/doubao=2,image/comfyui=1"
    RATE_DAILY = "image/doubao=1000,video/jimeng=200"
"""
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager


class RateLimiter:
    """线程安全的限速器。"""

    def __init__(self):
        self._lock = threading.RLock()
        # QPS：每个 key 的时间戳滑动窗口
        self._calls = defaultdict(deque)
        # 并发：每个 key 当前在飞数量
        self._inflight = defaultdict(int)
        # 日配额：每个 key 已用次数
        self._daily = defaultdict(int)
        self._daily_date = time.strftime("%Y-%m-%d")

        # 配置
        self.qps = {}            # {key: float}
        self.concurrency = {}    # {key: int}
        self.daily = {}          # {key: int}

        # 全局默认
        self.default_qps = 0.0        # 0 = 不限
        self.default_concurrency = 0  # 0 = 不限
        self.default_daily = 0        # 0 = 不限

    # ---------------- 配置 ----------------
    @staticmethod
    def _parse(spec):
        """解析 "key=value,key2=value2" 格式。"""
        out = {}
        for part in str(spec or "").split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            try:
                out[k] = float(v.strip()) if "." in v else int(v.strip())
            except ValueError:
                continue
        return out

    def configure(self, qps_spec="", concurrency_spec="", daily_spec="",
                  default_qps=0, default_concurrency=0, default_daily=0):
        """从配置字符串加载规则。"""
        with self._lock:
            self.qps = {k: float(v) for k, v in
                        self._parse(qps_spec).items() if v > 0}
            self.concurrency = {k: int(v) for k, v in
                                self._parse(concurrency_spec).items() if v > 0}
            self.daily = {k: int(v) for k, v in
                          self._parse(daily_spec).items() if v > 0}
            self.default_qps = float(default_qps or 0)
            self.default_concurrency = int(default_concurrency or 0)
            self.default_daily = int(default_daily or 0)
        return self

    def _get(self, mapping, key, default):
        """取配置：精确匹配 → 环节级匹配（image/*）→ 全局默认。"""
        if key in mapping:
            return mapping[key]
        kind = key.split("/")[0] if "/" in key else ""
        if kind and ("%s/*" % kind) in mapping:
            return mapping["%s/*" % kind]
        return default

    # ---------------- 日配额 ----------------
    def _roll_date(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily.clear()
            self._daily_date = today

    # ---------------- 核心：获取一个槽位 ----------------
    def acquire(self, key, timeout=300.0, poll=0.1):
        """阻塞直到可以调用。超时抛 TimeoutError。"""
        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._roll_date()

                # 1. 日配额
                limit_daily = self._get(self.daily, key, self.default_daily)
                if limit_daily > 0 and self._daily[key] >= limit_daily:
                    raise RuntimeError(
                        "已超出 %s 的日配额（%d 次）" % (key, limit_daily))

                # 2. 并发上限
                limit_conc = self._get(self.concurrency, key,
                                       self.default_concurrency)
                if limit_conc > 0 and self._inflight[key] >= limit_conc:
                    wait_reason = "并发"
                else:
                    # 3. QPS 限速（滑动 1 秒窗口）
                    limit_qps = self._get(self.qps, key, self.default_qps)
                    if limit_qps > 0:
                        now = time.time()
                        window = self._calls[key]
                        while window and now - window[0] > 1.0:
                            window.popleft()
                        if len(window) >= limit_qps:
                            wait_reason = "QPS"
                        else:
                            wait_reason = None
                    else:
                        wait_reason = None

                if wait_reason is None:
                    self._inflight[key] += 1
                    self._daily[key] += 1
                    self._calls[key].append(time.time())
                    return self

            if time.time() >= deadline:
                raise TimeoutError(
                    "等待 %s 的限速槽位超时（%s）" % (key, wait_reason))
            time.sleep(poll)

    def release(self, key):
        """调用结束，释放并发槽位。"""
        with self._lock:
            if self._inflight[key] > 0:
                self._inflight[key] -= 1

    @contextmanager
    def slot(self, key, timeout=300.0):
        """上下文管理器：自动 acquire/release。"""
        self.acquire(key, timeout=timeout)
        try:
            yield
        finally:
            self.release(key)

    # ---------------- 状态查询 ----------------
    def status(self):
        """返回各 key 的当前状态（给 Web 用）。"""
        with self._lock:
            self._roll_date()
            keys = set(self.qps) | set(self.concurrency) | set(self.daily) | \
                set(self._inflight)
            out = {}
            for k in sorted(keys):
                if k.endswith("/*"):
                    continue
                out[k] = {
                    "qps_limit": self._get(self.qps, k, self.default_qps),
                    "qps_used": len(self._calls.get(k, ())),
                    "concurrency_limit": self._get(
                        self.concurrency, k, self.default_concurrency),
                    "inflight": self._inflight.get(k, 0),
                    "daily_limit": self._get(self.daily, k,
                                             self.default_daily),
                    "daily_used": self._daily.get(k, 0),
                }
            return out

    def reset_daily(self):
        """手动清零日配额（测试用）。"""
        with self._lock:
            self._daily.clear()
            self._daily_date = time.strftime("%Y-%m-%d")


# 全局单例
limiter = RateLimiter()


def configure_from_config():
    """从 manju.config 加载限速规则。"""
    from .. import config
    limiter.configure(
        qps_spec=getattr(config, "RATE_QPS", ""),
        concurrency_spec=getattr(config, "RATE_CONCURRENCY", ""),
        daily_spec=getattr(config, "RATE_DAILY", ""),
        default_qps=getattr(config, "RATE_DEFAULT_QPS", 0),
        default_concurrency=getattr(config, "RATE_DEFAULT_CONCURRENCY", 0),
        default_daily=getattr(config, "RATE_DEFAULT_DAILY", 0),
    )
    return limiter
