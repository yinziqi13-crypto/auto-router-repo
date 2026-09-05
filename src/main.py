"""
Auto Router M2-6 主入口
FastAPI 代理骨架 + 流式透传 + 双 token 转发验证 + Provider 注册表

实现内容（M2-6 多供应商框架）：
  1. ProviderRegistry 初始化（config.json providers 配置）
  2. DecisionEngine 使用 provider_registry（不再直接持有单个 adapter）
  3. /v1/chat/completions 非流式 + 流式（SSE 透传）
  4. 双 token 转发（free → 402 → paid 重试 1 次）
  5. /health 健康检查 + /config 配置查看
  6. /router/stats 统计接口（M2-1 运营看板）
  7. /dashboard 运营看板 HTML 页面（M2-1）
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Dict, Any, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from .models import (
    ChatCompletionRequest, RouterConfig, TaskType, QuotaStatus,
)
from .providers import ProviderRegistry
from .adapter import NewAPIAdapter  # 向后兼容：类型引用
from .decision import DecisionEngine, StateManager
from .db import init_db

# ─────────────────────────────────────────────
# 日志配置
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("auto_router")

# ─────────────────────────────────────────────
# 全局对象
# ─────────────────────────────────────────────
app = FastAPI(title="Auto Router M2-6", version="0.2.0")

# 延迟初始化（startup 时加载 config）
config: RouterConfig = None  # type: ignore
provider_registry: ProviderRegistry = None  # type: ignore
state_mgr: StateManager = None  # type: ignore
_db_conn = None  # aiosqlite.Connection
decision_eng: DecisionEngine = None  # type: ignore
_cooldown_task: Optional[asyncio.Task] = None  # 后台冷却扫描任务


# ─────────────────────────────────────────────
# 启动 / 关闭
# ─────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global config, provider_registry, state_mgr, decision_eng, _db_conn
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg_dict = json.load(f)
        config = RouterConfig(**cfg_dict)
    else:
        raise RuntimeError("config.json not found. Copy config.example.json to config.json and fill in your tokens.")

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

    # M2-6: decision_eng 使用 provider_registry（不再直接持有单个 adapter）
    decision_eng = DecisionEngine(
        state_manager=state_mgr,
        config=config,
        db_conn=_db_conn,
        provider_registry=provider_registry,
    )
    logger.info(f"Auto Router M1-2 started, DB={db_path}, NewAPI={config.new_api_base_url}")

    # 启动后台冷却扫描循环
    global _cooldown_task
    _cooldown_task = asyncio.create_task(_cooldown_loop())
    logger.info("Cooldown scan loop started")


async def _cooldown_loop():
    """后台定时循环：每 60 秒调用 state_mgr.cooldown_scan()"""
    while True:
        await asyncio.sleep(StateManager.COOLDOWN_SCAN_INTERVAL)
        try:
            if state_mgr:
                await state_mgr.cooldown_scan()
        except Exception as e:
            logger.warning(f"[Cooldown] loop error: {e}")


@app.on_event("shutdown")
async def shutdown():
    global _db_conn, _cooldown_task, provider_registry
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


# ─────────────────────────────────────────────
# 依赖注入
# ─────────────────────────────────────────────

def get_config() -> RouterConfig:
    return config

def get_decision_eng() -> DecisionEngine:
    return decision_eng


# ─────────────────────────────────────────────
# /health 健康检查
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "auto_router",
        "version": "M2-6",
        "new_api": config.new_api_base_url if config else "not_loaded",
    }


# ─────────────────────────────────────────────
# /router/cooldown/status 冷却状态查看
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# 内部辅助函数
# ─────────────────────────────────────────────

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
    from .decision import AUTO_MODEL
    if model != AUTO_MODEL:
        known_models = set()
        for _p, _cfg in (config.provider_models or {}).items():
            if isinstance(_cfg, dict):
                known_models.update(_cfg.get("models", []))
            elif isinstance(_cfg, (list, tuple, set)):
                known_models.update(_cfg)
        if model not in known_models:
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

    # ── 流式：先决策并检查错误，再创建 StreamingResponse ──
    # 必须在响应头发出之前判断，否则 HTTP 已经是 200 无法再改状态码
    decision = await eng.decide(chat_req)
    if decision.error_code:
        return JSONResponse(
            status_code=decision.error_code,
            content={"error": {
                "message": decision.error_message or "routing failed",
                "type": "model_not_found" if decision.error_code == 404 else "no_available_pool",
            }},
        )

    return StreamingResponse(
        eng.route_stream(chat_req, decision),
        media_type="text/event-stream",
    )


# ─────────────────────────────────────────────
# /router/decisions 查看决策日志（从 DB 查询）
# 参数：limit(默认50), offset(默认0), model(可选), success_only(true/false)
# ─────────────────────────────────────────────

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

from datetime import timedelta

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

import os

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
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, status_code=200)


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
