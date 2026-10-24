"""formatter 单元测试 — 覆盖评分归一化、三渠道单篇格式化与每日简报。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from distribution.formatter import (
    DEFAULT_KNOWLEDGE_DIR,
    generate_daily_digest,
    json_to_markdown,
    json_to_telegram,
    json_to_weixin,
    normalized_score,
    score_grade,
)

SAMPLE_ARTICLE = {
    "id": "github-20260411-000",
    "title": "langgenius/dify",
    "source": "github",
    "source_url": "https://github.com/langgenius/dify",
    "summary": "Dify 是一个开源 LLM 应用开发平台。",
    "tags": ["LLM应用开发", "智能体 工作流"],
    "category": "AI Application",
    "innovation_score": 4,
    "collected_at": "2026-04-11T16:03:47.946653+00:00",
}


# ---------------------------------------------------------------------------
# 评分归一化
# ---------------------------------------------------------------------------


class TestNormalizedScore:
    def test_innovation_score_normalized(self) -> None:
        article = {"innovation_score": 4}
        assert normalized_score(article) == 0.8

    def test_innovation_score_capped_at_max(self) -> None:
        article = {"innovation_score": 9}
        assert normalized_score(article) == 1.0

    def test_missing_score_is_zero(self) -> None:
        assert normalized_score({}) == 0.0

    def test_null_score_is_zero(self) -> None:
        assert normalized_score({"innovation_score": None}) == 0.0

    def test_invalid_score_is_zero(self) -> None:
        assert normalized_score({"innovation_score": "abc"}) == 0.0

    @pytest.mark.parametrize(
        ("score", "grade"),
        [
            (1.0, "green"),
            (0.8, "green"),
            (0.79, "yellow"),
            (0.6, "yellow"),
            (0.59, "red"),
            (0.0, "red"),
        ],
    )
    def test_grade_thresholds(self, score: float, grade: str) -> None:
        assert score_grade(score) == grade


# ---------------------------------------------------------------------------
# json_to_markdown
# ---------------------------------------------------------------------------


class TestJsonToMarkdown:
    def test_renders_all_fields(self) -> None:
        md = json_to_markdown(SAMPLE_ARTICLE)
        assert md.startswith("## [langgenius/dify](")
        assert "https://github.com/langgenius/dify" in md
        assert "github" in md
        assert "2026-04-11" in md
        assert "🟢 0.8" in md
        assert "`LLM应用开发`" in md
        assert "Dify 是一个开源 LLM 应用开发平台。" in md

    def test_key_points_first_item_as_insight(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": ["亮点一", "亮点二"]}
        assert "💡 亮点一" in json_to_markdown(article)

    def test_key_insight_preferred_over_key_points(self) -> None:
        article = {
            **SAMPLE_ARTICLE,
            "key_insight": "一体化平台降低门槛",
            "key_points": ["亮点一"],
        }
        assert "💡 一体化平台降低门槛" in json_to_markdown(article)

    def test_url_field_fallback(self) -> None:
        article = {k: v for k, v in SAMPLE_ARTICLE.items() if k != "source_url"}
        article["url"] = "https://example.com/dify"
        assert "https://example.com/dify" in json_to_markdown(article)

    def test_missing_fields_render_placeholders(self) -> None:
        md = json_to_markdown({"title": "", "innovation_score": None})
        assert "（无标题）" in md
        assert "🔴 0.0" in md
        assert "（暂无摘要）" in md
        assert "unknown" in md


# ---------------------------------------------------------------------------
# json_to_telegram
# ---------------------------------------------------------------------------


class TestJsonToTelegram:
    def test_escapes_markdown_v2_special_chars(self) -> None:
        article = {
            **SAMPLE_ARTICLE,
            "title": "a_b [c] (d) ~e~ `f` >g< #h +i -j =k |l {m} .n !o *p*",
        }
        text = json_to_telegram(article)
        # 标题在链接实体内，逐字符验证转义结果
        assert "\\[c\\]" in text
        assert "\\(d\\)" in text
        assert "\\~e\\~" in text
        assert "\\`f\\`" in text
        assert "\\#h" in text
        assert "\\+i" in text
        assert "\\-j" in text
        assert "\\=k" in text
        assert "\\|l" in text
        assert "\\{m\\}" in text
        assert "\\.n" in text
        assert "\\!o" in text

    def test_bold_title_with_link(self) -> None:
        text = json_to_telegram(SAMPLE_ARTICLE)
        assert text.startswith(
            "📌 *[langgenius/dify](https://github.com/langgenius/dify)*"
        )

    def test_tag_spaces_replaced_with_underscore(self) -> None:
        text = json_to_telegram(SAMPLE_ARTICLE)
        assert "\\#智能体\\_工作流" in text

    def test_summary_and_score_and_source(self) -> None:
        text = json_to_telegram(SAMPLE_ARTICLE)
        # 中文全角句号不属于 MarkdownV2 转义字符集，应原样保留
        assert "Dify 是一个开源 LLM 应用开发平台。" in text
        assert "🟢 0\\.8" in text
        assert "来源：github" in text

    def test_ascii_dot_in_summary_escaped(self) -> None:
        article = {**SAMPLE_ARTICLE, "summary": "v1.2 released."}
        assert "v1\\.2 released\\." in json_to_telegram(article)


# ---------------------------------------------------------------------------
# json_to_weixin
# ---------------------------------------------------------------------------


class TestJsonToWeixin:
    def test_green_grade_uses_info_color(self) -> None:
        wx = json_to_weixin(SAMPLE_ARTICLE)
        assert '<font color="info">🟢 0.8</font>' in wx

    def test_yellow_grade_uses_comment_color(self) -> None:
        article = {**SAMPLE_ARTICLE, "innovation_score": 3}
        assert '<font color="comment">🟟 0.6</font>' in json_to_weixin(article)

    def test_red_grade_uses_warning_color(self) -> None:
        article = {**SAMPLE_ARTICLE, "innovation_score": 2}
        assert '<font color="warning">🔴 0.4</font>' in json_to_weixin(article)

    def test_title_link_and_tags(self) -> None:
        wx = json_to_weixin(SAMPLE_ARTICLE)
        assert "## [langgenius/dify](https://github.com/langgenius/dify)" in wx
        assert "#智能体_工作流" in wx


# ---------------------------------------------------------------------------
# generate_daily_digest
# ---------------------------------------------------------------------------


def _write_article(directory: Path, name: str, **overrides: object) -> Path:
    article = {**SAMPLE_ARTICLE, **overrides}
    path = directory / name
    path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
    return path


class TestGenerateDailyDigest:
    def test_returns_three_channels(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-04-11-github-dify.json")
        digest = generate_daily_digest(tmp_path, date="2026-04-11")
        assert set(digest) == {"markdown", "telegram", "weixin"}
        assert digest["markdown"].startswith("# 📚 AI 知识库日报 · 2026-04-11")
        assert "langgenius/dify" in digest["telegram"]
        assert "langgenius/dify" in digest["weixin"]

    def test_top_n_sorted_by_score_desc(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-04-11-a.json", title="low", innovation_score=2)
        _write_article(tmp_path, "2026-04-11-b.json", title="high", innovation_score=5)
        _write_article(tmp_path, "2026-04-11-c.json", title="mid", innovation_score=3)
        _write_article(tmp_path, "2026-04-11-d.json", title="extra", innovation_score=4)
        digest = generate_daily_digest(tmp_path, date="2026-04-11", top_n=2)
        body = digest["markdown"]
        high_pos = body.index("high")
        extra_pos = body.index("extra")
        assert "low" not in body
        assert "mid" not in body
        assert high_pos < extra_pos

    def test_empty_day_returns_placeholder(self, tmp_path: Path) -> None:
        digest = generate_daily_digest(tmp_path, date="2026-04-11")
        expected = "📭 2026-04-11 暂无新增知识条目"
        assert digest == {
            "markdown": expected,
            "telegram": expected,
            "weixin": expected,
        }

    def test_index_json_not_matched(self, tmp_path: Path) -> None:
        (tmp_path / "index.json").write_text("[]", encoding="utf-8")
        digest = generate_daily_digest(tmp_path, date="2026-04-11")
        assert "📭" in digest["markdown"]

    def test_corrupt_file_skipped_with_others_kept(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-04-11-good.json", title="good-item")
        (tmp_path / "2026-04-11-broken.json").write_text("{ not json", encoding="utf-8")
        digest = generate_daily_digest(tmp_path, date="2026-04-11")
        assert "good-item" in digest["markdown"]

    def test_invalid_date_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            generate_daily_digest(tmp_path, date="2026/04/11")

    def test_invalid_top_n_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="top_n"):
            generate_daily_digest(tmp_path, date="2026-04-11", top_n=0)

    def test_default_knowledge_dir_points_to_repo_root(self) -> None:
        assert DEFAULT_KNOWLEDGE_DIR.name == "articles"
        assert DEFAULT_KNOWLEDGE_DIR.parent.name == "knowledge"
        assert DEFAULT_KNOWLEDGE_DIR.exists()
