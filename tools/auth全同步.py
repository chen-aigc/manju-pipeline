# -*- coding: utf-8 -*-
"""让 auth 的所有读方法都先同步外部改动。"""
import io
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

P = r'D:\deepseek文件区\manju\src\manju\core\auth.py'
t = io.open(P, encoding='utf-8').read()
orig = len(t)
changes = []

# 需要重载的方法（读 users 的）
METHODS = [
    "login", "verify", "get_user", "list_users", "permissions_of",
    "can", "delete_user", "update_user", "add_user", "set_password",
]

# 在每个方法体的第一行（with self._lock: 之前）插入 self._load()
for m in METHODS:
    # 匹配 def xxx(self, ...):
    pat = re.compile(
        r'(    def %s\(self[^)]*\):\n(?:\s+"""(?:.|\n)*?"""\n)?)' % m
    )
    match = pat.search(t)
    if not match:
        continue

    header = match.group(1)
    if '_sync_before' in header:
        continue

    # 检查下一行是否已经是 with self._lock:
    after = t[match.end():match.end() + 80]
    if after.lstrip().startswith("with self._lock:"):
        # 插在 with 之前
        replacement = header + "        self._sync_before()\n"
        t = t[:match.end()] + "        self._sync_before()\n" + t[match.end():]
        changes.append("%s 前重载" % m)
    else:
        replacement = header + "        self._sync_before()\n"
        t = t[:match.end()] + "        self._sync_before()\n" + t[match.end():]
        changes.append("%s 前重载" % m)

# 加 _sync_before 方法
ANCHOR = "    def _load(self):"
SYNC_METHOD = '''    def _sync_before(self):
        """操作前同步外部改动（跨进程一致）。"""
        try:
            self._load()
        except Exception:  # noqa: BLE001
            pass

    def _load(self):'''

if "_sync_before(self)" not in t:
    # 找第一个 _load 定义
    idx = t.find("    def _load(self):")
    if idx >= 0:
        t = t[:idx] + SYNC_METHOD[SYNC_METHOD.find("    def _sync_before"):] + t[idx + len("    def _load(self):"):]
        changes.insert(0, "加 _sync_before 方法")

io.open(P, 'w', encoding='utf-8').write(t)
print("auth.py: %d -> %d" % (orig, len(t)))
print()
print("修改:")
for c in changes:
    print("  -", c)
