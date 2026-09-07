"""
Auto Router AIHub V4.0 主入口
FastAPI 代理骨架 + 流式透传 + 双 token 转发验证 + Provider 注册表

实现内容：
  M2-6  多供应商框架（ProviderRegistry / 多 provider）
  M2.5  流式状态码检查（stream_route_setup）+ 未知模型 404 + Agent 协议兼容
  M3-1e SIGTERM 优雅停机：lifespan + ASGI 活跃请求计数，systemd stop 等待在途请求
  M3-4d OpenAI 兼容模型列表端点 GET /v1/models

版本号：见 router/__init__.py 的 __version__ / __milestone__
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse

from .models import (
    ChatCompletionRequest, RouterConfig, TaskType, QuotaStatus,
)
from .providers import ProviderRegistry
from .adapter import NewAPIAdapter  # 向后兼容：类型引用
from .decision import DecisionEngine, StateManager
from .db import init_db
from . import __version__, __milestone__

# ────────────────────────────────────────────
# 日志配置
# ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("auto_router")

# Unix 时间戳（进程启动时刻），供 /v1/models 的 created 字段使用
_BOOT_TS = int(time.time())

# ────────────────────────────────────────────
# 全局对象（lifespan 启动时初始化）
# ────────────────────────────────────────────
config: RouterConfig = None  # type: ignore
provider_registry: ProviderRegistry = None  # type: ignore
state_mgr: StateManager = None  # type: ignore
_db_conn = None  # aiosqlite.Connection
decision_eng: DecisionEngine = None  # type: ignore
_cooldown_task: Optional[asyncio.Task] = None  # 后台冷却扫描任务

# M3-1e: 优雅停机 —— 活跃请求计数
_active_requests: int = 0
_active_requests_lock: asyncio.Lock = asyncio.Lock()
_shutdown_event: asyncio.Event = asyncio.Event()

# M3-1f: config 路径（可被测试 monkeypatch）
_CONFIG_PATH = Path(__file__).parent / "config.json"


# ────────────────────────────────────────────
# 启动 / 关闭（lifespan，取代 on_event）
# M3-1e：SIGTERM 到来时 uvicorn 会先停止接收新连接，
# 然后触发 lifespan 的 shutdown 段（yield 之后的代码）。
# 这里等待在途请求（_active_requests）归零后再释放资源。
# systemd 侧 TimeoutStopSec=15，等待上限取 13s 留 2s 余量。
# ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Auto Router 应用生命周期：启动初始化 / 关闭优雅停机"""
    global config, provider_registry, state_mgr, decision_eng, _db_conn

    # ── startup ──
    logger.info("Lifespan startup begin")
    config_path = _CONFIG_PATH
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg_dict = json.load(f)
        config = RouterConfig(**cfg_dict)
    else:
        raise RuntimeError("config.json not found. Copy config.example.json to config.json and fill in your tokens.")

    # M3-1f: 启动自检 —— config 必需字段完整性检查
    _token_missing = [
        k for k in ("new_api_base_url", "token_free", "token_paid")
        if not getattr(config, k, None)
    ]
    if _token_missing:
        raise RuntimeError(
            f"config.json 缺少必需字段: {', '.join(_token_missing)}. "
            f"请对照 config.example.json 补全。"
        )
    if not (config.provider_models or {}):
        raise RuntimeError(
            "config.json 的 provider_models 为空，没有任何模型可路由。"
            "请对照 config.example.json 填写。"
        )

    # 初始化 DB
    db_path = Path(__file__).parent / "router.db"
    _db_conn = await init_db(db_path)

    # 初始化各组件
    state_mgr = StateManager(str(db_path))
    await state_mgr.start()

    # M2-6: Provider 注册表（可插拔多供应商）
    provider_registry = ProviderRegistry()
    providers_cfg = getattr(config, "providers", None) or {}
    provider_registry.init_from_config(
        providers_cfg, default_base_url=config.new_api_base_url
    )

    # 确保至少有一个默认 provider（老 config 无 providers 时自动注册 new_api）
    if provider_registry.get_default() is None:
        provider_registry.init_from_config(
            {
                "new_api": {
                    "type": "new_api",
                    "base_url": config.new_api_base_url,
                    "is_default": True,
                }
            },
            default_base_url=config.new_api_base_url,
        )

    # M3-1f: New API 连通性检查（失败则拒绝启动）
    # 放在 provider_registry 初始化后，用 health_check() 做真实连接探测
    _any_healthy = False
    for _pname, _pinst in provider_registry.all().items():
        try:
            if await _pinst.health_check():
                _any_healthy = True
                logger.info(f"New API 连通性检查通过: provider={_pname}")
                break
        except Exception as _e:
            logger.warning(f"New API 连通性检查失败 provider={_pname}: {_e}")
    if not _any_healthy:
        raise RuntimeError(
            f"New API 连通性检查失败（所有 provider 均不可达 {config.new_api_base_url}）。"
            f"请确认 New API 正在运行，或检查 config.json 的 new_api_base_url。"
        )

    # M2-6: decision_eng 使用 provider_registry（不再直接持有单个 adapter）
    decision_eng = DecisionEngine(
        state_manager=state_mgr,
        config=config,
        db_conn=_db_conn,
        provider_registry=provider_registry,
    )
    logger.info(
        f"Auto Router {__version__} ({__milestone__}) started, "
        f"DB={db_path}, NewAPI={config.new_api_base_url}"
    )

    # 启动后台冷却扫描循环
    global _cooldown_task
    _cooldown_task = asyncio.create_task(_cooldown_loop())
    logger.info("Cooldown scan loop started")

    # 将控制权交给 FastAPI（服务开始接收请求）
    yield

    # ── shutdown（SIGTERM 后执行）──
    logger.info("Lifespan shutdown begin — waiting for active requests...")
    _shutdown_event.set()

    # 等待活跃请求完成：轮询 _active_requests，最多 13 秒
    for _i in range(13):
        async with _active_requests_lock:
            if _active_requests <= 0:
                logger.info("All active requests finished, proceeding shutdown")
                break
        await asyncio.sleep(1)
    else:
        async with _active_requests_lock:
            logger.warning(
                f"Shutdown timeout: {_active_requests} active requests still pending after 13s"
            )

    # 取消冷却扫描任务
    if _cooldown_task and not _cooldown_task.done():
        _cooldown_task.cancel()
        try:
            await _cooldown_task
        except asyncio.CancelledError:
            pass
        logger.info("Cooldown scan loop stopped")

    if state_mgr:
        await state_mgr.stop()
    if provider_registry:
        await provider_registry.close_all()
    if _db_conn:
        await _db_conn.close()
    logger.info("Auto Router shut down")


async def _cooldown_loop():
    """后台定时循环：每 60 秒调用 state_mgr.cooldown_scan()"""
    while True:
        await asyncio.sleep(StateManager.COOLDOWN_SCAN_INTERVAL)
        try:
            if state_mgr:
                await state_mgr.cooldown_scan()
        except Exception as e:
            logger.warning(f"[Cooldown] loop error: {e}")


# ────────────────────────────────────────────
# M3-1e: 纯 ASGI 中间件 —— 精确计数（含流式）
# ────────────────────────────────────────────

async def _incr_active_requests():
    """活跃请求数 +1（模块级，供闭包安全调用）"""
    global _active_requests
    async with _active_requests_lock:
        _active_requests += 1


async def _decr_active_requests():
    """活跃请求数 -1（模块级，供闭包安全调用）"""
    global _active_requests
    async with _active_requests_lock:
        _active_requests -= 1


class RequestCountMiddleware:
    """
    纯 ASGI 中间件（非 BaseHTTPMiddleware），
    在接收请求时 +1，在发送完整个响应体（含 SSE 流）后 -1。
    BaseHTTPMiddleware 的 call_next 返回时流式 body 尚未发完，计数不准。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 请求进入：+1
        await _incr_active_requests()

        original_send = send
        decremented = False

        async def wrapped_send(message):
            nonlocal decremented
            await original_send(message)
            # ASGI 响应结束标志：more_body=False 的 http.response.body 是最后一个消息
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and not decremented
            ):
                decremented = True
                await _decr_active_requests()

        try:
            await self.app(scope, receive, wrapped_send)
        except BaseException:
            # 异常路径（含 CancelledError）：若响应体未发完则补 -1
            if not decremented:
                await _decr_active_requests()
            raise

        # app 正常返回但从未发过结束 body（非标准响应路径）：补 -1
        if not decremented:
            await _decr_active_requests()


# ────────────────────────────────────────────
# App 实例（lifespan 须先定义后引用）
# ────────────────────────────────────────────
app = FastAPI(
    title="Auto Router AIHub",
    version=__version__,
    lifespan=lifespan,
)

# 注册 ASGI 中间件（必须在所有路由之前）
app.add_middleware(RequestCountMiddleware)


# ────────────────────────────────────────────
# 依赖注入
# ────────────────────────────────────────────

def get_config() -> RouterConfig:
    return config


def get_decision_eng() -> DecisionEngine:
    return decision_eng


# ────────────────────────────────────────────
# /health 健康检查
# ────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "auto_router",
        "version": __version__,
        "milestone": __milestone__,
        "new_api": config.new_api_base_url if config else "not_loaded",
    }


# ────────────────────────────────────────────
# /v1/models OpenAI 兼容模型列表（M3-4d）
# ────────────────────────────────────────────

def _discover_models(cfg: Optional[RouterConfig]) -> list[str]:
    """汇总当前配置下可受理的模型名（去重、排序）

    来源：
      1. provider_models 各 provider 显式持有的模型
      2. model_routes 里配置了推荐模型的任务类型

    注意：配置了 wildcard_providers 时 Auto Router 实际可受理任意模型，
    本端点只返回「显式已知」清单供客户端枚举，不代表能力上限。
    """
    found = set()
    if not cfg:
        return []
    for models in (cfg.provider_models or {}).values():
        if models:
            found.update(models)
    for target in (cfg.model_routes or {}).values():
        if target:
            found.add(target)
    return sorted(found)


@app.get("/v1/models")
async def list_models():
    """OpenAI 兼容的模型列表端点

    使 OpenAI SDK / Cursor / Cherry Studio 等客户端在 base_url
    直连 Auto Router(8080) 时也能正常枚举模型（Joint-1 时为 404）。
    """
    cfg = config
    data = [
        {
            "id": model_id,
            "object": "model",
            "created": _BOOT_TS,
            "owned_by": "auto-router",
        }
        for model_id in _discover_models(cfg)
    ]
    return {"object": "list", "data": data}


# ────────────────────────────────────────────
# /router/cooldown/status 冷却状态查看
# ────────────────────────────────────────────

@app.get("/router/cooldown/status")
async def get_cooldown_status():
    """返回当前所有 EXHAUSTED 状态的 provider/model/cooldown_until 剩余时间"""
    now = datetime.utcnow()
    items = []
    if state_mgr:
        for (provider, model), st in state_mgr._states.items():
            if st.status != QuotaStatus.EXHAUSTED:
                continue
            remaining = None
            if st.cooldown_until:
                delta = st.cooldown_until - now
                remaining = max(0, int(delta.total_seconds()))
            items.append({
                "provider": provider,
                "model": model,
                "status": st.status.value,
                "consecutive_402": st.consecutive_402,
                "cooldown_until": st.cooldown_until.isoformat() if st.cooldown_until else None,
                "remaining_seconds": remaining,
            })
    return {"total": len(items), "items": items}


# ────────────────────────────────────────────
# 内部辅助函数
# ────────────────────────────────────────────

async def _record_stream_decision(eng: DecisionEngine, decision):
    """流式请求结束后回写 decision event 到 DB（不阻塞）"""
    if eng._db:
        from .db import insert_decision_event
        try:
            await insert_decision_event(eng._db, decision)
        except Exception as e:
            logger.warning(f"[Stream] failed to write decision event: {e}")


# ────────────────────────────────────────────
# /v1/chat/completions 主端点
# ────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    eng: DecisionEngine = Depends(get_decision_eng),
):
    """
    OpenAI 兼容 /v1/chat/completions
    支持流式和非流式
    """
    # 1. 解析请求体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # 2. 提取字段（只解析必要字段，其余透传）
    try:
        model = body.get("model", "")
        stream = body.get("stream", False)
        messages = body.get("messages", [])
        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        # 透传字段（去掉已处理的）
        extra = {
            k: v for k, v in body.items()
            if k not in ("model", "stream", "messages", "temperature", "max_tokens")
        }
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request fields")

    from .models import ChatMessage
    try:
        chat_req = ChatCompletionRequest(
            model=model,
            messages=[ChatMessage(**m) for m in messages],
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Request validation failed: {e}")

    # ── M2.5-W5（规格 3）：未知模型 → 404，不静默回退 ──
    # 只在入口做精确匹配；容错匹配（fuzzy）不在 M2.5 范围，M3 作为独立功能加。
    # auto-free 是虚拟模型，跳过此检查交给决策层做自动选路。
    # M3-2c：配置了 wildcard_providers 时，点名任意模型都放行给决策引擎
    # （wildcard 语义 = 该 provider 接受任意模型，模型是否真存在由上游 400 兜底）。
    # 未配置 wildcard 时保持原 404 行为（向后兼容）。
    from .decision import AUTO_MODEL
    if model != AUTO_MODEL:
        known_models = set()
        for _p, _cfg in (config.provider_models or {}).items():
            if isinstance(_cfg, dict):
                known_models.update(_cfg.get("models", []))
            elif isinstance(_cfg, (list, tuple, set)):
                known_models.update(_cfg)
        if model not in known_models:
            wildcard_free = getattr(config, "wildcard_providers", []) or []
            wildcard_paid = getattr(config, "wildcard_paid_providers", []) or []
            if not (wildcard_free or wildcard_paid):
                return JSONResponse(
                    status_code=404,
                    content={"error": {
                        "message": f"model '{model}' not found in any provider pool",
                        "type": "model_not_found",
                    }},
                )

    # 3. 路由 + 转发（非流式）或 流式路由（M2.5 起走 route_stream）
    if not stream:
        # 非流式：route() 内部完成 决策 + forward + 402 换 token 重试
        resp, decision = await eng.route(chat_req)
        if resp["status_code"] == 200 and resp["body"]:
            return JSONResponse(content=resp["body"])
        # 透传 upstream 错误（含 W4 的 D/E 态：503 / 404）
        return JSONResponse(
            status_code=resp["status_code"] or 502,
            content={"error": resp["error"] or "upstream error"},
        )

    # ── 流式：先决策、再打开上游并检查状态码，最后才创建 StreamingResponse ──
    # M2.5-R1：必须在响应头发出之前判断上游状态，否则 HTTP 200 后无法改状态码。
    decision = await eng.decide(chat_req)
    if decision.error_code:
        return JSONResponse(
            status_code=decision.error_code,
            content={"error": {
                "message": decision.error_message or "routing failed",
                "type": "model_not_found" if decision.error_code == 404 else "no_available_pool",
            }},
        )

    generator, error, decision = await eng.stream_route_setup(chat_req, decision)
    if error:
        return JSONResponse(
            status_code=error["status_code"],
            content={"error": {
                "message": error["error"],
                "type": "upstream_error",
            }},
        )

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
    )


# ────────────────────────────────────────────
# /router/decisions 查看决策日志（从 DB 查询）
# 参数：limit(默认50), offset(默认0), model(可选), success_only(true/false)
# ────────────────────────────────────────────

@app.get("/router/decisions")
async def get_decisions(
    limit: int = 50,
    offset: int = 0,
    model: Optional[str] = None,
    success_only: Optional[bool] = None,
):
    from .db import query_decision_events
    records = await query_decision_events(
        _db_conn,
        limit=min(limit, 200),
        offset=offset,
        model=model,
        success_only=success_only,
    )
    return {
        "total": len(records),
        "limit": limit,
        "offset": offset,
        "model": model,
        "success_only": success_only,
        "decisions": [d.dict() for d in records],
    }


# ────────────────────────────────────────────
# /router/stats 统计接口（M2-1 运营看板）
# ────────────────────────────────────────────

@app.get("/router/stats")
async def get_stats(
    time_range: str = "24h",
):
    """
    从 router_decision_event 表统计（只读，不影响路由）
    返回：total/success/fail/rate、free/paid、分布、时序
    """
    from .db import query_stats, parse_time_range
    now = datetime.utcnow()
    window = parse_time_range(time_range)
    since = now - window
    stats = await query_stats(_db_conn, since, n_buckets=24)
    return stats


# ────────────────────────────────────────────
# /dashboard 运营看板 HTML 页面（M2-1）
# ────────────────────────────────────────────

DASHBOARD_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static", "dashboard.html"
)


@app.get("/dashboard")
async def get_dashboard():
    """返回运营看板 HTML 页面（纯静态，无登录）"""
    if not os.path.exists(DASHBOARD_HTML_PATH):
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    with open(DASHBOARD_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html, status_code=200)


# ────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
