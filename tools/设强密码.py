# -*- coding: utf-8 -*-
"""清理测试账号，设置强密码。"""
import json
import secrets
import string
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\deepseek文件区\manju\src')

from manju.core.auth import auth, validate_password, password_strength


def gen_strong_password(length=16):
    """生成强随机密码（避开易混淆字符）。"""
    lower = "abcdefghijkmnpqrstuvwxyz"
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    symbols = "!@#$%^&*-_=+"

    # 保证每类至少一个
    pwd = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    allchars = lower + upper + digits + symbols
    pwd += [secrets.choice(allchars) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


print("=" * 68)
print("清理测试账号 + 设置强密码")
print("=" * 68)
print()

# 1. 当前用户
print("1. 当前用户:")
users = auth.list_users()
for u in users:
    print("   %-10s role=%-9s display=%s"
          % (u.get("name"), u.get("role"), u.get("display", "")))

print()

# 2. 删除测试账号
print("2. 删除测试账号")
to_delete = []
for u in users:
    name = u.get("name")
    # tmpuser 是安全测试垃圾
    if name == "tmpuser":
        to_delete.append(name)
    # op1 / view1 是弱密码测试账号，先删，需要时重建
    elif name in ("op1", "view1"):
        to_delete.append(name)

if to_delete:
    for name in to_delete:
        ok, msg = auth.delete_user(name)
        print("   删除 %-10s %s %s" % (name, "✅" if ok else "❌", msg))
else:
    print("   没有要删的")

print()

# 3. 给 admin 设强密码
print("3. 设置 admin 新密码")
new_admin_pw = gen_strong_password(18)
ok, why = validate_password(new_admin_pw, username="admin")
if not ok:
    print("   ❌ 生成的密码不合规:", why)
else:
    ok2, msg2 = auth.set_password("admin", new_admin_pw, force=True)
    if ok2:
        score, desc = password_strength(new_admin_pw)
        print("   ✅ 已更新")
        print()
        print("   ┌─────────────────────────────────────────────┐")
        print("   │  新密码:  %-34s│" % new_admin_pw)
        print("   │  强度:    %-34s│" % ("%d/100 %s" % (score, desc)))
        print("   │  用户:    admin（管理员）                    │")
        print("   └─────────────────────────────────────────────┘")
    else:
        print("   ❌ 失败:", msg2)

print()

# 4. 重建 op1 / view1（强密码）
print("4. 重建 op1 / view1")
new_pwds = {}
for name, role, display in [
    ("op1", "operator", "操作员"),
    ("view1", "viewer", "观察员"),
]:
    pw = gen_strong_password(16)
    ok, msg = auth.add_user(name, pw, role=role, display=display)
    if ok:
        new_pwds[name] = pw
        print("   创建 %-8s %s  role=%s" % (name, "✅", role))
    else:
        print("   创建 %-8s ❌ %s" % (name, msg))

print()
if new_pwds:
    for name, pw in new_pwds.items():
        print("   %-8s → %s" % (name, pw))

print()
print("=" * 68)
print("最终账号列表")
print("=" * 68)
for u in auth.list_users():
    print("   %-10s role=%-9s disabled=%s"
          % (u.get("name"), u.get("role"), u.get("disabled")))

# 5. 保存凭证到文件
cred_file = r"D:\deepseek文件区\manju\projects\_credentials.txt"
with open(cred_file, "w", encoding="utf-8") as f:
    f.write("漫剧工厂 · 登录凭证\n")
    f.write("生成时间: 2026-11-18\n")
    f.write("=" * 50 + "\n\n")
    f.write("admin    %s   （管理员）\n" % new_admin_pw)
    for name, pw in new_pwds.items():
        role = "操作员" if name == "op1" else "观察员"
        f.write("%-8s %s   （%s）\n" % (name, pw, role))
    f.write("\n注意：此文件含明文密码，用完后请删除。\n")
    f.write("     也可以直接删掉这个文件，用上面的密码登录后自己改。\n")

print()
print("凭证已保存到: %s" % cred_file)
