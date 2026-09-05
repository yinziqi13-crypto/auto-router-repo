"""
Auto Router M1-1 数据模型
对应 M1-0.5 设计文档中的 router.db 数据模型
"""

from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# 枚举定义
# ─────────────────────────────────────────────

class QuotaStatus(str, Enum):
    """额度状态"""
    AVAILABLE = "available"          # 可用
    SUSPECTED_LOW = "suspected_low" # 疑似不足（连续 402）
    EXHAUSTED = "exhausted"         # 已耗尽
    UNKNOWN = "unknown"             # 未知（初始状态）


class ErrorType(str, Enum):
    """错误类型"""
    QUOTA_EXHAUSTED = "quota_exhausted"   # 402
    INVALID_MODEL = "invalid_model"         # 400
    UPSTREAM_ERROR = "upstream_error"       # 5xx
    CONNECTION_FAIL = "connection_fail"     # 连接失败
    TIMEOUT = "timeout"                    # 超时
    OTHER = "other"                        # 其他


class TaskType(str, Enum):
    """任务类型（三池关键词规则 v1）"""
    TEXT = "text"          # 文本生成
    VISION = "vision"      # 视觉理解
    AUDIO = "audio"        # 语音
    VIDEO = "video"        # 视频
    IMAGE = "image"        # 图片生成
    UNKNOWN = "unknown"    # 未知


# ─────────────────────────────────────────────
# 数据库模型（SQLAlchemy 风格，实际用 aiosqlite 原生）
# ─────────────────────────────────────────────

class QuotaStateRecord(BaseModel):
    """quota_state 表记录"""
    provider: str                       # 供应商标识（tencent_free / bailian_free / bailian_lite / tencent_plan）
    model: str                          # 逻辑模型名（deepseek-v4-flash）
    status: QuotaStatus = QuotaStatus.UNKNOWN
    consecutive_402: int = 0           # 连续 402 次数
    last_402_at: Optional[datetime] = None   # 最近一次 402 时间
    cooldown_until: Optional[datetime] = None # 冷却截止时间
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ErrorEventRecord(BaseModel):
    """error_event 表记录"""
    id: Optional[int] = None
    provider: str
    model: str
    error_type: ErrorType
    channel_id: int                    # New API 中的 channel ID
    status_code: Optional[int] = None  # HTTP 状态码
    error_msg: str = ""
    requested_at: datetime = Field(default_factory=datetime.utcnow)


class RouterDecisionEventRecord(BaseModel):
    """router_decision_event 表记录"""
    id: Optional[int] = None
    request_id: str                    # 请求唯一 ID（UUID）
    original_model: str                # 用户请求的原始模型名
    logical_model: str                 # 路由解析后的逻辑模型名
    task_type: TaskType = TaskType.UNKNOWN
    selected_provider: str             # 最终选中的供应商
    selected_token: str                # 使用的 token 类型（free / paid）
    fallback_reason: Optional[str] = None  # 降级原因（如 "402_exhausted"）
    quota_status_before: Optional[QuotaStatus] = None  # 决策前额度状态
    latency_ms: Optional[float] = None     # 请求耗时（毫秒）
    success: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────
# 内存状态模型（对应 M1-0.5 设计）
# ─────────────────────────────────────────────

COOLDOWN_BASE_HOURS = 1  # 首次 cooldown 时长（小时）
COOLDOWN_MAX_HOURS = 24  # 最大 cooldown 时长（小时）


class QuotaState:
    """
    内存额度状态（可再生缓存，由 DB 恢复）
    对应 M1-0.5 设计文档中的 in-memory 结构
    """
    def __init__(
        self,
        provider: str,
        model: str,
        status: QuotaStatus = QuotaStatus.UNKNOWN,
        consecutive_402: int = 0,
        cooldown_until: Optional[datetime] = None,
    ):
        self.provider = provider
        self.model = model
        self.status = status
        self.consecutive_402 = consecutive_402
        self.cooldown_until = cooldown_until
        self.last_updated = datetime.utcnow()

    def is_available(self) -> bool:
        """判断当前是否可用"""
        if self.status == QuotaStatus.AVAILABLE:
            return True
        if self.status == QuotaStatus.SUSPECTED_LOW:
            # 疑似不足但仍有额度，允许尝试
            return True
        return False

    def record_402(self):
        """记录一次 402 错误，设置指数退避 cooldown"""
        self.consecutive_402 += 1
        self.last_updated = datetime.utcnow()
        # 指数退避：1h → 2h → 4h → ... 上限 24h
        hours = min(COOLDOWN_BASE_HOURS * (2 ** (self.consecutive_402 - 1)),
                     COOLDOWN_MAX_HOURS)
        self.cooldown_until = datetime.utcnow() + timedelta(hours=hours)
        if self.consecutive_402 >= 3:
            self.status = QuotaStatus.EXHAUSTED

    def reset(self):
        """重置状态（用于冷却恢复）"""
        self.status = QuotaStatus.AVAILABLE
        self.consecutive_402 = 0
        self.cooldown_until = None
        self.last_updated = datetime.utcnow()


# ─────────────────────────────────────────────
# 请求/响应模型
# ─────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """OpenAI 兼容请求（仅接收必要字段）"""
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # 透传：不使用的字段原样转发
    extra: Dict[str, Any] = Field(default_factory=dict)


class RouterConfig(BaseModel):
    """Router 配置（从 config.json 加载）"""
    new_api_base_url: str = "http://127.0.0.1:3000"
    token_free: str = ""           # free 池 token (sk-...)
    token_paid: str = ""           # paid 池 token (sk-...)
    provider_models: Dict[str, list[str]] = Field(default_factory=dict)
    # 示例: {"tencent_free": ["deepseek-v4-flash"], "bailian_free": [...], "bailian_lite": [...], "tencent_plan": [...]}
    # 用于 _select_provider() 判断哪个 provider 支持该逻辑模型名
    model_mapping: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    # 示例: {"tencent_plan": {"deepseek-v4-flash": "deepseek-v4-flash-202605"}}
    # 只做模型名映射，不再承担"模型注册表"职责
    provider_priority: list[str] = Field(default_factory=list)
    # 示例: ["tencent_free", "bailian_free", "bailian_lite", "tencent_plan"]
    free_providers: list[str] = Field(default_factory=list)
    # 免费池 provider 列表（用于判断 free 是否 exhausted）
    # 示例: ["tencent_free", "bailian_free"]（不依赖名字含 free）
    three_pool_keywords: Dict[str, list[str]] = Field(default_factory=dict)
    # 任务类型检测关键词（中英文，每个类型 10-15 个）
    # 默认值在 DecisionEngine 里合并，此处仅为文档
    #
    # TEXT（默认）：不命中其他关键词时落这里
    # VISION：图片 URL / base64 / "看图" / "识别" 等
    #   ["图片", "图像", "image", "vision", "看", "识别", "截图", "photo", "picture", "分析图", "读图", "visual"]
    # AUDIO：语音 / 转写 / whisper 等
    #   ["语音", "audio", "语音识别", "转写", "whisper", "speech", "录音", "sound", "听", "音频", "asr", "tts"]
    # VIDEO：视频生成 / 视频理解等
    #   ["视频", "video", "生成视频", "视频理解", "movie", "clip", "vid"]
    # IMAGE：图片生成 / dall-e 等
    #   ["画", "生成图", "image generation", "图片生成", "dall-e", "绘图", "draw", "stable diffusion", "midjourney", "画图", "文生图"]
    routing_strategy: str = "cost_optimized"
    # 路由策略：cost_optimized（免费优先，当前实现）/ balanced / quality_optimized（占位）
    model_routes: Dict[str, Optional[str]] = Field(default_factory=dict)
    # 任务类型 → 推荐模型名映射
    # 示例: {"text": "deepseek-v4-flash", "vision": null, "audio": null, "image": null}
    # 若 task_type 对应模型为 null 或不在 model_routes → fallback 到 model_routes["text"]

    providers: Optional[Dict[str, Dict[str, Any]]] = None
    # M2-6 多供应商配置，格式：
    # {
    #   "new_api": {"type": "new_api", "base_url": "http://127.0.0.1:3000", "is_default": true},
    #   "deepseek_direct": {"type": "openai_direct", "base_url": "https://api.deepseek.com", "api_key": "sk-xxx"}
    # }
