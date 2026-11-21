# -*- coding: utf-8 -*-
"""密码安全审查：算法、强度策略、泄露风险。"""
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\deepseek文件区\manju\src')

from manju import config
from manju.core import auth as A
from pathlib import Path

print("=" * 66)
print("密码机制审查")
print("=" * 66)

# 1. 哈希算法
print()
print("1. 哈希算法")
import inspect
src = inspect.getsource(A)
i = src.find("def _hash_password")
print(src[i:i + 500])

# 2. 密码策略
print()
print("2. 密码策略")
i2 = src.find("def set_password")
print(src[i2:i2 + 400])

# 3. 存储内容
print()
print("3. 存储文件内容（脱敏）")
p = Path(config.PROJECTS_DIR) / "_users.json"
if p.exists():
    d = json.loads(p.read_text(encoding="utf-8"))
    print("  enabled:", d.get("enabled"))
    print("  secret 长度:", len(d.get("secret") or ""))
    print("  token_ttl:", d.get("token_ttl"))
    print("  用户:")
    for name, u in (d.get("users") or {}).items():
        print("    %-10s role=%-9s 有salt=%s iter=%s hash长度=%d "
              "pw_changed_at=%s"
              % (name, u.get("role"),
                 bool(u.get("salt")), u.get("iter"),
                 len(u.get("hash") or ""),
                 u.get("pw_changed_at", "<无>")))
else:
    print("  文件不存在")

# 4. 强度校验函数
print()
print("4. 密码强度校验")
for fn_name in ("validate_password", "_check_password", "check_password_strength"):
    if hasattr(A, fn_name):
        print("  找到:", fn_name)
        break
else:
    print("  ⚠️ 没有独立的强度校验函数")

# 测试各种密码
print()
print("  当前 set_password 的校验规则：")
test_pwds = ["123456", "abc", "a", "", "password", "A" * 100,
             "admin123456", "P@ssw0rd!2024"]
for pw in test_pwds:
    ok = len(pw) >= 6
    print("    %-18r 长度%d → %s" % (pw, len(pw), "接受" if ok else "拒绝"))
