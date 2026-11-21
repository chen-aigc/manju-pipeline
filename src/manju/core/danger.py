# -*- coding: utf-8 -*-
"""危险操作保护：二次确认 + 通知。

设计：
    1. 所有危险操作必须先申请一个 token（带 TTL）
    2. 执行时必须带上这个 token，且请求体必须与申请时一致
    3. 执行后自动推送飞书通知（含操作详情和释放空间）

为什么需要 token：
    - 防止误点：前端必须走"申请 → 展示确认 → 执行"三步
    - 防重放：token 用一次就失效
    - 防篡改：执行时的参数必须和申请时一致

用法：
    from manju.core.danger import danger

    # 1. 申请
    tok, preview = danger.request("resource.cleanup",
                                  {"max_safety": "normal"}, user="admin")
    # preview = {"affected": [...] , "freed_mb": 12.2, "summary": "..."}

    # 2. 执行
    ok, msg = danger.execute(tok, {"max_safety": "normal"}, user="admin")
    # 执行后自动发飞书通知
"""
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


# 操作类型 → 描述和危险等级
OPS = {
    "resource.cleanup": {
        "name": "清理资源",
        "level": "warn",
        "desc": "删除临时文件/旧日志/废弃测试项目",
    },
    "resource.lru": {
        "name": "LRU 淘汰",
        "level": "warn",
        "desc": "按最久未用顺序删除文件",
    },
    "project.delete": {
        "name": "删除项目",
        "level": "error",
        "desc": "永久删除整个项目目录",
    },
    "chapter.delete": {
        "name": "删除集",
        "level": "error",
        "desc": "永久删除某一集",
    },
    "snapshot.delete": {
        "name": "删除快照",
        "level": "warn",
        "desc": "删除项目快照",
    },
    "snapshot.restore": {
        "name": "还原快照",
        "level": "warn",
        "desc": "用快照覆盖当前文件",
    },
    "script.rollback": {
        "name": "回滚剧本",
        "level": "warn",
        "desc": "用旧版本覆盖当前剧本",
    },
    "user.delete": {
        "name": "删除用户",
        "level": "error",
        "desc": "永久删除用户账号",
    },
    "alerts.clear": {
        "name": "清空告警",
        "level": "info",
        "desc": "清空所有告警历史",
    },
    "auth.disable": {
        "name": "关闭鉴权",
        "level": "error",
        "desc": "关闭登录验证，所有人可访问",
    },
    "trash.purge": {
        "name": "永久删除",
        "level": "error",
        "desc": "从回收站永久删除，无法还原",
    },
    "trash.clear": {
        "name": "清空回收站",
        "level": "error",
        "desc": "永久删除回收站里的所有内容",
    },
    "batch.delete": {
        "name": "批量删除",
        "level": "warn",
        "desc": "把选中镜头的产物移入回收站",
    },
    "asset.replace": {
        "name": "替换素材",
        "level": "info",
        "desc": "用本地文件替换某镜素材（旧版会备份）",
    },
    "asset.restore": {
        "name": "还原素材版本",
        "level": "warn",
        "desc": "用某个变体覆盖当前镜头素材",
    },
    "asset.choose": {
        "name": "选用版本",
        "level": "info",
        "desc": "标记某版本为当前选用（不覆盖文件）",
    },
    "trash.restore": {
        "name": "还原回收站",
        "level": "info",
        "desc": "把回收站条目还原到原位置",
    },
    "shot.restore": {
        "name": "还原镜头",
        "level": "warn",
        "desc": "用某版本覆盖当前镜头文件",
    },
}

TOKEN_TTL = 300          # 确认 token 有效期（秒）
MAX_PENDING = 50         # 最多同时挂起多少个确认


class DangerManager:
    """危险操作管理器。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._pending: Dict[str, Dict] = {}     # token -> 待执行
        self._history: List[Dict] = []          # 执行记录（内存）

    # ---------------- 申请确认 ----------------
    def request(self, op, params, user="", preview=None):
        """申请一个危险操作的确认 token。

        Args:
            op:      操作类型（见 OPS）
            params:  执行参数
            user:    发起人
            preview: 预览信息（受影响的东西）

        Returns:
            {"token": ..., "op": ..., "info": ..., "preview": ..., "expires_in": ...}
        """
        info = OPS.get(op)
        if not info:
            return None, "未知操作：%s（可选：%s）" % (op, ", ".join(OPS))

        with self._lock:
            self._cleanup_expired()

            # 太多挂起就清最旧的
            if len(self._pending) >= MAX_PENDING:
                oldest = sorted(self._pending.items(),
                                key=lambda x: x[1]["created"])
                for k, _ in oldest[:len(self._pending) - MAX_PENDING + 1]:
                    self._pending.pop(k, None)

            tok = secrets.token_urlsafe(16)
            self._pending[tok] = {
                "op": op,
                "info": info,
                "params": params,
                "params_hash": self._hash_params(params),
                "user": user,
                "preview": preview,
                "created": time.time(),
                "expires": time.time() + TOKEN_TTL,
            }

        return {
            "token": tok,
            "op": op,
            "name": info["name"],
            "level": info["level"],
            "desc": info["desc"],
            "preview": preview,
            "expires_in": TOKEN_TTL,
            "warning": self._warning_text(op, params, preview),
        }, ""

    def _warning_text(self, op, params, preview):
        """生成给人看的警告文案。"""
        info = OPS.get(op, {})
        lines = ["即将执行：**%s**" % info.get("name", op),
                 "说明：%s" % info.get("desc", "")]
        if preview:
            if preview.get("freed_mb"):
                lines.append("将释放：**%.1f MB**" % preview["freed_mb"])
            if preview.get("affected_count"):
                lines.append("影响条目：**%d** 个" % preview["affected_count"])
            if preview.get("summary"):
                lines.append(preview["summary"])
        lines.append("此操作不可撤销，请确认。")
        return "\n".join(lines)

    def _hash_params(self, params):
        """参数归一化后计算哈希。

        关键：要剔除 None 值和空值，
        否则 {"a": 1} 和 {"a": 1, "b": None} 会被当成不同参数。
        """
        if not isinstance(params, dict):
            params = {}

        def norm(v):
            if v is None:
                return "__NONE__"     # 保留占位，但两边一致
            if isinstance(v, (list, tuple)):
                return [norm(x) for x in v]
            if isinstance(v, dict):
                return {k: norm(x) for k, x in v.items()}
            return v

        # 统一：只保留有意义的键（None/空列表/空字符串 视为未传）
        cleaned = {}
        for k, v in params.items():
            if v is None or v == [] or v == "":
                continue
            cleaned[k] = norm(v)

        s = json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    def _cleanup_expired(self):
        now = time.time()
        expired = [k for k, v in self._pending.items() if v["expires"] < now]
        for k in expired:
            self._pending.pop(k, None)

    # ---------------- 校验 ----------------
    def verify(self, token, params=None):
        """校验 token 是否有效（不消费）。

        Args:
            token:  申请时拿到的 token
            params: 执行时的参数。None 表示不校验参数（仅用于查询状态）

        Returns:
            (ok, info_or_error)
        """
        with self._lock:
            self._cleanup_expired()
            p = self._pending.get(token)
            if not p:
                return False, "确认已过期或不存在，请重新发起"

            # 只有显式传了 params 才校验（None 跳过，用于"只看状态"）
            if params is not None:
                if self._hash_params(params) != p["params_hash"]:
                    return False, ("参数与申请时不一致，请重新发起"
                                   "（申请时：%r，现在：%r）"
                                   % (p["params"], params))

            return True, {
                "op": p["op"],
                "name": p["info"]["name"],
                "level": p["info"]["level"],
                "preview": p["preview"],
                "user": p["user"],
                "params": p["params"],
                "expires_in": int(p["expires"] - time.time()),
            }

    # ---------------- 消费（执行） ----------------
    def consume(self, token, params=None):
        """消费 token（执行前调用，保证一次性）。

        重要：执行时必须传 params，用来防止"申请 A 执行 B"。

        Returns:
            (ok, info_or_error)
        """
        with self._lock:
            ok, info = self.verify(token, params)
            if not ok:
                return False, info
            self._pending.pop(token, None)
            return True, info

    # ---------------- 通知 ----------------
    def notify(self, op, params, result, user=""):
        """执行后推送通知（不阻塞主流程）。"""
        try:
            from .. import notify
            info = OPS.get(op, {})
            level = info.get("level", "info")
            name = info.get("name", op)

            lines = ["**操作**：%s" % name,
                     "**发起人**：%s" % (user or "未知"),
                     "**时间**：%s" % time.strftime("%Y-%m-%d %H:%M:%S")]

            if params:
                for k, v in params.items():
                    if isinstance(v, (str, int, float, bool)):
                        lines.append("**%s**：%s" % (k, v))

            if isinstance(result, dict):
                if result.get("freed_mb"):
                    lines.append("**释放空间**：%.1f MB" % result["freed_mb"])
                if result.get("removed_count"):
                    lines.append("**删除条目**：%d 个" % result["removed_count"])
                if result.get("msg"):
                    lines.append("**结果**：%s" % result["msg"])
                if result.get("failed_count"):
                    lines.append("**失败**：%d 个" % result["failed_count"])

            title = {"error": "🔴 危险操作已执行",
                     "warn": "⚠️ 删除操作已执行",
                     "info": "ℹ️ 操作已执行"}.get(level, "操作已执行")

            notify.send(title, "\n".join(lines), level)
        except Exception:  # noqa: BLE001
            pass

    def log(self, op, params, result, user="", success=True):
        """记录到历史。"""
        with self._lock:
            self._history.append({
                "op": op,
                "name": OPS.get(op, {}).get("name", op),
                "params": params,
                "result": (result if isinstance(result, dict)
                           else {"msg": str(result)}),
                "user": user,
                "success": success,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            if len(self._history) > 500:
                self._history = self._history[-500:]

    def history(self, limit=50):
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def pending(self):
        """当前挂起的确认（给界面显示）。"""
        with self._lock:
            self._cleanup_expired()
            return [
                {"token": k[:8] + "...",
                 "op": v["op"],
                 "name": v["info"]["name"],
                 "user": v["user"],
                 "expires_in": int(v["expires"] - time.time())}
                for k, v in self._pending.items()
            ]

    def cancel(self, token):
        with self._lock:
            p = self._pending.pop(token, None)
            return (True, "已取消") if p else (False, "不存在")


# 全局单例
danger = DangerManager()
