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
        upstream_model = self._resolve_model(request.model, model_mapping)
        payload = self._build_payload(request, upstream_model, stream=True)
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        async with self._client.stream(
            "POST", url, headers=headers, json=payload
        ) as resp:
            if resp.status_code != 200:
                error_text = await resp.aread()
                raise RuntimeError(
                    f"upstream {resp.status_code}: {error_text[:500]}"
                )
            async for chunk in resp.aiter_bytes():
                yield chunk

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
        payload = {
            "model": upstream_model,
            "messages": [m.dict() for m in request.messages],
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        payload.update(request.extra)
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
