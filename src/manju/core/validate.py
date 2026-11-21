# -*- coding: utf-8 -*-
"""统一的输入校验：名称、路径、参数。

设计原则：
    白名单优先 —— 只允许明确安全的字符
    长度限制 —— 防止资源耗尽
    保留字检查 —— 防止覆盖系统目录
"""
import re

# 项目名/用户名/集名允许的字符：
# 中文、字母、数字、下划线、连字符、空格、点（但不能是纯点）
ALLOWED_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-. ]*$")

# 危险字符（路径分隔符、shell 元字符、控制字符）
DANGEROUS_CHARS = set('\\/:*?"<>|$`;&\'%!(){}[]#~@^\n\r\t\0')

# 保留名（Windows）
RESERVED = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    ".", "..",
}

MAX_NAME_LEN = 64


def validate_name(name, what="名称"):
    """校验项目名/用户名/集名等。

    Returns:
        (ok, error_msg)

    规则：
        1. 非空
        2. 长度 1..64
        3. 不含危险字符
        4. 不是保留名
        5. 不能以点开头/结尾
        6. 不能是纯数字（避免和集号混淆）
    """
    if not isinstance(name, str):
        return False, "%s必须是文本" % what
    s = name.strip()
    if not s:
        return False, "%s不能为空" % what
    if len(s) > MAX_NAME_LEN:
        return False, "%s太长（%d > %d）" % (what, len(s), MAX_NAME_LEN)

    bad = [c for c in s if c in DANGEROUS_CHARS]
    if bad:
        return False, "%s含非法字符：%r" % (what, bad[:3])

    # 控制字符
    if any(ord(c) < 32 for c in s):
        return False, "%s含控制字符" % what

    if s.lower() in RESERVED:
        return False, "%s是系统保留名：%s" % (what, s)

    if s.startswith(".") or s.endswith("."):
        return False, "%s不能以点开头或结尾" % what

    if s.startswith("-"):
        return False, "%s不能以连字符开头" % what

    return True, ""


def validate_username(name):
    """用户名更严格：只允许字母数字下划线，3..32 位。"""
    if not isinstance(name, str):
        return False, "用户名必须是文本"
    if not name:
        return False, "用户名不能为空"
    if len(name) < 3 or len(name) > 32:
        return False, "用户名长度需 3-32 位"
    if not re.match(r"^[a-zA-Z0-9_]+$", name):
        return False, "用户名只能含字母、数字、下划线"
    if name.lower() in RESERVED:
        return False, "用户名是保留字"
    return True, ""


def safe_filename(name, fallback="file"):
    """把任意字符串转成安全文件名。"""
    import re as _re
    if not name:
        return fallback
    s = _re.sub(r'[\\/:*?"<>|$`;&\'%!(){}[\]#~@^\n\r\t\0]', "_", str(name))
    s = s.strip("._ ")
    if not s or s.lower() in RESERVED:
        return fallback
    return s[:100]


def validate_int(value, what="数值", lo=None, hi=None, default=None):
    """校验整数范围。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        if default is not None:
            return True, default
        return False, "%s必须是整数" % what
    if lo is not None and n < lo:
        return False, "%s不能小于 %d" % (what, lo) if default is None else (True, max(lo, n))
    if hi is not None and n > hi:
        return False, "%s不能大于 %d" % (what, hi) if default is None else (True, min(hi, n))
    return True, n
