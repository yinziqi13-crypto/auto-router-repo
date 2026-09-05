"""
Auto Router M1-2 数据库层
对应 M1-0.5 设计文档中的 router.db 数据模型

三张表：
  1. quota_state   — 额度状态（可再生缓存）
  2. error_event   — 错误事件
  3. router_decision_event — 路由决策事件

规则（按 ADR-002 + 孙磊 4 点确认）：
  1. QuotaState 是可再生缓存，冷启动时从 DB 恢复
  2. 每次请求结束后异步写入 decision event
  3. ErrorEvent 在异常时异步写入
  4. 不用 New API 数据库，只用 router.db
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, AsyncGenerator, Dict, Any

import aiosqlite

from .models import (
    QuotaStatus, ErrorType, TaskType,
    QuotaStateRecord, ErrorEventRecord, RouterDecisionEventRecord,
)

logger = logging.getLogger("auto_router.db")

# ─────────────────────────────────────────────
# 数据库路径
# ─────────────────────────────────────────────

DEFAULT_DB_PATH = Path(__file__).parent / "router.db"


# ─────────────────────────────────────────────
# 初始化
# ─────────────────────────────────────────────

INIT_SQL = """
-- quota_state 表
CREATE TABLE IF NOT EXISTS quota_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'unknown',
    consecutive_402   INTEGER NOT NULL DEFAULT 0,
    last_402_at TEXT,               -- ISO8601
    cooldown_until   TEXT,          -- ISO8601
    updated_at  TEXT    NOT NULL,   -- ISO8601
    UNIQUE(provider, model)
);

CREATE INDEX IF NOT EXISTS idx_quota_state_pk ON quota_state(provider, model);

-- error_event 表
CREATE TABLE IF NOT EXISTS error_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    error_type   TEXT    NOT NULL,
    channel_id   INTEGER NOT NULL,
    status_code  INTEGER,
    error_msg    TEXT    NOT NULL DEFAULT '',
    requested_at TEXT    NOT NULL    -- ISO8601
);

CREATE INDEX IF NOT EXISTS idx_error_event_pm ON error_event(provider, model);
CREATE INDEX IF NOT EXISTS idx_error_event_type ON error_event(error_type);
CREATE INDEX IF NOT EXISTS idx_error_event_time ON error_event(requested_at);

-- router_decision_event 表
CREATE TABLE IF NOT EXISTS router_decision_event (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT    NOT NULL,
    original_model      TEXT    NOT NULL,
    logical_model       TEXT    NOT NULL,
    task_type           TEXT    NOT NULL DEFAULT 'unknown',
    selected_provider   TEXT    NOT NULL,
    selected_token      TEXT    NOT NULL,
    fallback_reason     TEXT,
    quota_status_before TEXT,
    latency_ms          REAL,
    success             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL   -- ISO8601
);

CREATE INDEX IF NOT EXISTS idx_decision_request_id ON router_decision_event(request_id);
CREATE INDEX IF NOT EXISTS idx_decision_model ON router_decision_event(logical_model);
CREATE INDEX IF NOT EXISTS idx_decision_time ON router_decision_event(created_at);
"""


async def init_db(db_path: Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    """
    初始化数据库（如不存在则建表）
    返回已打开的连接（调用方负责 close）
    """
    conn = await aiosqlite.connect(str(db_path))
    # WAL 模式提升并发读写性能
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    for stmt in INIT_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            await conn.execute(stmt)
    await conn.commit()
    logger.info(f"DB initialized at {db_path}")
    return conn


# ─────────────────────────────────────────────
# QuotaState CRUD
# ─────────────────────────────────────────────

async def upsert_quota_state(
    conn: aiosqlite.Connection,
    record: QuotaStateRecord,
) -> None:
    """upsert quota_state 记录"""
    await conn.execute("""
        INSERT INTO quota_state
            (provider, model, status, consecutive_402, last_402_at,
             cooldown_until, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, model) DO UPDATE SET
            status=excluded.status,
            consecutive_402=excluded.consecutive_402,
            last_402_at=excluded.last_402_at,
            cooldown_until=excluded.cooldown_until,
            updated_at=excluded.updated_at
    """, (
        record.provider,
        record.model,
        record.status.value,
        record.consecutive_402,
        record.last_402_at.isoformat() if record.last_402_at else None,
        record.cooldown_until.isoformat() if record.cooldown_until else None,
        record.updated_at.isoformat(),
    ))
    await conn.commit()


async def load_quota_state(
    conn: aiosqlite.Connection,
    provider: str,
    model: str,
) -> Optional[QuotaStateRecord]:
    """从 DB 加载单条 quota_state（冷启动时用）"""
    async with conn.execute(
        "SELECT provider, model, status, consecutive_402, last_402_at, "
        "cooldown_until, updated_at FROM quota_state "
        "WHERE provider=? AND model=?",
        (provider, model),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return QuotaStateRecord(
        provider=row[0],
        model=row[1],
        status=QuotaStatus(row[2]),
        consecutive_402=row[3],
        last_402_at=datetime.fromisoformat(row[4]) if row[4] else None,
        cooldown_until=datetime.fromisoformat(row[5]) if row[5] else None,
        updated_at=datetime.fromisoformat(row[6]),
    )


# ─────────────────────────────────────────────
# ErrorEvent CRUD
# ─────────────────────────────────────────────

async def insert_error_event(
    conn: aiosqlite.Connection,
    record: ErrorEventRecord,
) -> int:
    """写入 error_event，返回自增 id"""
    cursor = await conn.execute("""
        INSERT INTO error_event
            (provider, model, error_type, channel_id, status_code, error_msg, requested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        record.provider,
        record.model,
        record.error_type.value,
        record.channel_id,
        record.status_code,
        record.error_msg,
        record.requested_at.isoformat(),
    ))
    await conn.commit()
    return cursor.lastrowid  # type: ignore


# ─────────────────────────────────────────────
# RouterDecisionEvent CRUD
# ─────────────────────────────────────────────

async def insert_decision_event(
    conn: aiosqlite.Connection,
    record: RouterDecisionEventRecord,
) -> int:
    """写入 router_decision_event，返回自增 id"""
    cursor = await conn.execute("""
        INSERT INTO router_decision_event
            (request_id, original_model, logical_model, task_type,
             selected_provider, selected_token, fallback_reason,
             quota_status_before, latency_ms, success, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.request_id,
        record.original_model,
        record.logical_model,
        record.task_type.value,
        record.selected_provider,
        record.selected_token,
        record.fallback_reason,
        record.quota_status_before.value if record.quota_status_before else None,
        record.latency_ms,
        1 if record.success else 0,
        record.created_at.isoformat(),
    ))
    await conn.commit()
    return cursor.lastrowid  # type: ignore


async def query_decision_events(
    conn: aiosqlite.Connection,
    limit: int = 50,
    offset: int = 0,
    model: Optional[str] = None,
    success_only: Optional[bool] = None,
) -> list[RouterDecisionEventRecord]:
    """查询决策事件（支持分页 + 过滤）"""
    sql = "SELECT id, request_id, original_model, logical_model, task_type, " \
          "selected_provider, selected_token, fallback_reason, " \
          "quota_status_before, latency_ms, success, created_at FROM router_decision_event WHERE 1=1"
    params: list[Any] = []

    if model:
        sql += " AND logical_model=?"
        params.append(model)
    if success_only is not None:
        sql += " AND success=?"
        params.append(1 if success_only else 0)

    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = await conn.execute_fetchall(sql, params)
    return [
        RouterDecisionEventRecord(
            id=row[0],
            request_id=row[1],
            original_model=row[2],
            logical_model=row[3],
            task_type=TaskType(row[4]),
            selected_provider=row[5],
            selected_token=row[6],
            fallback_reason=row[7],
            quota_status_before=QuotaStatus(row[8]) if row[8] else None,
            latency_ms=row[9],
            success=bool(row[10]),
            created_at=datetime.fromisoformat(row[11]),
        )
        for row in rows
    ]


# ─────────────────────────────────────────────
# Stats 聚合（M2-1 运营看板）
# ─────────────────────────────────────────────

def parse_time_range(time_range: str) -> timedelta:
    """
    解析 time_range 参数 → timedelta
    支持：24h / 12h / 30m / 1d / 7d
    默认：24h
    """
    tr = (time_range or "24h").strip().lower()
    try:
        if tr.endswith("h"):
            return timedelta(hours=float(tr[:-1]))
        if tr.endswith("d"):
            return timedelta(days=float(tr[:-1]))
        if tr.endswith("m"):
            return timedelta(minutes=float(tr[:-1]))
        if tr.endswith("s"):
            return timedelta(seconds=float(tr[:-1]))
    except ValueError:
        pass
    # 纯数字 → 按小时理解
    try:
        return timedelta(hours=float(tr))
    except ValueError:
        return timedelta(hours=24)


async def query_stats(
    conn: aiosqlite.Connection,
    since: datetime,
    n_buckets: int = 24,
) -> Dict[str, Any]:
    """
    从 router_decision_event 表聚合统计（只读，不碰内存状态）

    返回字段（额外带 hourly_requests 供折线图使用）：
      total_requests / success_count / fail_count / success_rate
      free_count / paid_count / free_ratio
      provider_distribution / model_distribution / task_type_distribution
      avg_latency_ms / fallback_count / hourly_requests
    """
    since_iso = since.isoformat()

    # 1) 汇总指标
    async with conn.execute(
        "SELECT COUNT(*), "
        "       SUM(CASE WHEN success=1 THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), "
        "       AVG(latency_ms), "
        "       SUM(CASE WHEN selected_token='free' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN selected_token='paid' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN fallback_reason IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM router_decision_event WHERE created_at >= ?",
        (since_iso,),
    ) as cur:
        row = await cur.fetchone()

    total = row[0] or 0
    success_count = row[1] or 0
    fail_count = row[2] or 0
    avg_latency_raw = row[3]
    free_count = row[4] or 0
    paid_count = row[5] or 0
    fallback_count = row[6] or 0

    # 2) 渠道分布
    async with conn.execute(
        "SELECT selected_provider, COUNT(*) FROM router_decision_event "
        "WHERE created_at >= ? GROUP BY selected_provider ORDER BY COUNT(*) DESC",
        (since_iso,),
    ) as cur:
        provider_rows = await cur.fetchall()
    provider_distribution = {p: c for p, c in provider_rows}

    # 3) 模型分布
    async with conn.execute(
        "SELECT logical_model, COUNT(*) FROM router_decision_event "
        "WHERE created_at >= ? GROUP BY logical_model ORDER BY COUNT(*) DESC",
        (since_iso,),
    ) as cur:
        model_rows = await cur.fetchall()
    model_distribution = {m: c for m, c in model_rows}

    # 4) 任务类型分布
    async with conn.execute(
        "SELECT task_type, COUNT(*) FROM router_decision_event "
        "WHERE created_at >= ? GROUP BY task_type ORDER BY COUNT(*) DESC",
        (since_iso,),
    ) as cur:
        task_rows = await cur.fetchall()
    task_type_distribution = {t: c for t, c in task_rows}

    # 5) 时间序列（最近 n_buckets 个小时，空桶补 0）
    #    created_at 是 ISO8601 UTC（无时区），substr(created_at,1,13) = 'YYYY-MM-DDTHH'
    async with conn.execute(
        "SELECT substr(created_at, 1, 13), COUNT(*) FROM router_decision_event "
        "WHERE created_at >= ? GROUP BY substr(created_at, 1, 13)",
        (since_iso,),
    ) as cur:
        hour_rows = await cur.fetchall()
    hour_counts = {h: c for h, c in hour_rows}

    # 补齐空桶：从 since 所在小时开始，连续 n_buckets 个小时
    bucket_start = since.replace(minute=0, second=0, microsecond=0)
    hourly_requests = []
    for i in range(n_buckets):
        ts = bucket_start + timedelta(hours=i)
        key = ts.isoformat()[:13]
        hourly_requests.append({
            "hour": key,                     # 'YYYY-MM-DDTHH'（UTC）
            "count": hour_counts.get(key, 0),
        })

    # 6) 组装
    return {
        "time_range": f"{since.isoformat()}Z ~ now",
        "total_requests": total,
        "success_count": success_count,
        "fail_count": fail_count,
        "success_rate": round(success_count / total, 4) if total else 0.0,
        "free_count": free_count,
        "paid_count": paid_count,
        "free_ratio": round(free_count / total, 4) if total else 0.0,
        "provider_distribution": provider_distribution,
        "model_distribution": model_distribution,
        "task_type_distribution": task_type_distribution,
        "avg_latency_ms": round(avg_latency_raw, 2) if avg_latency_raw is not None else 0.0,
        "fallback_count": fallback_count,
        "hourly_requests": hourly_requests,
    }
