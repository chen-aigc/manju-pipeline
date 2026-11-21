# -*- coding: utf-8 -*-
"""网络急救诊断器 · 单文件版

设计约束（来自产品讨论）：
    - 纯本地，无云、无域名、不备案
    - 绝不扫描局域网（只测本机→网关、网关→外网）
    - 多线程探测，超时 0.5 秒，大字提示
    - 能输出一份可打印的报告（渠道甩锅用）
    - 授权：机器码 + 离线激活码，不依赖 U 盘

用法：
    python 网络诊断.py               图形界面
    python 网络诊断.py --cli         命令行（出一份报告）
    python 网络诊断.py --report      只生成报告文件

依赖：仅标准库（tkinter / socket / threading / uuid / hashlib）
"""
import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

TIMEOUT = 0.5           # 单次探测超时（秒）
TCP_TARGETS = [("www.baidu.com", 443), ("223.5.5.5", 53)]
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetDoctor"

# ---------------------------------------------------------------- 探测
def run_cmd(cmd, timeout=3):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="gbk", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def get_local_ip_and_gateway():
    """返回 (本机IP, 网关IP, DNS列表)。

    只用系统命令，绝不扫描网段。
    网关走 `route print -4`（比 ipconfig 可靠：ipconfig 在英文系统、
    只有 IPv6 网关时会漏读）。
    """
    ip = gw = ""
    dns = []

    if platform.system() == "Windows":
        # 1) 网关：route print -4 的第一条 0.0.0.0 路由
        rc, out = run_cmd(["route", "print", "-4"], timeout=5)
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" \
                    and parts[1] == "0.0.0.0":
                cand = parts[2]
                if cand.count(".") == 3 and cand != "0.0.0.0":
                    gw = cand
                    ip = parts[3] if parts[3].count(".") == 3 else ""
                    break

        # 2) ipconfig 补充：本机 IP / DNS（网关优先用上面的）
        rc, out = run_cmd(["ipconfig", "/all"], timeout=5)
        for i, ln in enumerate(out.splitlines()):
            s = ln.strip()
            low = s.lower()
            if ("ipv4" in low or "ip 地址" in s) and ":" in s and not ip:
                cand = s.rsplit(":", 1)[-1].strip()
                cand = cand.split("(")[0].strip()
                if cand.count(".") == 3 and not cand.startswith("127.") \
                        and not cand.startswith("169.254"):
                    ip = cand
            if ("dns" in low or "dns 服务器" in s) and len(dns) < 2:
                parts = s.rsplit(":", 1)
                if len(parts) == 2:
                    cand = parts[1].strip()
                    if cand.count(".") == 3:
                        dns.append(cand)

        # 3) 如果 ip 还是空的，用出口 IP 反推（连 UDP，不发包）
        if not ip:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("223.5.5.5", 53))
                ip = s.getsockname()[0]
                s.close()
            except Exception:  # noqa: BLE001
                pass
    else:
        rc, out = run_cmd(["ip", "route"])
        for ln in out.splitlines():
            if ln.startswith("default") and not gw:
                gw = ln.split()[2]
        rc, out = run_cmd(["hostname", "-I"])
        ip = (out.split() or [""])[0]

    return ip, gw, dns


def tcp_probe(host, port, timeout=TIMEOUT):
    """TCP 连接测试，返回 (成功与否, 耗时秒)。"""
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, time.time() - t0
    except Exception:  # noqa: BLE001
        return False, time.time() - t0


def ping_probe(host, timeout_ms=500):
    """Ping，失败时退回 TCP 80/443（有些设备禁 ICMP）。"""
    if platform.system() == "Windows":
        rc, out = run_cmd(["ping", "-n", "1", "-w", str(timeout_ms), host], timeout=3)
        if rc == 0 and ("TTL=" in out or "ttl=" in out):
            ms = 0.0
            for tok in out.replace("=", " ").split():
                if tok.isdigit() and 0 <= int(tok) <= 3000:
                    ms = int(tok) / 1000.0
                    break
            return True, ms
        ok, cost = tcp_probe(host, 443, timeout_ms / 1000)
        if not ok:
            ok, cost = tcp_probe(host, 80, timeout_ms / 1000)
        return ok, cost
    rc, out = run_cmd(["ping", "-c", "1", "-W", "1", host], timeout=3)
    return rc == 0, 0.0


# ---------------------------------------------------------------- 判定
def diagnose(progress=None):
    """执行全部检测并给出结论。progress(stage, text) 用于界面更新。"""
    def say(stage, text):
        if progress:
            progress(stage, text)

    say("每", "读取本机网络配置…")
    ip, gw, dns = get_local_ip_and_gateway()
    t0 = time.time()

    results = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "hostname": socket.gethostname(),
               "local_ip": ip, "gateway": gw, "dns": dns,
               "checks": []}

    # 1. 本机有没有拿到 IP
    if not ip:
        results["verdict"] = "NO_IP"
        results["title"] = "网线没插 / 网卡被禁用"
        results["hint"] = ("检查电脑后面的网线两头是否插紧，灯是否亮。\n"
                           "如果是无线，检查是否连上了 WiFi。")
        results["duration"] = time.time() - t0
        return results
    results["checks"].append({"name": "本机 IP", "ok": True, "detail": ip})

    # 2. 网关
    if not gw:
        results["verdict"] = "NO_GATEWAY"
        results["title"] = "没有默认网关"
        results["hint"] = "网络设置可能被改乱了，建议重新连接路由器或重启电脑。"
        results["duration"] = time.time() - t0
        return results

    say("网关", "正在连接路由器（%s）…" % gw)
    gw_ok, gw_ms = ping_probe(gw, int(TIMEOUT * 1000))
    results["checks"].append({
        "name": "连路由器", "ok": gw_ok,
        "detail": "%.0f ms" % (gw_ms * 1000) if gw_ok else "超时"})
    if gw_ms * 1000 > 100:
        results["checks"].append({
            "name": "路由器延迟", "ok": False,
            "detail": "%.0f ms（偏高，网线或路由器可能有问题）" % (gw_ms * 1000)})

    # 3. 外网
    say("外网", "正在测试外网（百度 443）…")
    wan_ok = False
    wan_ms = 0.0
    wan_detail = "全部超时"
    for host, port in TCP_TARGETS:
        ok, cost = tcp_probe(host, port, TIMEOUT)
        if ok:
            wan_ok = True
            wan_ms = cost
            wan_detail = "%s:%d  %.0f ms" % (host, port, cost * 1000)
            break
    results["checks"].append({"name": "连外网", "ok": wan_ok, "detail": wan_detail})

    # 4. DNS
    say("DNS", "正在测试域名解析…")
    dns_ok = False
    dns_detail = "解析失败"
    t1 = time.time()
    try:
        socket.setdefaulttimeout(TIMEOUT)
        socket.gethostbyname("www.baidu.com")
        dns_ok = True
        dns_detail = "%.0f ms" % ((time.time() - t1) * 1000)
    except Exception:  # noqa: BLE001
        dns_detail = "超时"
    results["checks"].append({"name": "DNS 解析", "ok": dns_ok, "detail": dns_detail})

    # ---- 判定矩阵 ----
    if gw_ok and wan_ok and dns_ok:
        results["verdict"] = "OK"
        results["title"] = "网络正常"
        results["hint"] = "如果网页打不开，可能是网站本身问题，或者浏览器故障。"
    elif gw_ok and wan_ok and not dns_ok:
        results["verdict"] = "DNS_FAIL"
        results["title"] = "能上网但打不开网页（DNS 故障）"
        results["hint"] = ("网络是通的，但域名无法解析。\n"
                           "建议把 DNS 改成 223.5.5.5 或 114.114.114.114。")
    elif gw_ok and not wan_ok:
        results["verdict"] = "WAN_DOWN"
        results["title"] = "路由器正常，外网不通"
        results["hint"] = ("最可能：宽带欠费 / 光猫断电 / 运营商故障。\n"
                           "请拨打宽带客服确认。这不是路由器的问题。")
    else:
        results["verdict"] = "LAN_DOWN"
        results["title"] = "连不上路由器"
        results["hint"] = ("最可能：网线松动、路由器断电或死机。\n"
                           "1) 检查网线两头\n2) 给路由器断电 10 秒再通电")

    results["duration"] = time.time() - t0
    return results


# ---------------------------------------------------------------- 授权
def machine_code():
    """机器码：CPU + 主板 + MAC 的哈希，取 16 位。"""
    parts = [platform.node(), platform.processor(), str(uuid.getnode())]
    if platform.system() == "Windows":
        rc, out = run_cmd(["wmic", "baseboard", "get", "serialnumber"], timeout=3)
        if rc == 0:
            for ln in out.splitlines():
                ln = ln.strip()
                if ln and ln.upper() != "SERIALNUMBER":
                    parts.append(ln)
    raw = "|".join(parts).encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:16].upper()


def make_license(code, years=1, start_count=0, expire=None):
    """生成授权字符串（发给客户粘贴）。"""
    expire = expire or (datetime.now() + timedelta(days=365 * years)).strftime("%Y-%m-%d")
    payload = {"c": code, "e": expire, "n": start_count}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hashlib.sha256(("MANJU" + body).encode()).hexdigest()[:12].upper()
    return "%s.%s" % (body.replace('"', "'"), sig)


def verify_license(lic):
    """校验授权串，返回 (是否有效, 说明, 到期日)。"""
    try:
        body, sig = lic.rsplit(".", 1)
        body = body.replace("'", '"')
        want = hashlib.sha256(("MANJU" + body).encode()).hexdigest()[:12].upper()
        if want != sig:
            return False, "授权码无效", None
        data = json.loads(body)
        expire = datetime.strptime(data["e"], "%Y-%m-%d").date()
        if expire < datetime.now().date():
            return False, "授权已过期（%s）" % data["e"], expire
        return True, "有效期至 %s" % data["e"], expire
    except Exception:  # noqa: BLE001
        return False, "授权码格式错误", None


def license_path():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "license.json"


def load_state():
    p = license_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"license": "", "starts": 0, "last_seen": ""}


def save_state(st):
    license_path().write_text(json.dumps(st, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def get_license_status():
    """三重到期判定：到期日 + 运行计数 + 时间回溯。"""
    st = load_state()
    code = machine_code()
    if not st.get("license"):
        return {"ok": False, "reason": "未激活", "machine_code": code,
                "expire": "", "day_left": 0}

    body = st["license"].split(".")[0].replace("'", '"')
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "授权数据损坏", "machine_code": code,
                "expire": "", "day_left": 0}

    if data.get("c") != code:
        return {"ok": False, "reason": "授权码与本机不匹配", "machine_code": code,
                "expire": "", "day_left": 0}

    ok, msg, expire = verify_license(st["license"])
    today = datetime.now().date().isoformat()
    if st.get("last_seen") and today < st["last_seen"]:
        return {"ok": False, "reason": "检测到系统时间被回拨", "machine_code": code,
                "expire": msg, "day_left": 0}

    st["last_seen"] = today
    st["starts"] = int(st.get("starts") or 0) + 1
    save_state(st)

    day_left = 0
    if expire:
        day_left = (expire - datetime.now().date()).days
    return {"ok": ok, "reason": msg, "machine_code": code,
            "expire": msg, "day_left": day_left}


# ---------------------------------------------------------------- 报告
def write_report(res, path=None):
    """生成可打印的文本报告（渠道拿去给客户看）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = path or (DATA_DIR / ("诊断报告_%s.txt"
                                % datetime.now().strftime("%Y%m%d_%H%M%S")))
    lines = []
    w = 46
    lines.append("=" * w)
    lines.append("           网络故障诊断报告".center(w))
    lines.append("=" * w)
    lines.append("")
    lines.append("检测时间：%s" % res["time"])
    lines.append("电脑名称：%s" % res["hostname"])
    lines.append("本机 IP ：%s" % (res["local_ip"] or "未获取到"))
    lines.append("路由器  ：%s" % (res["gateway"] or "未获取到"))
    lines.append("")
    lines.append("-" * w)
    lines.append("【检测明细】")
    def pad(txt, width):
        """按显示宽度补齐（中文算 2 格）。"""
        n = sum(2 if ord(ch) > 127 else 1 for ch in txt)
        return txt + " " * max(0, width - n)

    for c in res["checks"]:
        mark = "正常" if c["ok"] else "异常"
        lines.append("  %s %s %s" % (pad(c["name"], 14), pad(mark, 6),
                                     c["detail"]))
    lines.append("")
    lines.append("-" * w)
    lines.append("【结论】")
    lines.append("  %s" % res["title"])
    lines.append("")
    for ln in (res["hint"] or "").splitlines():
        lines.append("  %s" % ln)
    lines.append("")
    lines.append("-" * w)
    lines.append("检测耗时：%.2f 秒" % res["duration"])
    lines.append("")
    lines.append("说明：本报告仅检测「本机 → 路由器」与「路由器 → 外网」")
    lines.append("      两段链路，不扫描局域网内任何其他设备。")
    lines.append("=" * w)
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- GUI
def run_gui():
    import tkinter as tk
    from tkinter import font as tkfont

    st = get_license_status()

    root = tk.Tk()
    root.title("网络急救")
    root.geometry("720x560")
    root.configure(bg="#f4f6f9")

    big = tkfont.Font(family="Microsoft YaHei", size=26, weight="bold")
    mid = tkfont.Font(family="Microsoft YaHei", size=12)
    small = tkfont.Font(family="Microsoft YaHei", size=10)

    title = tk.Label(root, text="网络急救", font=big, bg="#f4f6f9", fg="#111827")
    title.pack(pady=(24, 4))
    sub = tk.Label(root, text="点一下，告诉你到底哪里坏了", font=mid,
                   bg="#f4f6f9", fg="#6b7280")
    sub.pack()

    verdict = tk.Label(root, text="", font=big, bg="#f4f6f9", fg="#111827")
    verdict.pack(pady=(22, 6))

    hint = tk.Label(root, text="", font=mid, bg="#f4f6f9", fg="#374151",
                    justify="center")
    hint.pack(pady=(0, 12))

    detail = tk.Text(root, height=8, font=small, bg="#ffffff", fg="#374151",
                     relief="flat", highlightthickness=1,
                     highlightbackground="#e5e7eb")
    detail.pack(fill="x", padx=36, pady=(4, 10))

    btn_frame = tk.Frame(root, bg="#f4f6f9")
    btn_frame.pack(pady=6)

    def do_diag():
        verdict.config(text="检测中…", fg="#6b7280")
        hint.config(text="")
        detail.delete("1.0", "end")
        btn.config(state="disabled")

        def progress(stage, text):
            root.after(0, lambda: verdict.config(text=text, fg="#6b7280"))

        def work():
            res = diagnose(progress)
            path = write_report(res)

            def show():
                color = "#16a34a" if res["verdict"] == "OK" else "#dc2626"
                verdict.config(text=res["title"], fg=color)
                hint.config(text=res["hint"])
                detail.delete("1.0", "end")
                for c in res["checks"]:
                    detail.insert("end", "%-12s %-4s %s\n" % (
                        c["name"], "正常" if c["ok"] else "异常", c["detail"]))
                detail.insert("end", "\n报告已保存：%s\n" % path)
                btn.config(state="normal")
            root.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    btn = tk.Button(btn_frame, text="开始检测", command=do_diag,
                    font=("Microsoft YaHei", 14, "bold"),
                    bg="#2563eb", fg="white", activebackground="#1d4ed8",
                    relief="flat", padx=36, pady=12)
    btn.pack(side="left", padx=6)

    def show_code():
        top = tk.Toplevel(root)
        top.title("授权信息")
        top.geometry("520x300")
        tk.Label(top, text="本机机器码（发给供应商激活）",
                 font=mid).pack(pady=(16, 6))
        e1 = tk.Entry(top, font=("Consolas", 14), justify="center", width=24)
        e1.insert(0, st["machine_code"])
        e1.config(state="readonly")
        e1.pack(pady=4)
        tk.Label(top, text="授权状态：%s" % st["reason"], font=small,
                 fg="#16a34a" if st["ok"] else "#dc2626").pack(pady=6)
        tk.Label(top, text="粘贴授权码后回车激活", font=small).pack(pady=(10, 2))
        e2 = tk.Entry(top, font=("Consolas", 10), width=52)
        e2.pack(pady=4)

        def activate(_evt=None):
            lic = e2.get().strip()
            ok, msg, _ = verify_license(lic)
            body = lic.split(".")[0].replace("'", '"')
            try:
                same = json.loads(body).get("c") == st["machine_code"]
            except Exception:  # noqa: BLE001
                same = False
            if ok and same:
                s2 = load_state()
                s2["license"] = lic
                save_state(s2)
                tk.Label(top, text="激活成功", fg="#16a34a", font=mid).pack()
            else:
                tk.Label(top, text="激活失败：" + (msg if not ok else "机器码不匹配"),
                         fg="#dc2626", font=small).pack()

        e2.bind("<Return>", activate)
        tk.Button(top, text="激活", command=activate, bg="#2563eb", fg="white",
                  relief="flat", padx=20, pady=6).pack(pady=10)

    lic_btn = tk.Button(btn_frame, text="授权", command=show_code,
                        font=("Microsoft YaHei", 11),
                        bg="#e5e7eb", fg="#374151", relief="flat",
                        padx=18, pady=12)
    lic_btn.pack(side="left", padx=6)

    foot = tk.Label(root, text="剩余授权：%s" % (st["reason"] or "-"),
                    font=small, bg="#f4f6f9", fg="#9ca3af")
    foot.pack(side="bottom", pady=10)

    if not st["ok"]:
        verdict.config(text="未激活", fg="#dc2626")
        hint.config(text="点「授权」获取机器码，发给供应商换取激活码。")
        btn.config(state="disabled")

    root.mainloop()


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="网络急救诊断器")
    ap.add_argument("--cli", action="store_true", help="命令行模式")
    ap.add_argument("--report", action="store_true", help="只生成报告")
    ap.add_argument("--machine", action="store_true", help="显示机器码")
    ap.add_argument("--activate", default="", help="用授权码激活")
    ap.add_argument("--issue", default="", help="[供应商] 为指定机器码生成授权，可加 --years")
    ap.add_argument("--years", type=int, default=1)
    args = ap.parse_args()

    if args.machine:
        print("机器码：", machine_code())
        return 0

    if args.issue:
        print(make_license(args.issue.strip().upper(), years=args.years))
        return 0

    if args.activate:
        ok, msg, _ = verify_license(args.activate)
        if not ok:
            print("激活失败：", msg)
            return 1
        st = load_state()
        st["license"] = args.activate
        save_state(st)
        print("激活成功：", msg)
        return 0

    if args.cli or args.report:
        res = diagnose(lambda s, t: print("  " + t) if not args.report else None)
        print()
        print(res["title"])
        print(res["hint"])
        print()
        for c in res["checks"]:
            print("  %-10s %-4s %s" % (c["name"],
                                       "正常" if c["ok"] else "异常", c["detail"]))
        p = write_report(res)
        print("\n报告：", p)
        return 0 if res["verdict"] == "OK" else 2

    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
