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
from datetime import datetime, timedelta, timezone
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
"""generate_daily_digest 平铺模式默认取前 N 篇。"""

DEFAULT_PER_GROUP_TOP_N = 2
"""generate_daily_digest 分组模式每个 category 默认取前 N 篇。"""

MAX_BRIEF_CHARS = 80
"""一句话简介（article_brief）长度上限。"""

MAX_TELEGRAM_SUMMARY_CHARS = 500
"""单篇 Telegram 渲染时 summary 截断阈值。"""

MAX_TELEGRAM_TITLE_CHARS = 120
"""单篇 Telegram 渲染时标题截断阈值。"""

MAX_TELEGRAM_MESSAGE_CHARS = 4096
"""Telegram 单条消息长度上限（以字符数近似，另预留省略提示空间）。"""

_TELEGRAM_OMITTED_RESERVE = 100
"""digest Telegram 消息为省略提示行预留的字符数。"""

TELEGRAM_OMITTED_TEMPLATE = "…（篇幅超限，已省略 {n} 段）"
"""digest Telegram 消息超限丢块后附加的提示模板。"""

UNCATEGORIZED_LABEL = "未分类"
"""category 缺失时的分组名。"""

EMPTY_DIGEST_TEMPLATE = "📭 {date} 暂无新增知识条目"
"""当日无文章时的提示文案模板。"""

DIGEST_TITLE_TEMPLATE = "📚 AI 知识库日报 · {date}"
"""digest 标题模板。"""

WEEKLY_DIGEST_TITLE_TEMPLATE = "📚 AI 知识库周报 · {range}"
"""跨日期周报标题模板（``{range}`` 形如 ``2026-10-25 ~ 2026-10-31``）。"""

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


def _truncate_chars(text: str, max_chars: int) -> str:
    """按字符数截断文本，超出时尾部追加 ``…``。

    Args:
        text: 原文本。
        max_chars: 截断上限（含省略号计入前的原文部分）。

    Returns:
        原文本（不超限时）或截断加省略号后的文本。

    Raises:
        ValueError: ``max_chars`` 非正。
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars 必须为正整数，收到: {max_chars}")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def article_brief(article: Article) -> str:
    """提取一句话简介，供简报等紧凑场景替代长 ``summary``。

    取值优先级：``key_insight``（V3 早期字段，兼容保留，现行数据为空）
    > ``key_points`` 首个非空项（现行数据 83% 覆盖）> 截断的 ``summary``。

    Args:
        article: 知识条目。

    Returns:
        不超过 ``MAX_BRIEF_CHARS`` 字符的简介；全空时返回占位文案。
    """
    insight = str(article.get("key_insight") or "").strip()
    if insight:
        return _truncate_chars(insight, MAX_BRIEF_CHARS)

    key_points = article.get("key_points")
    if isinstance(key_points, list):
        for item in key_points:
            text = str(item).strip()
            if text:
                return _truncate_chars(text, MAX_BRIEF_CHARS)

    summary = str(article.get("summary") or "").strip()
    if summary:
        return _truncate_chars(summary, MAX_BRIEF_CHARS)
    return "（暂无摘要）"


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

    等价于日期窗口 ``[date_str, date_str]`` 的 :func:`_load_articles_between`，
    保留原签名供既有调用方与测试使用。

    Args:
        knowledge_dir: 知识条目目录。
        date_str: ``YYYY-MM-DD`` 格式日期。

    Returns:
        解析成功的条目与文件名二元组列表。
    """
    return _load_articles_between(knowledge_dir, date_str, date_str)


def _load_articles_between(
    knowledge_dir: Path, since: str, until: str
) -> list[tuple[Article, str]]:
    """扫描目录中文件名日期前缀落在 ``[since, until]``（含端点）的条目。

    ``index.json`` / ``recommended.json`` 等非日期命名文件的前缀字典序
    大于任何 ``YYYY-MM-DD`` 字符串，天然被窗口过滤排除。单个文件解析
    失败仅告警跳过，不中断整体 digest。

    Args:
        knowledge_dir: 知识条目目录。
        since: 起始日期（``YYYY-MM-DD``，含）。
        until: 结束日期（``YYYY-MM-DD``，含）。

    Returns:
        解析成功的条目与文件名二元组列表，按文件名升序。
    """
    articles: list[tuple[Article, str]] = []
    for filepath in sorted(knowledge_dir.glob("*.json")):
        if not (since <= filepath.name[:10] <= until):
            continue
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


def json_to_markdown(
    article: Article, brief: bool = False, heading_level: int = 2
) -> str:
    """单条知识条目 → 通用 Markdown。

    呈现字段：标题（链接）、来源、采集日期、评分（🟢🟟🔴）、标签、
    摘要、亮点（``key_insight`` 或 ``key_points``）、原文链接。

    Args:
        article: 知识条目。
        brief: True 时正文用一句话简介（``article_brief``）替代长
            ``summary``，并省略 💡 亮点行（简介本身即亮点）。
        heading_level: 条目标题的 Markdown 层级（1-6），分组简报中
            条目为 3 级、组标题为 2 级。

    Returns:
        Markdown 格式文本。
    """
    title = str(article.get("title") or "").strip() or "（无标题）"
    url = _article_url(article)
    score = normalized_score(article)
    source = str(article.get("source") or "unknown")
    tags = _article_tags(article)

    heading_prefix = "#" * max(1, min(heading_level, 6))
    heading = f"[{title}]({url})" if url else title
    tag_text = " ".join(f"`{tag}`" for tag in tags)

    if brief:
        body = article_brief(article)
        insight = ""
    else:
        body = _article_summary(article)
        insight = str(article.get("key_insight") or "").strip()
        if not insight:
            key_points = article.get("key_points")
            if isinstance(key_points, list) and key_points:
                insight = str(key_points[0]).strip()

    lines = [
        f"{heading_prefix} {heading}",
        "",
        f"**来源**：{source} ｜ **日期**：{_article_date(article)} ｜ "
        f"**评分**：{_score_emoji(score)} {score:.1f}",
    ]
    if tag_text:
        lines.append("")
        lines.append(f"**标签**：{tag_text}")
    lines.append("")
    lines.append(body)
    if insight:
        lines.append("")
        lines.append(f"> 💡 {insight}")
    if url:
        lines.append("")
        lines.append(f"**原文**：{url}")
    return "\n".join(lines)


def json_to_telegram(article: Article, brief: bool = False) -> str:
    """单条知识条目 → Telegram MarkdownV2 文本。

    正文全部经 MarkdownV2 转义；标题渲染为链接并加粗，附摘要、评分、
    来源与标签。标签中的空格替换为下划线（空格会打断 ``#tag`` 的连续性）。
    标题与摘要分别截断至 ``MAX_TELEGRAM_TITLE_CHARS`` /
    ``MAX_TELEGRAM_SUMMARY_CHARS``，保证单篇远低于单条消息 4096 上限，
    完整内容经文末原文链接查看。

    Args:
        article: 知识条目。
        brief: True 时正文用一句话简介（``article_brief``）替代长摘要。

    Returns:
        ``parse_mode=MarkdownV2`` 可发送的文本。
    """
    title = _truncate_chars(
        str(article.get("title") or "").strip() or "（无标题）",
        MAX_TELEGRAM_TITLE_CHARS,
    )
    url = _article_url(article)
    score = normalized_score(article)
    source = _escape_telegram(str(article.get("source") or "unknown"))

    if brief:
        body = article_brief(article)
    else:
        body = _truncate_chars(_article_summary(article), MAX_TELEGRAM_SUMMARY_CHARS)

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
        _escape_telegram(body),
        f"{_score_emoji(score)} {_escape_telegram(f'{score:.1f}')} ｜ 来源：{source}",
    ]
    if tags:
        lines.append(tags)
    if url:
        # 独立成行的 URL 处于正文上下文，须全量转义（含 "."）；
        # 仅链接目标 (...) 内才只需转义 "\" 与 ")"
        lines.append(_escape_telegram(url))
    return "\n".join(lines)


def json_to_weixin(
    article: Article, brief: bool = False, heading_level: int = 2
) -> str:
    """单条知识条目 → 企业微信群机器人 Markdown 文本。

    企业微信 markdown 支持 ``[text](url)`` 链接、标题与三种内置字体色
    （``info`` 绿 / ``comment`` 灰 / ``warning`` 橙红），评分按档位染色：
    🟢→info、🟟→comment、🔴→warning。

    Args:
        article: 知识条目。
        brief: True 时正文用一句话简介（``article_brief``）替代长摘要。
        heading_level: 条目标题的 Markdown 层级（1-6），分组简报中
            条目为 3 级、组标题为 2 级。

    Returns:
        ``msgtype=markdown`` 的 ``content`` 文本。
    """
    title = str(article.get("title") or "").strip() or "（无标题）"
    url = _article_url(article)
    score = normalized_score(article)
    source = str(article.get("source") or "unknown")
    color = _WEIXIN_COLOR_BY_GRADE[score_grade(score)]

    heading_prefix = "#" * max(1, min(heading_level, 6))
    heading = f"[{title}]({url})" if url else title
    tags = " ".join(f"#{tag.replace(' ', '_')}" for tag in _article_tags(article))
    body = article_brief(article) if brief else _article_summary(article)

    lines = [
        f"{heading_prefix} {heading}",
        f'> 评分：<font color="{color}">{_score_emoji(score)} {score:.1f}</font>'
        f" ｜ 来源：{source} ｜ {_article_date(article)}",
        "",
        body,
    ]
    if tags:
        lines.append("")
        lines.append(f"标签：{tags}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 每日简报
# ---------------------------------------------------------------------------


def _normalize_date(date: str | None) -> str:
    """校验并归一化日期参数。

    Args:
        date: ``YYYY-MM-DD`` 格式日期；``None`` 取当前 UTC 日期
            （与 Organizer 落盘命名的时区一致）。

    Returns:
        ``YYYY-MM-DD`` 字符串。

    Raises:
        ValueError: ``date`` 非 ``YYYY-MM-DD`` 格式。
    """
    if date is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"date 需为 YYYY-MM-DD 格式，收到: {date!r}") from e
    return date


def _group_top_articles(
    ranked_articles: list[Article], per_group_top_n: int
) -> list[tuple[str, list[Article]]]:
    """将已按评分降序的条目按 ``category`` 动态分组，每组取前 N 篇。

    category 取值随分析产出动态变化（如 ``AI Application`` /
    ``Research`` 等），不做硬编码枚举；组间按组内最高分降序。

    Args:
        ranked_articles: 已按 ``normalized_score`` 降序排列的条目。
        per_group_top_n: 每组保留的条数上限。

    Returns:
        ``(category, 条目列表)`` 二元组列表，组间按组内最高分降序。
    """
    groups: dict[str, list[Article]] = {}
    for article in ranked_articles:
        category = str(article.get("category") or "").strip() or UNCATEGORIZED_LABEL
        groups.setdefault(category, []).append(article)
    sections = [
        (category, members[:per_group_top_n]) for category, members in groups.items()
    ]
    sections.sort(key=lambda pair: normalized_score(pair[1][0]), reverse=True)
    return sections


def _fit_telegram_message(header: str, blocks: list[str]) -> str:
    """拼接 digest Telegram 消息，超限时丢弃尾部整块并附省略提示。

    以逻辑块（分组简报为一组、平铺简报为一篇）为丢弃粒度，避免在块
    内部截断破坏 MarkdownV2 转义配对；总长控制在
    ``MAX_TELEGRAM_MESSAGE_CHARS`` 内。

    Args:
        header: 必须保留的标题行（已转义）。
        blocks: 已渲染的 Telegram 文本块。

    Returns:
        不超过消息上限的拼接文本。
    """
    kept: list[str] = []
    length = len(header)
    budget = MAX_TELEGRAM_MESSAGE_CHARS - _TELEGRAM_OMITTED_RESERVE
    for block in blocks:
        extra = len(block) + 2
        if length + extra > budget:
            break
        kept.append(block)
        length += extra

    omitted = len(blocks) - len(kept)
    parts = [header, *kept]
    if omitted:
        parts.append(_escape_telegram(TELEGRAM_OMITTED_TEMPLATE.format(n=omitted)))
    return "\n\n".join(parts)


def generate_daily_digest(
    knowledge_dir: str | Path | None = None,
    date: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    grouped: bool = True,
    per_group_top_n: int = DEFAULT_PER_GROUP_TOP_N,
    brief: bool = True,
) -> dict[str, str]:
    """生成某日知识条目的三渠道简报。

    用 ``Path.glob("{date}-*.json")`` 扫描目录，按归一化评分降序排序。
    ``grouped=True``（默认）按 ``category`` 动态分组、每组取前
    ``per_group_top_n`` 篇、组间按组内最高分降序；``grouped=False``
    平铺取全局前 ``top_n`` 篇。``brief=True``（默认）正文用一句话简介
    （``article_brief``）替代长 ``summary``，简报体积约为全文摘要的一半。

    Telegram 渠道逐篇限制标题/摘要长度，拼接后若整体超出单条消息
    4096 上限，按逻辑块从尾部丢弃并附省略提示；微信 markdown 的
    content 4096 字节上限由 ``publisher`` 负责分段，本模块不截断。

    Args:
        knowledge_dir: 知识条目目录；``None`` 时用主仓库默认路径
            （相对本模块定位，与调用方 cwd 无关）。
        date: ``YYYY-MM-DD`` 格式日期；``None`` 时取当前 UTC 日期。
        top_n: 平铺模式（``grouped=False``）取前 N 篇，需为正整数。
        grouped: 是否按 category 分组。
        per_group_top_n: 分组模式每组取前 N 篇，需为正整数。
        brief: 正文是否用一句话简介替代长摘要。

    Returns:
        ``{"markdown": ..., "telegram": ..., "weixin": ...}``；当日无
        文章时三个值均为 ``"📭 {date} 暂无新增知识条目"``。

    Raises:
        ValueError: ``date`` 非 ``YYYY-MM-DD`` 格式，或 ``top_n`` /
            ``per_group_top_n`` 非正。
    """
    if top_n <= 0:
        raise ValueError(f"top_n 必须为正整数，收到: {top_n}")
    if per_group_top_n <= 0:
        raise ValueError(f"per_group_top_n 必须为正整数，收到: {per_group_top_n}")

    date_str = _normalize_date(date)

    directory = (
        Path(knowledge_dir) if knowledge_dir is not None else DEFAULT_KNOWLEDGE_DIR
    )
    entries = _load_articles_by_date(directory, date_str)
    if not entries:
        logger.info("[Formatter] %s 无知识条目 (目录: %s)", date_str, directory)
        empty = EMPTY_DIGEST_TEMPLATE.format(date=date_str)
        return {"markdown": empty, "telegram": empty, "weixin": empty}

    return _assemble_digest(
        entries,
        DIGEST_TITLE_TEMPLATE.format(date=date_str),
        date_str,
        directory,
        top_n,
        grouped,
        per_group_top_n,
        brief,
    )


def generate_range_digest(
    knowledge_dir: str | Path | None = None,
    since: str | None = None,
    until: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    grouped: bool = True,
    per_group_top_n: int = DEFAULT_PER_GROUP_TOP_N,
    brief: bool = True,
) -> dict[str, str]:
    """生成日期窗口 ``[since, until]``（含端点）的三渠道周报。

    文件名日期前缀落在窗口内的全部条目按归一化评分降序参与分组/平铺
    选取，组装逻辑与 :func:`generate_daily_digest` 完全一致，仅标题与
    窗口标注不同。窗口内无文章时三渠道均为占位提示。

    Args:
        knowledge_dir: 知识条目目录；``None`` 时用主仓库默认路径。
        since: 起始日期（``YYYY-MM-DD``，含）；``None`` 取 ``until``
            前 6 天（近 7 天窗口）。
        until: 结束日期（``YYYY-MM-DD``，含）；``None`` 取当前 UTC 日期。
        top_n: 平铺模式（``grouped=False``）取前 N 篇，需为正整数。
        grouped: 是否按 category 分组。
        per_group_top_n: 分组模式每组取前 N 篇，需为正整数。
        brief: 正文是否用一句话简介替代长摘要。

    Returns:
        ``{"markdown": ..., "telegram": ..., "weixin": ...}``。

    Raises:
        ValueError: 日期非 ``YYYY-MM-DD`` 格式、``since`` 晚于 ``until``，
            或 ``top_n`` / ``per_group_top_n`` 非正。
    """
    if top_n <= 0:
        raise ValueError(f"top_n 必须为正整数，收到: {top_n}")
    if per_group_top_n <= 0:
        raise ValueError(f"per_group_top_n 必须为正整数，收到: {per_group_top_n}")

    until_str = _normalize_date(until)
    if since is None:
        since_str = (
            datetime.strptime(until_str, "%Y-%m-%d") - timedelta(days=6)
        ).strftime("%Y-%m-%d")
    else:
        since_str = _normalize_date(since)
    if since_str > until_str:
        raise ValueError(f"since 不得晚于 until: {since_str} > {until_str}")

    directory = (
        Path(knowledge_dir) if knowledge_dir is not None else DEFAULT_KNOWLEDGE_DIR
    )
    entries = _load_articles_between(directory, since_str, until_str)
    label = f"{since_str} ~ {until_str}"
    if not entries:
        logger.info(
            "[Formatter] %s 无知识条目 (目录: %s)", label, directory
        )
        empty = EMPTY_DIGEST_TEMPLATE.format(date=label)
        return {"markdown": empty, "telegram": empty, "weixin": empty}

    return _assemble_digest(
        entries,
        WEEKLY_DIGEST_TITLE_TEMPLATE.format(range=label),
        label,
        directory,
        top_n,
        grouped,
        per_group_top_n,
        brief,
    )


def _assemble_digest(
    entries: list[tuple[Article, str]],
    title: str,
    log_label: str,
    directory: Path,
    top_n: int,
    grouped: bool,
    per_group_top_n: int,
    brief: bool,
) -> dict[str, str]:
    """按归一化评分降序组装三渠道简报（日报与周报共用）。

    Args:
        entries: ``(article, filename)`` 二元组列表。
        title: 已格式化的完整标题行。
        log_label: 日志与统计使用的窗口标注（单日日期或日期区间）。
        directory: 知识条目目录（仅用于日志）。
        top_n: 平铺模式取前 N 篇。
        grouped: 是否按 category 分组。
        per_group_top_n: 分组模式每组取前 N 篇。
        brief: 正文是否用一句话简介。

    Returns:
        ``{"markdown": ..., "telegram": ..., "weixin": ...}``。
    """
    ranked = sorted(
        entries,
        key=lambda pair: (
            normalized_score(pair[0]),
            str(pair[0].get("collected_at") or ""),
        ),
        reverse=True,
    )

    if grouped:
        sections = _group_top_articles(
            [article for article, _ in ranked], per_group_top_n
        )
        selected = sum(len(members) for _, members in sections)

        md_sections: list[str] = []
        tg_sections: list[str] = []
        wx_sections: list[str] = []
        for category, members in sections:
            md_sections.append(
                f"## 🗂 {category}\n\n"
                + "\n\n".join(
                    json_to_markdown(a, brief=brief, heading_level=3) for a in members
                )
            )
            tg_sections.append(
                f"*🗂 {_escape_telegram(category)}*\n\n"
                + "\n\n".join(json_to_telegram(a, brief=brief) for a in members)
            )
            wx_sections.append(
                f"## 🗂 {category}\n\n"
                + "\n\n".join(
                    json_to_weixin(a, brief=brief, heading_level=3) for a in members
                )
            )
        markdown_result = f"# {title}\n\n" + "\n\n---\n\n".join(md_sections)
        telegram_result = _fit_telegram_message(_escape_telegram(title), tg_sections)
        weixin_result = f"## {title}\n\n" + "\n\n".join(wx_sections)
    else:
        top = [article for article, _ in ranked[:top_n]]
        selected = len(top)

        markdown_result = f"# {title}\n\n" + "\n\n---\n\n".join(
            json_to_markdown(a, brief=brief) for a in top
        )
        telegram_result = _fit_telegram_message(
            _escape_telegram(title),
            [json_to_telegram(a, brief=brief) for a in top],
        )
        weixin_result = f"## {title}\n\n" + "\n\n".join(
            json_to_weixin(a, brief=brief) for a in top
        )

    logger.info(
        "[Formatter] %s 简报生成：%d/%d 篇入选，分组=%s (目录: %s)",
        log_label,
        selected,
        len(entries),
        grouped,
        directory,
    )
    return {
        "markdown": markdown_result,
        "telegram": telegram_result,
        "weixin": weixin_result,
    }


# ---------------------------------------------------------------------------
# 索引快速预览
# ---------------------------------------------------------------------------


def digest_from_index(
    date: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    index_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """基于 ``index.json`` 的快速预览：某日 Top N 标题与评分。

    与 ``generate_daily_digest`` 的全量 glob 读取不同，本函数只读一次
    索引文件（组织者维护的目录页），按 ``file`` 名前缀过滤当日条目，
    ``innovation_score`` 降序、``None`` 排最后（与 Bot 检索约定一致）取
    前 ``top_n``。需要详情时调用方再按返回的 ``file`` 回读
    ``knowledge/articles/{file}`` 全文（延迟加载）。

    Args:
        date: ``YYYY-MM-DD`` 格式日期；``None`` 时取当前 UTC 日期。
        top_n: 取前 N 篇，需为正整数。
        index_path: 索引文件路径；``None`` 用默认
            ``knowledge/articles/index.json``。

    Returns:
        索引条目原样子集（``title`` / ``category`` / ``innovation_score`` /
        ``stars`` / ``tags`` / ``collected_at`` / ``file``），事实字段
        原值引用不做换算。

    Raises:
        ValueError: ``date`` 格式非法或 ``top_n`` 非正。
        FileNotFoundError: 索引文件不存在。
    """
    if top_n <= 0:
        raise ValueError(f"top_n 必须为正整数，收到: {top_n}")

    date_str = _normalize_date(date)
    path = (
        Path(index_path)
        if index_path is not None
        else DEFAULT_KNOWLEDGE_DIR / "index.json"
    )

    with open(path, encoding="utf-8") as f:
        try:
            loaded = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("[Formatter] 索引解析失败，返回空列表 %s: %s", path, e)
            return []
    if not isinstance(loaded, list):
        logger.warning("[Formatter] 索引结构异常（非列表），返回空列表 %s", path)
        return []

    prefix = f"{date_str}-"
    matched = [
        entry
        for entry in loaded
        if isinstance(entry, dict) and str(entry.get("file") or "").startswith(prefix)
    ]
    # 降序排序下 None 沉底：有效评分 rank=1 大于 None 的 rank=0
    matched.sort(
        key=lambda entry: (
            1 if _to_float(entry.get("innovation_score")) > 0 else 0,
            _to_float(entry.get("innovation_score")),
        ),
        reverse=True,
    )
    logger.info(
        "[Formatter] %s 索引预览：%d/%d 条命中 (索引: %s)",
        date_str,
        min(len(matched), top_n),
        len(matched),
        path,
    )
    return matched[:top_n]
