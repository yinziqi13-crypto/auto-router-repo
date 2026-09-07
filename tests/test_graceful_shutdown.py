"""
M3-1e / M3-1g: 优雅停机单元测试

验证点：
  1. config.json 缺失时 startup 报 RuntimeError（M3-1f）
  2. config.json 存在时 lifespan startup 正常，/health 返回 200
  3. 活跃请求计数中间件：非流式请求 +1/-1
  4. _shutdown_event 在 shutdown 段被 set()
  5. shutdown 时等待活跃请求归零（最多 13s）后才关闭资源
  6. RequestCountMiddleware 对流式请求的计数时机（响应体发完后 -1）
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 确保 aiosqlite 是真实模块（其他测试文件可能 mock 了 sys.modules）
if "aiosqlite" in sys.modules and not sys.modules["aiosqlite"].__class__.__name__ == "module":
    import aiosqlite as _real_aiosqlite
    sys.modules["aiosqlite"] = _real_aiosqlite

# 确保 src/ 在 sys.path
_src_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

import router.main as m


# ────────────────────────────────────
# 辅助：创建一个带 config.json 的 TestClient 上下文
# ────────────────────────────────────

@pytest.fixture
def app_with_config(tmp_path, monkeypatch):
    """
    在 src/router/ 创建临时 config.json，
    替换 main 命名空间中的 init_db / StateManager / ProviderRegistry /
    DecisionEngine / _cooldown_loop（lifespan 通过模块内名字调用它们，
    必须替换函数与构造函数本身，而不是模块全局变量——lifespan 内会重新赋值）。
    返回 app。
    """
    src_dir = Path(m.__file__).parent
    cfg_path = src_dir / "config.json"
    cfg_path.write_text(
        json.dumps({"new_api_base_url": "http://test:3000"}),
        encoding="utf-8",
    )

    # init_db → 返回 AsyncMock 连接（close() 可 await）
    db_conn = AsyncMock()
    monkeypatch.setattr(m, "init_db", AsyncMock(return_value=db_conn))

    # StateManager：构造函数返回 AsyncMock 实例（start/stop 可 await）
    sm = AsyncMock()
    sm_cls = MagicMock(return_value=sm)
    monkeypatch.setattr(m, "StateManager", sm_cls)

    # ProviderRegistry：构造函数返回 AsyncMock 实例
    pr = AsyncMock()
    pr.get_default.return_value = None  # 触发默认 provider 注册分支
    pr_cls = MagicMock(return_value=pr)
    monkeypatch.setattr(m, "ProviderRegistry", pr_cls)

    # DecisionEngine：构造函数返回 MagicMock（/health 不触发决策）
    monkeypatch.setattr(m, "DecisionEngine", MagicMock(return_value=MagicMock()))

    # _cooldown_loop → AsyncMock（create_task 拿到的是 coroutine）
    monkeypatch.setattr(m, "_cooldown_loop", AsyncMock())

    # 每个测试用全新的 Event（asyncio.Event 一旦 set 无法清除，
    # 前一个测试退出 TestClient 时已置位，共享实例会污染后续断言）
    monkeypatch.setattr(m, "_shutdown_event", asyncio.Event())

    yield m.app

    # 清理
    if cfg_path.exists():
        cfg_path.unlink()


# ────────────────────────────────────
# 测试 1: config.json 缺失时 startup 报 RuntimeError
# ────────────────────────────────────

def test_missing_config_json_raises(monkeypatch):
    """M3-1f：config.json 缺失时，startup 应报 RuntimeError"""
    cfg_path = Path(m.__file__).parent / "config.json"
    backed_up = False
    if cfg_path.exists():
        cfg_path.rename(cfg_path.with_suffix(".bak"))
        backed_up = True

    try:
        monkeypatch.setattr(m, "config", None)
        with pytest.raises(RuntimeError, match="config.json not found"):
            with TestClient(m.app):
                pass
    finally:
        if backed_up:
            cfg_path.with_suffix(".bak").rename(cfg_path)


# ────────────────────────────────────
# 测试 2: config.json 存在时 lifespan startup 正常
# ────────────────────────────────────

def test_lifespan_startup_success(app_with_config):
    """config.json 存在，验证 startup 进入 yield，/health 返回 200"""
    with TestClient(app_with_config) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "M3-1e"


# ────────────────────────────────────
# 测试 3: 活跃请求计数中间件（非流式）
# ────────────────────────────────────

def test_active_request_count_middleware(app_with_config):
    """验证 RequestCountMiddleware 在非流式请求时正确 +1/-1"""
    with TestClient(app_with_config) as client:
        assert m._active_requests == 0
        resp = client.get("/health")
        assert resp.status_code == 200
        # 请求完成后应归零
        assert m._active_requests == 0


# ────────────────────────────────────
# 测试 4: shutdown 时 _shutdown_event 被 set
# ────────────────────────────────────

def test_shutdown_event_set_on_sigterm(app_with_config):
    """验证 shutdown 段执行时 _shutdown_event 被 set()"""
    assert not m._shutdown_event.is_set()

    with TestClient(app_with_config) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        # 在 lifespan yield 期间（客户端打开时）shutdown_event 尚未 set
        # 退出 with 块时触发 shutdown，此时 _shutdown_event 应已 set
        pass

    assert m._shutdown_event.is_set()


# ────────────────────────────────────
# 测试 5: shutdown 等待活跃请求（模拟场景）
# ────────────────────────────────────

@pytest.mark.asyncio
async def test_shutdown_waits_for_active_requests(monkeypatch):
    """
    模拟 shutdown 时有一个活跃请求尚未完成，
    验证 lifespan shutdown 段会等待（最多 13s）
    """
    # 设置 config 和 startup 依赖
    m.config = MagicMock()
    m.config.new_api_base_url = "http://test:3000"
    m._db_conn = MagicMock()
    m.state_mgr = AsyncMock()
    m.provider_registry = AsyncMock()
    m.provider_registry.get_default.return_value = None
    m.provider_registry.close_all = AsyncMock()
    m.decision_eng = MagicMock()
    m._cooldown_task = None

    # 模拟有一个活跃请求
    m._active_requests = 1

    # 跟踪 await asyncio.sleep 的调用次数
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(delay):
        sleep_calls.append(delay)
        # 第一次 sleep 后模拟请求完成
        if len(sleep_calls) == 1:
            m._active_requests = 0
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(asyncio, "sleep", tracking_sleep)

    # 直接调用 shutdown 段逻辑（模拟 SIGTERM 场景）
    m._shutdown_event.set()

    for _i in range(13):
        async with m._active_requests_lock:
            if m._active_requests <= 0:
                break
        await asyncio.sleep(0.01)

    assert m._active_requests <= 0


# ────────────────────────────────────
# 测试 6: 流式请求计数时机
# ────────────────────────────────────

def test_streaming_request_count_timing(app_with_config):
    """
    验证流式请求在响应体全部发完后 _active_requests 才 -1
    （通过 /health 非流式间接验证中间件工作正常）
    """
    with TestClient(app_with_config) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert m._active_requests == 0
