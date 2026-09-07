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
    """可控额度状态：exhausted 集合内的 (provider, model) 返回 EXHAUSTED。

    同时模拟真实 StateManager 的 402 累计行为：连续 3 次后自动进入 EXHAUSTED。
    """

    def __init__(self, exhausted=frozenset()):
        self.exhausted = set(exhausted)
        self.recorded_402 = []
        self._counts: Dict[Tuple[str, str], int] = {}

    def get_status(self, provider, model):
        return (
            QuotaStatus.EXHAUSTED
            if (provider, model) in self.exhausted
            else QuotaStatus.AVAILABLE
        )

    async def record_402(self, provider, model):
        self.recorded_402.append((provider, model))
        key = (provider, model)
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] >= 3:
            self.exhausted.add(key)


class FakeStreamResponse:
    """模拟 httpx.Response 的最小流式响应，供 FakeTransport.open_stream 返回。"""

    def __init__(self, status_code: int, body: bytes = b"data: ok\n\n"):
        self.status_code = status_code
        self._body = body
        self._read = False

    async def aread(self) -> bytes:
        self._read = True
        return self._body

    async def aiter_bytes(self):
        if not self._read:
            # 模拟分块返回
            chunk_size = 8
            for i in range(0, len(self._body), chunk_size):
                yield self._body[i:i + chunk_size]

    async def aclose(self):
        pass


class FakeTransport:
    """可控 transport：按顺序返回预设响应，并记录每次收到的 token。

    只替换最底层通道，保留真实 ProviderRegistry，
    这样 business provider → transport 的映射逻辑仍然被真实执行。
    """

    def __init__(self, responses=None, stream_responses=None):
        self.responses = list(responses or [])
        self.stream_responses = list(stream_responses or [])
        self.calls = []          # 每次 forward/open_stream 收到的 token
        self.requests = []       # 每次 forward 收到的 request
        self.stream_requests = []  # 每次 open_stream 收到的 request

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
        self.calls.append(token)
        self.stream_requests.append(request)
        if self.stream_responses:
            status, body = self.stream_responses.pop(0)
        else:
            status, body = 200, b"data: ok\n\n"
        if isinstance(body, str):
            body = body.encode()
        return FakeStreamResponse(status, body)

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
        # 不设置 wildcard_providers，保持旧行为（M3-2c 前的测试不变）
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
# M2.5-R2：free-only 模型 402 额度状态更新
# ─────────────────────────────────────────

class TestFreeOnly402State:
    async def test_free_only_402_counts_toward_exhausted(self):
        """free-only 模型连续 3 次 402 后，第 4 次直接 503 不再访问上游。"""
        state = FakeState()
        # 4 次都返回 402，但第 4 次不应被调用
        transport = FakeTransport([(402, None)] * 3)
        eng = build_engine(state, transport)

        # 前 3 次：每次访问上游并记录 402
        for i in range(3):
            resp, _ = await eng.route(req("free-only-model"))
            assert resp["status_code"] == 402, f"第 {i + 1} 次应返回 402"

        # 第 4 次：应直接 503，不访问上游
        transport.calls.clear()
        resp, decision = await eng.route(req("free-only-model"))
        assert resp["status_code"] == 503, "第 4 次应直接 503"
        assert transport.calls == [], "第 4 次不应再访问上游"
        assert state.recorded_402 == [
            ("tencent_free", "free-only-model"),
            ("tencent_free", "free-only-model"),
            ("tencent_free", "free-only-model"),
        ], "前 3 次 402 都应被记录"


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


# ─────────────────────────────────────────
# M2.5-R1：流式路径在 StreamingResponse 之前检查上游状态
# ─────────────────────────────────────────

class TestStreamRouteSetup:
    async def test_stream_upstream_500_returns_error_before_yield(self):
        """上游 500 时 stream_route_setup 应返回 error，不返回生成器。"""
        transport = FakeTransport(stream_responses=[(500, b"server error")])
        eng = build_engine(FakeState(), transport)
        generator, error, decision = await eng.stream_route_setup(req("deepseek-v4-pro"))
        assert generator is None
        assert error is not None
        assert error["status_code"] == 500
        assert decision.selected_token == "paid"

    async def test_stream_free_402_then_paid_500_returns_error(self):
        """free 402 → paid 500，最终应返回 500 错误。"""
        transport = FakeTransport(stream_responses=[(402, b"exhausted"), (500, b"bad")])
        eng = build_engine(FakeState(), transport)
        generator, error, decision = await eng.stream_route_setup(req("deepseek-v4-flash"))
        assert generator is None
        assert error["status_code"] == 500
        assert transport.calls == ["sk-free-test", "sk-paid-test"]
        assert decision.selected_token == "paid"

    async def test_stream_free_402_then_paid_200_returns_generator(self):
        """free 402 → paid 200，应返回生成器，可正常迭代 SSE。"""
        transport = FakeTransport(stream_responses=[
            (402, b"exhausted"),
            (200, b"data: {\"ok\": true}\n\n"),
        ])
        eng = build_engine(FakeState(), transport)
        generator, error, decision = await eng.stream_route_setup(req("deepseek-v4-flash"))
        assert generator is not None
        assert error is None
        assert transport.calls == ["sk-free-test", "sk-paid-test"]
        chunks = []
        async for chunk in generator:
            chunks.append(chunk)
        assert b"data:" in b"".join(chunks)

    async def test_stream_connection_failure_returns_502(self):
        """open_stream 抛异常时，应返回 502 错误。"""
        class ExplodingTransport(FakeTransport):
            async def open_stream(self, token, request, model_mapping=None):
                raise ConnectionError("upstream down")

        eng = build_engine(FakeState(), ExplodingTransport())
        generator, error, decision = await eng.stream_route_setup(req("deepseek-v4-pro"))
        assert generator is None
        assert error["status_code"] == 502


# ─────────────────────────────────────────
# M2.5-R3：默认关键词表不随请求增长
# ─────────────────────────────────────────

class TestTaskKeywordsStable:
    def test_keywords_do_not_grow_after_many_calls(self):
        """连续调用 _detect_task_type 100 次，全局 DEFAULT_TASK_KEYWORDS 长度不变。"""
        from router.decision import DEFAULT_TASK_KEYWORDS, DecisionEngine
        eng = build_engine(FakeState(), FakeTransport([]))
        original_len = {k: len(v) for k, v in DEFAULT_TASK_KEYWORDS.items()}
        custom_cfg = make_cfg()
        custom_cfg.three_pool_keywords = {"vision": ["新增词"]}
        eng_custom = DecisionEngine(
            state_manager=FakeState(),
            config=custom_cfg,
            provider_registry=eng._provider_registry,
        )
        for _ in range(100):
            eng_custom._detect_task_type(req("auto-free"))
        for k, ln in original_len.items():
            assert len(DEFAULT_TASK_KEYWORDS[k]) == ln, f"DEFAULT_TASK_KEYWORDS['{k}'] 被污染"


# ─────────────────────────────────────────
# M2.5-R4：直接调用引擎时 __pydantic_extra__ 不丢失
# ─────────────────────────────────────────

class TestDirectEngineExtraFields:
    async def test_tools_passed_through_route(self):
        """直接构造 ChatCompletionRequest(..., tools=...) 调用 route()，tools 应到达上游 payload。"""
        transport = FakeTransport([(200, {"ok": True})])
        eng = build_engine(FakeState(), transport)
        r = ChatCompletionRequest(
            model="deepseek-v4-flash",
            messages=[ChatMessage(role="user", content="hi")],
            tools=[{"type": "function", "function": {"name": "fn"}}],
        )
        await eng.route(r)
        forwarded = transport.requests[0]
        payload = forwarded.model_dump()
        assert "tools" in payload, "tools 应保留在转发请求中"
