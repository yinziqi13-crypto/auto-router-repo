"""
M2.5 P0 修复回归测试（WorkBuddy W1~W6 + 规格 6）

设计原则（针对 M2 审查报告里「任意 key 都返回同一个 mock」的假阳性批评）：
  - 使用**真实 ProviderRegistry**，只把最底层的 transport 换成可控假实现
  - 五态用可控的 FakeState，而不是把 get_status 配成永远返回同一个值
  - 402 重试断言「调用次数」与「每次用的 token」，而不只是断言最终状态
"""
import pytest

from router.models import (
    RouterConfig, ChatCompletionRequest, ChatMessage, QuotaStatus, TaskType,
)
from router.decision import DecisionEngine, AUTO_MODEL
from router.providers import ProviderRegistry, NewAPIProvider


# ─────────────────────────────────────────
# 测试夹具
# ─────────────────────────────────────────

class FakeState:
    """可控额度状态：exhausted 集合内的 (provider, model) 返回 EXHAUSTED"""

    def __init__(self, exhausted=frozenset()):
        self.exhausted = exhausted
        self.recorded_402 = []

    def get_status(self, provider, model):
        return (
            QuotaStatus.EXHAUSTED
            if (provider, model) in self.exhausted
            else QuotaStatus.AVAILABLE
        )

    async def record_402(self, provider, model):
        self.recorded_402.append((provider, model))


class FakeTransport:
    """可控 transport：按顺序返回预设响应，并记录每次收到的 token。

    只替换最底层通道，保留真实 ProviderRegistry，
    这样 business provider → transport 的映射逻辑仍然被真实执行。
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []          # 每次 forward 收到的 token
        self.requests = []

    async def forward(self, token, request, model_mapping=None):
        self.calls.append(token)
        self.requests.append(request)
        if self.responses:
            status, body = self.responses.pop(0)
        else:
            status, body = 200, {"ok": True}
        return {
            "status_code": status,
            "body": body,
            "error": None if status == 200 else "upstream error",
            "headers": {},
            "latency_ms": 1,
        }

    async def forward_stream(self, token, request, model_mapping=None):
        raise NotImplementedError

    async def open_stream(self, token, request, model_mapping=None):
        raise NotImplementedError

    async def health_check(self):
        return True


def make_cfg():
    return RouterConfig(
        new_api_base_url="http://127.0.0.1:3000",
        token_free="sk-free-test",
        token_paid="sk-paid-test",
        provider_models={
            "tencent_free": ["deepseek-v4-flash", "free-only-model"],
            "bailian_free": ["deepseek-v4-flash", "qwen3.8-flash"],
            "tencent_plan": ["deepseek-v4-flash", "deepseek-v4-pro"],
        },
        model_mapping={
            "tencent_plan": {"deepseek-v4-flash": "deepseek-v4-flash-202605"}
        },
        provider_priority=["tencent_free", "bailian_free", "tencent_plan"],
        free_providers=["tencent_free", "bailian_free"],
        model_routes={"text": "deepseek-v4-flash", "vision": None},
    )


def req(model, **kw):
    return ChatCompletionRequest(
        model=model, messages=[ChatMessage(role="user", content="你好")], **kw
    )


def build_engine(state, transport):
    """真实 ProviderRegistry + 假 transport"""
    reg = ProviderRegistry()
    reg._providers["new_api"] = transport
    reg._provider_config["new_api"] = {
        "type": "new_api", "base_url": "http://127.0.0.1:3000", "is_default": True,
    }
    return DecisionEngine(state_manager=state, config=make_cfg(), provider_registry=reg)


# ─────────────────────────────────────────
# W4：五态候选池选择
# ─────────────────────────────────────────

class TestFiveStatePool:
    def test_state_a_free_available(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        s = eng._select_pool("deepseek-v4-flash")
        assert s["state"] == "A"
        assert s["token"] == "free"
        assert s["provider"] == "tencent_free"
        assert s["fallback_reason"] is None

    def test_state_b_all_free_exhausted_with_paid(self):
        state = FakeState(exhausted={
            ("tencent_free", "deepseek-v4-flash"),
            ("bailian_free", "deepseek-v4-flash"),
        })
        eng = build_engine(state, FakeTransport([]))
        s = eng._select_pool("deepseek-v4-flash")
        assert s["state"] == "B"
        assert s["token"] == "paid"
        assert s["fallback_reason"] == "exhausted_skip"

    def test_state_c_paid_only_model_uses_paid_token(self):
        """回归：旧逻辑在此处 token 仍为 free（P0-4）"""
        eng = build_engine(FakeState(), FakeTransport([]))
        s = eng._select_pool("deepseek-v4-pro")
        assert s["state"] == "C"
        assert s["token"] == "paid"
        assert s["provider"] == "tencent_plan"
        assert s["fallback_reason"] == "paid_only"

    def test_state_d_free_exhausted_no_paid_503(self):
        state = FakeState(exhausted={("tencent_free", "free-only-model")})
        eng = build_engine(state, FakeTransport([]))
        s = eng._select_pool("free-only-model")
        assert s["state"] == "D"
        assert s["error_code"] == 503

    def test_state_e_unknown_model_404(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        s = eng._select_pool("no-such-model")
        assert s["state"] == "E"
        assert s["error_code"] == 404


# ─────────────────────────────────────────
# W3：auto-free 路由（点名模型不被改写）
# ─────────────────────────────────────────

class TestAutoFreeRouting:
    def test_named_model_preserved(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        _, lm = eng._resolve_logical_model(req("qwen3.8-flash"))
        assert lm == "qwen3.8-flash"

    def test_named_paid_model_preserved(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        _, lm = eng._resolve_logical_model(req("deepseek-v4-pro"))
        assert lm == "deepseek-v4-pro"

    def test_auto_free_triggers_routing(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        tt, lm = eng._resolve_logical_model(req(AUTO_MODEL))
        assert lm == "deepseek-v4-flash"
        assert tt == TaskType.TEXT

    async def test_decide_preserves_named_model(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        d = await eng.decide(req("qwen3.8-flash"))
        assert d.logical_model == "qwen3.8-flash"
        assert d.original_model == "qwen3.8-flash"


# ─────────────────────────────────────────
# W1：402 重试的 provider 解析（P0-2 回归）
# ─────────────────────────────────────────

class Test402Retry:
    async def test_402_switches_to_paid_and_calls_twice(self):
        """回归：修复前 registry.get('tencent_plan') 返回 None → HTTP 500"""
        transport = FakeTransport([(402, None), (200, {"ok": True})])
        eng = build_engine(FakeState(), transport)
        resp, decision = await eng.route(req("deepseek-v4-flash"))
        assert len(transport.calls) == 2, "free 402 后应重试一次 paid"
        assert transport.calls[0] == "sk-free-test"
        assert transport.calls[1] == "sk-paid-test"
        assert resp["status_code"] == 200
        assert decision.selected_token == "paid"

    async def test_402_records_quota_state(self):
        state = FakeState()
        transport = FakeTransport([(402, None), (200, {"ok": True})])
        eng = build_engine(state, transport)
        await eng.route(req("deepseek-v4-flash"))
        assert state.recorded_402, "402 必须记录，否则每次都要探测"

    async def test_paid_only_model_uses_paid_token_on_first_call(self):
        """回归：修复前此处首次就用 free token（P0-4）"""
        transport = FakeTransport([(200, {"ok": True})])
        eng = build_engine(FakeState(), transport)
        await eng.route(req("deepseek-v4-pro"))
        assert len(transport.calls) == 1
        assert transport.calls[0] == "sk-paid-test"

    async def test_unknown_model_returns_404_without_calling_upstream(self):
        transport = FakeTransport([])
        eng = build_engine(FakeState(), transport)
        resp, decision = await eng.route(req("no-such-model"))
        assert resp["status_code"] == 404
        assert transport.calls == [], "未知模型不应打到上游"


# ─────────────────────────────────────────
# W2：OpenAI 协议兼容（P0-7）
# ─────────────────────────────────────────

class TestProtocolCompat:
    def test_multimodal_content_array(self):
        m = ChatMessage(role="user", content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
        ])
        assert isinstance(m.content, list) and len(m.content) == 2

    def test_null_content_normalized(self):
        """回归：assistant + tool_calls 标准写法 content 为 null（定稿规格 5 漏了此项）"""
        m = ChatMessage(role="assistant", content=None,
                        tool_calls=[{"id": "c1", "type": "function"}])
        assert m.content == ""
        assert m.tool_calls[0]["id"] == "c1"

    def test_tool_message_fields(self):
        m = ChatMessage(role="tool", content="result", tool_call_id="c1", name="fn")
        assert m.tool_call_id == "c1" and m.name == "fn"

    def test_unknown_fields_kept_in_pydantic_extra(self):
        r = ChatCompletionRequest(model="x", messages=[],
                                  tools=[{"type": "function"}],
                                  stream_options={"include_usage": True})
        extra = getattr(r, "__pydantic_extra__", None) or {}
        assert "tools" in extra and "stream_options" in extra

    def test_build_payload_forwards_unknown_fields(self):
        """回归：只加 extra='allow' 不够，_build_payload 必须合并 __pydantic_extra__"""
        prov = NewAPIProvider(base_url="http://127.0.0.1:3000")
        r = ChatCompletionRequest(
            model="deepseek-v4-flash",
            messages=[ChatMessage(role="user", content="hi")],
            tools=[{"type": "function"}],
        )
        payload = prov._build_payload(r, "deepseek-v4-flash")
        assert "tools" in payload

    def test_build_payload_excludes_none_fields(self):
        prov = NewAPIProvider(base_url="http://127.0.0.1:3000")
        r = ChatCompletionRequest(
            model="deepseek-v4-flash",
            messages=[ChatMessage(role="user", content="hi")],
        )
        payload = prov._build_payload(r, "deepseek-v4-flash")
        assert "tool_calls" not in payload["messages"][0]
        assert "tool_call_id" not in payload["messages"][0]

    def test_task_type_detection_survives_multimodal(self):
        """回归：content 放开为数组后，.lower() 会崩（P0-7 连带）"""
        eng = build_engine(FakeState(), FakeTransport([]))
        r = ChatCompletionRequest(model="deepseek-v4-flash", messages=[
            ChatMessage(role="user", content=[
                {"type": "text", "text": "看图说说"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ])
        ])
        assert eng._detect_task_type(r) == TaskType.VISION


# ─────────────────────────────────────────
# W6：Provider 接口（P0-5 前提）
# ─────────────────────────────────────────

class TestStreamInterface:
    def test_open_stream_declared_on_abc(self):
        from router.providers import Provider
        assert "open_stream" in Provider.__abstractmethods__

    def test_new_api_provider_implements_open_stream(self):
        assert hasattr(NewAPIProvider, "open_stream")
        prov = NewAPIProvider(base_url="http://127.0.0.1:3000")
        assert callable(prov.open_stream)

    def test_route_stream_exists(self):
        eng = build_engine(FakeState(), FakeTransport([]))
        assert hasattr(eng, "route_stream")
        assert hasattr(eng, "_sse_error")
