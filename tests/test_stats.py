"""
tests/test_stats.py — M2-1 运营看板统计接口单测

每个测试：
  1. 创建临时 router.db（in-memory SQLite）
  2. 插入已知 decision 记录
  3. 调 /router/stats → 断言聚合结果正确

兼容性说明：
test_decision.py / test_cooldown.py 在模块顶层 mock 了 sys.modules["aiosqlite"]，
且 router.db / router.decision 在它们收集期就已被导入（带着 MagicMock 的 aiosqlite）。
因此本文件不依赖模块顶层导入，所有获取模块都在 fixture/测试内部通过
`_load_router_modules()` 完成 —— 它先把真实 aiosqlite 塞回 sys.modules，
再 `importlib.reload(router.db)` 强制重执行，拿到引用真实 aiosqlite 的 db 模块。
"""
import asyncio
import importlib
import os
import sys
from datetime import datetime, timedelta
from typing import AsyncGenerator

import pytest
import pytest_asyncio


def _load_router_modules():
    """
    获取引用真实 aiosqlite 的 router.db 模块（强制 reload）。
    所有需要的名字都从 db_mod 上取（db.py 顶部 from .models import ... 导出了它们）：
      db_mod.aiosqlite / db_mod.TaskType / db_mod.QuotaStatus /
      db_mod.RouterDecisionEventRecord / db_mod.insert_decision_event /
      db_mod.query_stats / db_mod.parse_time_range / db_mod.init_db
    """
    # 确保项目根在 sys.path
    project_root = os.path.join(os.path.dirname(__file__), "..")
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 1. 用真实 aiosqlite 覆盖 sys.modules（先删 mock 再 import）
    if "aiosqlite" in sys.modules:
        del sys.modules["aiosqlite"]
    import aiosqlite as _real_ai
    sys.modules["aiosqlite"] = _real_ai

    # 2. reload router.db → 其内部 `import aiosqlite` 拿到真实模块
    import router.db
    db_mod = importlib.reload(router.db)
    return db_mod


# ── fixtures ──

@pytest_asyncio.fixture(scope="function")
async def db_conn() -> AsyncGenerator:
    """临时内存 DB，每次测试独立"""
    db_mod = _load_router_modules()
    conn = await db_mod.aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("""
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
            created_at          TEXT    NOT NULL
        )
    """)
    await conn.commit()
    yield conn
    await conn.close()


def _make_record(db_mod, request_id: str, model: str = "deepseek-v4-flash",
                 provider: str = "tencent_free", token: str = "free",
                 success: bool = True, latency_ms: float = 800.0,
                 task_type=None, fallback_reason=None,
                 minutes_ago: int = 0):
    """构造一条 decision event 记录"""
    if task_type is None:
        task_type = db_mod.TaskType.TEXT
    ts = datetime.utcnow() - timedelta(minutes=minutes_ago)
    return db_mod.RouterDecisionEventRecord(
        request_id=request_id,
        original_model=model,
        logical_model=model,
        task_type=task_type,
        selected_provider=provider,
        selected_token=token,
        fallback_reason=fallback_reason,
        quota_status_before=db_mod.QuotaStatus.AVAILABLE,
        latency_ms=latency_ms,
        success=success,
        created_at=ts,
    )


# ── parse_time_range 测试（不依赖 DB） ──

class TestParseTimeRange:
    def test_default_24h(self):
        assert _load_router_modules().parse_time_range("24h") == timedelta(hours=24)

    def test_1d(self):
        assert _load_router_modules().parse_time_range("1d") == timedelta(days=1)

    def test_30m(self):
        assert _load_router_modules().parse_time_range("30m") == timedelta(minutes=30)

    def test_7d(self):
        assert _load_router_modules().parse_time_range("7d") == timedelta(days=7)

    def test_invalid_fallback(self):
        """非法字符串 → 回退 24h"""
        assert _load_router_modules().parse_time_range("foobar") == timedelta(hours=24)


# ── query_stats 直接测试 ──

class TestQueryStats:
    @pytest.mark.asyncio
    async def test_empty_db(self, db_conn):
        """空 DB → 所有计数 0，hourly 有 24 个空桶"""
        db_mod = _load_router_modules()
        now = datetime.utcnow()
        since = now - timedelta(hours=24)
        stats = await db_mod.query_stats(db_conn, since, n_buckets=24)
        assert stats["total_requests"] == 0
        assert stats["success_count"] == 0
        assert stats["fail_count"] == 0
        assert stats["success_rate"] == 0.0
        assert stats["free_count"] == 0
        assert stats["paid_count"] == 0
        assert stats["free_ratio"] == 0.0
        assert stats["avg_latency_ms"] == 0.0
        assert stats["fallback_count"] == 0
        assert len(stats["hourly_requests"]) == 24
        assert all(h["count"] == 0 for h in stats["hourly_requests"])

    @pytest.mark.asyncio
    async def test_basic_aggregation(self, db_conn):
        """插入 5 条记录（3 成功/2 失败；3 free/2 paid）→ 聚合正确"""
        db_mod = _load_router_modules()
        now = datetime.utcnow()
        records = [
            _make_record(db_mod, "r1", success=True,  token="free", latency_ms=600.0, minutes_ago=1),
            _make_record(db_mod, "r2", success=True,  token="free", latency_ms=700.0, minutes_ago=2),
            _make_record(db_mod, "r3", success=True,  token="paid", latency_ms=900.0, minutes_ago=3),
            _make_record(db_mod, "r4", success=False, token="free", latency_ms=500.0, minutes_ago=4),
            _make_record(db_mod, "r5", success=False, token="paid", latency_ms=550.0, minutes_ago=5),
        ]
        for r in records:
            await db_mod.insert_decision_event(db_conn, r)

        since = now - timedelta(hours=1)
        stats = await db_mod.query_stats(db_conn, since, n_buckets=24)

        assert stats["total_requests"] == 5
        assert stats["success_count"] == 3
        assert stats["fail_count"] == 2
        assert abs(stats["success_rate"] - 0.6) < 0.01
        assert stats["free_count"] == 3
        assert stats["paid_count"] == 2
        assert abs(stats["free_ratio"] - 0.6) < 0.01
        assert stats["fallback_count"] == 0
        # avg_latency ≈ (600+700+900+500+550)/5 = 650
        assert abs(stats["avg_latency_ms"] - 650.0) < 1.0

    @pytest.mark.asyncio
    async def test_distribution_counts(self, db_conn):
        """provider / model / task_type 分布键与计数正确"""
        db_mod = _load_router_modules()
        records = [
            _make_record(db_mod, "r1", model="deepseek-v4-flash", provider="tencent_free",
                         task_type=db_mod.TaskType.TEXT),
            _make_record(db_mod, "r2", model="deepseek-v4-flash", provider="tencent_free",
                         task_type=db_mod.TaskType.TEXT),
            _make_record(db_mod, "r3", model="qwen3.8-flash", provider="bailian_free",
                         task_type=db_mod.TaskType.VISION),
            _make_record(db_mod, "r4", model="deepseek-v4-flash", provider="tencent_plan",
                         task_type=db_mod.TaskType.TEXT),
        ]
        for r in records:
            await db_mod.insert_decision_event(db_conn, r)

        now = datetime.utcnow()
        stats = await db_mod.query_stats(db_conn, now - timedelta(hours=1), n_buckets=24)

        assert stats["provider_distribution"]["tencent_free"] == 2
        assert stats["provider_distribution"]["bailian_free"] == 1
        assert stats["provider_distribution"]["tencent_plan"] == 1

        assert stats["model_distribution"]["deepseek-v4-flash"] == 3
        assert stats["model_distribution"]["qwen3.8-flash"] == 1

        assert stats["task_type_distribution"]["text"] == 3
        assert stats["task_type_distribution"]["vision"] == 1

    @pytest.mark.asyncio
    async def test_fallback_count(self, db_conn):
        """fallback_reason 非 NULL → fallback_count 计数"""
        db_mod = _load_router_modules()
        records = [
            _make_record(db_mod, "r1", fallback_reason=None),
            _make_record(db_mod, "r2", fallback_reason="402_exhausted"),
            _make_record(db_mod, "r3", fallback_reason="402_exhausted"),
            _make_record(db_mod, "r4", fallback_reason=None),
        ]
        for r in records:
            await db_mod.insert_decision_event(db_conn, r)

        now = datetime.utcnow()
        stats = await db_mod.query_stats(db_conn, now - timedelta(hours=1), n_buckets=24)
        assert stats["fallback_count"] == 2

    @pytest.mark.asyncio
    async def test_time_range_filter(self, db_conn):
        """只统计 time_range 窗口内的记录"""
        db_mod = _load_router_modules()
        now = datetime.utcnow()
        r1 = _make_record(db_mod, "r1", minutes_ago=30)   # 30 分钟前（在 1h 窗口内）
        r2 = _make_record(db_mod, "r2", minutes_ago=90)   # 90 分钟前（不在 1h 窗口内）
        await db_mod.insert_decision_event(db_conn, r1)
        await db_mod.insert_decision_event(db_conn, r2)

        since_1h = now - timedelta(hours=1)
        stats = await db_mod.query_stats(db_conn, since_1h, n_buckets=24)
        assert stats["total_requests"] == 1

        since_2h = now - timedelta(hours=2)
        stats2 = await db_mod.query_stats(db_conn, since_2h, n_buckets=24)
        assert stats2["total_requests"] == 2

    @pytest.mark.asyncio
    async def test_hourly_buckets(self, db_conn):
        """hourly_requests 每小时桶计数正确"""
        db_mod = _load_router_modules()
        now = datetime.utcnow()
        r1 = _make_record(db_mod, "r1", minutes_ago=5)    # 当前小时
        r2 = _make_record(db_mod, "r2", minutes_ago=65)   # 1 小时前
        await db_mod.insert_decision_event(db_conn, r1)
        await db_mod.insert_decision_event(db_conn, r2)

        since = now - timedelta(hours=2)
        stats = await db_mod.query_stats(db_conn, since, n_buckets=24)
        non_empty = [h for h in stats["hourly_requests"] if h["count"] > 0]
        assert len(non_empty) >= 2


# ── /router/stats 接口集成测试 ──

@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_stats_endpoint_with_real_db(tmp_path):
    """
    模拟真实部署环境：
      1. 临时目录建真实 router.db（用 db.init_db 完整建表）
      2. 插入已知记录
      3. monkeypatch main._db_conn 指向该连接
      4. 直接调 get_stats 协程
    """
    db_mod = _load_router_modules()

    # 1. 建真实 DB（临时文件）
    db_path = tmp_path / "router.db"
    conn = await db_mod.init_db(db_path)

    # 2. 插入 4 条已知记录
    records = [
        _make_record(db_mod, "e1", model="deepseek-v4-flash", provider="tencent_free",
                     token="free", success=True, latency_ms=600.0, minutes_ago=1),
        _make_record(db_mod, "e2", model="deepseek-v4-flash", provider="tencent_free",
                     token="free", success=True, latency_ms=700.0, minutes_ago=2,
                     fallback_reason="402_exhausted"),
        _make_record(db_mod, "e3", model="qwen3.8-flash", provider="bailian_free",
                     token="free", success=True, latency_ms=800.0, minutes_ago=3,
                     task_type=db_mod.TaskType.VISION),
        _make_record(db_mod, "e4", model="deepseek-v4-flash", provider="tencent_plan",
                     token="paid", success=False, latency_ms=900.0, minutes_ago=4,
                     fallback_reason="402_exhausted"),
    ]
    for r in records:
        await db_mod.insert_decision_event(conn, r)

    # 3. 动态导入 main（绕过 mock 污染），monkeypatch DB 连接
    #    注意：main.py 可能已在 test_decision 收集期被导入（带 mock aiosqlite），
    #    但 get_stats 内部是 `from .db import query_stats` + 用传入的 _db_conn，
    #    而 query_stats 只用 conn 参数做真实 SQL，不受模块级 aiosqlite 影响。
    main_mod = importlib.import_module("router.main")
    old_conn = main_mod._db_conn
    main_mod._db_conn = conn

    try:
        # 4. 直接调 endpoint 协程
        stats = await main_mod.get_stats(time_range="1h")

        assert stats["total_requests"] == 4
        assert stats["success_count"] == 3
        assert stats["fail_count"] == 1
        assert abs(stats["success_rate"] - 0.75) < 0.01
        assert stats["free_count"] == 3
        assert stats["paid_count"] == 1
        assert abs(stats["free_ratio"] - 0.75) < 0.01
        assert abs(stats["avg_latency_ms"] - 750.0) < 1.0   # (600+700+800+900)/4
        assert stats["fallback_count"] == 2                 # e2 + e4
        assert stats["provider_distribution"]["tencent_free"] == 2
        assert stats["provider_distribution"]["bailian_free"] == 1
        assert stats["provider_distribution"]["tencent_plan"] == 1
        assert stats["model_distribution"]["deepseek-v4-flash"] == 3
        assert stats["model_distribution"]["qwen3.8-flash"] == 1
        assert stats["task_type_distribution"]["text"] == 3
        assert stats["task_type_distribution"]["vision"] == 1
        assert len(stats["hourly_requests"]) == 24
    finally:
        main_mod._db_conn = old_conn
        await conn.close()
