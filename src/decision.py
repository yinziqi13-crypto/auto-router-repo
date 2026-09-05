"""
Decision Engine — 决策引擎（M1-1 ~ M1-6 修复版）
职责（按 ADR-002 + 孙磊 5 点确认）：
  1. 判断免费额度状态（查 QuotaState）
  2. 选择 provider（free 优先，402 后换 paid）
  3. 如果 token-free 返回 402：
     - State Manager 标记 exhausted
     - 换 token-paid 再调 adapter.forward() 1 次
     - 其他 400/5xx/连接错误 不重试
  4. 记录 RouterDecisionEvent
  5. decide() 与 route() 降级逻辑必须一致
     规则：所有持有该模型的 free provider 全 exhausted 才降级 paid
          单个 free provider exhausted 不降级，选下一个可用 free provider
"""

import uuid
import logging
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

import aiosqlite

from .models import (
    QuotaState, QuotaStatus, TaskType, RouterDecisionEventRecord,
    ChatCompletionRequest, ErrorType,
)
from .providers import ProviderRegistry, NewAPIProvider

logger = logging.getLogger("auto_router.decision")


# ─────────────────────────────────────
# 任务类型检测默认关键词（M2-3）
# config.json 的 three_pool_keywords 有同名类别则覆盖合并，
# 没有则用这里的内置默认（每类 10-15 个，中英文覆盖）
# ─────────────────────────────────────

DEFAULT_TASK_KEYWORDS: Dict[str, List[str]] = {
    "vision": [
        "图片", "图像", "看图", "识别图片", "图片识别", "图片内容",
        "分析图片", "读取图片", "截图", "照片", "图里", "图上",
        "image", "photo", "picture", "vision", "visual", "screenshot",
        "describe this image", "what's in this image",
    ],
    "audio": [
        "语音", "语音识别", "语音转文字", "语音内容", "转写", "转录", "听写",
        "音频", "录音", "说话内容", "说了什么", "asr", "tts",
        "whisper", "speech", "audio", "transcribe", "transcription",
        "voice to text",
    ],
    "video": [
        "视频", "视频生成", "生成视频", "视频理解", "视频内容", "视频分析",
        "video", "movie", "clip", "vid", "footage", "视频里",
    ],
    "image": [
        "生成图片", "图片生成", "文生图", "图生图", "画图", "绘图", "绘画",
        "画一张", "画一幅", "生成图", "作画", "ai绘画", "ai画图",
        "dall-e", "dalle", "image generation", "generate image",
        "generate an image", "midjourney", "stable diffusion", "text to image",
    ],
}

# 检测优先级：生成类（image/video）先于理解类（audio/vision），
# 因为 "生成一张图片" 应判 IMAGE 而非 VISION
DETECTION_ORDER: List[str] = ["image", "video", "audio", "vision"]


# ─────────────────────────────────────
# 辅助函数：统一 free 池状态判断
# ─────────────────────────────────────

def _free_pool_check(
    config_free_providers: List[str],
    config_provider_models: Dict[str, List[str]],
    state_get_status,
    logical_model: str,
) -> Tuple[List[str], List[str], bool]:
    """
    检查 free 池状态（decide() 和 route() 共用）
    返回：(free_providers, free_available, all_free_exhausted)
    - free_providers: 持有该模型的 free provider 列表（来自 config.free_providers）
    - free_available:  其中未 exhausted 的列表
    - all_free_exhausted: 是否全部 exhausted（调用方用这个决定是否降级）
    """
    # 只用 config.free_providers 判断，不依赖名字匹配
    free_providers = [
        p for p in config_free_providers
        if logical_model in config_provider_models.get(p, [])
    ]
    free_available = [
        p for p in free_providers
        if state_get_status(p, logical_model) != QuotaStatus.EXHAUSTED
    ]
    all_free_exhausted = bool(free_providers) and not free_available
    return free_providers, free_available, all_free_exhausted


class DecisionEngine:
    """
    决策引擎
    对应 M1-0.5 设计文档中的 Decision Engine 模块

    M2-6 多供应商框架：
    - 优先使用 provider_registry（多供应商）
    - 兼容旧模式：传入 adapter（单个 NewAPIAdapter）时自动注册到内部 registry
    """

    def __init__(
        self,
        state_manager: "StateManager",
        config: "RouterConfig",
        db_conn: Optional[aiosqlite.Connection] = None,
        adapter: Optional[NewAPIProvider] = None,
        provider_registry: Optional[ProviderRegistry] = None,
    ):
        self.state = state_manager
        self.config = config
        self._db = db_conn
        self._decision_log: list[RouterDecisionEventRecord] = []

        # M2-6: Provider 注册表（多供应商）
        if provider_registry:
            self._provider_registry = provider_registry
        else:
            # 向后兼容：用单个 adapter 构造内部 registry
            self._provider_registry = ProviderRegistry()
            if adapter:
                self._provider_registry._providers["new_api"] = adapter
                self._provider_registry._provider_config["new_api"] = {
                    "type": "new_api",
                    "base_url": getattr(config, "new_api_base_url", ""),
                    "is_default": True,
                }
        # provider_name → transport_name 映射（M2-6：当前所有 provider 都走 new_api）
        # 未来接 deepseek_direct 时，对应 provider 映射到 deepseek_direct transport
        self._provider_transport: Dict[str, str] = {}
        for pname in (config.provider_models or {}).keys():
            # 默认走 new_api transport
            self._provider_transport[pname] = "new_api"

    # ── 公共：选 provider（纯候选匹配，不过滤 exhausted）─┤

    def _get_provider(self, provider_name: str):
        """M2-6：通过 provider 名拿 transport 实例。

        provider_name 是业务层 provider（tencent_free 等），
        transport 是实际通道（new_api / deepseek_direct 等）。
        当前所有 provider 都走 new_api transport；找不到时 fallback 默认 transport。
        """
        transport_name = self._provider_transport.get(provider_name, "new_api")
        prov = self._provider_registry.get(transport_name)
        if prov is None:
            # fallback：default transport
            prov = self._provider_registry.get_default()
        if prov is None:
            logger.error(
                f"[Decision] no transport for provider '{provider_name}' "
                f"(transport '{transport_name}' not registered)"
            )
        return prov

    def _pick_provider(
        self, model: str, prefer_free: bool = True
    ) -> Optional[str]:
        """
        选 provider（纯候选匹配 + 优先级排序，不过滤 exhausted）
        返回排序后第一个候选（可能 exhausted，由调用方负责状态判断）
        """
        priority = self.config.provider_priority or list(
            self.config.provider_models.keys()
        )

        # 1. 模型匹配：找出所有持用该模型的 provider
        candidates = [
            p for p, models in self.config.provider_models.items()
            if model in models
        ]
        if not candidates:
            return priority[0] if priority else None

        # 2. 优先级排序
        def _idx(p):
            try:
                return priority.index(p)
            except ValueError:
                return 999
        candidates.sort(key=_idx)

        # 3. prefer_free 过滤（用 config.free_providers 显式列表，不靠名字推断）
        free_provider_set = set(self.config.free_providers or [])
        if prefer_free:
            free_cands = [p for p in candidates if p in free_provider_set]
            return (free_cands[0] if free_cands else candidates[0])
        else:
            paid_cands = [p for p in candidates if p not in free_provider_set]
            return (paid_cands[0] if paid_cands else (candidates[-1] if candidates else None))

    # ── decide()：只决策不转发（流式用） ──┤

    async def decide(
        self, request: ChatCompletionRequest
    ) -> RouterDecisionEventRecord:
        """
        只做路由决策，不调上游（用于流式请求）
        降级逻辑与 route() 完全一致：
          所有持有该模型的 free provider 全 exhausted → 才降级 paid
        """
        request_id = str(uuid.uuid4())
        task_type = self._detect_task_type(request)
        logical_model = self._resolve_route_model(task_type, request.model)

        # free 池状态（共用逻辑，用 config.free_providers 显式列表）
        free_providers, free_available, all_free_exhausted = _free_pool_check(
            self.config.free_providers,
            self.config.provider_models,
            lambda p, m: self.state.get_status(p, m),
            logical_model,
        )
        provider_paid = self._pick_provider(logical_model, prefer_free=False)

        if all_free_exhausted and provider_paid:
            # 所有 free provider 都 exhausted → 降级 paid
            decision = RouterDecisionEventRecord(
                request_id=request_id,
                original_model=request.model,
                logical_model=logical_model,
                task_type=task_type,
                selected_provider=provider_paid,
                selected_token="paid",
                fallback_reason="exhausted_skip",
                quota_status_before=self.state.get_status(provider_paid, logical_model),
            )
        else:
            # free 池有可用 provider → 选第一个可用的 free provider
            selected = (
                free_available[0]
                if free_available
                else (provider_paid or "unknown")
            )
            decision = RouterDecisionEventRecord(
                request_id=request_id,
                original_model=request.model,
                logical_model=logical_model,
                task_type=task_type,
                selected_provider=selected,
                selected_token="free",
                quota_status_before=self.state.get_status(selected, logical_model),
            )

        return decision

    # ── route()：决策 + 转发（非流式用） ──┤

    async def route(
        self, request: ChatCompletionRequest
    ) -> Tuple[Dict[str, Any], RouterDecisionEventRecord]:
        """
        主路由入口
        返回：(upstream_response, decision_event)

        降级逻辑与 decide() 完全一致：
          所有持有该模型的 free provider 全 exhausted → 才降级 paid
          单个 free provider exhausted → 选下一个可用 free provider（不降级）
        """
        request_id = str(uuid.uuid4())
        task_type = self._detect_task_type(request)
        logical_model = self._resolve_route_model(task_type, request.model)

        # free 池状态（共用逻辑，用 config.free_providers 显式列表）
        free_providers, free_available, all_free_exhausted = _free_pool_check(
            self.config.free_providers,
            self.config.provider_models,
            lambda p, m: self.state.get_status(p, m),
            logical_model,
        )
        provider_paid = self._pick_provider(logical_model, prefer_free=False)

        # 构建初始 decision
        if all_free_exhausted and provider_paid:
            # 所有 free provider 都 exhausted → 直接走 paid
            provider_selected = provider_paid
            initial_token = "paid"
            fallback_reason = "exhausted_skip"
        else:
            # free 池有可用 provider → 选第一个可用的
            available = (
                free_available[0]
                if free_available
                else (provider_paid or "unknown")
            )
            provider_selected = available
            initial_token = "free"
            fallback_reason = None

        decision = RouterDecisionEventRecord(
            request_id=request_id,
            original_model=request.model,
            logical_model=logical_model,
            task_type=task_type,
            selected_provider=provider_selected,
            selected_token=initial_token,
            fallback_reason=fallback_reason,
            quota_status_before=(
                self.state.get_status(provider_selected, logical_model)
                if provider_selected != "unknown" else None
            ),
        )

        # M2-3：构造转发请求（用 logical_model 替代 original_model 做后续路由）
        forward_req = ChatCompletionRequest(
            model=logical_model,
            messages=request.messages,
            stream=request.stream,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            extra=request.extra,
        )

        # 如果 free 全 exhausted → 直接用 paid token 转发，不走 free 尝试
        if all_free_exhausted and provider_paid:
            # M2-6: 通过 registry 拿 provider 实例
            _prov = self._get_provider(provider_paid)
            if _prov is None:
                logger.error(f"[Decision] provider '{provider_paid}' not registered")
                return {
                    "status_code": 500,
                    "body": None,
                    "error": f"provider '{provider_paid}' not registered",
                    "latency_ms": 0,
                }, decision

            token_paid = self.config.token_paid
            mapping_paid = self.config.model_mapping.get(provider_paid, {})
            start = datetime.utcnow()
            resp = await _prov.forward(token_paid, forward_req, mapping_paid)
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            decision.latency_ms = latency
            decision.success = resp["status_code"] == 200
            self._decision_log.append(decision)
            if self._db:
                from .db import insert_decision_event
                try:
                    await insert_decision_event(self._db, decision)
                except Exception as e:
                    logger.warning(f"[Decision] failed to write decision event: {e}")
            return resp, decision

        # 第一次：尝试 free token（选中的 provider 可能是 free 或 paid）
        token_free = self.config.token_free
        mapping_free = self.config.model_mapping.get(provider_selected, {})

        # M2-6: 通过 registry 拿 provider 实例
        _prov = self._get_provider(provider_selected)
        if _prov is None:
            logger.error(f"[Decision] provider '{provider_selected}' not registered")
            return {
                "status_code": 500,
                "body": None,
                "error": f"provider '{provider_selected}' not registered",
                "latency_ms": 0,
            }, decision

        resp = await _prov.forward(token_free, forward_req, mapping_free)
        decision.latency_ms = resp["latency_ms"]

        # 检查 402
        if resp["status_code"] == 402 and provider_paid:
            logger.info(
                f"[Decision] 402 from {provider_selected}, "
                f"switching to paid provider {provider_paid}"
            )
            decision.fallback_reason = "402_exhausted"
            decision.selected_provider = provider_paid
            decision.selected_token = "paid"

            # 标记状态
            await self.state.record_402(provider_selected, logical_model)

            # 换 paid token 重试 1 次（仅限 402 场景）
            token_paid = self.config.token_paid
            mapping_paid = self.config.model_mapping.get(provider_paid, {})

            # M2-6: 通过 registry 拿 provider 实例（402 后目标 provider）
            _prov2 = self._provider_registry.get(provider_paid)
            if _prov2 is None:
                logger.error(f"[Decision] provider '{provider_paid}' not registered for 402 retry")
                resp = {
                    "status_code": 500,
                    "body": None,
                    "error": f"provider '{provider_paid}' not registered",
                    "latency_ms": 0,
                }
            else:
                start = datetime.utcnow()
                resp = await _prov2.forward(token_paid, forward_req, mapping_paid)
                latency = (datetime.utcnow() - start).total_seconds() * 1000
                decision.latency_ms = latency
            decision.quota_status_before = self.state.get_status(
                provider_paid, logical_model
            )

        # 记录 decision event
        decision.success = resp["status_code"] == 200
        self._decision_log.append(decision)

        # 写入 DB（异步，不阻塞返回）
        if self._db:
            from .db import insert_decision_event
            try:
                await insert_decision_event(self._db, decision)
            except Exception as e:
                logger.warning(f"[Decision] failed to write decision event: {e}")

        return resp, decision

    # ── 内部：任务类型检测（M2-3 扩充版） ──┤

    def _detect_task_type(self, request: ChatCompletionRequest) -> TaskType:
        """
        任务类型检测（M2-3）
        优先级：image > video > audio > vision
        （生成类先于理解类，避免"生成图片"误判为 vision）
        关键词来源：DEFAULT_TASK_KEYWORDS 合并 config.three_pool_keywords
        """
        # 合并默认关键词和 config 自定义关键词
        merged: Dict[str, list[str]] = dict(DEFAULT_TASK_KEYWORDS)
        cfg_kw = self.config.three_pool_keywords or {}
        for task_type, kw_list in cfg_kw.items():
            if task_type not in merged:
                merged[task_type] = []
            merged[task_type].extend(kw_list)

        # 取最后一条 user 消息（与 v3.9 行为一致）
        user_msg = ""
        for msg in reversed(request.messages):
            if msg.role == "user":
                user_msg = msg.content.lower()
                break

        # 按优先级顺序检测（DETECTION_ORDER）
        for task_type in DETECTION_ORDER:
            kw_list = merged.get(task_type, [])
            for kw in kw_list:
                if kw.lower() in user_msg:
                    try:
                        return TaskType(task_type)
                    except ValueError:
                        continue

        # 未命中任何关键词 → 默认 TEXT
        return TaskType.TEXT

    # ── 内部：按任务类型解析推荐模型（M2-3） ──┤

    def _resolve_route_model(
        self, task_type: TaskType, original_model: str
    ) -> str:
        """
        根据 task_type 从 config.model_routes 拿推荐模型名
        规则：
          1. task_type 在 model_routes 中且值为非空 → 用该模型
          2. task_type 不在 model_routes 或值为 null → fallback 到 model_routes["text"]
          3. model_routes["text"] 也缺失/为空 → 保留 original_model（向后兼容）
        """
        routes: Dict[str, Optional[str]] = self.config.model_routes or {}
        # routing_strategy 占位分支（当前只实现 cost_optimized）
        strategy = self.config.routing_strategy or "cost_optimized"
        if strategy != "cost_optimized":
            # balanced / quality_optimized 等策略后续实现，当前按 cost_optimized 行为兜底
            logger.info("[Routing] strategy=%s not implemented, using cost_optimized behavior", strategy)

        # 1. 精确命中 task_type
        recommended = routes.get(task_type.value if hasattr(task_type, "value") else str(task_type))
        if isinstance(recommended, str) and recommended:
            return recommended

        # 2. fallback 到 text
        text_model = routes.get("text")
        if text_model:
            return text_model

        # 3. 兜底：原模型
        return original_model

    def get_decision_log(self) -> list[RouterDecisionEventRecord]:
        """获取决策日志（用于 RouterDecisionEvent 回写）"""
        return self._decision_log


# ─────────────────────────────────────
# State Manager（M1-2：DB 持久化）
# ─────────────────────────────────────

from pathlib import Path
from .db import init_db, upsert_quota_state, load_quota_state


class StateManager:
    """
    状态管理器（M1-2）
    - 内存：QuotaState 对象（高速查询）
    - 持久化：router.db → quota_state 表
    - 冷启动：从 DB 恢复内存状态
    对应 M1-0.5 设计文档中的 State Manager 模块
    """

    def __init__(self, db_path: str = "router.db"):
        self.db_path = db_path
        self._states: Dict[Tuple[str, str], QuotaState] = {}
        self._conn: Optional[aiosqlite.Connection] = None

    async def start(self):
        """启动：初始化 DB + 恢复内存状态"""
        self._conn = await init_db(Path(self.db_path))
        await self._restore_from_db()
        logger.info("[State] started, db=%s", self.db_path)

    async def stop(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("[State] stopped")

    # ── 内存查询 ──

    def get(self, provider: str, model: str) -> QuotaState:
        """从内存获取状态（启动时已全量从 DB 恢复）"""
        key = (provider, model)
        if key not in self._states:
            self._states[key] = QuotaState(provider, model)
        return self._states[key]

    def get_status(self, provider: str, model: str) -> Optional[QuotaStatus]:
        st = self.get(provider, model)
        return st.status if st else None

    # ── 状态更新 + 持久化 ──

    async def record_402(self, provider: str, model: str):
        st = self.get(provider, model)
        st.record_402()
        logger.info(
            f"[State] {provider}/{model} 402 count={st.consecutive_402}, "
            f"status={st.status}"
        )
        await self._persist(st)

    async def reset(self, provider: str, model: str):
        st = self.get(provider, model)
        st.reset()
        await self._persist(st)

    # ── 内部 ──

    async def _persist(self, st: QuotaState):
        """将内存状态写入 DB"""
        if not self._conn:
            return
        from .models import QuotaStateRecord
        rec = QuotaStateRecord(
            provider=st.provider,
            model=st.model,
            status=st.status,
            consecutive_402=st.consecutive_402,
            last_402_at=st.last_updated if st.consecutive_402 > 0 else None,
            cooldown_until=st.cooldown_until,
            updated_at=st.last_updated,
        )
        await upsert_quota_state(self._conn, rec)

    COOLDOWN_SCAN_INTERVAL = 60  # 秒

    async def cooldown_scan(self):
        """
        遍历内存 _states，将 cooldown_until 已到期的 EXHAUSTED 记录重置为 AVAILABLE
        应在后台定时循环调用，异常只记日志不抛出
        """
        now = datetime.utcnow()
        reset_count = 0
        for (provider, model), st in list(self._states.items()):
            if st.status != QuotaStatus.EXHAUSTED:
                continue
            if not st.cooldown_until:
                continue
            if now >= st.cooldown_until:
                try:
                    await self.reset(provider, model)
                    reset_count += 1
                    logger.info(
                        "[Cooldown] %s/%s reset to AVAILABLE "
                        "(cooldown_until=%s)",
                        provider, model, st.cooldown_until
                    )
                except Exception as e:
                    logger.warning(
                        "[Cooldown] failed to reset %s/%s: %s",
                        provider, model, e
                    )
        if reset_count:
            logger.info("[Cooldown] scan done, reset %d state(s)", reset_count)

    async def _restore_from_db(self):
        """冷启动：从 DB 恢复内存状态"""
        if not self._conn:
            return
        from .models import QuotaStateRecord
        # 全量加载 quota_state 表到内存
        async with self._conn.execute(
            "SELECT provider, model, status, consecutive_402, "
            "last_402_at, cooldown_until, updated_at FROM quota_state"
        ) as cur:
            async for row in cur:
                key = (row[0], row[1])
                st = QuotaState(row[0], row[1])
                try:
                    st.status = QuotaStatus(row[2])
                except ValueError:
                    st.status = QuotaStatus.UNKNOWN
                st.consecutive_402 = row[3] or 0
                if row[5]:
                    try:
                        st.cooldown_until = datetime.fromisoformat(row[5])
                    except ValueError:
                        st.cooldown_until = None
                st.last_updated = (
                    datetime.fromisoformat(row[6]) if row[6] else datetime.utcnow()
                )
                self._states[key] = st
        logger.info("[State] restored %d quota states from DB", len(self._states))
