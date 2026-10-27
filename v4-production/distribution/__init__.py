"""v4 分发层 — 格式化（formatter）与发送（publisher）。"""

from .formatter import (
    article_brief,
    digest_from_index,
    generate_daily_digest,
    json_to_markdown,
    json_to_telegram,
    json_to_weixin,
    normalized_score,
)
from .publisher import (
    BasePublisher,
    PublishResult,
    TelegramPublisher,
    WeixinPublisher,
    publish_daily_digest,
)

__all__ = [
    "BasePublisher",
    "PublishResult",
    "TelegramPublisher",
    "WeixinPublisher",
    "article_brief",
    "digest_from_index",
    "generate_daily_digest",
    "json_to_markdown",
    "json_to_telegram",
    "json_to_weixin",
    "normalized_score",
    "publish_daily_digest",
]
