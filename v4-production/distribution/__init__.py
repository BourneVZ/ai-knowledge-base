"""v4 分发层 — 格式化（formatter）与发送（publisher，待建）。"""

from .formatter import (
    generate_daily_digest,
    json_to_markdown,
    json_to_telegram,
    json_to_weixin,
    normalized_score,
)

__all__ = [
    "generate_daily_digest",
    "json_to_markdown",
    "json_to_telegram",
    "json_to_weixin",
    "normalized_score",
]
