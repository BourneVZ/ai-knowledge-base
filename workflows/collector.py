"""Collector 节点 — 从 GitHub Search API 与 RSS 采集 AI 相关内容。

信息源（并集）：
- GitHub Search API
  （搜索词由 ``config.yaml`` 的 ``collection.github_search_query`` 决定）
- RSS（源列表由 ``rss_sources.yaml`` 配置，每源/总量上限由 ``config.yaml`` 控制）

产出报告式摘要（``title``/``url``/``description``/``stars`` 等），不携带原始全量响应。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError

from core.config import load_config
from core.rss_sources import load_rss_sources
from core.utils import parse_rss_items, strip_html
from safeguards.security import sanitize_input
from workflows.state import KBState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

GITHUB_API_URL: str = "https://api.github.com/search/repositories"
"""GitHub Search API 端点（查询参数动态拼接）。"""

DEFAULT_SEARCH_QUERY: str = "ai OR llm OR agent in:topics"
"""config 未提供 github_search_query 时的兜底搜索词。"""

DEFAULT_GITHUB_LIMIT: int = 10
"""plan 未提供 github_limit 时的 GitHub 每页采集量兜底值。"""

DEFAULT_RSS_PER_SOURCE_LIMIT: int = 3
"""config 未提供 rss_per_source_limit 时的每源兜底值。"""

DEFAULT_RSS_TOTAL_LIMIT: int = 10
"""config 未提供 rss_total_limit 时的总量兜底值。"""

DEFAULT_TIMEOUT: int = 15
"""网络请求超时（秒）。"""

MAX_RETRIES: int = 3
"""采集请求最大尝试次数（含首次）。"""

BROWSER_UA: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""RSS 抓取默认 User-Agent。

urllib 默认 UA（``Python-urllib/3.x``）会被 Cloudflare（openai.com）与
Reddit 的反爬直接拒绝（403/429），GitHub Actions 数据中心 IP 上尤甚。
"""

RETRY_BACKOFF_BASE_SECONDS: float = 1.0
"""重试指数退避基数：第 n 次失败后等待 ``base * 2**n`` 秒；429 优先尊重
``Retry-After`` 响应头。"""

DESCRIPTION_MAX_LENGTH: int = 300
"""RSS 描述最大长度（字符），截断过长描述以控制下游分析 prompt 体积。"""

SANITIZED_FIELDS: tuple[str, ...] = ("title", "description")
"""进入 LLM prompt 前需要清洗的外部自由文本字段。

GitHub description / RSS title 等均为不受信外部输入，直接拼进 prompt 存在
Prompt 注入风险（OWASP LLM Top 1）；url / stars / collected_at 等结构化
事实字段不参与清洗。
"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _http_get(url: str, headers: dict[str, str] | None = None) -> str:
    """带指数退避重试的 HTTP GET（urllib）。

    默认携带浏览器 User-Agent（调用方显式传入的 headers 优先级更高）；
    失败后按 ``base * 2**attempt`` 退避重试，HTTP 429 优先尊重
    ``Retry-After`` 响应头。

    Args:
        url: 请求地址。
        headers: 可选请求头（覆盖同名默认头）。

    Returns:
        响应文本。

    Raises:
        RuntimeError: 全部尝试失败。
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            merged_headers = {"User-Agent": BROWSER_UA}
            if headers:
                merged_headers.update(headers)
            req = urllib.request.Request(url, headers=merged_headers)
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                return str(resp.read().decode("utf-8"))
        except HTTPError as e:
            last_error = e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after is not None and str(retry_after).isdigit():
                delay = float(str(retry_after))
            else:
                delay = RETRY_BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "[Collector] 请求失败 (attempt %d/%d): %s — HTTP %s，"
                "%.1fs 后重试",
                attempt + 1,
                MAX_RETRIES,
                url,
                e.code,
                delay,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
        except Exception as e:
            last_error = e
            delay = RETRY_BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                "[Collector] 请求失败 (attempt %d/%d): %s — %s: %s，"
                "%.1fs 后重试",
                attempt + 1,
                MAX_RETRIES,
                url,
                type(e).__name__,
                e,
                delay,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
    raise RuntimeError(f"采集请求失败: {url}") from last_error


def _collect_github(query: str, github_limit: int) -> list[dict[str, Any]]:
    """从 GitHub Search API 采集 AI 相关仓库。

    Args:
        query: GitHub 搜索词（q 参数）。
        github_limit: 每页采集条数上限。

    Returns:
        采集到的仓库列表。
    """
    params = urllib.parse.urlencode(
        {"q": query, "sort": "stars", "per_page": github_limit}, safe=":"
    )
    url = f"{GITHUB_API_URL}?{params}"
    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        data: dict[str, object] = json.loads(_http_get(url, headers=headers))
    except Exception as e:
        logger.error("[Collector] GitHub 采集失败: %s", e)
        return []

    sources: list[dict[str, Any]] = []
    items = data.get("items")
    if isinstance(items, list):
        for repo in items:
            if not isinstance(repo, dict):
                continue
            sources.append(
                {
                    "source": "github",
                    "title": repo.get("full_name", ""),
                    "url": repo.get("html_url", ""),
                    "description": repo.get("description", ""),
                    "stars": repo.get("stargazers_count", 0),
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    return sources


def _collect_rss(per_source_limit: int, total_limit: int) -> list[dict[str, Any]]:
    """从配置的 RSS 源采集 AI 相关内容（同时控制每源上限与总量上限）。

    Args:
        per_source_limit: 每个 feed 最多取几条。
        total_limit: 所有 feed 累计最多取几条。

    Returns:
        采集到的条目列表。
    """
    collected_at = datetime.now(timezone.utc).isoformat()
    sources: list[dict[str, Any]] = []
    for name, feed_url in load_rss_sources().items():
        if len(sources) >= total_limit:
            break
        try:
            xml_text = _http_get(feed_url)
        except Exception as e:
            logger.error("[Collector] RSS 源 %s 请求失败: %s", name, e)
            continue
        per_source_count = 0
        for item in parse_rss_items(xml_text):
            if len(sources) >= total_limit or per_source_count >= per_source_limit:
                break
            title = item.get("title", "")
            link = item.get("link", "")
            if not title or not link:
                continue
            sources.append(
                {
                    "source": f"rss/{name}",
                    "title": title,
                    "url": link,
                    "description": strip_html(item.get("description", ""))[
                        :DESCRIPTION_MAX_LENGTH
                    ],
                    "stars": None,
                    "collected_at": collected_at,
                }
            )
            per_source_count += 1
    return sources


def _persist_raw(source_name: str, items: list[dict[str, Any]]) -> str:
    """将原始采集数据落盘到 ``knowledge/raw/``，返回相对 ``raw_path``。

    文件名带时间戳，避免同日多次采集互相覆盖（遵守「禁止覆盖 raw 记录」约束）。

    Args:
        source_name: 来源标识（``github``、``rss``）。
        items: 该来源采集到的原始条目列表。

    Returns:
        相对项目根目录的 ``raw_path`` 字符串。
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw_dir = os.path.join(base_dir, "knowledge", "raw")
    os.makedirs(raw_dir, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    filename = f"{source_name}-{stamp}.json"
    filepath = os.path.join(raw_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    logger.info("[Collector] 原始数据已落盘: %s (%d 条)", filename, len(items))
    return f"knowledge/raw/{filename}"


def _sanitize_sources(sources: list[dict[str, Any]]) -> None:
    """对外部输入做注入检测与清洗（原地修改 ``title`` / ``description``）。

    在原始数据落盘 *之后* 调用：``knowledge/raw/`` 保留原始记录（遵守
    「raw 记录不可覆盖」约束），只有进入下游 LLM prompt 的工作副本被清洗。

    Args:
        sources: 采集结果列表（原地清洗）。
    """
    for source in sources:
        for key in SANITIZED_FIELDS:
            raw = str(source.get(key, "") or "")
            cleaned, warnings = sanitize_input(raw)
            if warnings:
                logger.warning(
                    "[Collector] 输入安全警告 (%s.%s): %s",
                    source.get("source", ""),
                    key,
                    warnings,
                )
            source[key] = cleaned


# ---------------------------------------------------------------------------
# 节点定义
# ---------------------------------------------------------------------------


def collect_node(state: KBState) -> dict[str, Any]:
    """采集节点：采集 GitHub Search + RSS，产出 ``sources``。

    Args:
        state: 工作流共享状态。

    Returns:
        部分状态更新，仅包含 ``sources`` 字段。
    """
    logger.info("[Collector] 开始采集 GitHub + RSS")
    plan = state.get("plan") or {}
    cfg = load_config()
    coll = cfg.get("collection", {})

    github_limit = int(plan.get("github_limit", DEFAULT_GITHUB_LIMIT))
    github_query = str(coll.get("github_search_query", DEFAULT_SEARCH_QUERY))
    # 时间窗限定"近期新建仓库"：sort=stars 不加时间窗只会反复抓到
    # 史上最高星的老牌仓库，抓不到新崛起的项目
    created_within_days = int(coll.get("github_created_within_days", 0))
    if created_within_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=created_within_days)).strftime(
            "%Y-%m-%d"
        )
        github_query = f"{github_query} created:>{cutoff}"
        logger.info(
            "[Collector] GitHub 时间窗: 近 %d 天新建（created:>%s）",
            created_within_days,
            cutoff,
        )
    rss_per_source = int(coll.get("rss_per_source_limit", DEFAULT_RSS_PER_SOURCE_LIMIT))
    rss_total = int(coll.get("rss_total_limit", DEFAULT_RSS_TOTAL_LIMIT))

    github_sources = _collect_github(github_query, github_limit)
    rss_sources = _collect_rss(rss_per_source, rss_total)

    # 分来源落盘原始采集记录，并回填 raw_path（AGENTS.md 必填字段）
    github_raw = _persist_raw("github", github_sources)
    rss_raw = _persist_raw("rss", rss_sources)
    for source in github_sources:
        source["raw_path"] = github_raw
    for source in rss_sources:
        source["raw_path"] = rss_raw

    sources = github_sources + rss_sources
    # 原始记录已落盘；此处清洗进入下游 LLM 的文本，阻断 Prompt 注入
    _sanitize_sources(sources)
    logger.info("[Collector] 采集到 %d 条原始数据", len(sources))
    return {"sources": sources}
