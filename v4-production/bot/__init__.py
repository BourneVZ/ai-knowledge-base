"""v4 交互层 — 知识库机器人（knowledge_bot）。"""

from .knowledge_bot import (
    Intent,
    KnowledgeBot,
    KnowledgeSearchEngine,
    Permission,
    PermissionManager,
    SubscriptionManager,
    recognize_intent,
)

__all__ = [
    "Intent",
    "KnowledgeBot",
    "KnowledgeSearchEngine",
    "Permission",
    "PermissionManager",
    "SubscriptionManager",
    "recognize_intent",
]
