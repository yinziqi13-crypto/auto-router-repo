"""
Provider 抽象层（M2-6 多供应商框架）
定义 Provider ABC + NewAPIProvider 实现
后续接 DeepSeek 直连、OpenAI 直连时只需新增 Provider 实现
"""
import abc
import json
import logging
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from .models import ChatCompletionRequest

logger = logging.getLogger("auto_router.provider")


class Provider(abc.ABC):
    """供应商抽象基类

    每个供应商（New API / DeepSeek 直连 / OpenAI 直连）实现此接口
    """

    @abc.abstractmethod
    async def forward(
        self,
        token: str,
        request: ChatCompletionRequest,
        model_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """非流式转发，返回 {status_code, headers, body, error, latency_ms}"""

    @abc.abstractmethod
    async def forward_stream(
        self,
        token: str,
        request: ChatCompletionRequest,
        model_mapping: Optional[Dict[str, str]] = None,
    ) -> AsyncGenerator[bytes, None]:
        """流式转发，yield 原始 SSE 字节流"""

    @abc.abstractmethod
    async def open_stream(
        self,
        token: str,
        request: ChatCompletionRequest,
        model_mapping: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """M2.5-W6（P0-5）：打开上游流式响应并返回未消费 body 的 response 对象。

        调用方负责：先检查 resp.status_code，再决定是否 aiter_bytes()；
        无论成功失败，都必须在使用完毕后 resp.aclose()。
        这是流式 402 降级的前提——必须在向客户端发出首个 chunk 之前拿到状态码。
        """

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """健康检查，返回 True 表示可用"""


class NewAPIProvider(Provider):
    """New API 供应商实现（对接 New API / v1/chat/completions）"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    async def forward(
        self,
        token: str,
        request: ChatCompletionRequest,
        model_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        upstream_model = self._resolve_model(request.model, model_mapping)
        payload = self._build_payload(request, upstream_model)
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        start = datetime.utcnow()
        try:
            resp = await self._client.post(url, headers=headers, json=payload)
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            body = None
            error = None
            if resp.status_code == 200:
                body = resp.json()
            else:
                error = resp.text[:500]
                logger.warning(f"[NewAPIProvider] upstream {resp.status_code}: {error}")

            return {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": body,
                "error": error,
                "latency_ms": latency,
            }
        except httpx.ConnectError as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            logger.error(f"[NewAPIProvider] connection failed: {e}")
            return {
                "status_code": 0,
                "headers": {},
                "body": None,
                "error": f"connection_fail: {str(e)}",
                "latency_ms": latency,
            }
        except httpx.TimeoutException as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            return {
                "status_code": 0,
                "headers": {},
                "body": None,
                "error": f"timeout: {str(e)}",
                "latency_ms": latency,
            }
        except Exception as e:
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            return {
                "status_code": 0,
                "headers": {},
                "body": None,
                "error": f"unexpected: {str(e)}",
                "latency_ms": latency,
            }

    async def forward_stream(
        self,
        token: str,
        request: ChatCompletionRequest,
        model_mapping: Optional[Dict[str, str]] = None,
    ) -> AsyncGenerator[bytes, None]:
        """纯透传 SSE（无需状态检查的场景使用）

        与 open_stream() 的区别：本方法自带生命周期管理，错误直接抛 RuntimeError。
        """
        resp = await self.open_stream(token, request, model_mapping)
        try:
            if resp.status_code != 200:
                error_text = await resp.aread()
                raise RuntimeError(
                    f"upstream {resp.status_code}: {error_text[:500]}"
                )
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    async def open_stream(
        self,
        token: str,
        request: ChatCompletionRequest,
        model_mapping: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """M2.5-W6：打开上游流式响应，返回未消费 body 的 response。

        实现要点（与定稿规格 4 的差异，已修正）：
        不能用 `async with client.stream(...)` —— 该 context 退出时会关闭 response，
        返回给调用方后 body 已被消费。改用 client.send(..., stream=True)，
        生命周期交由调用方管理。
        """
        upstream_model = self._resolve_model(request.model, model_mapping)
        payload = self._build_payload(request, upstream_model, stream=True)
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        req = self._client.build_request("POST", url, json=payload, headers=headers)
        resp = await self._client.send(req, stream=True)
        return resp  # 调用方负责 aclose()

    async def health_check(self) -> bool:
        """简单健康检查：尝试连接 base_url 的 /health 或根路径"""
        try:
            resp = await self._client.get(f"{self.base_url}/health", timeout=5.0)
            return resp.status_code in (200, 404)  # 404 也表示服务在跑
        except Exception:
            return False

    def _resolve_model(
        self, model: str, mapping: Optional[Dict[str, str]]
    ) -> str:
        if mapping and model in mapping:
            return mapping[model]
        return model

    def _build_payload(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        stream: bool = False,
    ) -> dict:
        # M2.5-W2 配套修复（定稿规格未覆盖，但不改则 P0-7 无效）：
        # 1) .dict() 是 Pydantic v1 写法，v2 下废弃；且会把 tool_calls=None 等
        #    空字段原样发给上游，New API 可能拒绝。改用 model_dump(exclude_none=True)
        # 2) extra="allow" 捕获的未知字段进入 __pydantic_extra__，不在 request.extra 里，
        #    两个来源都要合并，否则 tools / stream_options 等仍然传不上去
        payload = {
            "model": upstream_model,
            "messages": [
                m.model_dump(exclude_none=True, mode="json")
                for m in request.messages
            ],
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        # 显式 extra 字段（main.py 构造时写入）
        payload.update(request.extra or {})
        # extra="allow" 捕获的未知字段（直接 **kwargs 构造时写入）
        pyd_extra = getattr(request, "__pydantic_extra__", None) or {}
        payload.update(pyd_extra)
        return payload

    async def close(self):
        await self._client.aclose()


class ProviderRegistry:
    """Provider 注册表（单例）

    根据 config.json 的 providers 配置初始化 Provider 实例
    """

    def __init__(self):
        self._providers: Dict[str, Provider] = {}
        self._provider_config: Dict[str, dict] = {}

    def init_from_config(self, providers_config: Dict[str, dict], default_base_url: str):
        """从 config.json 的 providers 字段初始化

        providers_config 格式：
        {
            "new_api": {
                "type": "new_api",
                "base_url": "http://127.0.0.1:3000",
                "is_default": true
            }
        }
        """
        self._provider_config = providers_config
        for name, cfg in providers_config.items():
            ptype = cfg.get("type", name)
            if ptype == "new_api":
                base_url = cfg.get("base_url", default_base_url)
                self._providers[name] = NewAPIProvider(base_url=base_url)
                logger.info(f"[Registry] registered '{name}' as NewAPIProvider({base_url})")
            else:
                logger.warning(f"[Registry] unknown provider type: {ptype} for '{name}'")

    def get(self, name: str) -> Optional[Provider]:
        return self._providers.get(name)

    def get_default(self) -> Optional[Provider]:
        for name, cfg in self._provider_config.items():
            if cfg.get("is_default"):
                return self._providers.get(name)
        # fallback：返回第一个
        for p in self._providers.values():
            return p
        return None

    def all(self) -> Dict[str, Provider]:
        return dict(self._providers)

    async def close_all(self):
        for name, provider in self._providers.items():
            try:
                await provider.close()
                logger.info(f"[Registry] closed provider '{name}'")
            except Exception as e:
                logger.warning(f"[Registry] close '{name}' failed: {e}")
