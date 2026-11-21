# -*- coding: utf-8 -*-
"""漫剧工厂 鉴权开关

用法:
    python 鉴权开关.py on     开启登录
    python 鉴权开关.py off    关闭登录（本机自用）
    python 鉴权开关.py        查看状态
"""
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\deepseek文件区\manju\src')

from manju.core.auth import auth


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()

    if arg == "on":
        auth.set_enabled(True)
        print("鉴权已开启")
        print()
        print("可用账号:")
        print("  admin    管理员")
        print("  op1      操作员")
        print("  view1    观察员")
        print()
        print("凭证见 projects/_credentials.txt")
        print("注意: 需重启 Web 服务才生效")
        return 0

    if arg == "off":
        auth.set_enabled(False)
        print("鉴权已关闭（本机免登录）")
        print()
        print("注意: 需重启 Web 服务才生效")
        return 0

    # 查看状态
    on = auth.enabled()
    print("当前鉴权: %s" % ("开启（需要登录）" if on else "关闭（免登录）"))
    print()
    print("用户数: %d" % len(auth.list_users()))
    if auth.list_users():
        print("用户列表:")
        for u in auth.list_users():
            print("  %-10s %s" % (u.get("name"), u.get("role")))
    print()
    print("用法:")
    print("  python 鉴权开关.py on    开启登录")
    print("  python 鉴权开关.py off   关闭登录")
    print("  python 鉴权开关.py       查看状态")
    return 0


if __name__ == "__main__":
    sys.exit(main())
