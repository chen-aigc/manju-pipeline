# -*- coding: utf-8 -*-
"""启动漫剧工厂：检查依赖 → 启动 Web → 打开浏览器。

用法：
    python 启动.py         正常启动
    python 启动.py --restart  强制重启
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = r'D:\deepseek文件区\manju'
PYTHON = os.path.join(BASE_DIR, '.venv', 'Scripts', 'python.exe')
PORT = 8501
URL = 'http://127.0.0.1:%d' % PORT


def port_open(port):
    """端口是否已被监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def health_ok():
    """API 是否响应。"""
    try:
        with urllib.request.urlopen(URL + '/api/health', timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def kill_port(port):
    """杀掉占用端口的进程。"""
    try:
        out = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=10)
        pids = set()
        for line in (out.stdout or '').split('\n'):
            if ':%d' % port in line and 'LISTENING' in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            subprocess.run(['taskkill', '/F', '/PID', pid],
                           capture_output=True, timeout=10)
        if pids:
            print("  已停止旧进程:", ', '.join(pids))
        return True
    except Exception as exc:
        print("  停止旧进程失败:", exc)
        return False


def main():
    restart = '--restart' in sys.argv

    print("=" * 56)
    print("  漫剧工厂")
    print("=" * 56)
    print()

    # 1. 检查 Python
    if not os.path.exists(PYTHON):
        print("❌ 找不到 Python:", PYTHON)
        input("\n按回车退出...")
        return 1
    print("✅ Python OK")

    # 2. 端口状态
    running = port_open(PORT)
    if running and health_ok():
        if restart:
            print("  强制重启...")
            kill_port(PORT)
            time.sleep(2)
            running = False
        else:
            print("✅ 服务已在运行")
            print()
            print("   打开:", URL)
            webbrowser.open(URL)
            print()
            print("（窗口将在 3 秒后关闭）")
            time.sleep(3)
            return 0

    if running and not health_ok():
        print("⚠️ 端口被占用但服务无响应，强制重启")
        kill_port(PORT)
        time.sleep(2)

    # 3. 启动服务
    print("  启动服务...")
    env = os.environ.copy()
    env['PYTHONPATH'] = os.path.join(BASE_DIR, 'src')
    env['PYTHONIOENCODING'] = 'utf-8'

    proc = subprocess.Popen(
        [PYTHON, '-m', 'uvicorn', 'manju.web.server:app',
         '--host', '127.0.0.1', '--port', str(PORT),
         '--log-level', 'warning'],
        cwd=BASE_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW
        if sys.platform == 'win32' else 0,
    )

    # 4. 等服务起来
    for i in range(40):
        time.sleep(0.5)
        if health_ok():
            print("✅ 服务已就绪（%.1f 秒）" % ((i + 1) * 0.5))
            print()
            print("   打开:", URL)
            webbrowser.open(URL)
            print()
            print("   （可以直接关掉这个窗口，服务会继续运行）")
            time.sleep(2)
            return 0
        if proc.poll() is not None:
            print("❌ 服务启动失败（进程已退出）")
            input("\n按回车退出...")
            return 1

    print("⚠️ 服务启动超时")
    print("   手动检查:", URL)
    input("\n按回车退出...")
    return 1


if __name__ == '__main__':
    sys.exit(main())
