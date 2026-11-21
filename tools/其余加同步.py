# -*- coding: utf-8 -*-
"""给 alerts / spend / policy 的读方法加同步。"""
import io
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

results = []


def add_sync(path, label, methods):
    """在指定方法体开头插入 self._load()。"""
    t = io.open(path, encoding='utf-8').read()
    orig = len(t)
    added = []

    for m in methods:
        pat = re.compile(
            r'(    def %s\(self[^)]*\):\n(?:        """(?:.|\n)*?"""\n)?)'
            % re.escape(m))
        match = pat.search(t)
        if not match:
            continue
        nxt = t[match.end():match.end() + 100]
        if nxt.startswith("        self._load()"):
            continue
        t = t[:match.end()] + "        self._load()\n" + t[match.end():]
        added.append(m)

    io.open(path, 'w', encoding='utf-8').write(t)
    results.append((label, orig, len(t), added))


# alerts：读方法
add_sync(
    r'D:\deepseek文件区\manju\src\manju\core\alerting.py',
    "alerting",
    ["list", "stats", "config_summary"],
)

# spend：读方法 + 累加方法（累加前要先读最新）
add_sync(
    r'D:\deepseek文件区\manju\src\manju\core\spend.py',
    "spend",
    ["shot_summary", "job_summary", "daily_summary", "all_jobs",
     "add_shot_cost", "add_job_cost", "set_budget"],
)

# policy：读方法
add_sync(
    r'D:\deepseek文件区\manju\src\manju\core\rollback_policy.py',
    "rollback_policy",
    ["get", "summary", "list_waiting", "set"],
)

print("=" * 68)
print("修改汇总")
print("=" * 68)
for label, o, n, added in results:
    print("  %-18s %5d -> %5d" % (label, o, n))
    print("     加了同步: %s" % (", ".join(added) if added else "（无）"))
