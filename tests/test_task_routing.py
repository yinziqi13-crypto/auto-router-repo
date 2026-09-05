"""
M2-3 四池路由单元测试
覆盖：
  1. mock 请求含 vision 关键词 → task_type=VISION，但 model_routes[vision]=null → fallback 到 text 模型 → 走 free 路由
  2. mock 请求纯文本 → task_type=TEXT → 走 deepseek-v4-flash → 走 free 路由
  3. routing_strategy 不是 cost_optimized 时不报错（占位分支正常返回）
  4. model_routes 里 task_type 对应模型为 null 时 fallback 逻辑
不连接实时上游，使用 unittest.mock + pytest
"""

import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

sys.modules["aiosqlite"] = MagicMock()
sys.modules["aiosqlite"].connect = MagicMock()
sys.modules["aiosqlite"].Connection = MagicMock()

from router.models import (
    TaskType, RouterConfig, QuotaStatus, QuotaState, ChatCompletionRequest, ChatMessage,
)
from router.decision import DecisionEngine, _free_pool_check

# ──────────────────────────────────
# Fixtures
# ──────────────────────────────────

@pytest.fixture
def config():
    cfg = MagicMock(spec=RouterConfig)
    cfg.free_providers = ["tencent_free", "bailian_free"]
    cfg.provider_models = {
        "tencent_free": ["deepseek-v4-flash", "qwen3.8-flash"],
        "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash"],
        "bailian_lite": ["qwen3.8-flash", "qwen3.7-max"],
        "tencent_plan": ["deepseek-v4-flash", "deepseek-v4-pro"],
    }
    cfg.provider_priority = ["tencent_free", "bailian_free", "bailian_lite", "tencent_plan"]
    cfg.model_mapping = {
        "tencent_plan": {"deepseek-v4-flash": "deepseek-v4-flash-202605"}
    }
    cfg.token_free = "sk-free-test"
    cfg.token_paid = "sk-paid-test"
    cfg.three_pool_keywords = {}
    # M2-3 新字段
    cfg.routing_strategy = "cost_optimized"
    cfg.model_routes = {
        "text": "deepseek-v4-flash",
        "vision": None,
        "audio": None,
        "image": None,
    }
    return cfg


@pytest.fixture
def state_manager():
    sm = MagicMock()
    sm.get_status = MagicMock(return_value=None)
    sm.get = MagicMock()
    qstate = QuotaState("dummy", "dummy", status=QuotaStatus.AVAILABLE)
    sm.get.return_value = qstate
    sm.record_402 = AsyncMock()
    sm.reset = AsyncMock()
    return sm


@pytest.fixture
def adapter():
    adapt = MagicMock()
    adapt.forward = AsyncMock()
    return adapt


@pytest.fixture
def engine(config, state_manager, adapter):
    eng = DecisionEngine(
        state_manager=state_manager,
        adapter=adapter,
        config=config,
    )
    eng._db = None
    return eng


def make_request(model: str, content: str = "hi", stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
        stream=stream,
    )


# ──────────────────────────────────
# 测试一：vision 关键词 → task_type=VISION → model_routes[vision]=null → fallback text
# ──────────────────────────────────

class TestVisionFallback:
    """测试 vision 类型 fallback 到 text 模型"""

    @pytest.mark.asyncio
    async def test_vision_keyword_fallback_to_text(self, engine, config, state_manager):
        """请求含'看图'关键词 → task_type=VISION，model_routes[vision]=None → fallback deepseek-v4-flash"""
        req = make_request("deepseek-v4-flash", content="帮我看图里的代码")
        decision = await engine.decide(req)

        assert decision.task_type == TaskType.VISION
        # model_routes[vision] is None → fallback to text → deepseek-v4-flash
        assert decision.logical_model == "deepseek-v4-flash"
        # free 池可用 → token=free
        assert decision.selected_token == "free"

    @pytest.mark.asyncio
    async def test_vision_keyword_with_image_url(self, engine, config, state_manager):
        """请求含图片 URL → task_type=VISION → fallback text 模型"""
        req = make_request("deepseek-v4-flash", content="分析这张图片里的内容 http://example.com/pic.jpg")
        decision = await engine.decide(req)

        assert decision.task_type == TaskType.VISION
        assert decision.logical_model == "deepseek-v4-flash"


# ──────────────────────────────────
# 测试二：纯文本 → task_type=TEXT → deepseek-v4-flash → free 路由
# ──────────────────────────────────

class TestTextRouting:
    """测试 text 类型正常路由"""

    @pytest.mark.asyncio
    async def test_text_request_uses_deepseek(self, engine, config, state_manager):
        """纯文本请求 → task_type=TEXT → logical_model=deepseek-v4-flash → free 路由"""
        req = make_request("deepseek-v4-flash", content="写一段 Python 代码")
        decision = await engine.decide(req)

        assert decision.task_type == TaskType.TEXT
        assert decision.logical_model == "deepseek-v4-flash"
        assert decision.selected_token == "free"

    @pytest.mark.asyncio
    async def test_unknown_keyword_fallback_to_text(self, engine, config, state_manager):
        """不命中任何关键词 → task_type=TEXT（默认）"""
        req = make_request("deepseek-v4-flash", content="abc 123")
        decision = await engine.decide(req)

        assert decision.task_type == TaskType.TEXT


# ──────────────────────────────────
# 测试三：routing_strategy 非 cost_optimized 不报错
# ──────────────────────────────────

class TestRoutingStrategy:
    """测试路由策略占位分支"""

    @pytest.mark.asyncio
    async def test_balanced_strategy_no_error(self, engine, config, state_manager):
        """routing_strategy=balanced 时（未实现）不报错，按 cost_optimized 行为兜底"""
        config.routing_strategy = "balanced"
        req = make_request("deepseek-v4-flash", content="hi")
        decision = await engine.decide(req)  # 不应抛异常
        assert decision.task_type == TaskType.TEXT
        assert decision.logical_model == "deepseek-v4-flash"

    @pytest.mark.asyncio
    async def test_quality_strategy_no_error(self, engine, config, state_manager):
        """routing_strategy=quality_optimized 时不报错"""
        config.routing_strategy = "quality_optimized"
        req = make_request("deepseek-v4-flash", content="hi")
        decision = await engine.decide(req)
        assert decision.task_type == TaskType.TEXT


# ──────────────────────────────────
# 测试四：model_routes 缺失/为空时的 fallback
# ──────────────────────────────────

class TestModelRoutesFallback:
    """测试 model_routes 各种 fallback 场景"""

    @pytest.mark.asyncio
    async def test_model_routes_missing_text_fallback_to_original(self, engine, config, state_manager):
        """model_routes 没有 text 键 → fallback 到 original_model"""
        del config.model_routes["text"]
        req = make_request("qwen3.8-flash", content="hi")
        decision = await engine.decide(req)
        # 没有 text 映射 → 保留 original_model
        assert decision.logical_model == "qwen3.8-flash"

    @pytest.mark.asyncio
    async def test_model_routes_all_none_fallback(self, engine, config, state_manager):
        """model_routes 所有值都是 None → 全部 fallback 到 text=None → 保留 original_model"""
        config.model_routes = {
            "text": None,
            "vision": None,
            "audio": None,
            "image": None,
        }
        req = make_request("deepseek-v4-flash", content="hi")
        decision = await engine.decide(req)
        assert decision.logical_model == "deepseek-v4-flash"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
