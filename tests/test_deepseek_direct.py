"""
M3-3: DeepSeek 直连 Provider 单元测试

验证点：
  1. OpenAIDirectProvider 正确注册到 ProviderRegistry
  2. transport 映射：deepseek_direct provider → deepseek_direct transport
  3. 路由选择：deepseek-v4-pro 在 free 池可用时走 free，全 exhausted 走 deepseek_direct
  4. DeepSeek direct 作为 paid 降级路径
  5. health_check 正常工作
"""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保 src/ 在 sys.path
_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from router.models import (
    RouterConfig, QuotaStatus, ChatCompletionRequest, ChatMessage,
)
from router.providers import ProviderRegistry, OpenAIDirectProvider, NewAPIProvider
from router.decision import DecisionEngine


# ──────────────────────────────────
# Fixtures
# ──────────────────────────────────

@pytest.fixture
def direct_config():
    """Config with deepseek_direct provider"""
    return RouterConfig(
        new_api_base_url="http://127.0.0.1:3000",
        token_free="sk-free-test",
        token_paid="sk-paid-test",
        provider_models={
            "tencent_free": ["deepseek-v4-flash", "deepseek-v4-pro"],
            "bailian_free": ["deepseek-v4-flash"],
            "bailian_lite": ["deepseek-v4-pro"],
            "tencent_plan": ["deepseek-v4-flash", "deepseek-v4-pro"],
            "deepseek_direct": ["deepseek-v4-flash", "deepseek-v4-pro"],
        },
        wildcard_providers=["tencent_free", "bailian_free"],
        wildcard_paid_providers=["bailian_lite", "tencent_plan"],
        model_mapping={},
        free_providers=["tencent_free", "bailian_free"],
        provider_priority=[
            "tencent_free", "bailian_free",
            "bailian_lite", "tencent_plan",
            "deepseek_direct",
        ],
        providers={
            "new_api": {
                "type": "new_api",
                "base_url": "http://127.0.0.1:3000",
                "is_default": True,
            },
            "deepseek_direct": {
                "type": "openai_direct",
                "base_url": "https://api.deepseek.com",
                "api_key": "sk-test-deepseek-key",
                "is_default": False,
            },
        },
    )


@pytest.fixture
def state_manager():
    sm = MagicMock()
    sm.get_status = MagicMock(return_value=None)
    sm.get = MagicMock()
    qstate = MagicMock()
    qstate.status = QuotaStatus.AVAILABLE
    sm.get.return_value = qstate
    sm.record_402 = AsyncMock()
    sm.reset = AsyncMock()
    return sm


@pytest.fixture
def engine(direct_config, state_manager):
    eng = DecisionEngine(
        state_manager=state_manager,
        config=direct_config,
        db_conn=None,
    )
    eng._db = None
    return eng


@pytest.fixture
def exhausted_engine(direct_config):
    """Engine where all free + paid (except deepseek_direct) are exhausted"""
    sm = MagicMock()
    sm.get_status = MagicMock(return_value=QuotaStatus.EXHAUSTED)
    sm.get = MagicMock()
    qstate = MagicMock()
    qstate.status = QuotaStatus.EXHAUSTED
    sm.get.return_value = qstate
    sm.record_402 = AsyncMock()
    sm.reset = AsyncMock()

    eng = DecisionEngine(
        state_manager=sm,
        config=direct_config,
        db_conn=None,
    )
    eng._db = None
    return eng


def make_req(model: str, content: str = "hi") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
    )


# ──────────────────────────────────
# Test 1: ProviderRegistry registration
# ──────────────────────────────────

class TestProviderRegistration:
    def test_openai_direct_registered(self, direct_config):
        """ProviderRegistry 正确注册 OpenAIDirectProvider"""
        reg = ProviderRegistry()
        reg.init_from_config(direct_config.providers, default_base_url="http://127.0.0.1:3000")
        assert "new_api" in reg.all()
        assert "deepseek_direct" in reg.all()
        assert isinstance(reg.get("new_api"), NewAPIProvider)
        assert isinstance(reg.get("deepseek_direct"), OpenAIDirectProvider)

    def test_missing_api_key_skipped(self):
        """api_key 为空时跳过注册"""
        reg = ProviderRegistry()
        reg.init_from_config(
            {"bad_provider": {"type": "openai_direct", "base_url": "https://api.test.com", "api_key": ""}},
            default_base_url="http://127.0.0.1:3000",
        )
        assert "bad_provider" not in reg.all()

    def test_deepseek_direct_type_alias(self):
        """type=deepseek_direct 也能注册为 OpenAIDirectProvider"""
        reg = ProviderRegistry()
        reg.init_from_config(
            {"ds": {"type": "deepseek_direct", "base_url": "https://api.deepseek.com", "api_key": "sk-test"}},
            default_base_url="http://127.0.0.1:3000",
        )
        assert isinstance(reg.get("ds"), OpenAIDirectProvider)


# ──────────────────────────────────
# Test 2: Transport mapping
# ──────────────────────────────────

class TestTransportMapping:
    def test_deepseek_direct_uses_own_transport(self, engine):
        """deepseek_direct provider 映射到 deepseek_direct transport"""
        assert engine._provider_transport.get("deepseek_direct") == "deepseek_direct"

    def test_new_api_providers_use_new_api_transport(self, engine):
        """tencent_free/bailian_free 等映射到 new_api transport"""
        assert engine._provider_transport.get("tencent_free") == "new_api"
        assert engine._provider_transport.get("bailian_free") == "new_api"
        assert engine._provider_transport.get("tencent_plan") == "new_api"


# ──────────────────────────────────
# Test 3: Routing with deepseek_direct available
# ──────────────────────────────────

class TestRoutingWithDirect:
    def test_free_available_routes_to_free(self, engine):
        """free 池可用时走 free，不走 deepseek_direct"""
        s = engine._select_pool("deepseek-v4-pro")
        assert s["state"] == "A"
        assert s["provider"] == "tencent_free"
        assert s["token"] == "free"

    def test_all_free_exhausted_routes_to_paid_not_direct(self, exhausted_engine):
        """free 全 exhausted → 走 paid (bailian_lite/tencent_plan)，不走 deepseek_direct"""
        s = exhausted_engine._select_pool("deepseek-v4-pro")
        assert s["state"] == "B"
        # bailian_lite 在 priority 中排第3（index 2），tencent_plan 第4（index 3）
        assert s["provider"] in ("bailian_lite", "tencent_plan")
        assert s["token"] == "paid"


# ──────────────────────────────────
# Test 4: deepseek_direct as last resort paid
# ──────────────────────────────────

class TestDeepSeekAsLastResort:
    def test_model_only_in_deepseek_direct_routes_there(self, direct_config):
        """模型只在 deepseek_direct → C 态，直接路由到 deepseek_direct"""
        # 临时给 deepseek_direct 加一个独占模型
        direct_config.provider_models["deepseek_direct"].append("deepseek-v4-exclusive")
        sm = MagicMock()
        sm.get_status = MagicMock(return_value=None)
        sm.get = MagicMock()
        sm.record_402 = AsyncMock()
        sm.reset = AsyncMock()

        eng = DecisionEngine(
            state_manager=sm,
            config=direct_config,
            db_conn=None,
        )
        eng._db = None

        s = eng._select_pool("deepseek-v4-exclusive")
        assert s["state"] == "C"  # 无 free 候选 → C 态 paid
        assert s["provider"] == "deepseek_direct"
        assert s["token"] == "paid"
        assert s["fallback_reason"] == "paid_only"

    def test_deepseek_direct_in_paid_candidates(self, engine):
        """deepseek-v4-pro 的 paid 候选包含 deepseek_direct"""
        s = engine._select_pool("deepseek-v4-pro")
        assert "deepseek_direct" in s["paid_candidates"]


# ──────────────────────────────────
# Test 5: OpenAIDirectProvider health_check
# ──────────────────────────────────

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self):
        """health_check 返回 True 当 /models 返回 200"""
        provider = OpenAIDirectProvider(
            base_url="https://api.test.com",
            api_key="sk-test",
        )
        # Mock the httpx client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        provider._client = MagicMock()
        provider._client.get = AsyncMock(return_value=mock_resp)

        result = await provider.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        """health_check 返回 False 当连接失败"""
        provider = OpenAIDirectProvider(
            base_url="https://api.test.com",
            api_key="sk-test",
        )
        provider._client = MagicMock()
        provider._client.get = AsyncMock(side_effect=Exception("connection failed"))

        result = await provider.health_check()
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
