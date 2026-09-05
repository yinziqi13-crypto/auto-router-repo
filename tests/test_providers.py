"""
M2-6 多供应商框架单测
验证 ProviderRegistry 能正确注册和路由 Provider 实例
"""
import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

# 提前 mock aiosqlite（decision.py 顶层 import 依赖）
sys.modules["aiosqlite"] = MagicMock()

from router.models import ChatCompletionRequest, ChatMessage
from router.providers import Provider, NewAPIProvider, ProviderRegistry


def make_request(model: str = "deepseek-v4-flash") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="hi")],
    )


# ──────────────────────────────────────────
# 测试 Provider ABC（抽象类不可直接实例化）
# ──────────────────────────────────────────

class TestProviderABC:
    def test_abstract_methods(self):
        """Provider ABC 有 abstractmethod forward/forward_stream/health_check"""
        for name in ["forward", "forward_stream", "health_check"]:
            assert name in Provider.__abstractmethods__

    def test_cannot_instantiate_abc(self):
        """Provider 是抽象类，不能直接实例化"""
        with pytest.raises(TypeError):
            Provider()  # type: ignore


# ──────────────────────────────────────────
# 测试 ProviderRegistry（核心：路由到对应 Provider）
# ──────────────────────────────────────────

class TestProviderRegistry:
    def test_init_from_config_registers_new_api(self):
        """init_from_config 注册 new_api → ProviderRegistry 可 get 到"""
        reg = ProviderRegistry()
        reg.init_from_config(
            {
                "new_api": {
                    "type": "new_api",
                    "base_url": "http://127.0.0.1:3000",
                    "is_default": True,
                }
            },
            default_base_url="http://127.0.0.1:3000",
        )
        p = reg.get("new_api")
        assert p is not None
        assert isinstance(p, NewAPIProvider)
        assert p.base_url == "http://127.0.0.1:3000"

    def test_unknown_type_warns_but_no_crash(self, caplog):
        """未知 provider type → warning，不崩溃"""
        import logging
        reg = ProviderRegistry()
        with caplog.at_level(logging.WARNING):
            reg.init_from_config(
                {
                    "deepseek_direct": {
                        "type": "openai_direct",  # 未实现，M3 才接
                        "base_url": "https://api.deepseek.com",
                        "api_key": "sk-xxx",
                    }
                },
                default_base_url="http://127.0.0.1:3000",
            )
        assert reg.get("deepseek_direct") is None
        assert any("unknown provider type" in r.message for r in caplog.records)

    def test_get_default_returns_is_default(self):
        """is_default=true 的 provider 由 get_default 返回"""
        reg = ProviderRegistry()
        reg.init_from_config(
            {
                "new_api": {
                    "type": "new_api",
                    "base_url": "http://127.0.0.1:3000",
                    "is_default": True,
                }
            },
            default_base_url="http://127.0.0.1:3000",
        )
        assert reg.get_default() is reg.get("new_api")

    def test_get_default_fallback_first(self):
        """无 is_default 时返回第一个注册的"""
        reg = ProviderRegistry()
        reg._providers["p1"] = MagicMock(spec=NewAPIProvider)
        reg._provider_config["p1"] = {"type": "new_api"}
        assert reg.get_default() is reg._providers["p1"]

    @pytest.mark.asyncio
    async def test_close_all(self):
        """close_all 关闭所有 provider"""
        reg = ProviderRegistry()
        p1 = MagicMock(spec=NewAPIProvider)
        p1.close = AsyncMock()
        p2 = MagicMock(spec=NewAPIProvider)
        p2.close = AsyncMock()
        reg._providers["p1"] = p1
        reg._providers["p2"] = p2

        await reg.close_all()
        p1.close.assert_awaited_once()
        p2.close.assert_awaited_once()


# ──────────────────────────────────────────
# 测试 NewAPIProvider 的 forward 转发行为
# ──────────────────────────────────────────

class TestNewAPIProviderForward:
    @pytest.fixture
    def provider(self):
        """用 mock httpx client 的 NewAPIProvider"""
        p = NewAPIProvider.__new__(NewAPIProvider)
        p.base_url = "http://127.0.0.1:3000"
        p._client = MagicMock()
        return p

    @pytest.mark.asyncio
    async def test_forward_success(self, provider):
        """forward 200 → body 解析 + latency"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "resp1", "choices": []}
        mock_resp.headers = {"content-type": "application/json"}
        provider._client.post = AsyncMock(return_value=mock_resp)

        result = await provider.forward(
            "sk-test", make_request(), model_mapping={}
        )
        assert result["status_code"] == 200
        assert result["body"]["id"] == "resp1"
        assert result["error"] is None
        assert "latency_ms" in result

    @pytest.mark.asyncio
    async def test_forward_402(self, provider):
        """forward 402 → error 捕获（配额耗尽）"""
        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.text = "quota exhausted"
        mock_resp.headers = {}
        provider._client.post = AsyncMock(return_value=mock_resp)

        result = await provider.forward("sk-test", make_request())
        assert result["status_code"] == 402
        assert "quota" in result["error"]

    @pytest.mark.asyncio
    async def test_forward_connection_error(self, provider):
        """连接失败 → status_code=0 + error"""
        from httpx import ConnectError
        provider._client.post = AsyncMock(side_effect=ConnectError("boom"))
        result = await provider.forward("sk-test", make_request())
        assert result["status_code"] == 0
        assert "connection" in result["error"]

    @pytest.mark.asyncio
    async def test_model_mapping_applied(self, provider):
        """model_mapping 命中时替换模型名"""
        provider._client.post = AsyncMock()
        # 手动构造返回（避免 run_until_complete）
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "r1"}
        mock_resp.headers = {}
        provider._client.post.return_value = mock_resp

        await provider.forward(
            "sk-test",
            make_request("deepseek-v4-flash"),
            model_mapping={"deepseek-v4-flash": "deepseek-v4-flash-202605"},
        )
        call_kwargs = provider._client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["model"] == "deepseek-v4-flash-202605"
