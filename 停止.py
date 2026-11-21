# -*- coding: utf-8 -*-
"""停止漫剧工厂 Web 服务。"""
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')

PORT = 8501

print("查找 %d 端口的进程..." % PORT)

try:
    out = subprocess.run(['netstat', '-ano'], capture_output=True,
                         text=True, timeout=10)
    pids = set()
    for line in (out.stdout or '').split('\n'):
        if ':%d' % PORT in line and 'LISTENING' in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])

    if not pids:
        print("没有运行中的服务")
    else:
        for pid in pids:
            print("停止 PID", pid)
            subprocess.run(['taskkill', '/F', '/PID', pid],
                           capture_output=True, timeout=10)
        print("已停止")
except Exception as exc:
    print("失败:", exc)

input("\n按回车关闭...")