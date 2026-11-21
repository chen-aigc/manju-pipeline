# -*- coding: utf-8 -*-
"""诊断：为什么 viewer 能关闭鉴权。"""
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\deepseek文件区\manju\src')

BASE = "http://127.0.0.1:8501"


def http(method, path, data=None, token=None):
    h = {"Content-Type": "application/json"}
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, repr(e)


from manju.core.auth import auth
auth.set_enabled(True)
print("鉴权已开:", auth.enabled())
print()

# 1. 权限检查
print("=" * 62)
print("1. 权限矩阵")
print("=" * 62)
for role in ("admin", "operator", "viewer", "guest"):
    perms = auth.permissions_of(role)
    has = "user.write" in perms
    print("  %-10s user.write: %-4s  权限数: %d"
          % (role, "✅" if has else "❌", len(perms)))

print()
print("  用 can() 检查:")
for role in ("admin", "operator", "viewer"):
    u = {"name": "x", "role": role}
    print("    can(%s, user.write) = %s" % (role, auth.can(u, "user.write")))

# 2. _perm_for
print()
print("=" * 62)
print("2. _perm_for 判定")
print("=" * 62)
from manju.web.server import _perm_for
for m in ("GET", "POST"):
    print("  %s /api/auth/enabled -> %r" % (m, _perm_for(m, "/api/auth/enabled")))

# 3. 实际测试
print()
print("=" * 62)
print("3. 实际请求")
print("=" * 62)
st, body = http("POST", "/api/auth/login",
                {"username": "view1", "password": "k5KQ8FayXUt#J4U3"})
print("  viewer 登录:", st)
if st != 200:
    print("  登录失败:", body[:150])
else:
    vt = json.loads(body)["token"]
    st_me, me = http("GET", "/api/auth/me", token=vt)
    print("  viewer /me:", st_me, me[:200])
    print()

    tests = [
        ("GET", "/api/users", None),
        ("POST", "/api/settings", {}),
        ("POST", "/api/auth/enabled", {"enabled": True}),
    ]
    for m, p, d in tests:
        s2, b2 = http(m, p, d, vt)
        expect = 403
        mark = "✅" if s2 == expect else "❌"
        print("  %s viewer %-4s %-22s -> %s %s"
              % (mark, m, p, s2, b2[:80] if s2 != 403 else ""))

# 4. 中间件源码
print()
print("=" * 62)
print("4. 中间件源码")
print("=" * 62)
from manju.web import server as srv
import inspect
print(inspect.getsource(srv.auth_middleware))
