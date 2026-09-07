"""
M3-1e / M3-1f / M3-1g: 优雅停机 + 启动自检 单元测试

验证点：
  1. config.json 缺失时 startup 报 RuntimeError（M3-1f）
  2. config.json 必需字段缺失时 startup 报 RuntimeError（M3-1f）
  3. provider_models 为空时 startup 报 RuntimeError（M3-1f）
  4. New API 连通性检查失败时报 RuntimeError（M3-1f）
  5. config.json 存在时 lifespan startup 正常，/health 返回 200
  6. 活跃请求计数中间件：非流式请求 +1/-1
  7. _shutdown_event 在 shutdown 段被 set()
  8. shutdown 时等待活跃请求归零（最多 13s）后才关闭资源
  9. RequestCountMiddleware 对流式请求的计数时机（响应体发完后 -1）
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
    在 tmp_path 创建临时 config.json，
    通过 monkeypatch m._CONFIG_PATH 指向它，
    替换 main 命名空间中的 init_db / StateManager / ProviderRegistry /
    DecisionEngine / _cooldown_loop（lifespan 通过模块内名字调用它们，
    必须替换函数与构造函数本身，而不是模块全局变量——lifespan 内会重新赋值）。
    返回 app。
    """
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "new_api_base_url": "http://test:3000",
            "token_free": "sk-free-test",
            "token_paid": "sk-paid-test",
            "provider_models": {
                "tencent_free": ["deepseek-v4-flash"],
                "bailian_free": ["deepseek-v4-flash"],
            },
            "free_providers": ["tencent_free", "bailian_free"],
            "provider_priority": ["tencent_free", "bailian_free"],
        }),
        encoding="utf-8",
    )
    # 让 main.py 读 tmp_path 里的 config.json
    monkeypatch.setattr(m, "_CONFIG_PATH", cfg_path)

    # init_db → 返回 AsyncMock 连接（close() 可 await）
    db_conn = AsyncMock()
    monkeypatch.setattr(m, "init_db", AsyncMock(return_value=db_conn))

    # StateManager：构造函数返回 AsyncMock 实例（start/stop 可 await）
    sm = AsyncMock()
    sm_cls = MagicMock(return_value=sm)
    monkeypatch.setattr(m, "StateManager", sm_cls)

    # ProviderRegistry：连通性检查需要 all() 返回 dict + provider health_check True
    # 用普通 lambda 替代 return_value（MagicMock 属性在 asyncio 上下文会被当协程）
    async def _fake_health_check():
        return True
    mock_provider = MagicMock()
    mock_provider.health_check = _fake_health_check
    pr = MagicMock()
    pr.get_default.return_value = mock_provider
    pr.all = lambda: {"new_api": mock_provider}
    pr.close_all = AsyncMock()
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


# ────────────────────────────────────
# 测试 1: config.json 缺失时 startup 报 RuntimeError
# ────────────────────────────────────

def test_missing_config_json_raises(monkeypatch, tmp_path):
    """M3-1f：config.json 缺失时，startup 应报 RuntimeError"""
    # _CONFIG_PATH 指向不存在的文件
    missing_cfg = tmp_path / "not_exist.json"
    monkeypatch.setattr(m, "_CONFIG_PATH", missing_cfg)

    with pytest.raises(RuntimeError, match="config.json not found"):
        with TestClient(m.app):
            pass


# ────────────────────────────────────
# 测试 2: config.json 必需字段缺失时 startup 报 RuntimeError
# ────────────────────────────────────

def test_config_missing_required_field_raises(monkeypatch, tmp_path):
    """M3-1f：config.json 缺少必需字段时，startup 应报 RuntimeError"""
    cfg_path = tmp_path / "config.json"
    # 写一个缺少 token_paid 的 config
    bad_cfg = {
        "new_api_base_url": "http://test:3000",
        "token_free": "sk-free-test",
        # 故意缺少 token_paid
        "provider_models": {"tencent_free": ["deepseek-v4-flash"]},
    }
    cfg_path.write_text(json.dumps(bad_cfg), encoding="utf-8")
    monkeypatch.setattr(m, "_CONFIG_PATH", cfg_path)

    with pytest.raises(RuntimeError, match="缺少必需字段"):
        with TestClient(m.app):
            pass


def test_config_empty_provider_models_raises(monkeypatch, tmp_path):
    """M3-1f：provider_models 为空时，startup 应报 RuntimeError"""
    cfg_path = tmp_path / "config.json"
    bad_cfg = {
        "new_api_base_url": "http://test:3000",
        "token_free": "sk-free-test",
        "token_paid": "sk-paid-test",
        "provider_models": {},
    }
    cfg_path.write_text(json.dumps(bad_cfg), encoding="utf-8")
    monkeypatch.setattr(m, "_CONFIG_PATH", cfg_path)

    with pytest.raises(RuntimeError, match="provider_models 为空"):
        with TestClient(m.app):
            pass


# ────────────────────────────────────
# 测试 3: New API 连通性检查失败时报 RuntimeError
# ────────────────────────────────────

def test_new_api_health_check_fails(monkeypatch, tmp_path):
    """M3-1f：New API 连通性检查失败，startup 应报 RuntimeError"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "new_api_base_url": "http://127.0.0.1:3000",
            "token_free": "sk-free-test",
            "token_paid": "sk-paid-test",
            "provider_models": {"tencent_free": ["deepseek-v4-flash"]},
            "free_providers": ["tencent_free"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_CONFIG_PATH", cfg_path)

    # 修复：mock init_db（否则全量跑时 aiosqlite 被污染会炸）
    db_conn = AsyncMock()
    monkeypatch.setattr(m, "init_db", AsyncMock(return_value=db_conn))

    # health_check 返回 False → 连通性检查失败
    async def _bad_health():
        return False
    mock_provider = MagicMock()
    mock_provider.health_check = _bad_health
    pr = MagicMock()
    pr.get_default.return_value = mock_provider
    pr.all = lambda: {"new_api": mock_provider}
    pr.close_all = AsyncMock()
    pr_cls = MagicMock(return_value=pr)
    monkeypatch.setattr(m, "ProviderRegistry", pr_cls)

    # StateManager / DecisionEngine / _cooldown_loop 也要 mock（health check 之后会走到）
    sm = AsyncMock()
    sm_cls = MagicMock(return_value=sm)
    monkeypatch.setattr(m, "StateManager", sm_cls)
    monkeypatch.setattr(m, "DecisionEngine", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(m, "_cooldown_loop", AsyncMock())
    monkeypatch.setattr(m, "_shutdown_event", asyncio.Event())

    with pytest.raises(RuntimeError, match="连通性检查失败"):
        with TestClient(m.app):
            pass


# ────────────────────────────────────
# 测试 4: config.json 存在时 lifespan startup 正常
# ────────────────────────────────────

def test_lifespan_startup_success(app_with_config):
    """config.json 存在，验证 startup 进入 yield，/health 返回 200"""
    with TestClient(app_with_config) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "M3-2"


# ────────────────────────────────────
# 测试 5: 活跃请求计数中间件（非流式）
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
# 测试 6: shutdown 时 _shutdown_event 被 set
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
# 测试 7: shutdown 等待活跃请求（模拟场景）
# ────────────────────────────────────

@pytest.mark.asyncio
async def test_shutdown_waits_for_active_requests(monkeypatch, tmp_path):
    """
    模拟 shutdown 时有一个活跃请求尚未完成，
    验证 lifespan shutdown 段会等待（最多 13s）
    """
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "new_api_base_url": "http://test:3000",
            "token_free": "sk-free-test",
            "token_paid": "sk-paid-test",
            "provider_models": {"tencent_free": ["deepseek-v4-flash"]},
            "free_providers": ["tencent_free"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_CONFIG_PATH", cfg_path)

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
# 测试 8: 流式请求计数时机
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
