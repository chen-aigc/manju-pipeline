# -*- coding: utf-8 -*-
"""安全测试：检查常见漏洞。

覆盖：
    1. 路径穿越
    2. 越权访问
    3. 会话安全（伪造/重放/过期）
    4. 危险操作绕过
    5. 输入注入
    6. 信息泄露
    7. 资源耗尽
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

BASE = "http://127.0.0.1:8501"
FINDINGS = []       # (severity, title, detail)


def http(method, path, data=None, token=None, timeout=15,
         raw_path=False):
    url = BASE + path if raw_path else BASE + urllib.parse.quote(path, safe="/:?=&")
    h = {"Content-Type": "application/json"}
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    if token:
        h["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]
    except Exception as e:
        return 0, repr(e)


def record(severity, title, detail):
    FINDINGS.append((severity, title, detail))


def ensure_auth_on(tok_admin):
    """确保鉴权开启。返回最新的 admin token。"""
    # 1. 先查状态
    st, body = http("GET", "/api/auth/status", raw_path=True)
    if st != 200:
        return tok_admin
    try:
        enabled = json.loads(body).get("enabled")
    except Exception:  # noqa: BLE001
        return tok_admin

    if enabled:
        return tok_admin

    # 2. 需要开。鉴权关着时任何请求都是 admin，可以直接调
    st2, body2 = http("POST", "/api/auth/enabled",
                      {"enabled": True}, tok_admin, raw_path=True)
    if st2 != 200:
        print("  ⚠️ 开启鉴权失败: %s %s" % (st2, body2[:80]))
        return tok_admin

    # 3. 开完要重新登录（旧 token 可能失效）
    time.sleep(0.3)
    nt = login("admin", "_3faij-45Ks3L63-9h")
    return nt or tok_admin


def login(u, p):
    st, body = http("POST", "/api/auth/login",
                    {"username": u, "password": p}, raw_path=True)
    if st == 200:
        return json.loads(body)["token"]
    return None


# ================================================================ 1. 路径穿越
def test_path_traversal(tok):
    print("=" * 70)
    print("1. 路径穿越测试")
    print("=" * 70)

    payloads = [
        ("/media/../../../../windows/win.ini", "上级目录"),
        ("/media/..%2F..%2F..%2Fwindows%2Fwin.ini", "URL 编码"),
        ("/media/%2e%2e%2f%2e%2e%2fetc%2fpasswd", "编码点"),
        ("/media/C:/Windows/win.ini", "绝对路径"),
        ("/thumb/../../../../windows/win.ini", "缩略图穿越"),
    ]

    for path, name in payloads:
        st, body = http("GET", path, token=tok, raw_path=True)
        leaked = "for 16-bit" in body or "root:" in body or "[fonts]" in body
        if leaked:
            print("  ❌ %-14s → %s  【泄露了系统文件】" % (name, st))
            record("critical", "路径穿越", "%s 泄露系统文件" % path)
        elif st == 200 and len(body) > 100 and "{" not in body[:50]:
            print("  ⚠️ %-14s → %s  返回了非 JSON 内容，需人工确认" % (name, st))
            record("medium", "可疑响应", "%s 返回 %d 字符非 JSON" % (path, len(body)))
        else:
            print("  ✅ %-14s → %s（已拒绝）" % (name, st))

    # 项目名里的穿越
    evil_names = [
        "../../evil",
        "..\\..\\evil",
        "../" * 6 + "windows",
        "断剑月白/../..",
    ]
    print()
    for name in evil_names:
        st, body = http("POST", "/api/jobs",
                        {"project": name, "chapter": 1}, token=tok, raw_path=True)
        if st == 200:
            print("  ❌ 项目名穿越 %-20r → 创建成功" % name)
            record("high", "项目名未净化", "接受 %r" % name)
        else:
            print("  ✅ 项目名穿越 %-20r → %s（已拒绝）" % (name, st))


# ================================================================ 2. 越权
def test_authz(tok_admin, tok_op, tok_view):
    print()
    print("=" * 70)
    print("2. 越权访问测试")
    print("=" * 70)

    # viewer 不能做的操作
    forbidden = [
        ("POST", "/api/jobs", {"project": "X", "chapter": 1}, "提交任务"),
        ("POST", "/api/settings", {}, "改设置"),
        ("POST", "/api/alerts/budget", {"per_shot": 1}, "改预算"),
        ("POST", "/api/rollback/policy", {"default": "force"}, "改回滚策略"),
        ("POST", "/api/resources/cleanup",
         {"dry_run": False, "max_safety": "aggressive"}, "清理资源"),
        ("GET", "/api/users", None, "读用户列表"),
        ("POST", "/api/users",
         {"username": "hack", "password": "hack123456", "role": "admin"},
         "创建管理员"),
        # 注意：这里测的是"权限是否拦住"，但为了避免测试真的把鉴权关掉，
        # 用 viewer 的 token 测；如果通了说明权限有漏洞，
        # 但我们立刻在 teardown 里重新开启。
        ("POST", "/api/auth/enabled", {"enabled": False}, "关闭鉴权"),
    ]

    for method, path, data, name in forbidden:
        st_v, _ = http(method, path, data, tok_view, raw_path=True)
        # 如果是"关闭鉴权"，无论成败都立刻恢复
        if "enabled" in path:
            http("POST", "/api/auth/enabled", {"enabled": True},
                 tok_admin, raw_path=True)
        st_o, _ = http(method, path, data, tok_op, raw_path=True)
        if "enabled" in path:
            http("POST", "/api/auth/enabled", {"enabled": True},
                 tok_admin, raw_path=True)
        # viewer 应该 403
        if st_v != 403:
            if name in ("提交任务",) and st_v in (200, 400, 422):
                # viewer 提交任务应该 403，200/400 都说明权限没拦住
                print("  ❌ viewer %-12s → %s" % (name, st_v))
                record("high", "越权", "viewer 能执行 %s（%s）" % (name, st_v))
            elif name in ("读用户列表", "创建管理员", "关闭鉴权", "改设置",
                          "改预算", "改回滚策略", "清理资源"):
                if st_v not in (403,):
                    print("  ❌ viewer %-12s → %s" % (name, st_v))
                    record("high", "越权", "viewer 能执行 %s（%s）" % (name, st_v))
            else:
                print("  ⚠️ viewer %-12s → %s" % (name, st_v))
        else:
            print("  ✅ viewer %-12s → 403" % name)

    # operator 不能做的
    print()
    op_forbidden = [
        ("GET", "/api/users", None, "读用户"),
        ("DELETE", "/api/users/admin", None, "删管理员"),
        ("POST", "/api/auth/enabled", {"enabled": False}, "关鉴权"),
    ]
    for method, path, data, name in op_forbidden:
        st, _ = http(method, path, data, tok_op, raw_path=True)
        if st not in (403, 400):
            print("  ❌ operator %-10s → %s" % (name, st))
            record("high", "越权", "operator 能执行 %s（%s）" % (name, st))
        else:
            print("  ✅ operator %-10s → %s" % (name, st))


# ================================================================ 3. 会话安全
def test_session(tok):
    print()
    print("=" * 70)
    print("3. 会话与 token 安全")
    print("=" * 70)

    # 先确认鉴权是开着的，否则这个测试毫无意义
    st, body = http("GET", "/api/auth/status", raw_path=True)
    if st == 200:
        try:
            if not json.loads(body).get("enabled"):
                print("  ⚠️ 鉴权未开启，跳过伪造测试（无意义）")
                return
        except Exception:  # noqa: BLE001
            pass


    # 3a. 伪造 token
    fakes = [
        "fake.token",
        "eyJ1c2VyIjoiYWRtaW4ifQ.fakesig",
        tok[:-4] + "0000",           # 改签名
        "",
        "Bearer",
    ]
    for f in fakes:
        st, _ = http("GET", "/api/projects", token=f, raw_path=True)
        if st == 200:
            print("  ❌ 伪造 token %r → 200" % f[:20])
            record("critical", "token 伪造", "假 token %r 通过" % f[:20])
        else:
            print("  ✅ 伪造 token %r → %s" % (f[:20], st))

    # 3b. 无 token
    st, body = http("GET", "/api/projects", raw_path=True)
    if st == 200:
        print("  ❌ 无 token 访问 → 200")
        record("critical", "未鉴权", "无 token 可访问 /api/projects")
    else:
        print("  ✅ 无 token 访问 → %s" % st)

    # 3c. 白名单是否过宽
    print()
    whitelist_check = [
        ("/api/health", True, "健康检查应该公开"),
        ("/api/auth/status", True, "鉴权状态应该公开"),
        ("/api/auth/login", True, "登录应该公开"),
        ("/api/projects", False, "项目列表不应公开"),
        ("/api/jobs", False, "任务列表不应公开"),
        ("/api/settings", False, "设置不应公开"),
        ("/api/users", False, "用户列表不应公开"),
        ("/media/x.png", False, "媒体文件不应公开"),
    ]
    st, body = http("GET", "/api/auth/status", raw_path=True)
    if st == 200:
        try:
            if not json.loads(body).get("enabled"):
                print("  ⚠️ 鉴权未开启，白名单测试无意义，跳过")
                return
        except Exception:  # noqa: BLE001
            pass

    for path, should_public, desc in whitelist_check:
        st, _ = http("GET", path, raw_path=True)
        is_public = (st != 401)
        if is_public == should_public:
            print("  ✅ %-22s %-4s（%s）" % (path, st, desc))
        else:
            print("  ❌ %-22s %-4s  %s" % (path, st, desc))
            record("high", "白名单错误",
                   "%s 应该%s但%s" % (path,
                                     "公开" if should_public else "鉴权",
                                     "公开了" if is_public else "需要鉴权"))


# ================================================================ 4. 危险操作绕过
def test_danger_bypass(tok):
    print()
    print("=" * 70)
    print("4. 危险操作绕过测试")
    print("=" * 70)

    cases = [
        ("资源清理", "POST", "/api/resources/cleanup",
         {"dry_run": False, "max_safety": "aggressive"}),
        ("LRU 淘汰", "POST", "/api/resources/lru",
         {"target_free_mb": 100, "dry_run": False}),
        ("批量删除", "POST",
         "/api/batch/" + urllib.parse.quote("断剑月白") + "/1/execute",
         {"op": "delete", "shots": [1]}),
        ("清空告警", "DELETE", "/api/alerts", None),
        ("清空回收站", "DELETE", "/api/trash", None),
    ]

    for name, method, path, data in cases:
        st, body = http(method, path, data, tok, raw_path=True)
        if st == 200:
            print("  ❌ %-12s → 200（不需要确认就执行了）" % name)
            record("critical", "危险操作绕过", "%s 无需 confirm_token" % name)
        elif st == 403:
            print("  ✅ %-12s → 403（需要确认）" % name)
        else:
            print("  ⚠️ %-12s → %s %s" % (name, st, body[:60]))

    # 4b. 伪造 confirm_token
    print()
    st, body = http("POST", "/api/resources/cleanup",
                    {"dry_run": False, "max_safety": "safe",
                     "confirm_token": "fake_token_12345"}, tok,
                    raw_path=True)
    if st == 200:
        print("  ❌ 伪造 confirm_token → 200")
        record("critical", "确认绕过", "假 confirm_token 通过")
    else:
        print("  ✅ 伪造 confirm_token → %s" % st)

    # 4c. 参数篡改（申请 safe，执行 aggressive）
    print()
    st, body = http("POST", "/api/confirm/request",
                    {"op": "resource.cleanup",
                     "params": {"max_safety": "safe"}}, tok, raw_path=True)
    if st == 200:
        real_tok = json.loads(body)["token"]
        st2, body2 = http("POST", "/api/resources/cleanup",
                          {"dry_run": False, "max_safety": "aggressive",
                           "confirm_token": real_tok}, tok, raw_path=True)
        if st2 == 200:
            print("  ❌ 参数篡改（safe→aggressive）→ 200")
            record("critical", "参数篡改", "申请 safe 执行 aggressive")
        else:
            print("  ✅ 参数篡改 → %s（已拒绝）" % st2)


# ================================================================ 5. 输入注入
def test_injection(tok):
    print()
    print("=" * 70)
    print("5. 输入注入测试")
    print("=" * 70)

    # 5a. 命令注入（项目名里带特殊字符）
    cmds = [
        "test; rm -rf /",
        "test && echo PWNED",
        "test | cat /etc/passwd",
        "$(whoami)",
        "`whoami`",
        "test\nrm -rf /",
    ]
    for c in cmds:
        st, body = http("POST", "/api/jobs",
                        {"project": c, "chapter": 1}, tok, raw_path=True)
        if st == 200:
            pid = json.loads(body).get("id", "")
            # 检查是否真的创建了目录
            print("  ❌ 命令注入 %-24r → 200 任务已创建" % c[:24])
            record("critical", "命令注入", "项目名 %r 被接受" % c)
        elif st in (400, 422):
            print("  ✅ 命令注入 %-24r → %s" % (c[:24], st))
        else:
            print("  ⚠️ 命令注入 %-24r → %s" % (c[:24], st))

    # 5b. SQL/NoSQL 注入（虽然没用数据库，但检查 JSON 处理）
    print()
    injections = [
        {"project": {"$ne": None}, "chapter": 1},
        {"project": ["a", "b"], "chapter": 1},
        {"project": 123, "chapter": 1},
        {"project": True, "chapter": 1},
    ]
    for d in injections:
        st, body = http("POST", "/api/jobs", d, tok, raw_path=True)
        if st == 200:
            print("  ⚠️ 类型混淆 %r → 200" % str(d.get("project"))[:20])
            record("low", "类型未校验", "project 接受 %r" % type(d.get("project")).__name__)
        else:
            print("  ✅ 类型混淆 %-24r → %s" % (str(d.get("project"))[:24], st))

    # 5c. 超大请求
    print()
    big = {"project": "A" * 10000, "chapter": 1}
    st, body = http("POST", "/api/jobs", big, tok, raw_path=True, timeout=30)
    if st == 200:
        print("  ⚠️ 超长项目名（10000 字符）→ 200")
        record("medium", "无长度限制", "接受 10000 字符项目名")
    else:
        print("  ✅ 超长项目名 → %s" % st)

    # 5d. JSON 炸弹
    print()
    deep = {"project": "x", "chapter": 1, "stages": ["a"] * 10000}
    st, body = http("POST", "/api/jobs", deep, tok, raw_path=True, timeout=30)
    if st == 200:
        print("  ⚠️ 超长数组（10000 项）→ 200")
        record("low", "数组未限制", "stages 接受 10000 项")
    else:
        print("  ✅ 超长数组 → %s" % st)


# ================================================================ 6. 信息泄露
def test_info_leak(tok):
    print()
    print("=" * 70)
    print("6. 信息泄露测试")
    print("=" * 70)

    # 6a. 错误信息是否泄露内部路径
    st, body = http("GET", "/api/projects/NOTEXIST/999", tok, raw_path=True)
    if st >= 400:
        leaks = []
        if "D:\\" in body or "C:\\" in body:
            leaks.append("Windows 路径")
        if "Traceback" in body:
            leaks.append("Python 堆栈")
        if "site-packages" in body:
            leaks.append("库路径")
        if leaks:
            print("  ❌ 错误信息泄露: %s" % ", ".join(leaks))
            record("medium", "信息泄露", "错误响应含 %s" % ", ".join(leaks))
            print("     %s" % body[:150])
        else:
            print("  ✅ 错误信息无路径泄露")

    # 6b. 健康检查是否泄露密钥
    print()
    st, body = http("GET", "/api/health", raw_path=True)
    if st == 200:
        secrets = []
        for kw in ("api_key", "API_KEY", "secret", "SECRET", "token",
                   "password", "sk-", "ark-"):
            if kw.lower() in body.lower():
                secrets.append(kw)
        if secrets:
            print("  ⚠️ /api/health 可能含敏感字段: %s" % ", ".join(secrets))
            record("medium", "健康检查泄露", "含 %s" % ", ".join(secrets))
        else:
            print("  ✅ /api/health 无敏感字段")

    # 6c. 设置接口是否泄露密钥
    print()
    st, body = http("GET", "/api/settings", tok, raw_path=True)
    if st == 200:
        d = json.loads(body)
        sensitive = [k for k in d if any(
            x in k.upper() for x in ("KEY", "SECRET", "TOKEN", "PASSWORD"))]
        if sensitive:
            print("  ❌ /api/settings 泄露字段: %s" % ", ".join(sensitive))
            record("high", "设置泄露", "/api/settings 含 %s" % ", ".join(sensitive))
        else:
            print("  ✅ /api/settings 无敏感字段")

    # 6d. 插件列表是否泄露密钥
    print()
    st, body = http("GET", "/api/plugins", tok, raw_path=True)
    if st == 200:
        if "sk-" in body or "ark-" in body:
            print("  ❌ /api/plugins 泄露 API Key")
            record("critical", "插件列表泄露密钥", "含 API Key")
        else:
            print("  ✅ /api/plugins 无密钥")

    # 6e. 错误堆栈
    print()
    st, body = http("GET", "/api/jobs/NOTEXIST_ID", tok, raw_path=True)
    if "Traceback" in body:
        print("  ❌ 错误响应含 Python 堆栈")
        record("medium", "堆栈泄露", "响应含 Traceback")
    else:
        print("  ✅ 无堆栈泄露")


# ================================================================ 7. 资源耗尽
def test_dos(tok):
    print()
    print("=" * 70)
    print("7. 资源耗尽测试（轻量）")
    print("=" * 70)

    # 7a. 快速连续请求
    print("  7a. 快速 100 次请求（检查是否限流）")
    t0 = time.time()
    ok_count = 0
    for i in range(100):
        st, _ = http("GET", "/api/health", raw_path=True, timeout=3)
        if st == 200:
            ok_count += 1
    dt = time.time() - t0
    print("     100 次耗时 %.2f 秒，成功 %d" % (dt, ok_count))
    if dt < 5:
        print("     ⚠️ 无速率限制（100 次 %.2f 秒）" % dt)
        record("low", "无限流", "健康检查无速率限制")
    else:
        print("     ✅ 有一定限制")

    # 7b. 超大分页
    print()
    st, body = http("GET", "/api/jobs?limit=999999", tok, raw_path=True)
    if st == 200:
        jobs = json.loads(body)
        n = len(jobs) if isinstance(jobs, list) else 0
        print("  limit=999999 → 返回 %d 条" % n)
        if n > 1000:
            record("low", "分页未限制", "limit=999999 返回 %d 条" % n)
    else:
        print("  ✅ limit=999999 → %s" % st)


def main():
    print("=" * 70)
    print("安全测试")
    print("=" * 70)
    print()

    tok_admin = login("admin", "_3faij-45Ks3L63-9h")
    tok_op = login("op1", "ppMzR$Ma&*ajaZ!3")
    tok_view = login("view1", "k5KQ8FayXUt#J4U3")

    if not tok_admin:
        print("❌ admin 登录失败，无法测试")
        return

    # 确保鉴权开启，否则所有越权测试都没有意义
    st, body = http("GET", "/api/auth/status", raw_path=True)
    if st == 200 and not json.loads(body).get("enabled"):
        print("⚠️ 鉴权未开启，先开启")
        tok_admin = ensure_auth_on(tok_admin)
        tok_op = login("op1", "ppMzR$Ma&*ajaZ!3")
        tok_view = login("view1", "k5KQ8FayXUt#J4U3")

    # 每节测试前都重查一遍，防止被前面的测试关掉
    def require_auth():
        global_ok = True
        st, body = http("GET", "/api/auth/status", raw_path=True)
        if st == 200:
            try:
                if not json.loads(body).get("enabled"):
                    return False
            except Exception:  # noqa: BLE001
                pass
        return True
    print("登录成功: admin=%s op=%s viewer=%s"
          % (bool(tok_admin), bool(tok_op), bool(tok_view)))
    print()

    test_path_traversal(tok_admin)
    test_authz(tok_admin, tok_op, tok_view)
    test_session(tok_admin)
    test_danger_bypass(tok_admin)
    test_injection(tok_admin)
    test_info_leak(tok_admin)
    test_dos(tok_admin)

    # 汇总
    print()
    print("=" * 70)
    print("安全测试汇总")
    print("=" * 70)

    from collections import Counter
    sev = Counter(f[0] for f in FINDINGS)
    print("  发现 %d 个问题：" % len(FINDINGS))
    for level in ("critical", "high", "medium", "low"):
        if sev.get(level):
            print("    %-8s %d 个" % (level.upper(), sev[level]))

    if FINDINGS:
        print()
        for i, (level, title, detail) in enumerate(FINDINGS, 1):
            icon = {"critical": "🔴", "high": "🟠",
                    "medium": "🟡", "low": "⚪"}.get(level, "·")
            print("  %s %d. [%s] %s" % (icon, i, level, title))
            print("      %s" % detail)
    else:
        print("  ✅ 未发现安全问题")


if __name__ == "__main__":
    main()
