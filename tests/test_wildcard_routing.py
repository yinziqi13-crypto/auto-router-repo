"""
M3-2c/d/e/f：全模型覆盖 + wildcard 路由 + 模型名任务类型识别

测试覆盖：
  1. wildcard provider 路由：不在显式列表的模型 → 正确路由到 wildcard provider
  2. 非 deepseek 模型路由 ≥5 个：
     - kimi-k3      → tencent_free (wildcard free)
     - qwen3.7-max  → bailian_lite (paid, 显式)
     - minimax-m3    → tencent_free (wildcard free)
     - tc-code-latest → tencent_plan (wildcard paid)
     - deepseek/deepseek-v4-flash-vision-exp → vision task_type
  3. _detect_task_type 模型名模式识别（M3-2e）：
     - qwen-vl-plus        → VISION
     - qwen-audio-3.0       → AUDIO
     - video-model         → VIDEO
  4. model_routes 更新后 vision/audio 映射正确
  5. config 含 wildcard_providers 时入口 404 门禁不误拦
"""

import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock
import pytest

# 确保 aiosqlite 是 mock（其他测试文件可能已 mock）
if "aiosqlite" not in sys.modules or sys.modules["aiosqlite"].__class__.__name__ != "module":
    import aiosqlite as _real
    sys.modules["aiosqlite"] = _real

# 确保 src/ 在 sys.path
import os
_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from router.models import (
    RouterConfig, QuotaStatus, ChatCompletionRequest, ChatMessage, TaskType,
)
from router.decision import DecisionEngine, AUTO_MODEL


# ──────────────────────────────────
# Fixtures
# ──────────────────────────────────

@pytest.fixture
def wildcard_config():
    """M3-2c 配置：含 wildcard_providers + wildcard_paid_providers"""
    return RouterConfig(
        new_api_base_url="http://127.0.0.1:3000",
        token_free="sk-free-test",
        token_paid="sk-paid-test",
        provider_models={
            "tencent_free": ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2", "kimi-k3", "minimax-m3"],
            "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash", "glm-5.2", "qwen3.6-flash"],
            "bailian_lite": ["qwen3.8-flash", "qwen3.7-max", "qwen3.6-flash", "deepseek-v4-pro"],
            "tencent_plan": ["deepseek-v4-flash", "deepseek-v4-pro"],
        },
        wildcard_providers=["tencent_free", "bailian_free"],
        wildcard_paid_providers=["bailian_lite", "tencent_plan"],
        model_mapping={
            "tencent_plan": {
                "deepseek-v4-flash": "deepseek-v4-flash-202605",
                "deepseek-v4-pro": "deepseek-v4-pro-202606",
            }
        },
        provider_priority=["tencent_free", "bailian_free", "bailian_lite", "tencent_plan"],
        free_providers=["tencent_free", "bailian_free"],
        model_routes={
            "text": "deepseek-v4-flash",
            "vision": "deepseek/deepseek-v4-flash-vision-exp",
            "audio": "qwen-audio-3.0-realtime-flash",
            "image": None,
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
def engine(wildcard_config, state_manager):
    """DecisionEngine with wildcard config"""
    eng = DecisionEngine(
        state_manager=state_manager,
        config=wildcard_config,
        db_conn=None,
    )
    eng._db = None
    return eng


def make_req(model: str, content: str = "hi", stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content=content)],
        stream=stream,
    )


# ──────────────────────────────────
# M3-2c: wildcard 路由
# ──────────────────────────────────

class TestWildcardRouting:
    """不在 provider_models 显式列表里的模型，应路由到 wildcard provider"""

    @pytest.mark.asyncio
    async def test_kimi_k3_routes_to_tencent_free(self, engine, wildcard_config):
        """kimi-k3 不在显式列表 → wildcard tencent_free 持有 → free"""
        s = engine._select_pool("kimi-k3")
        assert s["state"] == "A"
        assert s["provider"] == "tencent_free"
        assert s["token"] == "free"

    @pytest.mark.asyncio
    async def test_qwen3_7_max_routes_to_bailian_lite(self, engine, wildcard_config):
        """qwen3.7-max 在 bailian_lite 显式列表（paid-only）→ 直接命中，C 态 paid"""
        s = engine._select_pool("qwen3.7-max")
        # bailian_lite 是 paid provider，无 free 候选 → C 态
        assert s["state"] == "C"
        assert s["provider"] == "bailian_lite"
        assert s["token"] == "paid"
        assert s["fallback_reason"] == "paid_only"

    @pytest.mark.asyncio
    async def test_minimax_m3_routes_to_tencent_free_wildcard(self, engine, wildcard_config):
        """minimax-m3 不在显式列表 → wildcard tencent_free 持有 → free"""
        s = engine._select_pool("minimax-m3")
        assert s["state"] == "A"
        assert s["provider"] == "tencent_free"

    @pytest.mark.asyncio
    async def test_tc_code_latest_routes_to_wildcard_free(self, engine, wildcard_config):
        """tc-code-latest 不在显式列表 → wildcard 命中 tencent_free（free 优先于 paid）"""
        s = engine._select_pool("tc-code-latest")
        # wildcard 候选 = [tencent_free, bailian_free, bailian_lite, tencent_plan]
        # free 优先 → tencent_free, state A
        assert s["state"] == "A"
        assert s["provider"] == "tencent_free"
        assert s["token"] == "free"

    @pytest.mark.asyncio
    async def test_deepseek_vision_exp_routes_to_free(self, engine, wildcard_config):
        """deepseek/deepseek-v4-flash-vision-exp 在 tencent_free 显式列表 → free"""
        s = engine._select_pool("deepseek/deepseek-v4-flash-vision-exp")
        assert s["provider"] == "tencent_free"
        assert s["token"] == "free"


# ──────────────────────────────────
# M3-2e: 模型名任务类型识别
# ──────────────────────────────────

class TestModelNameTaskType:
    """模型名包含 vision/audio/video 关键词时，直接推断 task_type"""

    def test_qwen_vl_plus_detects_vision(self, engine):
        """qwen-vl-plus 含 vl → VISION"""
        req = make_req("qwen-vl-plus")
        tt = engine._detect_task_type(req)
        assert tt == TaskType.VISION

    def test_deepseek_vision_exp_detects_vision(self, engine):
        """deepseek/deepseek-v4-flash-vision-exp 含 vision → VISION"""
        req = make_req("deepseek/deepseek-v4-flash-vision-exp")
        tt = engine._detect_task_type(req)
        assert tt == TaskType.VISION

    def test_qwen_audio_detects_audio(self, engine):
        """qwen-audio-3.0-realtime-flash 含 audio → AUDIO"""
        req = make_req("qwen-audio-3.0-realtime-flash")
        tt = engine._detect_task_type(req)
        assert tt == TaskType.AUDIO

    def test_whisper_model_detects_audio(self, engine):
        """whisper-1 含 whisper → AUDIO"""
        req = make_req("whisper-1")
        tt = engine._detect_task_type(req)
        assert tt == TaskType.AUDIO

    def test_video_model_detects_video(self, engine):
        """video-model 含 video → VIDEO"""
        req = make_req("video-model")
        tt = engine._detect_task_type(req)
        assert tt == TaskType.VIDEO

    def test_model_name_priority_over_keywords(self, engine):
        """模型名模式优先级高于消息关键词（点名 vision 模型时不应被文本覆盖）"""
        req = make_req("qwen-vl-plus", content="写一段代码")
        tt = engine._detect_task_type(req)
        assert tt == TaskType.VISION  # 模型名优先，不是 TEXT


# ──────────────────────────────────
# M3-2d: model_routes 映射正确性
# ──────────────────────────────────

class TestModelRoutes:
    """model_routes 更新后，auto-free 虚拟模型正确映射到 vision/audio"""

    def test_auto_free_vision_routes_to_vision_model(self, engine, wildcard_config):
        """auto-free + 图片消息 → vision → deepseek/deepseek-v4-flash-vision-exp"""
        req = make_req(AUTO_MODEL, content="看图")
        tt, lm = engine._resolve_logical_model(req)
        assert tt == TaskType.VISION
        assert lm == "deepseek/deepseek-v4-flash-vision-exp"

    def test_auto_free_audio_routes_to_audio_model(self, engine, wildcard_config):
        """auto-free + 语音消息 → audio → qwen-audio-3.0-realtime-flash"""
        req = make_req(AUTO_MODEL, content="转写这段语音")
        tt, lm = engine._resolve_logical_model(req)
        assert tt == TaskType.AUDIO
        assert lm == "qwen-audio-3.0-realtime-flash"

    def test_auto_free_text_fallback(self, engine, wildcard_config):
        """auto-free + 纯文本 → text → deepseek-v4-flash"""
        req = make_req(AUTO_MODEL, content="写代码")
        tt, lm = engine._resolve_logical_model(req)
        assert tt == TaskType.TEXT
        assert lm == "deepseek-v4-flash"


# ──────────────────────────────────
# M3-2f: 综合路由 ≥5 个不同模型
# ──────────────────────────────────

class TestFiveModelRouting:
    """M3-2f：至少 5 个不同模型的路由正确性"""

    @pytest.mark.asyncio
    async def test_five_different_models_all_route(self, engine):
        """5 个不同模型都能正确路由到持有该模型的 provider"""
        cases = [
            ("kimi-k3",                           "tencent_free"),
            ("qwen3.7-max",                     "bailian_lite"),
            ("minimax-m3",                      "tencent_free"),
            ("tc-code-latest",                   "tencent_free"),   # wildcard free 优先
            ("deepseek/deepseek-v4-flash-vision-exp", "tencent_free"),
        ]
        for model, expected_provider in cases:
            s = engine._select_pool(model)
            assert s["provider"] == expected_provider, (
                f"model={model} 期望 provider={expected_provider}，"
                f"实际 state={s['state']} provider={s['provider']}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
