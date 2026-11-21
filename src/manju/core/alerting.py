# -*- coding: utf-8 -*-
"""告警系统：分级、去重、聚合、多通道、历史。

四个级别：
    info     只记录，不推送
    warn     推送（飞书/Webhook）
    error    推送 + 记录
    critical 推送 + 记录 + 立即标记

去重策略：
    同一 (kind, key) 在 cooldown 秒内只推一次
    默认 300 秒（5 分钟）

聚合策略：
    同一个 key 的告警累计次数，推送时带上"第 N 次"

通道：
    log      控制台（永远有）
    feishu   飞书机器人（配了 FEISHU_WEBHOOK 才启用）
    webhook  自定义 HTTP POST（配了 ALERT_WEBHOOK_URL 才启用）
    desktop  Windows 托盘通知（可选）

存储：
    <projects_dir>/_alerts.json
    最多保留 500 条

用法：
    from manju.core.alerting import alerts

    alerts.emit("cost", "job1/shot_003", "warn",
                "镜头 shot_003 成本 ¥2.70 超阈值", value=2.7, limit=1.5)

    items = alerts.list(limit=50)
    alerts.ack(alert_id)          # 确认（不再重复推送）
    alerts.mute("cost", 3600)     # 静音 1 小时
"""
import json
from .store import JsonStore
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional


LEVELS = ("info", "warn", "error", "critical")
LEVEL_ORDER = {lv: i for i, lv in enumerate(LEVELS)}


class AlertManager:
    """告警管理器。"""

    def __init__(self, store_path=None, cooldown=300, max_keep=500):
        self._lock = threading.RLock()
        self.store_path = Path(store_path) if store_path else None
        self.cooldown = int(cooldown)          # 秒
        self.max_keep = int(max_keep)

        # 存储
        self.items: List[Dict] = []            # 告警历史（新的在后）
        # 去重
        self._last_push: Dict[str, float] = {}  # key -> ts
        self._counts: Dict[str, int] = defaultdict(int)  # key -> 次数
        # 静音
        self._muted: Dict[str, float] = {}     # kind -> 到期时间

        # 配置
        self.min_level = "warn"                # 低于此级别不推送
        self.channels = {"log": True, "feishu": True, "webhook": False}
        self.webhook_url = ""

        # 跨进程同步的持久化层
        self._store = JsonStore(self.store_path, default=lambda: {
            "items": [],
        }) if self.store_path else None

        self._load()

    # ---------------- 配置 ----------------
    def configure(self, min_level=None, feishu=None, webhook_url=None,
                  cooldown=None):
        with self._lock:
            if min_level and min_level in LEVELS:
                self.min_level = min_level
            if feishu is not None:
                self.channels["feishu"] = bool(feishu)
            if webhook_url is not None:
                self.webhook_url = webhook_url
                self.channels["webhook"] = bool(webhook_url)
            if cooldown is not None:
                self.cooldown = int(cooldown)
        return self.config_summary()

    def load_from_config(self):
        """从 manju.config 读配置。"""
        try:
            from .. import config
            self.configure(
                min_level=getattr(config, "ALERT_MIN_LEVEL", "warn"),
                webhook_url=getattr(config, "ALERT_WEBHOOK_URL", ""),
                cooldown=getattr(config, "ALERT_COOLDOWN_SEC", 300),
            )
            feishu = bool(getattr(config, "FEISHU_WEBHOOK", ""))
            self.channels["feishu"] = feishu
        except Exception:  # noqa: BLE001
            pass
        return self.config_summary()

    def config_summary(self):
        self._load()
        return {
            "min_level": self.min_level,
            "channels": dict(self.channels),
            "webhook_url": self.webhook_url[:40] + "..."
                           if len(self.webhook_url) > 40 else self.webhook_url,
            "cooldown": self.cooldown,
            "muted": {k: round(v - time.time(), 0)
                      for k, v in self._muted.items() if v > time.time()},
        }

    # ---------------- 静音 ----------------
    def mute(self, kind, seconds=3600):
        with self._lock:
            self._muted[kind] = time.time() + int(seconds)
        return True, "已静音 %s %d 秒" % (kind, seconds)

    def unmute(self, kind):
        with self._lock:
            self._muted.pop(kind, None)
        return True, "已取消静音 %s" % kind

    def _is_muted(self, kind):
        exp = self._muted.get(kind)
        if not exp:
            return False
        if exp < time.time():
            self._muted.pop(kind, None)
            return False
        return True

    # ---------------- 发送告警 ----------------
    def emit(self, kind, key, level, message, **extra):
        """发一条告警。

        Args:
            kind:    类别（cost / vram / job / provider / consistency）
            key:     唯一标识（用于去重），如 "job1/shot_003"
            level:   info / warn / error / critical
            message: 文本
            extra:   附加上下文（value / limit / job_id / shot_id ...）

        Returns:
            {"id": ..., "pushed": bool, "suppressed": bool, "reason": ...}
        """
        level = level if level in LEVELS else "warn"
        now = time.time()
        full_key = "%s:%s" % (kind, key)

        with self._lock:
            self._counts[full_key] += 1
            count = self._counts[full_key]

            alert = {
                "id": "%d_%d" % (int(now * 1000), self._counts[full_key]),
                "kind": kind,
                "key": key,
                "level": level,
                "message": message,
                "count": count,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "ts_unix": now,
                "acked": False,
                "extra": extra,
            }
            self.items.append(alert)
            if len(self.items) > self.max_keep:
                self.items = self.items[-self.max_keep:]

            # 判断是否推送
            should_push = LEVEL_ORDER[level] >= LEVEL_ORDER[self.min_level]
            muted = self._is_muted(kind)
            last = self._last_push.get(full_key, 0)
            in_cooldown = (now - last) < self.cooldown

            if not should_push:
                reason = "低于推送级别"
            elif muted:
                reason = "已静音"
            elif in_cooldown:
                reason = "冷却中（%d 秒内已发过）" % self.cooldown
            else:
                reason = ""

            pushed = not reason
            if pushed:
                self._last_push[full_key] = now

            self._save()

        # 通道发送（在锁外，避免网络阻塞）
        results = {}
        if pushed:
            results = self._send_all(alert)

        return {
            "id": alert["id"],
            "pushed": pushed,
            "suppressed": not pushed,
            "reason": reason,
            "count": count,
            "channels": results,
        }

    def _send_all(self, alert):
        out = {}
        if self.channels.get("log"):
            out["log"] = self._send_log(alert)
        if self.channels.get("feishu"):
            out["feishu"] = self._send_feishu(alert)
        if self.channels.get("webhook") and self.webhook_url:
            out["webhook"] = self._send_webhook(alert)
        return out

    def _send_log(self, alert):
        sym = {"info": "·", "warn": "!", "error": "✗",
               "critical": "!!"}.get(alert["level"], "·")
        cnt = " (第 %d 次)" % alert["count"] if alert["count"] > 1 else ""
        print("[告警%s] %s%s" % (sym, alert["message"], cnt))
        return {"ok": True}

    def _send_feishu(self, alert):
        try:
            from .. import notify
            title = {
                "warn": "⚠️ 警告",
                "error": "🔴 错误",
                "critical": "🚨 严重",
                "info": "ℹ️ 提示",
            }.get(alert["level"], "提示")
            lines = ["**类别**：%s" % alert["kind"],
                     "**内容**：%s" % alert["message"]]
            if alert["count"] > 1:
                lines.append("**累计**：第 %d 次" % alert["count"])
            for k, v in (alert.get("extra") or {}).items():
                if isinstance(v, (str, int, float)):
                    lines.append("**%s**：%s" % (k, v))
            ok, msg = notify.send(title, "\n".join(lines), alert["level"])
            return {"ok": ok, "msg": msg}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": repr(exc)}

    def _send_webhook(self, alert):
        if not self.webhook_url:
            return {"ok": False, "msg": "未配置"}
        try:
            body = json.dumps(alert, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.webhook_url, data=body,
                                         method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {"ok": resp.status < 400,
                        "msg": "HTTP %s" % resp.status}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "msg": "HTTP %s" % exc.code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": repr(exc)}

    # ---------------- 查询 ----------------
    def list(self, limit=50, level=None, kind=None, only_unacked=False):
        self._load()
        with self._lock:
            items = list(self.items)
        if level:
            items = [x for x in items if x.get("level") == level]
        if kind:
            items = [x for x in items if x.get("kind") == kind]
        if only_unacked:
            items = [x for x in items if not x.get("acked")]
        items.sort(key=lambda x: x.get("ts_unix", 0), reverse=True)
        return items[:limit]

    def stats(self):
        """统计。"""
        self._load()
        with self._lock:
            items = list(self.items)
        by_level = defaultdict(int)
        by_kind = defaultdict(int)
        unacked = 0
        for x in items:
            by_level[x.get("level")] = by_level.get(x.get("level"), 0) + 1
            by_kind[x.get("kind")] = by_kind.get(x.get("kind"), 0) + 1
            if not x.get("acked"):
                unacked += 1
        return {
            "total": len(items),
            "unacked": unacked,
            "by_level": dict(by_level),
            "by_kind": dict(by_kind),
            "min_level": self.min_level,
            "cooldown": self.cooldown,
        }

    def ack(self, alert_id):
        """确认一条告警。"""
        with self._lock:
            for x in self.items:
                if x.get("id") == alert_id:
                    x["acked"] = True
                    self._save()
                    return True, "已确认"
        return False, "告警不存在"

    def ack_all(self):
        with self._lock:
            n = 0
            for x in self.items:
                if not x.get("acked"):
                    x["acked"] = True
                    n += 1
            self._save()
        return True, "已确认 %d 条" % n

    def clear(self):
        """清空历史（保留配置）。"""
        with self._lock:
            self.items = []
            self._last_push.clear()
            self._counts.clear()
            self._save()
        return True, "已清空"

    def reset_cooldowns(self):
        """清空冷却，让告警能立即重发（测试用）。"""
        with self._lock:
            self._last_push.clear()
        return True, "已清空冷却"

    # ---------------- 持久化 ----------------
    def _save(self):
        if not self.store_path:
            return
        try:
            if self._store:
                self._store.save({
                    "alerts": self._alerts,
                    "config": self._config,
                    "muted": self._muted,
                    "counter": self._counter,
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                return
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "items": self.items[-self.max_keep:],
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.store_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _load(self):
        if not self._store:
            return
        try:
            data = self._store.load()   # 自动检测外部改动
            self.items = data.get("items") or []
        except Exception:  # noqa: BLE001
            self.items = []

    def _save(self):
        if not self._store:
            return
        try:
            self._store.save({
                "items": self.items[-self.max_keep:],
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 全局实例
def make_alerts():
    try:
        from .. import config
        path = Path(config.PROJECTS_DIR) / "_alerts.json"
        a = AlertManager(path,
                         cooldown=int(getattr(config, "ALERT_COOLDOWN_SEC", 300)))
        a.load_from_config()
        return a
    except Exception:  # noqa: BLE001
        return AlertManager(None)


alerts = make_alerts()


# ---------------------------------------------------------------- 便捷函数
def cost_alert(job_id, shot_id, amount, limit):
    """成本超阈值告警。"""
    return alerts.emit(
        "cost", "%s/%s" % (job_id, shot_id), "warn",
        "镜头 %s 成本 ¥%.3f 超过阈值 ¥%.2f" % (shot_id, amount, limit),
        value=round(amount, 4), limit=limit,
        job_id=job_id, shot_id=shot_id)


def vram_alert(used_mb, limit_mb):
    """显存超阈值告警。"""
    return alerts.emit(
        "vram", "gpu0", "error" if used_mb > limit_mb else "warn",
        "显存占用 %.0f MB 超过阈值 %.0f MB" % (used_mb, limit_mb),
        value=round(used_mb, 1), limit=limit_mb)


def job_alert(job_id, project, status, reason=""):
    """任务失败/完成告警。"""
    level = "error" if status == "failed" else "info"
    msg = "任务 %s（%s）%s" % (job_id, project,
                               "失败" if status == "failed" else "完成")
    if reason:
        msg += "：%s" % reason
    return alerts.emit("job", job_id, level, msg,
                       job_id=job_id, project=project, status=status)


def consistency_alert(job_id, shot_id, level, notes):
    """一致性告警。"""
    return alerts.emit(
        "consistency", "%s/%s" % (job_id, shot_id),
        "warn" if level == "WARN" else "error",
        "镜头 %s 一致性 %s：%s" % (shot_id, level, "；".join(notes)[:100]),
        job_id=job_id, shot_id=shot_id)
