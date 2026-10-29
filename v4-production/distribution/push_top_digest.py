"""全库精选周报推送 — 每周日从全知识库选最值得看的 N 篇推送 Telegram。

入选三档（视角互补，标准从严）::

    A: ``innovation_score`` = 5                      AI 判定最强创新
    B: ``innovation_score`` >= 4 且 stars >= 10_000   技术 + 社区双验证
    C: 未评分且 stars >= 50_000                       历史高星仓库折算

修饰规则：同类目最多 :data:`CATEGORY_MAX` 篇（防大类刷屏）；同 title 去重；
已推荐过的条目（记录于 ``knowledge/recommended.json``）不再重复，精品池
逐周消化，清零后静默退出——不降档凑数。

用法::

    python v4-production/distribution/push_top_digest.py [--top 5] [--dry-run]

``--dry-run`` 只打印消息不发送、不写推荐记录。推送失败以退出码 1 结束，
CI 据此跳过推荐记录的提交（避免"标记已推但实际没收到"）。

目录名 ``v4-production`` 含连字符不能作包名，这里把自身父目录加入
``sys.path`` 后导入 ``distribution`` 包（与 ``tests/conftest.py`` 同法）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from distribution.formatter import (  # noqa: E402
    _fit_telegram_message,
    json_to_telegram,
)
from distribution.publisher import TelegramPublisher  # noqa: E402

logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
"""仓库根目录（v4-production/distribution 上两级）。"""

INDEX_PATH: Path = REPO_ROOT / "knowledge" / "articles" / "index.json"
"""知识库检索索引路径。"""

ARTICLES_DIR: Path = REPO_ROOT / "knowledge" / "articles"
"""知识条目目录（按 ``file`` 字段定位全文）。"""

RECOMMENDED_PATH: Path = REPO_ROOT / "knowledge" / "recommended.json"
"""已推荐记录：organizer 不会触碰的独立状态文件。"""

TIER_A: str = "A"
TIER_B: str = "B"
TIER_C: str = "C"
"""入选档位标识（A 创新分 5 / B 双验证 / C 高星折算）。"""

TIER_ORDER: dict[str, int] = {TIER_A: 0, TIER_B: 1, TIER_C: 2}
"""档位优先级（数字小者优先入选）。"""

MIN_SCORE_TIER_B: int = 4
"""B 档最低创新分。"""

MIN_STARS_TIER_B: int = 10_000
"""B 档最低 stars。"""

MIN_STARS_TIER_C: int = 50_000
"""C 档（未评分折算）最低 stars。"""

CATEGORY_MAX: int = 2
"""同类目每期最多入选篇数，保证类别视角多样。"""

DEFAULT_TOP: int = 5
"""默认每期推送篇数。"""

TIER_LABEL: dict[str, str] = {
    TIER_A: "创新分5",
    TIER_B: "分4+⭐1w",
    TIER_C: "⭐5w折算",
}
"""推送消息中的档位简注。"""


def _is_int(value: object) -> bool:
    """判断是否为不含 bool 的整数值。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _tier_of(entry: dict[str, Any]) -> str | None:
    """计算索引条目的入选档位，不达标返回 ``None``。"""
    score = entry.get("innovation_score")
    stars = entry.get("stars") or 0
    if _is_int(score) and score == 5:
        return TIER_A
    if _is_int(score) and score >= MIN_SCORE_TIER_B and stars >= MIN_STARS_TIER_B:
        return TIER_B
    if score is None and stars >= MIN_STARS_TIER_C:
        return TIER_C
    return None


def _load_recommended() -> list[dict[str, Any]]:
    """读取已推荐记录，缺失或损坏时按空列表处理。"""
    if not RECOMMENDED_PATH.is_file():
        return []
    try:
        with open(RECOMMENDED_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[TopDigest] 推荐记录读取失败，按空处理: %s", e)
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _save_recommended(records: list[dict[str, Any]]) -> None:
    """写入已推荐记录（UTF-8、缩进 2，与知识库 JSON 风格一致）。"""
    with open(RECOMMENDED_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def select_top(
    index: list[dict[str, Any]], recommended_files: set[str], top_n: int
) -> list[tuple[dict[str, Any], str]]:
    """从全库索引选出本期精选，返回 ``(索引条目, 档位)`` 列表。

    排序：档位 → 创新分 → stars → 新近度；同 title 只保留最优一条；
    同类目受 :data:`CATEGORY_MAX` 配额约束。
    """
    eligible: list[tuple[dict[str, Any], str]] = []
    seen_titles: set[str] = set()
    for entry in index:
        file_name = str(entry.get("file") or "")
        title = str(entry.get("title") or "").strip().lower()
        tier = _tier_of(entry)
        if tier is None or not file_name or file_name in recommended_files:
            continue
        if title and title in seen_titles:
            continue
        seen_titles.add(title)
        eligible.append((entry, tier))

    # 先按新近度降序，再稳定排序，保证同分档内新的在前
    eligible.sort(key=lambda p: str(p[0].get("collected_at") or ""), reverse=True)
    eligible.sort(
        key=lambda p: (
            TIER_ORDER[p[1]],
            -(p[0].get("innovation_score") or 0),
            -(p[0].get("stars") or 0),
        )
    )

    picked: list[tuple[dict[str, Any], str]] = []
    category_count: dict[str, int] = {}
    for entry, tier in eligible:
        category = str(entry.get("category") or "")
        if category_count.get(category, 0) >= CATEGORY_MAX:
            continue
        picked.append((entry, tier))
        category_count[category] = category_count.get(category, 0) + 1
        if len(picked) >= top_n:
            break
    return picked


def build_message(
    picked: list[tuple[dict[str, Any], str]], remaining: int
) -> str:
    """拼装 Telegram 精选消息（复用 formatter 的块渲染与超限裁剪）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = (
        f"🏆 全库精选 · {today} · 本期 {len(picked)} 篇 · 剩余池 {remaining} 条\n"
        "档位: A=创新分5 B=创新分4+⭐1w C=⭐5w折算 · 每类最多2篇 · 不重复推荐"
    )
    blocks: list[str] = []
    for entry, tier in picked:
        article_path = ARTICLES_DIR / str(entry.get("file"))
        try:
            with open(article_path, encoding="utf-8") as f:
                article: dict[str, Any] = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[TopDigest] 条目读取失败，跳过 %s: %s", article_path, e)
            continue
        block = json_to_telegram(article, brief=True)
        first_line, _, rest = block.partition("\n")
        blocks.append(f"{first_line}（档位 {TIER_LABEL[tier]}）\n{rest}")
    return _fit_telegram_message(header, blocks)


def main() -> int:
    """主流程：选片 → 推送 → 落推荐记录。

    Returns:
        0 表示推送成功或池空静默；1 表示推送失败（不写记录）。
    """
    parser = argparse.ArgumentParser(description="全库精选周报推送")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="每期篇数")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印消息，不发送不写记录"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-8s | %(name)s | %(message)s"
    )

    if not INDEX_PATH.is_file():
        logger.error("[TopDigest] 索引不存在: %s", INDEX_PATH)
        return 1
    with open(INDEX_PATH, encoding="utf-8") as f:
        index: list[dict[str, Any]] = [
            e for e in json.load(f) if isinstance(e, dict)
        ]

    recommended = _load_recommended()
    recommended_files = {str(r.get("file")) for r in recommended}
    picked = select_top(index, recommended_files, args.top)
    if not picked:
        logger.info("[TopDigest] 精品池已消化完，本期静默跳过")
        return 0

    tier_pool = [
        e for e in index if e.get("file") not in recommended_files and _tier_of(e)
    ]
    remaining = max(0, len(tier_pool) - len(picked))
    message = build_message(picked, remaining)

    if args.dry_run:
        print(message)
        return 0

    result = asyncio.run(TelegramPublisher().send_digest(message))
    if not result.success:
        print(f"[telegram] FAIL: {result.error}")
        return 1
    print(f"[telegram] OK, 本期 {len(picked)} 篇")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recommended.extend(
        {
            "file": entry.get("file"),
            "title": entry.get("title"),
            "tier": tier,
            "recommended_at": now,
        }
        for entry, tier in picked
    )
    _save_recommended(recommended)
    logger.info("[TopDigest] 推荐记录已更新: %s", RECOMMENDED_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
