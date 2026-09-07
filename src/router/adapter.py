"""
New API Adapter — 纯转发层（M2-6 多供应商框架）

现在 NewAPIAdapter 继承 NewAPIProvider（来自 providers.py）
保留类名 NewAPIAdapter 以保证 decision.py 现有代码无需改动
"""
import logging
from typing import Optional, AsyncGenerator, Dict, Any

from .models import ChatCompletionRequest, ErrorType
from .providers import NewAPIProvider

logger = logging.getLogger("auto_router.adapter")


class NewAPIAdapter(NewAPIProvider):
    """
    New API 适配器（继承自 NewAPIProvider）

    保持与 M1-1 代码完全兼容：
    - 构造参数只有 base_url
    - forward() / forward_stream() 签名不变
    - classify_error() 保留（Provider ABC 不要求此方法）
    """

    def __init__(self, base_url: str):
        super().__init__(base_url=base_url)

    def classify_error(self, status_code: int, error_msg: str) -> ErrorType:
        """根据 upstream 返回分类错误类型（兼容旧代码）"""
        if status_code == 402:
            return ErrorType.QUOTA_EXHAUSTED
        if status_code == 400:
            return ErrorType.INVALID_MODEL
        if status_code >= 500:
            return ErrorType.UPSTREAM_ERROR
        if "connection" in error_msg.lower() or status_code == 0:
            return ErrorType.CONNECTION_FAIL
        return ErrorType.OTHER
