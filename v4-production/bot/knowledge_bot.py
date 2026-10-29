"""knowledge_bot.py — 知识库交互机器人。

面向 Telegram 等聊天渠道的知识库交互层。``KnowledgeSearchEngine`` 在
``knowledge/articles/*.json`` 上做关键词、标签与日期范围组合检索；
``SubscriptionManager`` 管理用户标签订阅；``PermissionManager`` 管理
READ / WRITE / DELETE 三级权限（层级包含：DELETE ⊇ WRITE ⊇ READ），
两者均以 JSON 文件持久化。``KnowledgeBot`` 为统一入口：先用
``recognize_intent`` 做规则式意图识别（命令前缀优先，其次自然语言
关键词，不依赖 LLM），再经权限校验分发到各处理器。

本模块只产出回复文本，不直接发送消息；发送由 ``distribution.publisher``
等渠道层完成。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

type Article = dict[str, Any]
"""单条知识条目（``knowledge/articles/*.json`` 的反序列化结果）。"""

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
"""主仓库根目录（v4-production/bot → 上溯两级）。"""

DEFAULT_KNOWLEDGE_DIR = REPO_ROOT / "knowledge" / "articles"
"""知识条目目录默认值，相对本模块定位主仓库，与调用方 cwd 无关。"""

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "knowledge"
"""v4-production 运行时数据目录（订阅、权限等状态文件）。"""

DEFAULT_SUBSCRIPTIONS_FILE = DEFAULT_DATA_DIR / "subscriptions.json"
"""用户订阅状态文件默认路径。"""

DEFAULT_PERMISSIONS_FILE = DEFAULT_DATA_DIR / "permissions.json"
"""用户权限状态文件默认路径。"""

INDEX_FILENAME = "index.json"
"""知识条目目录中的索引文件名，检索时跳过（非单条条目）。"""

DEFAULT_SEARCH_LIMIT = 5
"""search 指令默认返回条数上限。"""

DEFAULT_TODAY_LIMIT = 10
"""today 指令默认返回条数上限。"""

DEFAULT_TOP_N = 5
"""top 指令默认返回条数。"""

MAX_TOP_N = 20
"""top 指令允许的最大条数。"""

_FILENAME_DATE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
"""条目文件名前缀中的落盘日期（``YYYY-MM-DD-*.json``）。"""


# ---------------------------------------------------------------------------
# 枚举与意图识别
# ---------------------------------------------------------------------------


class Intent(StrEnum):
    """用户消息识别出的意图类型。"""

    SEARCH = "search"
    TODAY = "today"
    TOP = "top"
    SUBSCRIBE = "subscribe"
    HELP = "help"
    UNKNOWN = "unknown"


class Permission(StrEnum):
    """三级权限，层级从低到高：READ < WRITE < DELETE。

    高层级自动包含低层级能力（如 DELETE 用户可直接执行 WRITE 操作）。
    """

    READ = "read"
    WRITE = "write"
    DELETE = "delete"

    @property
    def rank(self) -> int:
        """权限层级序号，用于大小比较（READ=1、WRITE=2、DELETE=3）。"""
        return _PERMISSION_RANK[self]


_PERMISSION_RANK: dict[Permission, int] = {
    Permission.READ: 1,
    Permission.WRITE: 2,
    Permission.DELETE: 3,
}
"""权限到层级序号的映射，与 ``Permission.rank`` 配套。"""

_COMMAND_TO_INTENT: dict[str, Intent] = {
    "search": Intent.SEARCH,
    "today": Intent.TODAY,
    "top": Intent.TOP,
    "subscribe": Intent.SUBSCRIBE,
    "help": Intent.HELP,
}
"""命令名（不含 ``/``）到意图的映射。"""

_COMMAND_PATTERN = re.compile(
    r"^/(?P<command>search|today|top|subscribe|help)"
    r"(?:@\w+)?"
    r"(?:[ \t]+(?P<args>.*))?$",
    re.IGNORECASE | re.DOTALL,
)
"""命令消息匹配：``/命令[@bot名] [参数]``。"""

_NL_INTENT_KEYWORDS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (Intent.HELP, ("帮助", "怎么用", "用法", "指令", "help")),
    (
        Intent.SUBSCRIBE,
        ("退订", "订阅", "unsubscribe", "subscribe", "关注"),
    ),
    (Intent.TOP, ("排行榜", "榜单", "排行", "热门", "精选", "top")),
    (Intent.TODAY, ("今天", "今日", "简报", "日报", "today")),
    (
        Intent.SEARCH,
        (
            "搜索",
            "搜一下",
            "搜搜",
            "检索",
            "查询",
            "查一下",
            "找找",
            "找一下",
            "search",
            "find",
        ),
    ),
)
"""自然语言关键词表，按优先级排序（SUBSCRIBE 先于 SEARCH，
使「查询我的订阅」命中订阅而非搜索）。"""

_ARG_FILLERS: tuple[str, ...] = (
    "帮我看看",
    "来一份",
    "帮我",
    "给我",
    "麻烦",
    "我想",
    "我要",
    "看看",
    "来个",
    "请",
    "一下",
)
"""自然语言参数两侧的口语填充词，识别后剔除。"""

_ARG_TRIM_CHARS = " \t,，。!！?？:：、的"
"""自然语言参数两侧的空白与标点。"""

_UNSUBSCRIBE_PREFIXES: tuple[str, ...] = (
    "取消",
    "退订",
    "删除",
    "移除",
    "remove",
    "rm",
    "off",
    "-",
)
"""subscribe 参数中表示「退订」的前缀词。"""


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """把关键词编译为匹配模式：ASCII 词加词边界，中文按子串匹配。"""
    if keyword.isascii():
        return re.compile(
            rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", re.IGNORECASE
        )
    return re.compile(re.escape(keyword), re.IGNORECASE)


def _build_nl_patterns() -> tuple[tuple[Intent, tuple[re.Pattern[str], ...]], ...]:
    """预编译自然语言关键词表，避免每次识别重复编译。"""
    table: list[tuple[Intent, tuple[re.Pattern[str], ...]]] = []
    for intent, keywords in _NL_INTENT_KEYWORDS:
        table.append((intent, tuple(_keyword_pattern(k) for k in keywords)))
    return tuple(table)


_NL_INTENT_PATTERNS = _build_nl_patterns()
"""预编译后的自然语言意图关键词模式表。"""


def _clean_nl_args(text: str) -> str:
    """剔除自然语言残留中的填充词与首尾标点，压缩空白。

    Args:
        text: 移除意图关键词后的剩余文本。

    Returns:
        清理后的参数字符串（可能为空）。
    """
    remainder = text
    changed = True
    while changed:
        changed = False
        stripped = remainder.strip(_ARG_TRIM_CHARS)
        if stripped != remainder:
            remainder = stripped
            changed = True
        for filler in _ARG_FILLERS:
            if remainder.startswith(filler):
                remainder = remainder[len(filler) :]
                changed = True
            elif remainder.endswith(filler):
                remainder = remainder[: -len(filler)]
                changed = True
    return " ".join(remainder.split())


def recognize_intent(text: str) -> tuple[Intent, str]:
    """规则式意图识别（不依赖 LLM）。

    匹配顺序：先命令前缀（``/search``、``/today``、``/top``、
    ``/subscribe``、``/help``，支持 ``@bot名`` 后缀与大小写不敏感），
    命中即返回、参数取命令后的剩余文本；未命中命令时按
    ``_NL_INTENT_KEYWORDS`` 的优先级匹配自然语言关键词（搜索、查询、
    今天、简报、订阅等），参数为剔除关键词与口语填充词后的文本。

    Args:
        text: 用户原始消息文本。

    Returns:
        ``(Intent, 参数字符串)`` 二元组；无法识别时为
        ``(Intent.UNKNOWN, "")``。
    """
    stripped = text.strip()
    if not stripped:
        return Intent.UNKNOWN, ""

    command_match = _COMMAND_PATTERN.match(stripped)
    if command_match is not None:
        intent = _COMMAND_TO_INTENT[command_match["command"].lower()]
        return intent, command_match["args"].strip() if command_match["args"] else ""

    for intent, patterns in _NL_INTENT_PATTERNS:
        for pattern in patterns:
            if pattern.search(stripped):
                return intent, _clean_nl_args(pattern.sub(" ", stripped, count=1))
    return Intent.UNKNOWN, ""


# ---------------------------------------------------------------------------
# 条目工具函数
# ---------------------------------------------------------------------------


def _parse_iso_datetime(value: object) -> datetime | None:
    """把 ISO 8601 字符串解析为 ``datetime``，失败返回 ``None``。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _article_local_day(article: Article, filename: str = "") -> date | None:
    """取条目的本地日期：优先 ``collected_at``，缺失时回退文件名前缀。

    Args:
        article: 知识条目。
        filename: 条目文件名（``YYYY-MM-DD-*.json``），作日期回退。

    Returns:
        本地日期；两者均不可用时为 ``None``。
    """
    dt = _parse_iso_datetime(article.get("collected_at"))
    if dt is not None:
        local = dt.astimezone() if dt.tzinfo is not None else dt
        return local.date()
    match = _FILENAME_DATE_PATTERN.match(filename)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _normalize_tag(tag: str) -> str:
    """标签归一化：去空白、去 ``#`` 前缀、转小写。"""
    return tag.strip().lstrip("#").strip().lower()


def _article_tags(article: Article) -> list[str]:
    """取条目 tags 字段的归一化列表；字段缺失或类型不符时为空。"""
    raw = article.get("tags")
    if not isinstance(raw, list):
        return []
    return [_normalize_tag(str(tag)) for tag in raw if str(tag).strip()]


def _searchable_text(article: Article) -> str:
    """拼接条目可检索文本（标题、描述、摘要、类别、标签）并小写。"""
    parts = [
        str(article.get(key) or "")
        for key in ("title", "description", "summary", "category")
    ]
    parts.extend(_article_tags(article))
    return " ".join(parts).lower()


def _numeric_or_none(value: object) -> float | None:
    """把评分 / stars 等字段安全转 ``float``，非数值（含 ``None``）返回 ``None``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_date_arg(value: str | None, name: str) -> date | None:
    """校验并解析 ``YYYY-MM-DD`` 日期参数。

    Args:
        value: 日期字符串；``None`` 表示未提供。
        name: 参数名，用于错误信息。

    Returns:
        解析后的 ``date``；未提供时为 ``None``。

    Raises:
        ValueError: ``value`` 非 ``YYYY-MM-DD`` 格式。
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"{name} 需为 YYYY-MM-DD 格式，收到: {value!r}") from e


def _format_article_line(index: int, article: Article) -> str:
    """把单条知识条目格式化为两行简报（序号、类别、标题、评分、链接）。

    Args:
        index: 序号（从 1 开始）。
        article: 知识条目。

    Returns:
        形如 ``1. [LLM 框架] 标题 ⭐5\\n   2026-07-01 · https://...`` 的文本。
    """
    title = str(article.get("title") or "(无标题)")
    category = str(article.get("category") or "未分类")
    score = _numeric_or_none(article.get("innovation_score"))
    score_part = f" ⭐{score:g}" if score is not None else ""
    day = _article_local_day(article)
    url = str(article.get("source_url") or "")
    tail = " · ".join(part for part in (day.isoformat() if day else "", url) if part)
    head = f"{index}. [{category}] {title}{score_part}"
    return f"{head}\n   {tail}" if tail else head


def _parse_search_args(
    args: str,
) -> tuple[list[str], list[str], str | None, str | None]:
    """解析 search 参数串中的过滤项。

    ``tag:标签`` 或 ``#标签`` 归入标签；``from:`` / ``to:`` 归入日期；
    其余按空白切分为关键词。

    Args:
        args: recognize_intent 返回的参数字符串。

    Returns:
        ``(关键词列表, 标签列表, date_from, date_to)`` 四元组。
    """
    keywords: list[str] = []
    tags: list[str] = []
    date_from: str | None = None
    date_to: str | None = None
    for token in args.split():
        lowered = token.lower()
        if lowered.startswith("tag:") and len(token) > 4:
            tags.append(_normalize_tag(token[4:]))
        elif lowered.startswith("#") and len(token) > 1:
            tags.append(_normalize_tag(token))
        elif lowered.startswith("from:"):
            date_from = token[5:]
        elif lowered.startswith("to:"):
            date_to = token[3:]
        else:
            keywords.append(token)
    return keywords, tags, date_from, date_to


# ---------------------------------------------------------------------------
# 搜索引擎
# ---------------------------------------------------------------------------


class KnowledgeSearchEngine:
    """知识条目搜索引擎：关键词、标签、日期范围组合过滤。

    每次查询重新扫描条目目录（文件量级为每日数十个，代价可忽略），
    以保证新落盘条目即时可见，不做缓存。单个文件解析失败仅告警跳过，
    不中断整体检索；``index.json`` 为索引文件，始终跳过。
    """

    def __init__(self, knowledge_dir: Path | None = None) -> None:
        """初始化搜索引擎。

        Args:
            knowledge_dir: 知识条目目录；``None`` 时用主仓库默认目录。
        """
        self._knowledge_dir = (
            knowledge_dir if knowledge_dir is not None else DEFAULT_KNOWLEDGE_DIR
        )

    @property
    def knowledge_dir(self) -> Path:
        """当前检索的知识条目目录。"""
        return self._knowledge_dir

    def _load_articles(self) -> list[tuple[Article, str]]:
        """扫描目录并反序列化为 ``(条目, 文件名)`` 列表。"""
        if not self._knowledge_dir.is_dir():
            logger.warning(
                "[KnowledgeBot] 知识条目目录不存在，按空结果处理: %s",
                self._knowledge_dir,
            )
            return []
        loaded: list[tuple[Article, str]] = []
        for filepath in sorted(self._knowledge_dir.glob("*.json")):
            if filepath.name == INDEX_FILENAME:
                continue
            try:
                with open(filepath, encoding="utf-8") as f:
                    article = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "[KnowledgeBot] 知识条目读取失败，已跳过 %s: %s", filepath, e
                )
                continue
            if isinstance(article, dict):
                loaded.append((article, filepath.name))
        return loaded

    def search(
        self,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Article]:
        """组合检索知识条目，按采集日期倒序返回。

        关键词之间、标签之间均为 AND 语义：每个关键词都须命中可检索
        文本（标题、描述、摘要、类别、标签，忽略大小写），每个标签都
        须包含于条目 tags。日期过滤基于 ``collected_at`` 的本地日期
        （缺失时回退文件名前缀）；启用日期过滤后无法确定日期的条目
        会被排除。

        Args:
            keywords: 关键词列表。
            tags: 标签列表（自动归一化）。
            date_from: 起始日期（含），``YYYY-MM-DD``。
            date_to: 截止日期（含），``YYYY-MM-DD``。
            limit: 返回条数上限。

        Returns:
            匹配的条目列表，最多 ``limit`` 条。

        Raises:
            ValueError: 日期参数格式非法、区间颠倒或 ``limit`` 非正。
        """
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到: {limit}")
        start = _parse_date_arg(date_from, "date_from")
        end = _parse_date_arg(date_to, "date_to")
        if start is not None and end is not None and start > end:
            raise ValueError(f"date_from 不能晚于 date_to：{start} > {end}")
        normalized_keywords = [kw.lower() for kw in keywords or [] if kw.strip()]
        normalized_tags = [
            _normalize_tag(tag) for tag in tags or [] if _normalize_tag(tag)
        ]
        date_filter_active = start is not None or end is not None

        matched: list[tuple[Article, date]] = []
        for article, filename in self._load_articles():
            day = _article_local_day(article, filename)
            if date_filter_active:
                if day is None:
                    continue
                if start is not None and day < start:
                    continue
                if end is not None and day > end:
                    continue
            if normalized_tags:
                article_tags = set(_article_tags(article))
                if not all(tag in article_tags for tag in normalized_tags):
                    continue
            if normalized_keywords:
                haystack = _searchable_text(article)
                if not all(kw in haystack for kw in normalized_keywords):
                    continue
            matched.append((article, day or date.min))
        matched.sort(key=lambda item: item[1], reverse=True)
        return [article for article, _ in matched[:limit]]

    def today(
        self, today: date | None = None, limit: int = DEFAULT_TODAY_LIMIT
    ) -> list[Article]:
        """检索指定日期（默认今天，本地时区）采集的条目。

        Args:
            today: 目标日期；``None`` 时取当前本地日期。
            limit: 返回条数上限。

        Returns:
            当日条目列表，按采集时间倒序。
        """
        day = today if today is not None else datetime.now().astimezone().date()
        day_str = day.isoformat()
        return self.search(date_from=day_str, date_to=day_str, limit=limit)

    def top(self, n: int = DEFAULT_TOP_N) -> list[Article]:
        """按创新评分排出前 N 条（评分口径与 formatter 一致，只看
        ``innovation_score``，不引入 relevance_score）。

        排序规则：无评分条目排最后；有评分时分数降序，同分按 stars
        降序，再按采集日期降序。

        Args:
            n: 返回条数。

        Returns:
            排名前 ``n`` 的条目列表。

        Raises:
            ValueError: ``n`` 非正。
        """
        if n <= 0:
            raise ValueError(f"n 必须为正整数，收到: {n}")
        items = self._load_articles()

        def sort_key(item: tuple[Article, str]) -> tuple[int, float, float, date]:
            article, filename = item
            score = _numeric_or_none(article.get("innovation_score"))
            stars = _numeric_or_none(article.get("stars"))
            day = _article_local_day(article, filename)
            return (
                1 if score is not None else 0,
                score if score is not None else 0.0,
                stars if stars is not None else 0.0,
                day or date.min,
            )

        items.sort(key=sort_key, reverse=True)
        return [article for article, _ in items[:n]]


# ---------------------------------------------------------------------------
# 订阅管理
# ---------------------------------------------------------------------------


class SubscriptionManager:
    """用户标签订阅管理（增、删、查），JSON 文件持久化。

    标签统一归一化（去 ``#`` 前缀、小写）；每个用户的订阅保持添加
    顺序且不重复。文件损坏时告警并按空订阅启动，不抛异常。
    """

    def __init__(self, store_path: Path | None = None) -> None:
        """初始化并加载持久化数据。

        Args:
            store_path: 状态文件路径；``None`` 时用默认路径。
        """
        self._store_path = (
            store_path if store_path is not None else DEFAULT_SUBSCRIPTIONS_FILE
        )
        self._subscriptions: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        """从状态文件恢复订阅；文件缺失、损坏或结构不符时按空处理。"""
        if not self._store_path.is_file():
            return
        try:
            with open(self._store_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "[KnowledgeBot] 订阅文件读取失败，按空订阅启动 %s: %s",
                self._store_path,
                e,
            )
            return
        stored = data.get("subscriptions") if isinstance(data, dict) else None
        if not isinstance(stored, dict):
            logger.warning(
                "[KnowledgeBot] 订阅文件结构异常（缺少 subscriptions 字段）: %s",
                self._store_path,
            )
            return
        for user_id, tags in stored.items():
            if isinstance(tags, list):
                normalized = {
                    tag for tag in (_normalize_tag(str(t)) for t in tags) if tag
                }
                self._subscriptions[str(user_id)] = sorted(normalized)

    def _save(self) -> None:
        """把订阅状态原子写回文件（先写临时文件再替换）。"""
        payload = {
            "subscriptions": {
                user_id: sorted(tags)
                for user_id, tags in sorted(self._subscriptions.items())
            }
        }
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._store_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp_path.replace(self._store_path)
        except OSError as e:
            logger.error("[KnowledgeBot] 订阅文件写入失败 %s: %s", self._store_path, e)

    def add_subscription(self, user_id: str, tag: str) -> bool:
        """为用户添加一个标签订阅。

        Args:
            user_id: 用户标识。
            tag: 标签（自动归一化）。

        Returns:
            添加成功返回 ``True``；标签为空或已订阅过返回 ``False``。
        """
        normalized = _normalize_tag(tag)
        if not normalized:
            return False
        user_tags = self._subscriptions.setdefault(user_id, [])
        if normalized in user_tags:
            return False
        user_tags.append(normalized)
        self._save()
        return True

    def remove_subscription(self, user_id: str, tag: str) -> bool:
        """移除用户的一个标签订阅。

        Args:
            user_id: 用户标识。
            tag: 标签（自动归一化）。

        Returns:
            移除成功返回 ``True``；未订阅过返回 ``False``。
        """
        normalized = _normalize_tag(tag)
        user_tags = self._subscriptions.get(user_id)
        if not user_tags or normalized not in user_tags:
            return False
        user_tags.remove(normalized)
        if not user_tags:
            del self._subscriptions[user_id]
        self._save()
        return True

    def list_subscriptions(self, user_id: str) -> list[str]:
        """查询用户的全部订阅标签。

        Args:
            user_id: 用户标识。

        Returns:
            标签列表（可能为空）。
        """
        return list(self._subscriptions.get(user_id, []))

    def subscribers_of(self, tag: str) -> list[str]:
        """查询订阅了某标签的全部用户，供推送侧（publisher）使用。

        Args:
            tag: 标签（自动归一化）。

        Returns:
            订阅该标签的用户标识列表。
        """
        normalized = _normalize_tag(tag)
        return [
            user_id
            for user_id, tags in self._subscriptions.items()
            if normalized in tags
        ]


# ---------------------------------------------------------------------------
# 权限管理
# ---------------------------------------------------------------------------


class PermissionManager:
    """三级权限管理（READ < WRITE < DELETE），JSON 文件持久化。

    未登记的用户默认 READ。高层级包含低层级能力：持有 DELETE 的
    用户自动通过 READ / WRITE 校验。文件损坏时告警并按默认权限
    启动，不抛异常。
    """

    def __init__(self, store_path: Path | None = None) -> None:
        """初始化并加载持久化数据。

        Args:
            store_path: 状态文件路径；``None`` 时用默认路径。
        """
        self._store_path = (
            store_path if store_path is not None else DEFAULT_PERMISSIONS_FILE
        )
        self._permissions: dict[str, Permission] = {}
        self._load()

    def _load(self) -> None:
        """从状态文件恢复权限；文件缺失、损坏或取值非法时按默认处理。"""
        if not self._store_path.is_file():
            return
        try:
            with open(self._store_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "[KnowledgeBot] 权限文件读取失败，按默认权限启动 %s: %s",
                self._store_path,
                e,
            )
            return
        stored = data.get("users") if isinstance(data, dict) else None
        if not isinstance(stored, dict):
            logger.warning(
                "[KnowledgeBot] 权限文件结构异常（缺少 users 字段）: %s",
                self._store_path,
            )
            return
        for user_id, value in stored.items():
            try:
                self._permissions[str(user_id)] = Permission(str(value))
            except ValueError:
                logger.warning(
                    "[KnowledgeBot] 忽略未知权限值 user=%s value=%r", user_id, value
                )

    def _save(self) -> None:
        """把权限状态原子写回文件（先写临时文件再替换）。"""
        payload = {
            "users": {
                user_id: permission.value
                for user_id, permission in sorted(self._permissions.items())
            }
        }
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._store_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp_path.replace(self._store_path)
        except OSError as e:
            logger.error("[KnowledgeBot] 权限文件写入失败 %s: %s", self._store_path, e)

    def get_permission(self, user_id: str) -> Permission:
        """查询用户权限等级。

        Args:
            user_id: 用户标识。

        Returns:
            用户权限；未登记用户返回 ``Permission.READ``。
        """
        return self._permissions.get(user_id, Permission.READ)

    def set_permission(self, user_id: str, permission: Permission) -> None:
        """设置用户权限等级并立即持久化。

        Args:
            user_id: 用户标识。
            permission: 目标权限等级。
        """
        self._permissions[user_id] = permission
        self._save()

    def check(self, user_id: str, required: Permission) -> bool:
        """校验用户是否达到所需权限等级（高层级自动通过低层级校验）。

        Args:
            user_id: 用户标识。
            required: 所需最低权限。

        Returns:
            达标返回 ``True``，否则 ``False``。
        """
        return self.get_permission(user_id).rank >= required.rank


# ---------------------------------------------------------------------------
# 机器人主入口
# ---------------------------------------------------------------------------


SEARCH_USAGE = (
    "用法：/search <关键词> [tag:标签] [from:YYYY-MM-DD] [to:YYYY-MM-DD]\n"
    "示例：/search agent tag:llm from:2026-07-01"
)
"""search 指令用法提示。"""

TOP_USAGE = "用法：/top [N]，N 为 1-20 的整数，例如 /top 10"
"""top 指令用法提示。"""

SUBSCRIBE_USAGE = (
    "用法：/subscribe <标签...> 订阅；/subscribe 取消 <标签...> 退订；"
    "/subscribe 查看当前订阅"
)
"""subscribe 指令用法提示。"""

UNKNOWN_INTENT_REPLY = (
    "🤔 未能理解该消息。输入 /help 查看可用指令，或直接说「搜索 llm」「今天的简报」。"
)
"""无法识别意图时的回复。"""

PERMISSION_DENIED_TEMPLATE = (
    "⛔ 权限不足：{action} 需要 {required} 权限，"
    "你当前为 {current} 权限，请联系管理员调整。"
)
"""权限校验未通过时的回复模板。"""

EMPTY_SEARCH_REPLY = "🔍 没有匹配的知识条目，换个关键词或放宽条件试试。"
"""搜索无结果时的回复。"""

EMPTY_TODAY_TEMPLATE = "📭 {date} 暂无新增知识条目。"
"""当日无条目时的回复模板。"""

EMPTY_TOP_REPLY = "📭 知识库暂无可排名的条目。"
"""排行榜为空时的回复。"""

SUBSCRIBE_EMPTY_REPLY = "📭 你还没有订阅任何标签。用法：/subscribe <标签>"
"""用户无订阅时的回复。"""

SUBSCRIBE_LIST_TEMPLATE = "📋 你的订阅（{n} 个）：\n{items}"
"""订阅列表回复模板。"""

SUBSCRIBE_ADDED_TEMPLATE = "✅ 已订阅标签：{tags}"
"""订阅成功回复模板。"""

SUBSCRIBE_DUP_TEMPLATE = "⚠️ 已订阅过，跳过：{tags}"
"""重复订阅回复模板。"""

SUBSCRIBE_REMOVED_TEMPLATE = "✅ 已退订标签：{tags}"
"""退订成功回复模板。"""

SUBSCRIBE_NOT_FOUND_TEMPLATE = "⚠️ 未订阅过，跳过：{tags}"
"""退订不存在回复模板。"""

HELP_TEXT = (
    "🤖 AI 知识库机器人指令：\n"
    "\n"
    "/search <关键词> [tag:标签] [from:日期] [to:日期] — 搜索知识条目\n"
    "/today — 今日新增知识简报\n"
    "/top [N] — 创新评分排行榜（默认 5，最多 20）\n"
    "/subscribe [标签...] — 订阅标签；加「取消」前缀退订\n"
    "/help — 显示本帮助\n"
    "\n"
    "也支持自然语言，例如：\n"
    "「搜索 llm agent」「今天的简报」「热门项目」「订阅 rag」\n"
    "\n"
    "权限说明：搜索类指令需 READ 权限（默认），订阅需 WRITE 权限。"
)
"""help 指令回复文本。"""

_INTENT_REQUIRED_PERMISSION: dict[Intent, Permission] = {
    Intent.SEARCH: Permission.READ,
    Intent.TODAY: Permission.READ,
    Intent.TOP: Permission.READ,
    Intent.SUBSCRIBE: Permission.WRITE,
    Intent.HELP: Permission.READ,
}
"""各意图所需最低权限：订阅需 WRITE，其余 READ。"""

_INTENT_ACTION: dict[Intent, str] = {
    Intent.SUBSCRIBE: "订阅管理",
    Intent.SEARCH: "搜索",
    Intent.TODAY: "今日简报",
    Intent.TOP: "排行榜",
    Intent.HELP: "帮助",
}
"""意图在权限提示中的动作描述。"""


class KnowledgeBot:
    """知识库交互主入口：意图识别 → 权限校验 → 指令分发。

    组合 ``KnowledgeSearchEngine``（检索）、``SubscriptionManager``
    （订阅）与 ``PermissionManager``（权限）。``handle_message`` 是
    统一入口，返回回复文本；不负责消息发送。
    """

    def __init__(
        self,
        knowledge_dir: Path | None = None,
        subscriptions_path: Path | None = None,
        permissions_path: Path | None = None,
    ) -> None:
        """初始化机器人及三个子模块。

        Args:
            knowledge_dir: 知识条目目录；``None`` 时用主仓库默认目录。
            subscriptions_path: 订阅状态文件路径；``None`` 用默认。
            permissions_path: 权限状态文件路径；``None`` 用默认。
        """
        self.search_engine = KnowledgeSearchEngine(knowledge_dir)
        self.subscriptions = SubscriptionManager(subscriptions_path)
        self.permissions = PermissionManager(permissions_path)
        self._handlers: dict[Intent, Callable[[str, str], str]] = {
            Intent.SEARCH: self._handle_search,
            Intent.TODAY: self._handle_today,
            Intent.TOP: self._handle_top,
            Intent.SUBSCRIBE: self._handle_subscribe,
            Intent.HELP: self._handle_help,
        }

    def handle_message(self, user_id: str, text: str) -> str:
        """处理一条用户消息并返回回复文本。

        流程：``recognize_intent`` 识别意图 → 按意图校验最低权限 →
        分发到对应处理器。权限不足时直接返回拒绝提示，不执行处理器。

        Args:
            user_id: 用户标识（如 Telegram chat id）。
            text: 用户原始消息文本。

        Returns:
            回复文本（绝不因内部错误抛出给调用方的用户侧异常由各
            处理器兜底；识别失败返回引导语）。
        """
        intent, args = recognize_intent(text)
        logger.info(
            "[KnowledgeBot] user=%s intent=%s args=%r", user_id, intent.value, args
        )
        handler = self._handlers.get(intent)
        if handler is None:
            return UNKNOWN_INTENT_REPLY
        required = _INTENT_REQUIRED_PERMISSION.get(intent, Permission.READ)
        if not self.permissions.check(user_id, required):
            current = self.permissions.get_permission(user_id)
            logger.info(
                "[KnowledgeBot] user=%s 权限不足，需要 %s，实际 %s",
                user_id,
                required.value,
                current.value,
            )
            return PERMISSION_DENIED_TEMPLATE.format(
                action=_INTENT_ACTION.get(intent, "该操作"),
                required=required.value.upper(),
                current=current.value.upper(),
            )
        return handler(user_id, args)

    def _handle_search(self, user_id: str, args: str) -> str:
        """处理搜索指令：解析过滤项并调用搜索引擎。

        Args:
            user_id: 用户标识（用于日志）。
            args: 参数串，见 ``_parse_search_args``。

        Returns:
            回复文本。
        """
        keywords, tags, date_from, date_to = _parse_search_args(args)
        if not keywords and not tags and date_from is None and date_to is None:
            return SEARCH_USAGE
        try:
            articles = self.search_engine.search(
                keywords=keywords,
                tags=tags,
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError as e:
            return f"⚠️ 参数错误：{e}\n{SEARCH_USAGE}"
        if not articles:
            return EMPTY_SEARCH_REPLY
        conditions = []
        if keywords:
            conditions.append("关键词 " + "/".join(keywords))
        if tags:
            conditions.append("标签 " + "/".join(tags))
        if date_from or date_to:
            conditions.append(f"日期 {date_from or '…'} ~ {date_to or '…'}")
        body = "\n".join(
            _format_article_line(index, article)
            for index, article in enumerate(articles, start=1)
        )
        return f"🔍 {'，'.join(conditions)}，共 {len(articles)} 条：\n{body}"

    def _handle_today(self, user_id: str, args: str) -> str:
        """处理今日简报指令：返回今天（本地时区）采集的条目列表。

        Args:
            user_id: 用户标识（用于日志）。
            args: 忽略（该指令无参数）。

        Returns:
            回复文本。
        """
        articles = self.search_engine.today()
        if not articles:
            today_str = datetime.now().astimezone().strftime("%Y-%m-%d")
            return EMPTY_TODAY_TEMPLATE.format(date=today_str)
        body = "\n".join(
            _format_article_line(index, article)
            for index, article in enumerate(articles, start=1)
        )
        return f"📚 今日新增 {len(articles)} 条：\n{body}"

    def _handle_top(self, user_id: str, args: str) -> str:
        """处理排行榜指令：按创新评分返回前 N 条。

        Args:
            user_id: 用户标识（用于日志）。
            args: 可选数字 N，缺省 5，超过上限截断为 20。

        Returns:
            回复文本。
        """
        n = DEFAULT_TOP_N
        if args.strip():
            try:
                n = int(args.strip())
            except ValueError:
                return TOP_USAGE
            if n < 1:
                return TOP_USAGE
            n = min(n, MAX_TOP_N)
        articles = self.search_engine.top(n=n)
        if not articles:
            return EMPTY_TOP_REPLY
        body = "\n".join(
            _format_article_line(index, article)
            for index, article in enumerate(articles, start=1)
        )
        return f"🏆 创新评分 Top {len(articles)}：\n{body}"

    def _handle_subscribe(self, user_id: str, args: str) -> str:
        """处理订阅指令：无参数查看、含退订前缀退订、否则添加订阅。

        Args:
            user_id: 用户标识。
            args: 标签参数串。

        Returns:
            回复文本。
        """
        tokens = args.split()
        if not tokens:
            tags = self.subscriptions.list_subscriptions(user_id)
            if not tags:
                return SUBSCRIBE_EMPTY_REPLY
            items = "\n".join(f"- {tag}" for tag in tags)
            return SUBSCRIBE_LIST_TEMPLATE.format(n=len(tags), items=items)

        if tokens[0].lower() in _UNSUBSCRIBE_PREFIXES:
            targets = [tag for tag in map(_normalize_tag, tokens[1:]) if tag]
            if not targets:
                return SUBSCRIBE_USAGE
            removed = [
                tag
                for tag in targets
                if self.subscriptions.remove_subscription(user_id, tag)
            ]
            missing = [tag for tag in targets if tag not in removed]
            parts = []
            if removed:
                parts.append(SUBSCRIBE_REMOVED_TEMPLATE.format(tags="、".join(removed)))
            if missing:
                parts.append(
                    SUBSCRIBE_NOT_FOUND_TEMPLATE.format(tags="、".join(missing))
                )
            return "\n".join(parts)

        targets = [tag for tag in map(_normalize_tag, tokens) if tag]
        if not targets:
            return SUBSCRIBE_USAGE
        added = [
            tag for tag in targets if self.subscriptions.add_subscription(user_id, tag)
        ]
        duplicated = [tag for tag in targets if tag not in added]
        parts = []
        if added:
            parts.append(SUBSCRIBE_ADDED_TEMPLATE.format(tags="、".join(added)))
        if duplicated:
            parts.append(SUBSCRIBE_DUP_TEMPLATE.format(tags="、".join(duplicated)))
        return "\n".join(parts)

    def _handle_help(self, user_id: str, args: str) -> str:
        """处理帮助指令：返回固定帮助文本。

        Args:
            user_id: 用户标识（用于日志）。
            args: 忽略。

        Returns:
            回复文本。
        """
        return HELP_TEXT
