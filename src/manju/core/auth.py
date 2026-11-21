# -*- coding: utf-8 -*-
"""用户与权限管理。

四个角色（RBAC）：
    admin     全部权限（含用户管理、删除项目）
    operator  可跑任务、改配置、管素材，不能删项目/改用户
    viewer    只读（看进度、看成片、看报告）
    guest     只能看健康状态

权限矩阵：
    资源            admin  operator  viewer  guest
    健康检查           ✓      ✓        ✓       ✓
    项目列表           ✓      ✓        ✓       ✗
    任务列表/进度       ✓      ✓        ✓       ✗
    提交任务           ✓      ✓        ✗       ✗
    取消任务           ✓      ✓        ✗       ✗
    改优先级/并发      ✓      ✓        ✗       ✗
    改配置             ✓      ✓        ✗       ✗
    改告警阈值         ✓      ✓        ✗       ✗
    上传素材           ✓      ✓        ✗       ✗
    改剧本/回滚        ✓      ✓        ✗       ✗
    选镜头版本         ✓      ✓        ✓       ✗
    建/还原快照        ✓      ✓        ✗       ✗
    删快照             ✓      ✗        ✗       ✗
    删项目/集          ✓      ✗        ✗       ✗
    用户管理           ✓      ✗        ✗       ✗

存储：
    <projects_dir>/_users.json
    密码用 PBKDF2-SHA256 + 每用户独立盐
    会话用签名 token（HMAC），有效期可配

用法：
    from manju.core.auth import auth

    auth.enabled()                  # 是否开启鉴权
    tok = auth.login("admin", "pw") # 登录，返回 token
    user = auth.verify(tok)         # 校验 token，返回用户
    auth.can(user, "job.submit")    # 权限检查
"""
import base64
import hashlib

from .store import JsonStore
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, Optional


# ---------------------------------------------------------------- 权限定义
ROLES = ("admin", "operator", "viewer", "guest")

# 权限点 → 允许的角色
PERMISSIONS = {
    # 只读
    "health.read":     ("admin", "operator", "viewer", "guest"),
    "project.read":    ("admin", "operator", "viewer"),
    "job.read":        ("admin", "operator", "viewer"),
    "media.read":      ("admin", "operator", "viewer"),
    "story.read":      ("admin", "operator", "viewer"),
    "audit.read":      ("admin", "operator", "viewer"),
    "spend.read":      ("admin", "operator", "viewer"),
    "gpu.read":        ("admin", "operator", "viewer"),
    "version.read":    ("admin", "operator", "viewer"),
    "snapshot.read":   ("admin", "operator", "viewer"),
    "plugins.read":    ("admin", "operator", "viewer"),

    # 写（跑任务）
    "job.submit":      ("admin", "operator"),
    "job.cancel":      ("admin", "operator"),
    "job.priority":    ("admin", "operator"),
    "job.concurrency": ("admin", "operator"),

    # 写（配置）
    "settings.write":  ("admin", "operator"),
    "budget.write":    ("admin", "operator"),
    "ratelimit.write": ("admin", "operator"),

    # 写（素材）
    "upload.write":    ("admin", "operator"),
    "version.write":   ("admin", "operator"),      # 归档/回滚剧本
    "version.choose":  ("admin", "operator", "viewer"),   # 选镜头版本（轻操作）
    "snapshot.create": ("admin", "operator"),
    "snapshot.restore": ("admin", "operator"),

    # 危险操作
    "snapshot.delete": ("admin",),
    "project.delete":  ("admin",),
    "chapter.delete":  ("admin",),
    "user.write":      ("admin",),
}

# 角色说明（给界面显示用）
ROLE_INFO = {
    "admin":    "管理员：全部权限",
    "operator": "操作员：跑任务、改配置、管素材",
    "viewer":   "观察者：只读",
    "guest":    "访客：仅健康检查",
}


# ---------------------------------------------------------------- 密码哈希
# ---------------- 密码强度 ----------------
# 常见弱密码（前 100 条够用了，实际项目可以用更大的表）
_WEAK_PASSWORDS = {
    "123456", "12345678", "123456789", "1234567890", "password",
    "password1", "password123", "qwerty", "qwerty123", "abc123",
    "111111", "000000", "123123", "666666", "888888", "admin",
    "admin123", "admin1234", "admin123456", "root", "root123",
    "letmein", "welcome", "welcome1", "monkey", "dragon",
    "master", "sunshine", "iloveyou", "trustno1", "passw0rd",
    "p@ssw0rd", "p@ssword", "1qaz2wsx", "zaq12wsx", "qazwsx",
    "asdfgh", "zxcvbn", "test", "test123", "test1234",
    "abc123456", "a123456789", "woaini", "5201314",
    "woaini1314", "123qwe", "qwe123", "admin888",
    "manju", "manju123", "manju123456",
}

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128


def validate_password(password, username=""):
    """密码强度校验。

    Returns:
        (ok, error_msg)

    规则：
        1. 长度 8..128
        2. 不能是纯数字
        3. 不能是纯字母
        4. 不能是常见弱密码
        5. 不能包含用户名
        6. 不能有 3 个以上连续重复字符
    """
    if not isinstance(password, str):
        return False, "密码必须是文本"
    if len(password) < MIN_PASSWORD_LEN:
        return False, "密码至少 %d 位（当前 %d 位）" % (
            MIN_PASSWORD_LEN, len(password))
    if len(password) > MAX_PASSWORD_LEN:
        return False, "密码不能超过 %d 位" % MAX_PASSWORD_LEN

    low = password.lower()

    # 弱密码表
    if low in _WEAK_PASSWORDS:
        return False, "密码太常见，请换一个"

    # 纯数字 / 纯字母
    has_digit = any(c.isdigit() for c in password)
    has_alpha = any(c.isalpha() for c in password)
    if not has_digit:
        return False, "密码需要包含数字"
    if not has_alpha:
        return False, "密码需要包含字母"

    # 包含用户名
    if username and len(username) >= 3 and username.lower() in low:
        return False, "密码不能包含用户名"

    # 连续重复
    run = 1
    for i in range(1, len(password)):
        if password[i] == password[i - 1]:
            run += 1
            if run >= 4:
                return False, "密码不能有 4 个以上连续相同字符"
        else:
            run = 1

    # 连续递增（1234 / abcd）
    asc_run = 1
    for i in range(1, len(password)):
        a, b = password[i - 1], password[i]
        if a.isdigit() and b.isdigit() and ord(b) == ord(a) + 1:
            asc_run += 1
            if asc_run >= 5:
                return False, "密码不能是连续数字（如 12345）"
        elif a.isalpha() and b.isalpha() and ord(b.lower()) == ord(a.lower()) + 1:
            asc_run += 1
            if asc_run >= 5:
                return False, "密码不能是连续字母（如 abcde）"
        else:
            asc_run = 1

    return True, ""


def password_strength(password):
    """估算密码强度，返回 0-100 的分数和描述。"""
    if not password:
        return 0, "空"
    score = 0

    # 长度分
    n = len(password)
    if n >= 8:
        score += 25
    if n >= 12:
        score += 15
    if n >= 16:
        score += 10

    # 字符种类
    kinds = 0
    if any(c.islower() for c in password):
        kinds += 1
    if any(c.isupper() for c in password):
        kinds += 1
    if any(c.isdigit() for c in password):
        kinds += 1
    if any(not c.isalnum() for c in password):
        kinds += 1
    score += kinds * 10

    # 扣分项
    if password.lower() in _WEAK_PASSWORDS:
        score = min(score, 10)
    if len(set(password)) < n * 0.5:
        score -= 15

    score = max(0, min(100, score))

    if score >= 80:
        desc = "很强"
    elif score >= 60:
        desc = "强"
    elif score >= 40:
        desc = "中等"
    elif score >= 20:
        desc = "弱"
    else:
        desc = "很弱"

    return score, desc


# ---------------- 哈希 ----------------
def _hash_password(password, salt=None, iterations=200000):
    """PBKDF2-SHA256。返回 (hash_hex, salt_hex, iterations)。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        bytes.fromhex(salt), iterations)
    return dk.hex(), salt, iterations


def _check_password(password, record):
    """验证密码。"""
    try:
        h, salt, it = record["hash"], record["salt"], int(record.get("iter", 200000))
    except (KeyError, TypeError, ValueError):
        return False
    calc, _, _ = _hash_password(password, salt, it)
    return hmac.compare_digest(calc, h)


# ---------------------------------------------------------------- token
def _b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())


class AuthManager:
    """用户与权限管理器。"""

    def __init__(self, store_path=None):
        self._lock = threading.RLock()
        self.store_path = Path(store_path) if store_path else None
        self.users: Dict[str, dict] = {}
        self.sessions: Dict[str, dict] = {}    # token -> {user, exp}
        self._revoked: set = set()             # 已吊销的 token
        # 跨进程同步的持久化层
        self._store = JsonStore(self.store_path, default=lambda: {
            "users": {}, "secret": "", "enabled": False,
            "token_ttl": 7 * 24 * 3600,
        }) if self.store_path else None
        self._secret = ""                       # 签名密钥
        self._enabled = False
        self.token_ttl = 7 * 24 * 3600          # 默认 7 天

        self._load()
        self._init_defaults()

    # ---------------- 初始化 ----------------
    def _init_defaults(self):
        """首次启动：创建默认管理员。"""
        with self._lock:
            if not self._secret:
                self._secret = secrets.token_hex(32)
            if not self.users:
                # 默认管理员：admin / 随机密码（打印到控制台）
                pw = secrets.token_urlsafe(9)
                self._add_user_locked("admin", pw, "admin",
                                      display="管理员", must_change=True)
                self._save_locked()
                print("=" * 62)
                print("[鉴权] 首次启动，已创建默认管理员")
                print("       用户名: admin")
                print("       密码  : %s" % pw)
                print("       （请登录后立即修改）" % ())
                print("=" * 62)

    # ---------------- 存储 ----------------
    def _sync_before(self):
        """操作前同步外部改动（跨进程一致）。"""
        try:
            self._load()
        except Exception:  # noqa: BLE001
            pass

    def _load(self):
        if not self._store:
            return
        try:
            data = self._store.load()   # 自动检测外部改动
            self.users = data.get("users") or {}
            self._secret = data.get("secret") or ""
            self._enabled = bool(data.get("enabled"))
            self.token_ttl = int(data.get("token_ttl") or self.token_ttl)

            # 迁移：老用户补 pw_changed_at（用 0 表示"从没改过"）
            migrated = False
            for u in self.users.values():
                if "pw_changed_at" not in u:
                    u["pw_changed_at"] = 0
                    migrated = True
            if migrated:
                self._save_locked()
        except Exception:  # noqa: BLE001
            self.users = {}

    def _save_locked(self):
        if not self._store:
            return
        try:
            self._store.save({
                "users": self.users,
                "secret": self._secret,
                "enabled": self._enabled,
                "token_ttl": self.token_ttl,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            return
        except Exception as exc:  # noqa: BLE001
            print("[auth] 保存失败：%r" % exc)
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "users": self.users,
                "secret": self._secret,
                "enabled": self._enabled,
                "token_ttl": self.token_ttl,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            tmp = self.store_path.with_name(self.store_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.store_path)
        except Exception:  # noqa: BLE001
            pass

    def _save(self):
        with self._lock:
            self._save_locked()

    # ---------------- 用户 ----------------
    def _add_user_locked(self, name, password, role, display="",
                         must_change=False):
        h, salt, it = _hash_password(password)
        self.users[name] = {
            "name": name,
            "display": display or name,
            "role": role if role in ROLES else "viewer",
            "hash": h,
            "salt": salt,
            "iter": it,
            "must_change": bool(must_change),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pw_changed_at": int(time.time()),
            "last_login": "",
            "disabled": False,
        }
        return self.users[name]

    def add_user(self, name, password, role, display="", must_change=False,
                 force=False):
        """新建用户。

        Args:
            force: True 时跳过密码强度校验（仅用于迁移/自动化）。
        """
        self._sync_before()
        name = (name or "").strip()
        if not name:
            return False, "用户名不能为空"
        if role not in ROLES:
            return False, "未知角色：%s（可选：%s）" % (role, ", ".join(ROLES))
        if not force:
            ok, why = validate_password(password, username=name)
            if not ok:
                return False, why
        elif len(password or "") < 6:
            return False, "密码至少 6 位"
        with self._lock:
            if name in self.users:
                return False, "用户已存在：%s" % name
            self._add_user_locked(name, password, role, display, must_change)
            self._save_locked()
        return True, "已创建用户 %s（%s）" % (name, role)

    def update_user(self, name, **fields):
        """改用户属性（role / display / disabled）。"""
        self._sync_before()
        with self._lock:
            u = self.users.get(name)
            if not u:
                return False, "用户不存在：%s" % name
            for k in ("role", "display", "disabled", "must_change"):
                if k in fields:
                    if k == "role" and fields[k] not in ROLES:
                        return False, "未知角色：%s" % fields[k]
                    u[k] = fields[k]
            self._save_locked()
        return True, "已更新 %s" % name

    def delete_user(self, name):
        self._sync_before()
        with self._lock:
            if name == "admin":
                return False, "不能删除 admin"
            if name not in self.users:
                return False, "用户不存在：%s" % name
            del self.users[name]
            # 清掉该用户所有会话并吊销
            dead = [k for k, v in self.sessions.items()
                    if v.get("user") == name]
            for k in dead:
                self._revoked.add(k)
                self.sessions.pop(k, None)
            self._save_locked()
        return True, "已删除 %s" % name

    def set_password(self, name, new_password, force=False):
        """改密码。force=True 时跳过强度校验（仅用于初始迁移）。"""
        self._sync_before()
        if not force:
            ok, why = validate_password(new_password, username=name)
            if not ok:
                return False, why
        if len(new_password or "") < 6:
            return False, "密码至少 6 位"
        with self._lock:
            u = self.users.get(name)
            if not u:
                return False, "用户不存在：%s" % name
            h, salt, it = _hash_password(new_password)
            u.update({"hash": h, "salt": salt, "iter": it,
                      "must_change": False,
                      "pw_changed_at": int(time.time())})
            # 改密码后让该用户的所有会话失效
            dead = [k for k, v in self.sessions.items()
                    if v.get("user") == name]
            for k in dead:
                self._revoked.add(k)
                self.sessions.pop(k, None)
            self._save_locked()
        return True, "已修改 %s 的密码" % name

    def list_users(self):
        """列出用户（不含密码哈希）。"""
        self._sync_before()
        with self._lock:
            out = []
            for name, u in sorted(self.users.items()):
                out.append({
                    "name": name,
                    "display": u.get("display", name),
                    "role": u.get("role", "viewer"),
                    "disabled": bool(u.get("disabled")),
                    "must_change": bool(u.get("must_change")),
                    "created_at": u.get("created_at", ""),
                    "last_login": u.get("last_login", ""),
                })
            return out

    def get_user(self, name):
        self._sync_before()
        with self._lock:
            u = self.users.get(name)
            if not u:
                return None
            return {
                "name": name,
                "display": u.get("display", name),
                "role": u.get("role", "viewer"),
                "disabled": bool(u.get("disabled")),
                "must_change": bool(u.get("must_change")),
            }

    # ---------------- 鉴权开关 ----------------
    def enabled(self):
        """鉴权是否开启。每次读文件，确保多进程一致。"""
        try:
            self._load()
        except Exception:  # noqa: BLE001
            pass
        return bool(self._enabled)

    def set_enabled(self, on):
        with self._lock:
            self._load()        # 先同步外部改动
            self._enabled = bool(on)
            self._save_locked()
        return self._enabled

    # ---------------- 登录 ----------------
    def login(self, name, password):
        """登录，返回 (token, 用户信息) 或 (None, 错误)。"""
        self._sync_before()
        with self._lock:
            u = self.users.get(name)
            if not u or not _check_password(password, u):
                return None, "用户名或密码错误"
            if u.get("disabled"):
                return None, "账号已禁用"

            # 签发 token
            tok = self._sign({
                "user": name,
                "role": u.get("role", "viewer"),
                "iat": int(time.time()),
                "exp": int(time.time()) + self.token_ttl,
                "jti": secrets.token_hex(8),
            })
            self.sessions[tok] = {
                "user": name,
                "role": u.get("role"),
                "exp": int(time.time()) + self.token_ttl,
            }
            u["last_login"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._save_locked()
        return tok, {
            "name": name,
            "display": u.get("display", name),
            "role": u.get("role", "viewer"),
            "must_change": bool(u.get("must_change")),
        }

    def logout(self, token):
        with self._lock:
            self.sessions.pop(token, None)
            if token:
                self._revoked.add(token)
                # 防止无限增长：超过 5000 个就清掉一半
                if len(self._revoked) > 5000:
                    self._revoked = set(list(self._revoked)[-2500:])

    def _sign(self, payload):
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._secret.encode(), body.encode(),
                       hashlib.sha256).hexdigest()[:32]
        return body + "." + sig

    def verify(self, token):
        """校验 token，返回用户信息或 None。"""
        self._sync_before()
        if not token:
            return None
        try:
            body, sig = token.rsplit(".", 1)
        except ValueError:
            return None
        expect = hmac.new(self._secret.encode(), body.encode(),
                          hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            data = json.loads(_b64d(body))
        except Exception:  # noqa: BLE001
            return None
        if int(data.get("exp", 0)) < int(time.time()):
            return None

        name = data.get("user")

        with self._lock:
            # 黑名单：改密码/注销的 token
            if token in self._revoked:
                return None

            # 签发时间早于"密码最后修改时间" → 失效
            u = self.users.get(name)
            if not u or u.get("disabled"):
                return None
            pw_changed = int(u.get("pw_changed_at") or 0)
            if pw_changed and int(data.get("iat", 0)) < pw_changed:
                return None

            return {
                "name": name,
                "display": u.get("display", name),
                "role": u.get("role", "viewer"),
                "must_change": bool(u.get("must_change")),
            }

    # ---------------- 权限检查 ----------------
    def can(self, user, perm):
        """判断用户是否有某权限。

        鉴权关闭时，任何人都是 admin（本地单机模式）。
        """
        self._sync_before()
        if not self._enabled:
            return True
        if not user:
            return False
        role = user.get("role", "guest")
        allowed = PERMISSIONS.get(perm)
        if allowed is None:
            return False
        return role in allowed

    def permissions_of(self, role):
        """某个角色的全部权限点。"""
        self._sync_before()
        return [p for p, roles in PERMISSIONS.items() if role in roles]

    # ---------------- 会话清理 ----------------
    def cleanup_sessions(self):
        """清理过期会话和吊销记录。"""
        now = int(time.time())
        with self._lock:
            self.sessions = {k: v for k, v in self.sessions.items()
                             if v.get("exp", 0) > now}
            # 吊销记录里只保留还没过期的 token
            keep = set()
            for tok in self._revoked:
                try:
                    body, _ = tok.rsplit(".", 1)
                    d = json.loads(_b64d(body))
                    if int(d.get("exp", 0)) > now:
                        keep.add(tok)
                except Exception:  # noqa: BLE001
                    pass
            self._revoked = keep
            return len(self.sessions)


# ---------------------------------------------------------------- 全局实例
def make_auth():
    try:
        from .. import config
        path = Path(config.PROJECTS_DIR) / "_users.json"
        a = AuthManager(path)
        # 从配置读 token 有效期
        ttl = int(getattr(config, "AUTH_TOKEN_TTL_HOURS", 168) or 168)
        a.token_ttl = ttl * 3600
        return a
    except Exception:  # noqa: BLE001
        return AuthManager(None)


auth = make_auth()
