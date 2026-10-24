"""分发格式化模块 — 将知识条目 JSON 转为各渠道可发送的纯文本。

纯函数模块，不发起任何网络请求；发送动作由 ``publisher`` 负责。
发布前的 ``approved`` 状态检查同样归 ``publisher``，本模块只做格式化。

知识条目 schema 见仓库根 ``AGENTS.md``。URL 字段落盘名为 ``source_url``
（采集/分析中间态为 ``url``），两者均兼容；亮点字段兼容已废弃 V3 早期的
``key_insight`` 与现行 ``key_points``。

评分说明：现行 schema 无 ``relevance_score``（V3 早期字段，已于 2026-10
统一为 innovation 体系），本模块以 ``innovation_score``（1-5 整数）除以 5
归一化为 0-1，阈值 >=0.8 🟢 / >=0.6 🟟 / 否则 🔴。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

type Article = dict[str, Any]
"""单条知识条目（``knowledge/articles/*.json`` 的反序列化结果）。"""

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
"""主仓库根目录（v4-production/distribution → 上溯两级）。"""

DEFAULT_KNOWLEDGE_DIR = REPO_ROOT / "knowledge" / "articles"
"""知识条目目录默认值，相对本模块定位主仓库，与调用方 cwd 无关。"""

MAX_INNOVATION_SCORE = 5
"""innovation_score 满分，归一化除数。"""

SCORE_GREEN_THRESHOLD = 0.8
"""🟢 档下限（innovation_score >= 4）。"""

SCORE_YELLOW_THRESHOLD = 0.6
"""🟟 档下限（innovation_score >= 3）。"""

DEFAULT_TOP_N = 5
"""generate_daily_digest 默认取前 N 篇。"""

EMPTY_DIGEST_TEMPLATE = "📭 {date} 暂无新增知识条目"
"""当日无文章时的提示文案模板。"""

DIGEST_TITLE_TEMPLATE = "📚 AI 知识库日报 · {date}"
"""digest 标题模板。"""

_TELEGRAM_ESCAPE_PATTERN = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")
"""Telegram MarkdownV2 需转义的字符集。"""

_WEIXIN_COLOR_BY_GRADE = {"green": "info", "yellow": "comment", "red": "warning"}
"""微信 markdown 三档染色映射：info 绿 / comment 灰 / warning 橙红。"""


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _to_float(value: object) -> float:
    """安全地把任意值转为 ``float``，失败返回 ``0.0``。

    Args:
        value: 待转换值（可能是 ``int``、``float``、``str``、``None`` 等）。

    Returns:
        转换后的浮点数；无法转换时为 ``0.0``。
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def normalized_score(article: Article) -> float:
    """计算条目的 0-1 归一化评分：``innovation_score``（1-5 整数）除以 5。

    Args:
        article: 知识条目。

    Returns:
        0-1 之间的浮点数（保留两位小数）；字段缺失或非法取 0.0。
    """
    innovation = _to_float(article.get("innovation_score"))
    if innovation <= 0.0:
        return 0.0
    return round(min(innovation, float(MAX_INNOVATION_SCORE)) / MAX_INNOVATION_SCORE, 2)


def score_grade(score: float) -> str:
    """按阈值返回评分档位。

    Args:
        score: ``normalized_score`` 产出的 0-1 评分。

    Returns:
        ``"green"``（>=0.8）、``"yellow"``（>=0.6）或 ``"red"``。
    """
    if score >= SCORE_GREEN_THRESHOLD:
        return "green"
    if score >= SCORE_YELLOW_THRESHOLD:
        return "yellow"
    return "red"


def _score_emoji(score: float) -> str:
    """返回评分对应的三色 emoji。"""
    return {"green": "🟢", "yellow": "🟟", "red": "🔴"}[score_grade(score)]


def _article_url(article: Article) -> str:
    """取原文链接：优先落盘字段 ``source_url``，回退中间态字段 ``url``。"""
    return str(article.get("source_url") or article.get("url") or "")


def _article_date(article: Article) -> str:
    """取采集日期：``collected_at`` 前 10 位（``YYYY-MM-DD``）。"""
    return str(article.get("collected_at") or "")[:10] or "unknown"


def _article_summary(article: Article) -> str:
    """取摘要文本，空摘要返回占位文案。"""
    summary = str(article.get("summary") or "").strip()
    return summary or "（暂无摘要）"


def _article_tags(article: Article) -> list[str]:
    """取标签列表，过滤非字符串项。"""
    tags = article.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags]


def _escape_telegram(text: str) -> str:
    """转义 Telegram MarkdownV2 特殊字符 ``_*[]()~`>#+-=|{}.!``。

    Args:
        text: 待转义的明文。

    Returns:
        可安全嵌入 MarkdownV2 正文的文本。
    """
    return _TELEGRAM_ESCAPE_PATTERN.sub(r"\\\1", text)


def _escape_telegram_url(url: str) -> str:
    """转义 URL 中 MarkdownV2 链接目标内必须转义的 ``\\`` 与 ``)``。"""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _load_articles_by_date(
    knowledge_dir: Path, date_str: str
) -> list[tuple[Article, str]]:
    """扫描目录中 ``{date}-*.json`` 并反序列化为 ``(article, filename)`` 列表。

    单个文件解析失败仅告警跳过，不中断整体 digest。

    Args:
        knowledge_dir: 知识条目目录。
        date_str: ``YYYY-MM-DD`` 格式日期。

    Returns:
        解析成功的条目与文件名二元组列表。
    """
    articles: list[tuple[Article, str]] = []
    for filepath in sorted(knowledge_dir.glob(f"{date_str}-*.json")):
        try:
            with open(filepath, encoding="utf-8") as f:
                article = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[Formatter] 知识条目读取失败，已跳过 %s: %s", filepath, e)
            continue
        if isinstance(article, dict):
            articles.append((article, filepath.name))
    return articles


# ---------------------------------------------------------------------------
# 单篇格式化
# ---------------------------------------------------------------------------


def json_to_markdown(article: Article) -> str:
    """单条知识条目 → 通用 Markdown。

    呈现字段：标题（链接）、来源、采集日期、评分（🟢🟟🔴）、标签、
    摘要、亮点（``key_insight`` 或 ``key_points``）、原文链接。

    Args:
        article: 知识条目。

    Returns:
        Markdown 格式文本。
    """
    title = str(article.get("title") or "").strip() or "（无标题）"
    url = _article_url(article)
    score = normalized_score(article)
    source = str(article.get("source") or "unknown")
    tags = _article_tags(article)

    heading = f"[{title}]({url})" if url else title
    tag_text = " ".join(f"`{tag}`" for tag in tags)

    insight = str(article.get("key_insight") or "").strip()
    if not insight:
        key_points = article.get("key_points")
        if isinstance(key_points, list) and key_points:
            insight = str(key_points[0]).strip()

    lines = [
        f"## {heading}",
        "",
        f"**来源**：{source} ｜ **日期**：{_article_date(article)} ｜ "
        f"**评分**：{_score_emoji(score)} {score:.1f}",
    ]
    if tag_text:
        lines.append("")
        lines.append(f"**标签**：{tag_text}")
    lines.append("")
    lines.append(_article_summary(article))
    if insight:
        lines.append("")
        lines.append(f"> 💡 {insight}")
    if url:
        lines.append("")
        lines.append(f"**原文**：{url}")
    return "\n".join(lines)


def json_to_telegram(article: Article) -> str:
    """单条知识条目 → Telegram MarkdownV2 文本。

    正文全部经 MarkdownV2 转义；标题渲染为链接并加粗，附摘要、评分、
    来源与标签。标签中的空格替换为下划线（空格会打断 ``#tag`` 的连续性）。

    Args:
        article: 知识条目。

    Returns:
        ``parse_mode=MarkdownV2`` 可发送的文本。
    """
    title = str(article.get("title") or "").strip() or "（无标题）"
    url = _article_url(article)
    score = normalized_score(article)
    source = _escape_telegram(str(article.get("source") or "unknown"))

    if url:
        heading = f"*[{_escape_telegram(title)}]({_escape_telegram_url(url)})*"
    else:
        heading = f"*{_escape_telegram(title)}*"

    tags = " ".join(
        f"\\#{_escape_telegram(tag.replace(' ', '_'))}"
        for tag in _article_tags(article)
    )

    lines = [
        f"📌 {heading}",
        _escape_telegram(_article_summary(article)),
        f"{_score_emoji(score)} {_escape_telegram(f'{score:.1f}')} ｜ 来源：{source}",
    ]
    if tags:
        lines.append(tags)
    if url:
        lines.append(_escape_telegram_url(url))
    return "\n".join(lines)


def json_to_weixin(article: Article) -> str:
    """单条知识条目 → 企业微信群机器人 Markdown 文本。

    企业微信 markdown 支持 ``[text](url)`` 链接、标题与三种内置字体色
    （``info`` 绿 / ``comment`` 灰 / ``warning`` 橙红），评分按档位染色：
    🟢→info、🟟→comment、🔴→warning。

    Args:
        article: 知识条目。

    Returns:
        ``msgtype=markdown`` 的 ``content`` 文本。
    """
    title = str(article.get("title") or "").strip() or "（无标题）"
    url = _article_url(article)
    score = normalized_score(article)
    source = str(article.get("source") or "unknown")
    color = _WEIXIN_COLOR_BY_GRADE[score_grade(score)]

    heading = f"[{title}]({url})" if url else title
    tags = " ".join(f"#{tag.replace(' ', '_')}" for tag in _article_tags(article))

    lines = [
        f"## {heading}",
        f'> 评分：<font color="{color}">{_score_emoji(score)} {score:.1f}</font>'
        f" ｜ 来源：{source} ｜ {_article_date(article)}",
        "",
        _article_summary(article),
    ]
    if tags:
        lines.append("")
        lines.append(f"标签：{tags}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 每日简报
# ---------------------------------------------------------------------------


def generate_daily_digest(
    knowledge_dir: str | Path | None = None,
    date: str | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, str]:
    """生成某日知识条目的三渠道简报。

    用 ``Path.glob("{date}-*.json")`` 扫描目录，按归一化评分降序取前
    ``top_n`` 篇（同分按 ``collected_at`` 降序），分别渲染通用 Markdown、
    Telegram MarkdownV2 与企业微信 Markdown。

    Args:
        knowledge_dir: 知识条目目录；``None`` 时用主仓库默认路径
            （相对本模块定位，与调用方 cwd 无关）。
        date: ``YYYY-MM-DD`` 格式日期；``None`` 时取当前 UTC 日期
            （与 Organizer 落盘命名的时区一致）。
        top_n: 取前 N 篇，需为正整数。

    Returns:
        ``{"markdown": ..., "telegram": ..., "weixin": ...}``；当日无
        文章时三个值均为 ``"📭 {date} 暂无新增知识条目"``。

    Raises:
        ValueError: ``date`` 非 ``YYYY-MM-DD`` 格式或 ``top_n`` 非正。
    """
    if top_n <= 0:
        raise ValueError(f"top_n 必须为正整数，收到: {top_n}")

    if date is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_str = date
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"date 需为 YYYY-MM-DD 格式，收到: {date_str!r}") from e

    directory = (
        Path(knowledge_dir) if knowledge_dir is not None else DEFAULT_KNOWLEDGE_DIR
    )
    entries = _load_articles_by_date(directory, date_str)
    if not entries:
        logger.info("[Formatter] %s 无知识条目 (目录: %s)", date_str, directory)
        empty = EMPTY_DIGEST_TEMPLATE.format(date=date_str)
        return {"markdown": empty, "telegram": empty, "weixin": empty}

    top = sorted(
        entries,
        key=lambda pair: (
            normalized_score(pair[0]),
            str(pair[0].get("collected_at") or ""),
        ),
        reverse=True,
    )[:top_n]
    articles = [article for article, _ in top]

    title = DIGEST_TITLE_TEMPLATE.format(date=date_str)
    markdown_body = "\n\n---\n\n".join(json_to_markdown(a) for a in articles)
    telegram_body = "\n\n".join(json_to_telegram(a) for a in articles)
    weixin_body = "\n\n".join(json_to_weixin(a) for a in articles)

    logger.info(
        "[Formatter] %s 简报生成：%d/%d 篇入选 (目录: %s)",
        date_str,
        len(articles),
        len(entries),
        directory,
    )
    return {
        "markdown": f"# {title}\n\n{markdown_body}",
        "telegram": f"{_escape_telegram(title)}\n\n{telegram_body}",
        "weixin": f"## {title}\n\n{weixin_body}",
    }
