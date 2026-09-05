"""
M1-6 修复后单元测试
验证 decide() / route() 降级逻辑一致性 + _free_pool_check 正确性
不连接实时上游，使用 unittest.mock + pytest
"""

import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# ──────────────────────────────────────────
# 提前 mock aiosqlite（decision.py 顶层 import）
# ──────────────────────────────────────────
sys.modules["aiosqlite"] = MagicMock()
sys.modules["aiosqlite"].connect = MagicMock()
sys.modules["aiosqlite"].Connection = MagicMock()

# 现在可以安全 import router 模块
from router.models import (
    RouterConfig,
    QuotaStatus,
    QuotaState,
    ChatCompletionRequest,
    ChatMessage,
    TaskType,
    RouterDecisionEventRecord,
)
from router.decision import _free_pool_check, DecisionEngine


# ──────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────

@pytest.fixture
def config():
    """模拟 RouterConfig（free_providers 显式列表）"""
    cfg = MagicMock(spec=RouterConfig)
    cfg.free_providers = ["tencent_free", "bailian_free"]
    cfg.provider_models = {
        "tencent_free": ["deepseek-v4-flash", "qwen3.8-flash", "glm-5.2"],
        "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash", "glm-5.2"],
        "bailian_lite": ["qwen3.8-flash", "qwen3.7-max", "qwen3.6-flash"],
        "tencent_plan": ["deepseek-v4-flash", "deepseek-v4-pro", "qwen3.8-flash"],
    }
    cfg.provider_priority = ["tencent_free", "bailian_free", "bailian_lite", "tencent_plan"]
    cfg.model_mapping = {
        "tencent_plan": {"deepseek-v4-flash": "deepseek-v4-flash-202605"}
    }
    cfg.token_free = "sk-free-test"
    cfg.token_paid = "sk-paid-test"
    cfg.three_pool_keywords = {}
    cfg.model_routes = {
        "text": "deepseek-v4-flash",
        "vision": None,
        "audio": None,
        "image": None,
    }
    cfg.routing_strategy = "cost_optimized"
    return cfg


@pytest.fixture
def state_manager():
    """Mock StateManager（内存，不依赖 DB）"""
    sm = MagicMock()
    sm.get_status = MagicMock(return_value=None)
    sm.get = MagicMock()
    qstate = QuotaState("dummy", "dummy", status=QuotaStatus.AVAILABLE)
    sm.get.return_value = qstate
    sm.record_402 = AsyncMock()
    sm.reset = AsyncMock()
    return sm


@pytest.fixture
def provider_registry():
    """Mock ProviderRegistry（返回固定 mock provider，所有 .get() 调用返回同一个）"""
    from router.providers import ProviderRegistry, NewAPIProvider
    import sys
    reg = MagicMock(spec=ProviderRegistry)
    # 关键：所有 .get() 返回同一个 mock，避免 test 里拿到的和 engine 里不是同一个
    mock_prov = MagicMock(spec=NewAPIProvider)
    mock_prov.forward = AsyncMock()
    mock_prov.forward_stream = AsyncMock()
    reg.get = MagicMock(return_value=mock_prov)
    reg.get_default = MagicMock(return_value=mock_prov)
    return reg


@pytest.fixture
def engine(config, state_manager, provider_registry):
    """DecisionEngine 实例（M2-6 新签名）"""
    eng = DecisionEngine(
        state_manager=state_manager,
        config=config,
        db_conn=None,
        provider_registry=provider_registry,
    )
    eng._db = None  # 不写 DB
    return eng


def make_request(model: str, stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="hi")],
        stream=stream,
    )


# ──────────────────────────────────────────
# 测试 _free_pool_check（纯函数）
# ──────────────────────────────────────────

class TestFreePoolCheck:
    """测试 _free_pool_check 辅助函数"""

    def _make_state_fn(self, status_map: dict):
        """返回一个 lambda，模拟 state_get_status(p, m)"""
        return lambda p, m: status_map.get((p, m))

    def test_all_free_exhausted(self):
        """场景：所有持有该模型的 free provider 都 exhausted → 降级"""
        status_map = {
            ("tencent_free", "deepseek-v4-flash"): QuotaStatus.EXHAUSTED,
            ("bailian_free", "deepseek-v4-flash"): QuotaStatus.EXHAUSTED,
        }
        free_providers, free_available, all_exhausted = _free_pool_check(
            ["tencent_free", "bailian_free"],
            {
                "tencent_free": ["deepseek-v4-flash"],
                "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash"],
            },
            self._make_state_fn(status_map),
            "deepseek-v4-flash",
        )
        assert "tencent_free" in free_providers
        assert "bailian_free" in free_providers
        assert free_available == []
        assert all_exhausted is True

    def test_one_free_exhausted_another_available(self):
        """场景：tencent_free exhausted，bailian_free 可用 → 不降级"""
        status_map = {
            ("tencent_free", "deepseek-v4-flash"): QuotaStatus.EXHAUSTED,
            # bailian_free 可用（get_status 返回 None = UNKNOWN = 可用）
        }
        free_providers, free_available, all_exhausted = _free_pool_check(
            ["tencent_free", "bailian_free"],
            {
                "tencent_free": ["deepseek-v4-flash"],
                "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash"],
            },
            self._make_state_fn(status_map),
            "deepseek-v4-flash",
        )
        assert "tencent_free" in free_providers
        assert "bailian_free" in free_providers
        assert "bailian_free" in free_available
        assert all_exhausted is False  # 还有可用 free

    def test_no_free_hold_model(self):
        """场景：free_providers 里没有任何 provider 持有该模型 → free_providers 为空"""
        status_map = {}
        free_providers, free_available, all_exhausted = _free_pool_check(
            ["tencent_free", "bailian_free"],
            {
                "tencent_free": ["deepseek-v4-flash"],
                "bailian_free": ["qwen3.8-flash"],  # 不持有 deepseek-v4-flash
            },
            self._make_state_fn(status_map),
            "deepseek-v4-flash",
        )
        # 只有 tencent_free 持有，bailian_free 不持有
        assert free_providers == ["tencent_free"]
        assert all_exhausted is False


# ──────────────────────────────────────────
# 测试 decide() vs route() 一致性
# ──────────────────────────────────────────

class TestDecideVsRoute:
    """测试 decide() 和 route() 降级逻辑一致"""

    @pytest.mark.asyncio
    async def test_decide_all_free_exhausted_returns_paid(self, engine, config, state_manager):
        """所有 free exhausted → decide() 返回 selected_token=paid"""
        def get_status(p, m):
            if p in config.free_providers and m == "deepseek-v4-flash":
                return QuotaStatus.EXHAUSTED
            return None
        state_manager.get_status.side_effect = get_status

        req = make_request("deepseek-v4-flash", stream=True)
        decision = await engine.decide(req)

        assert decision.selected_token == "paid"
        assert decision.fallback_reason == "exhausted_skip"

    @pytest.mark.asyncio
    async def test_decide_one_free_exhausted_bailian_available(self, engine, config, state_manager):
        """tencent_free exhausted，bailian_free 可用 → decide() 不降级（bailian 持有该模型）"""
        def get_status(p, m):
            if p == "tencent_free" and m == "deepseek-v4-flash":
                return QuotaStatus.EXHAUSTED
            return None
        state_manager.get_status.side_effect = get_status

        req = make_request("deepseek-v4-flash", stream=True)
        decision = await engine.decide(req)

        # bailian_free 持有 deepseek-v4-flash → free_available 非空 → 不降级
        assert decision.selected_token == "free"
        assert decision.selected_provider == "bailian_free"

    @pytest.mark.asyncio
    async def test_route_all_free_exhausted_uses_paid(self, engine, config, state_manager, provider_registry):
        """所有 free exhausted → route() 直接用 paid token，不尝试 free"""
        def get_status(p, m):
            if p in config.free_providers and m == "deepseek-v4-flash":
                return QuotaStatus.EXHAUSTED
            return None
        state_manager.get_status.side_effect = get_status

        # 拿到 mock provider（provider_registry.get() 返回同一个 mock）
        prov = provider_registry.get("tencent_plan")
        prov.forward.return_value = {
            "status_code": 200,
            "latency_ms": 100,
            "choices": [{"message": {"content": "hi"}}],
        }

        req = make_request("deepseek-v4-flash", stream=False)
        resp, decision = await engine.route(req)

        assert decision.selected_token == "paid"
        assert decision.fallback_reason == "exhausted_skip"
        # provider_registry.get().forward 应该用 paid token 调用
        call_args = prov.forward.call_args_list
        assert call_args[0][0][0] == config.token_paid

    @pytest.mark.asyncio
    async def test_route_402_triggers_fallback(self, engine, config, state_manager, provider_registry):
        """free token 返回 402 → route() 换 paid 重试一次"""
        state_manager.get_status.return_value = None

        prov = provider_registry.get("tencent_plan")
        prov.forward.side_effect = [
            {"status_code": 402, "latency_ms": 50},
            {
                "status_code": 200,
                "latency_ms": 120,
                "choices": [{"message": {"content": "ok"}}],
            },
        ]

        req = make_request("deepseek-v4-flash", stream=False)
        resp, decision = await engine.route(req)

        assert decision.selected_token == "paid"
        assert decision.fallback_reason == "402_exhausted"
        assert decision.success is True
        assert prov.forward.call_count == 2

    @pytest.mark.asyncio
    async def test_decide_and_route_same_token_when_free_available(self, engine, config, state_manager, provider_registry):
        """decide() 和 route() 使用相同输入和状态 → selected_token 一致（free 可用时）"""
        state_manager.get_status.return_value = None

        req = make_request("deepseek-v4-flash", stream=True)
        decision_decide = await engine.decide(req)

        prov = provider_registry.get("tencent_plan")
        prov.forward.return_value = {
            "status_code": 200,
            "latency_ms": 100,
            "choices": [{"message": {"content": "ok"}}],
        }
        resp, decision_route = await engine.route(req)

        assert decision_decide.selected_token == decision_route.selected_token
        assert decision_decide.selected_provider == decision_route.selected_provider


# ──────────────────────────────────────────
# 测试 _pick_provider（不再过滤 exhausted）
# ──────────────────────────────────────────

class TestPickProvider:
    @pytest.mark.asyncio
    async def test_pick_provider_prefer_free_returns_free(self, engine):
        """prefer_free=True → 返回 free provider（不过滤 exhausted）"""
        provider = engine._pick_provider("qwen3.8-flash", prefer_free=True)
        assert provider in ["tencent_free", "bailian_free"]

    @pytest.mark.asyncio
    async def test_pick_provider_paid_returns_paid(self, engine):
        """prefer_free=False → 返回 paid provider"""
        provider = engine._pick_provider("deepseek-v4-flash", prefer_free=False)
        assert provider == "tencent_plan"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
