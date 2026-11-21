# -*- coding: utf-8 -*-
"""log 插件：console、jsonl、csv、excel。"""
import csv
import json
import time
from pathlib import Path

from ..base import LogProvider
from ..registry import register
from ...core import atomic
from ...core.cost import fmt_yuan, to_yuan


@register("log", "console")
class ConsoleLog(LogProvider):
    def __init__(self, config=None):
        super().__init__(config)
        self.events = []

    def available(self):
        return True

    def log(self, event):
        self.events.append(event)
        line = "[%s] %s" % (event.get("stage", "-"),
                            event.get("msg") or event.get("status") or "")
        print("  " + line)

    def emit(self, out_path=None):
        return None


@register("log", "jsonl")
class JsonlLog(LogProvider):
    """给程序读：一行一条 JSON。"""

    def __init__(self, config=None):
        super().__init__(config)
        self.path = None
        if config and config.get("path"):
            self.path = Path(config["path"])

    def available(self):
        return self.path is not None

    def log(self, event):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(event)
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def emit(self, out_path=None):
        return self.path


@register("log", "csv")
class CsvLog(LogProvider):
    """简单表格：Excel/WPS 可打开。"""

    FIELDS = ("ts", "stage", "shot", "provider", "action", "attempt",
              "status", "elapsed", "cost_si", "detail")

    def __init__(self, config=None):
        super().__init__(config)
        self.path = None
        if config and config.get("path"):
            self.path = Path(config["path"])

    def available(self):
        return self.path is not None

    def log(self, event):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        rec = dict(event)
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(self.path, "a", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=self.FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(rec)

    def emit(self, out_path=None):
        return self.path


@register("log", "excel")
class ExcelLog(LogProvider):
    """xlsx 三张表：明细 / 按集汇总 / 按平台汇总。"""

    FIELDS = ("ts", "chapter", "stage", "shot", "provider", "action",
              "attempt", "status", "elapsed", "cost_si", "detail")

    def __init__(self, config=None):
        super().__init__(config)
        self.events = []

    def available(self):
        try:
            import openpyxl  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def log(self, event):
        rec = dict(event)
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.events.append(rec)

    def emit(self, out_path=None):
        out_path = Path(out_path or self.config.get("path") or "report.xlsx")
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("需要 openpyxl：pip install openpyxl") from exc

        wb = openpyxl.Workbook()

        # ---------- 明细 ----------
        ws = wb.active
        ws.title = "明细"
        headers = ["时间", "集", "环节", "镜头", "平台", "动作",
                   "尝试", "状态", "耗时(s)", "成本(元)", "说明"]
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            ws.cell(row=1, column=c).font = Font(bold=True)

        warn_fill = PatternFill("solid", fgColor="FFF2CC")   # 黄：降级
        fail_fill = PatternFill("solid", fgColor="F8CBAD")   # 红：失败

        for ev in self.events:
            si = ev.get("cost_si")
            ws.append([
                ev.get("ts", ""),
                ev.get("chapter", ""),
                ev.get("stage", ""),
                ev.get("shot", ""),
                ev.get("provider", ""),
                ev.get("action", ""),
                ev.get("attempt", ""),
                ev.get("status", ""),
                round(float(ev.get("elapsed") or 0), 2),
                float(to_yuan(si)) if si is not None else None,
                ev.get("detail", "") if isinstance(ev.get("detail"), str) else "",
            ])
            row = ws.max_row
            ws.cell(row=row, column=10).number_format = "0.0000"
            status = ev.get("status")
            if status == "degraded":
                for c in range(1, len(headers) + 1):
                    ws.cell(row=row, column=c).fill = warn_fill
            elif status == "failed":
                for c in range(1, len(headers) + 1):
                    ws.cell(row=row, column=c).fill = fail_fill

        ws.freeze_panes = "A2"
        _autofit(ws)

        # ---------- 按集汇总 ----------
        ws2 = wb.create_sheet("按集汇总")
        ws2.append(["集", "记录数", "成功", "降级", "失败",
                    "总耗时(s)", "成本(元)"])
        for c in range(1, 8):
            ws2.cell(row=1, column=c).font = Font(bold=True)

        by_chapter = {}
        for ev in self.events:
            ch = ev.get("chapter", "")
            b = by_chapter.setdefault(ch, {"n": 0, "ok": 0, "deg": 0,
                                           "fail": 0, "t": 0.0, "si": 0})
            b["n"] += 1
            st = ev.get("status")
            if st == "ok":
                b["ok"] += 1
            elif st == "degraded":
                b["deg"] += 1
            elif st == "failed":
                b["fail"] += 1
            b["t"] += float(ev.get("elapsed") or 0)
            b["si"] += int(ev.get("cost_si") or 0)

        for ch, b in sorted(by_chapter.items(), key=lambda x: str(x[0])):
            ws2.append([ch, b["n"], b["ok"], b["deg"], b["fail"],
                        round(b["t"], 1), float(to_yuan(b["si"]))])
            ws2.cell(row=ws2.max_row, column=7).number_format = "0.0000"
        ws2.freeze_panes = "A2"
        _autofit(ws2)

        # ---------- 按平台汇总 ----------
        ws3 = wb.create_sheet("按平台汇总")
        ws3.append(["平台", "调用次数", "失败次数", "成本(元)"])
        for c in range(1, 5):
            ws3.cell(row=1, column=c).font = Font(bold=True)

        by_provider = {}
        for ev in self.events:
            p = ev.get("provider", "")
            b = by_provider.setdefault(p, {"n": 0, "fail": 0, "si": 0})
            b["n"] += 1
            if ev.get("status") == "failed":
                b["fail"] += 1
            b["si"] += int(ev.get("cost_si") or 0)

        for p, b in sorted(by_provider.items(), key=lambda x: -x[1]["si"]):
            ws3.append([p, b["n"], b["fail"], float(to_yuan(b["si"]))])
            ws3.cell(row=ws3.max_row, column=4).number_format = "0.0000"
        ws3.freeze_panes = "A2"
        _autofit(ws3)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(out_path))
        return out_path


def _autofit(ws, max_width=40):
    """列宽自适应。"""
    for col in ws.columns:
        best = 8
        letter = col[0].column_letter
        for cell in col:
            v = cell.value
            if v is None:
                continue
            # 中文字符按 2 个宽度算
            w = sum(2 if ord(ch) > 127 else 1 for ch in str(v))
            best = max(best, min(w + 2, max_width))
        ws.column_dimensions[letter].width = best
