#!/usr/bin/env python3
"""知识条目 5 维度质量评分工具。

用法:
    python hooks/check_quality.py <json_file> [json_file2 ...]
    python hooks/check_quality.py knowledge/articles/*.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CN_BUZZWORDS: frozenset[str] = frozenset(
    {
        "赋能",
        "抓手",
        "闭环",
        "打通",
        "全链路",
        "底层逻辑",
        "颗粒度",
        "对齐",
        "拉通",
        "沉淀",
        "强大的",
        "革命性的",
    }
)

EN_BUZZWORDS: frozenset[str] = frozenset(
    {
        "groundbreaking",
        "revolutionary",
        "revolutionize",
        "game-changing",
        "game-changer",
        "cutting-edge",
        "best-in-class",
        "disruptive",
        "paradigm-shift",
        "next-gen",
        "next-generation",
        "unprecedented",
        "world-class",
    }
)

STANDARD_TAGS: frozenset[str] = frozenset(
    {
        "ai",
        "llm",
        "agent",
        "rag",
        "inference",
        "fine-tuning",
        "open-source",
        "multimodal",
        "code-generation",
        "chatbot",
        "workflow",
        "lightweight",
        "enterprise-ready",
        "automation",
        "beginner-friendly",
        "tutorial",
        "high-performance",
        "data-analysis",
        "local-first",
        "nlp",
        "machine-learning",
        "deep-learning",
        "agent-framework",
        "mcp",
        "orchestration",
        "evaluation",
        "benchmark",
        "security",
        "privacy",
        "transformer",
        "embedding",
        "vector-database",
    }
)

TECH_KEYWORDS: frozenset[str] = frozenset(
    {
        "llm",
        "agent",
        "rag",
        "inference",
        "fine-tun",
        "transformer",
        "workflow",
        "embedding",
        "vector",
        "prompt",
        "token",
        "model",
        "train",
        "deploy",
        "pipeline",
        "plugin",
        "orchestr",
        "memory",
        "tool",
        "copilot",
        "codex",
        "gpt",
        "openai",
        "gemini",
        "deepseek",
        "qwen",
        "llama",
        "mistral",
        "api",
        "sdk",
        "mcp",
        "chat",
        "multimodal",
    }
)

VALID_STATUSES: frozenset[str] = frozenset(
    {"draft", "needs_review", "approved", "published", "rejected"}
)

SUMMARY_GRADE_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (50, 25),
    (35, 20),
    (20, 12),
    (0, 0),
)

TECH_KEYWORD_BONUS: int = 5
TECH_BONUS_THRESHOLD: int = 2

BAR_WIDTH: int = 30

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class DimensionScore:
    """单个维度的评分结果。"""

    name: str
    score: float
    max_score: int
    detail: str = ""


@dataclass
class QualityReport:
    """单个文件的质量报告。"""

    file_path: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    total_score: float = 0.0
    grade: str = "C"


# ---------------------------------------------------------------------------
# 文件收集（复用 validate_json 模式）
# ---------------------------------------------------------------------------


def collect_files(paths: list[str]) -> list[Path]:
    """根据输入路径收集所有 JSON 文件。

    支持三种输入形式:
    - 目录路径: 递归收集目录下所有 ``.json`` 文件。
    - 通配符: 如 ``knowledge/articles/*.json``。
    - 单文件: 直接检查是否为 ``.json`` 文件。

    Args:
        paths: 文件路径、目录路径或通配符列表。

    Returns:
        排序后的 ``Path`` 对象列表。
    """
    files: set[Path] = set()
    for raw_path in paths:
        p = Path(raw_path)

        if "*" in raw_path or "?" in raw_path:
            expanded = sorted(Path().glob(raw_path))
            if not expanded:
                print(
                    f"警告: 通配符 '{raw_path}' 未匹配到任何文件，已跳过。",
                    file=sys.stderr,
                )
            for fp in expanded:
                if fp.suffix == ".json" and fp.is_file():
                    files.add(fp)
        elif p.is_dir():
            directory_json_files = sorted(p.rglob("*.json"))
            if not directory_json_files:
                print(
                    f"警告: 目录 '{raw_path}' 下未找到任何 .json 文件，已跳过。",
                    file=sys.stderr,
                )
            for fp in directory_json_files:
                if fp.is_file():
                    files.add(fp)
        elif p.is_file():
            if p.suffix == ".json":
                files.add(p)
            else:
                print(
                    f"警告: '{raw_path}' 不是 .json 文件，已跳过。",
                    file=sys.stderr,
                )
        else:
            print(
                f"警告: '{raw_path}' 不是有效的文件或目录，已跳过。",
                file=sys.stderr,
            )

    return sorted(files)


# ---------------------------------------------------------------------------
# 各维度打分函数
# ---------------------------------------------------------------------------


def score_summary_quality(data: dict[str, object]) -> DimensionScore:
    """评分维度 1：摘要质量（满分 25）。"""
    summary = data.get("summary")
    if not isinstance(summary, str) or len(summary) == 0:
        return DimensionScore("摘要质量", 0, 25, "缺少摘要或摘要为空")

    length = len(summary)
    for threshold, score_val in SUMMARY_GRADE_THRESHOLDS:
        if length >= threshold:
            base = score_val
            break
    else:
        base = 0

    found_count = sum(1 for kw in TECH_KEYWORDS if kw in summary.lower())
    tech_bonus = TECH_KEYWORD_BONUS if found_count >= TECH_BONUS_THRESHOLD else 0

    final = min(25, base + tech_bonus)

    detail = (
        f"长度 {length} 字"
        f"{'（含 ' + str(found_count) + ' 个技术关键词，奖励 +' + str(tech_bonus) + '）' if tech_bonus else ''}"
    )
    return DimensionScore("摘要质量", final, 25, detail)


def score_technical_depth(data: dict[str, object]) -> DimensionScore:
    """评分维度 2：技术深度（满分 25）。

    V3 契约优先：``innovation_score`` / ``difficulty_score``（1-5 制，×5 映射）；
    兼容旧条目的顶层 ``score`` / ``metadata.score``（1-10 制，×2.5 映射）。
    """
    for key in ("innovation_score", "difficulty_score"):
        raw = data.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            clamped = max(1, min(5, int(raw)))
            return DimensionScore(
                "技术深度", clamped * 5, 25, f"{key}={clamped}，映射 1-5 × 5"
            )

    raw_score: object = data.get("score")
    if raw_score is None:
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            raw_score = metadata.get("score")

    if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
        return DimensionScore(
            "技术深度", 0, 25, "缺少评分字段（innovation_score / score 均无效）"
        )

    clamped = max(1, min(10, int(raw_score)))
    mapped = round(clamped * 2.5, 1)
    return DimensionScore(
        "技术深度", mapped, 25, f"原始 score={clamped}，映射公式 score × 2.5"
    )


def score_format_compliance(data: dict[str, object]) -> DimensionScore:
    """评分维度 3：格式规范（满分 20）。

    五项各 4 分：id、title、source_url、status、时间戳。
    """
    points = 0
    details: list[str] = []

    def _check(field: str, label: str, validator: object) -> None:
        nonlocal points
        value = data.get(field)
        try:
            if validator(value):  # type: ignore[operator]
                points += 4
                details.append(f"{label}(+4)")
            else:
                details.append(f"{label}(0): 格式无效")
        except Exception:
            details.append(f"{label}(0): 格式无效")

    id_val = data.get("id")
    if isinstance(id_val, str) and len(id_val) > 0:
        points += 4
        details.append("id(+4)")
    else:
        details.append("id(0): 缺失或为空")

    title_val = data.get("title")
    if isinstance(title_val, str) and len(title_val.strip()) > 0:
        points += 4
        details.append("title(+4)")
    else:
        details.append("title(0): 缺失或为空")

    url_val = data.get("source_url")
    if isinstance(url_val, str) and re.match(r"^https?://", url_val):
        points += 4
        details.append("source_url(+4)")
    else:
        details.append("source_url(0): 缺失或格式无效")

    status_val = data.get("status")
    if isinstance(status_val, str) and status_val in VALID_STATUSES:
        points += 4
        details.append("status(+4)")
    else:
        details.append("status(0): 缺失或值不合法")

    ts_present = False
    for ts_key in ("collected_at", "analyzed_at"):
        ts_val = data.get(ts_key)
        if isinstance(ts_val, str) and len(ts_val) > 0:
            ts_present = True
            break
    if ts_present:
        points += 4
        details.append("时间戳(+4)")
    else:
        details.append("时间戳(0): collected_at 与 analyzed_at 均缺失")

    return DimensionScore("格式规范", points, 20, " | ".join(details))


def score_tag_precision(data: dict[str, object]) -> DimensionScore:
    """评分维度 4：标签精度（满分 15）。

    1-5 个标签为佳（AGENTS.md 未设上限，analyzer 实际产出 3-5 个），
    超过 5 个按超出数扣分；非标准标签轻微扣分（最多 3 分）。
    """
    tags = data.get("tags")
    if not isinstance(tags, list) or len(tags) == 0:
        return DimensionScore("标签精度", 0, 15, "标签缺失或为空")

    valid_tags = [t for t in tags if isinstance(t, str)]
    if len(valid_tags) == 0:
        return DimensionScore("标签精度", 0, 15, "无有效字符串标签")

    standard_count = sum(1 for t in valid_tags if t.lower() in STANDARD_TAGS)
    non_standard_count = len(valid_tags) - standard_count

    score = 15
    if len(valid_tags) > 5:
        score -= len(valid_tags) - 5
    score -= min(3, non_standard_count)
    score = max(5, score)
    detail = (
        f"{len(valid_tags)} 个标签（标准 {standard_count}，非标准 {non_standard_count}）"
    )
    return DimensionScore("标签精度", score, 15, detail)


def score_buzzword_free(data: dict[str, object]) -> DimensionScore:
    """评分维度 5：空洞词检测（满分 15）。

    扫描 ``summary`` 与 ``description`` 中的中英文空洞词，命中即扣分。
    """
    texts: list[str] = []
    for key in ("summary", "description"):
        val = data.get(key)
        if isinstance(val, str):
            texts.append(val)

    joined = " ".join(texts)
    cn_hits = [w for w in CN_BUZZWORDS if w in joined]
    en_hits = [w for w in EN_BUZZWORDS if w in joined.lower()]
    total_hits = len(cn_hits) + len(en_hits)

    if total_hits == 0:
        return DimensionScore("空洞词检测", 15, 15, "未命中任何空洞词，满分")

    penalty = min(15, total_hits * 3)
    score = max(0, 15 - penalty)
    all_hits = cn_hits + en_hits
    return DimensionScore(
        "空洞词检测",
        score,
        15,
        f"命中 {total_hits} 个空洞词（{', '.join(all_hits[:5])}"
        f"{'…' if total_hits > 5 else ''}），扣 {penalty} 分",
    )


# ---------------------------------------------------------------------------
# 进度条 & 报告输出
# ---------------------------------------------------------------------------


def render_bar(score: float, max_score: int | float) -> str:
    """渲染得分进度条（█/░）。"""
    ratio = max(0.0, min(1.0, score / max_score))
    filled = int(ratio * BAR_WIDTH)
    empty = BAR_WIDTH - filled
    return "█" * filled + "░" * empty


def determine_grade(total: float) -> str:
    """根据总分返回等级 A / B / C。"""
    if total >= 80:
        return "A"
    elif total >= 60:
        return "B"
    return "C"


def print_report(report: QualityReport) -> None:
    """打印单个文件的质量评分报告。"""
    sep = "─" * 44
    print(f"\n文件: {report.file_path}")
    print(sep)

    for dim in report.dimensions:
        bar = render_bar(dim.score, dim.max_score)
        print(f"  {dim.name:　<6s}  {bar}  {dim.score:>5.1f}/{dim.max_score}")
        if dim.detail:
            print(f"         {dim.detail}")

    print(sep)
    grade_icon = {"A": "★", "B": "●", "C": "✗"}.get(report.grade, "?")

    print(f"  {grade_icon} 总分: {report.total_score:.1f}/100  等级: {report.grade}")
    print(sep)


def analyze_file(file_path: Path) -> QualityReport:
    """对单个 JSON 文件进行 5 维度质量评分。

    Args:
        file_path: JSON 文件路径。

    Returns:
        完整的 ``QualityReport``。
    """
    path_str = str(file_path)
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return QualityReport(
            file_path=path_str,
            dimensions=[DimensionScore("JSON 解析", 0, 100, "文件无法解析")],
            grade="C",
        )

    if not isinstance(data, dict):
        return QualityReport(
            file_path=path_str,
            dimensions=[DimensionScore("根对象", 0, 100, "非 JSON 对象")],
            grade="C",
        )

    dims = [
        score_summary_quality(data),
        score_technical_depth(data),
        score_format_compliance(data),
        score_tag_precision(data),
        score_buzzword_free(data),
    ]
    total = round(sum(d.score for d in dims), 1)
    grade = determine_grade(total)

    return QualityReport(
        file_path=path_str,
        dimensions=dims,
        total_score=total,
        grade=grade,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    """脚本入口。

    收集文件、逐文件评分、打印报告、汇总统计。
    含 C 级文件返回 1，否则返回 0。
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="知识条目 5 维度质量评分工具",
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="待评分的 JSON 文件（支持 *.json 和 ? 通配符）",
    )
    args = parser.parse_args()

    files = collect_files(args.files)
    if not files:
        print("错误: 未找到匹配的 JSON 文件。", file=sys.stderr)
        return 1

    reports: list[QualityReport] = []
    for file_path in files:
        report = analyze_file(file_path)
        reports.append(report)
        print_report(report)

    grades = [r.grade for r in reports]
    count_a = grades.count("A")
    count_b = grades.count("B")
    count_c = grades.count("C")

    print(
        f"\n═══ 汇总统计 ═══\n"
        f"  文件总数: {len(reports)}\n"
        f"  A 级: {count_a}    B 级: {count_b}    C 级: {count_c}"
    )

    return 1 if count_c > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
