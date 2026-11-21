# -*- coding: utf-8 -*-
"""用量数据库：记录每次 API 调用的消耗（token / 张数 / 秒数 / 金额）。

用 SQLite，单文件，无依赖。位置：projects/.usage.db

设计原则：
    1. 只追加不修改（append-only），便于审计
    2. 一次调用一行，同一个任务的所有调用共享 job_id
    3. token 和金额都存，便于交叉校验
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_DB_PATH: Optional[Path] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,           -- ISO 时间
    job_id       TEXT,                       -- 任务 id（Web 上有）
    project      TEXT,                       -- 项目名
    chapter      INTEGER,                    -- 集数
    shot         INTEGER,                    -- 镜头号
    stage        TEXT,                       -- script/image/video/tts/bgm/edit/scene3d/radio
    provider     TEXT,                       -- deepseek/doubao/jimeng/ark/blender...
    model        TEXT,                       -- 具体模型名
    -- 用量
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    tokens_total INTEGER DEFAULT 0,
    images       INTEGER DEFAULT 0,          -- 出图张数
    seconds      REAL    DEFAULT 0,          -- 视频秒数
    chars        INTEGER DEFAULT 0,          -- 配音字符数
    -- 成本（微元，1 元 = 1000000 微元，用整数避免浮点误差）
    cost_micro   INTEGER DEFAULT 0,
    cost_source  TEXT,                       -- api_return/unit_price/local_estimate/unknown
    -- 状态
    status       TEXT DEFAULT 'ok',          -- ok/failed/degraded/skipped
    elapsed      REAL    DEFAULT 0,          -- 耗时秒
    error        TEXT                        -- 错误信息（截断 500 字）
);

CREATE INDEX IF NOT EXISTS idx_ts       ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_project  ON calls(project, chapter);
CREATE INDEX IF NOT EXISTS idx_stage    ON calls(stage);
CREATE INDEX IF NOT EXISTS idx_provider ON calls(provider);
CREATE INDEX IF NOT EXISTS idx_job      ON calls(job_id);

CREATE TABLE IF NOT EXISTS daily (
    day        TEXT PRIMARY KEY,             -- YYYY-MM-DD
    calls      INTEGER DEFAULT 0,
    cost_micro INTEGER DEFAULT 0,
    tokens     INTEGER DEFAULT 0
);
"""


def _db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        base = Path(getattr(config, "PROJECTS_DIR", None)
                    or (Path(config.ROOT) / "projects"))
        _DB_PATH = base / ".usage.db"
    return _DB_PATH


def _conn() -> sqlite3.Connection:
    """取连接（懒初始化，线程安全）。"""
    global _CONN
    if _CONN is not None:
        return _CONN

    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(p), timeout=15.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL：多进程读写不打架
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass
    conn.executescript(SCHEMA)
    conn.commit()

    _CONN = conn
    return conn


def record(*, stage: str = "", provider: str = "", model: str = "",
           project: str = "", chapter: int | None = None,
           shot: int | None = None, job_id: str = "",
           tokens_in: int = 0, tokens_out: int = 0,
           tokens_total: int = 0, images: int = 0, seconds: float = 0.0,
           chars: int = 0, cost_micro: int = 0,
           cost_source: str = "", status: str = "ok",
           elapsed: float = 0.0, error: str = "") -> None:
    """记一次调用。任何异常都不外抛（记账失败不能拖垮主流程）。"""
    try:
        with _LOCK:
            c = _conn()
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            day = ts[:10]

            c.execute(
                "INSERT INTO calls "
                "(ts, job_id, project, chapter, shot, stage, provider,"
                " model, tokens_in, tokens_out, tokens_total, images,"
                " seconds, chars, cost_micro, cost_source, status,"
                " elapsed, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, job_id or "", project or "", chapter, shot,
                 stage or "", provider or "", model or "",
                 int(tokens_in or 0), int(tokens_out or 0),
                 int(tokens_total or 0), int(images or 0),
                 float(seconds or 0), int(chars or 0),
                 int(cost_micro or 0), cost_source or "",
                 status or "ok", float(elapsed or 0),
                 (error or "")[:500]))

            c.execute(
                "INSERT INTO daily (day, calls, cost_micro, tokens) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "  calls = calls + 1, "
                "  cost_micro = cost_micro + excluded.cost_micro, "
                "  tokens = tokens + excluded.tokens",
                (day, int(cost_micro or 0), int(tokens_total or 0)))

            c.commit()
    except Exception:  # noqa: BLE001
        pass


def query(project: str = "", chapter: int | None = None,
          stage: str = "", provider: str = "",
          since: str = "", until: str = "",
          limit: int = 500) -> List[Dict[str, Any]]:
    """查明细。"""
    try:
        with _LOCK:
            c = _conn()
            sql = "SELECT * FROM calls WHERE 1=1"
            args: List[Any] = []
            if project:
                sql += " AND project = ?"
                args.append(project)
            if chapter is not None:
                sql += " AND chapter = ?"
                args.append(int(chapter))
            if stage:
                sql += " AND stage = ?"
                args.append(stage)
            if provider:
                sql += " AND provider = ?"
                args.append(provider)
            if since:
                sql += " AND ts >= ?"
                args.append(since)
            if until:
                sql += " AND ts <= ?"
                args.append(until)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(max(1, min(5000, int(limit))))

            rows = c.execute(sql, args).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def stats(project: str = "", chapter: int | None = None,
          since: str = "", until: str = "") -> Dict[str, Any]:
    """汇总：按阶段、按服务商、按天。"""
    try:
        with _LOCK:
            c = _conn()
            where = "WHERE 1=1"
            args: List[Any] = []
            if project:
                where += " AND project = ?"
                args.append(project)
            if chapter is not None:
                where += " AND chapter = ?"
                args.append(int(chapter))
            if since:
                where += " AND ts >= ?"
                args.append(since)
            if until:
                where += " AND ts <= ?"
                args.append(until)

            total = c.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(cost_micro) AS cost, "
                "SUM(tokens_total) AS tokens, "
                "SUM(images) AS images, "
                "SUM(seconds) AS seconds, "
                "SUM(chars) AS chars, "
                "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                "FROM calls " + where, args).fetchone()

            by_stage = c.execute(
                "SELECT stage, COUNT(*) AS n, SUM(cost_micro) AS cost, "
                "SUM(tokens_total) AS tokens, SUM(images) AS images, "
                "SUM(seconds) AS seconds, SUM(chars) AS chars "
                "FROM calls " + where +
                " GROUP BY stage ORDER BY cost DESC", args).fetchall()

            by_provider = c.execute(
                "SELECT provider, model, COUNT(*) AS n, "
                "SUM(cost_micro) AS cost, SUM(tokens_total) AS tokens "
                "FROM calls " + where +
                " GROUP BY provider, model ORDER BY cost DESC",
                args).fetchall()

            by_day = c.execute(
                "SELECT substr(ts,1,10) AS day, COUNT(*) AS n, "
                "SUM(cost_micro) AS cost, SUM(tokens_total) AS tokens "
                "FROM calls " + where +
                " GROUP BY substr(ts,1,10) ORDER BY day DESC LIMIT 60",
                args).fetchall()

            return {
                "total": {
                    "calls": total["n"] or 0,
                    "cost_yuan": round((total["cost"] or 0) / 1e6, 4),
                    "cost_micro": total["cost"] or 0,
                    "tokens": total["tokens"] or 0,
                    "images": total["images"] or 0,
                    "seconds": round(total["seconds"] or 0, 2),
                    "chars": total["chars"] or 0,
                    "ok": total["ok"] or 0,
                    "failed": total["failed"] or 0,
                },
                "by_stage": [_agg_row(r) for r in by_stage],
                "by_provider": [_agg_row(r) for r in by_provider],
                "by_day": [dict(r) | {
                    "cost_yuan": round((r["cost"] or 0) / 1e6, 4)}
                    for r in by_day],
            }
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)[:200], "total": {}, "by_stage": [],
                "by_provider": [], "by_day": []}


def _agg_row(r) -> Dict[str, Any]:
    d = dict(r)
    d["cost_yuan"] = round((d.get("cost") or 0) / 1e6, 4)
    if "seconds" in d:
        d["seconds"] = round(d["seconds"] or 0, 2)
    return d


def _row_to_dict(r) -> Dict[str, Any]:
    d = dict(r)
    d["cost_yuan"] = round((d.get("cost_micro") or 0) / 1e6, 4)
    return d


def health() -> Dict[str, Any]:
    """数据库健康检查。"""
    try:
        with _LOCK:
            c = _conn()
            n = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            p = _db_path()
            return {
                "ok": True,
                "path": str(p),
                "size_mb": round(p.stat().st_size / 1e6, 2)
                if p.exists() else 0,
                "rows": n,
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200]}
