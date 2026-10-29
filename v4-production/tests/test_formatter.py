"""formatter 单元测试 — 覆盖评分归一化、三渠道单篇格式化与每日简报。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from distribution.formatter import (
    DEFAULT_KNOWLEDGE_DIR,
    MAX_BRIEF_CHARS,
    MAX_TELEGRAM_SUMMARY_CHARS,
    MAX_TELEGRAM_TITLE_CHARS,
    article_brief,
    digest_from_index,
    generate_daily_digest,
    generate_range_digest,
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

    def test_top_n_sorted_by_score_desc_flat_mode(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-04-11-a.json", title="low", innovation_score=2)
        _write_article(tmp_path, "2026-04-11-b.json", title="high", innovation_score=5)
        _write_article(tmp_path, "2026-04-11-c.json", title="mid", innovation_score=3)
        _write_article(tmp_path, "2026-04-11-d.json", title="extra", innovation_score=4)
        digest = generate_daily_digest(
            tmp_path, date="2026-04-11", top_n=2, grouped=False
        )
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


class TestGenerateRangeDigest:
    def test_collects_across_dates_in_window(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-10-27-github-a.json", title="tuesdayitem")
        _write_article(tmp_path, "2026-10-31-github-b.json", title="saturdayitem")
        digest = generate_range_digest(
            tmp_path, since="2026-10-25", until="2026-10-31"
        )
        assert digest["markdown"].startswith(
            "# 📚 AI 知识库周报 · 2026-10-25 ~ 2026-10-31"
        )
        assert "tuesdayitem" in digest["telegram"]
        assert "saturdayitem" in digest["weixin"]

    def test_excludes_out_of_window(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-10-27-github-in.json", title="in-item")
        _write_article(tmp_path, "2026-10-20-github-out.json", title="out-item")
        digest = generate_range_digest(
            tmp_path, since="2026-10-25", until="2026-10-31"
        )
        assert "in-item" in digest["markdown"]
        assert "out-item" not in digest["markdown"]

    def test_default_since_is_six_days_before_until(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-10-25-github-edge.json", title="edge-item")
        digest = generate_range_digest(tmp_path, until="2026-10-31")
        assert "edge-item" in digest["markdown"]

    def test_empty_window_returns_placeholder(self, tmp_path: Path) -> None:
        (tmp_path / "index.json").write_text("[]", encoding="utf-8")
        (tmp_path / "recommended.json").write_text("[]", encoding="utf-8")
        digest = generate_range_digest(
            tmp_path, since="2026-10-25", until="2026-10-31"
        )
        expected = "📭 2026-10-25 ~ 2026-10-31 暂无新增知识条目"
        assert digest == {
            "markdown": expected,
            "telegram": expected,
            "weixin": expected,
        }

    def test_since_after_until_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="since"):
            generate_range_digest(tmp_path, since="2026-10-31", until="2026-10-25")

    def test_invalid_top_n_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="top_n"):
            generate_range_digest(tmp_path, since="2026-10-25", top_n=0)


# ---------------------------------------------------------------------------
# 特性 1：一句话简介（article_brief）
# ---------------------------------------------------------------------------


class TestArticleBrief:
    def test_key_insight_takes_priority(self) -> None:
        article = {
            **SAMPLE_ARTICLE,
            "key_insight": "一体化平台降低门槛",
            "key_points": ["亮点一"],
        }
        assert article_brief(article) == "一体化平台降低门槛"

    def test_key_points_first_non_empty(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": ["", "  ", "亮点二"]}
        assert article_brief(article) == "亮点二"

    def test_summary_truncated_as_fallback(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": []}
        brief = article_brief(article)
        assert brief.startswith("Dify 是一个开源")
        assert len(brief) <= MAX_BRIEF_CHARS + 1

    def test_empty_article_returns_placeholder(self) -> None:
        assert article_brief({}) == "（暂无摘要）"

    def test_long_insight_truncated_with_ellipsis(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_insight": "长" * 200}
        brief = article_brief(article)
        assert len(brief) == MAX_BRIEF_CHARS + 1
        assert brief.endswith("…")


# ---------------------------------------------------------------------------
# 特性 2：brief 模式下的三渠道渲染
# ---------------------------------------------------------------------------


class TestBriefRendering:
    def test_markdown_brief_uses_short_text_without_insight(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": ["一句话亮点"]}
        md = json_to_markdown(article, brief=True)
        assert "一句话亮点" in md
        assert "💡" not in md
        assert "Dify 是一个开源 LLM 应用开发平台。" not in md

    def test_markdown_brief_false_keeps_summary_and_insight(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": ["一句话亮点"]}
        md = json_to_markdown(article, brief=False)
        assert "Dify 是一个开源 LLM 应用开发平台。" in md
        assert "💡 一句话亮点" in md

    def test_markdown_heading_level(self) -> None:
        md = json_to_markdown(SAMPLE_ARTICLE, heading_level=3)
        assert md.startswith("### [langgenius/dify](")

    def test_telegram_brief(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": ["一句话亮点"]}
        text = json_to_telegram(article, brief=True)
        assert "一句话亮点" in text
        assert "Dify 是一个开源 LLM 应用开发平台。" not in text

    def test_weixin_brief_and_heading_level(self) -> None:
        article = {**SAMPLE_ARTICLE, "key_points": ["一句话亮点"]}
        wx = json_to_weixin(article, brief=True, heading_level=3)
        assert wx.startswith("### [langgenius/dify](")
        assert "一句话亮点" in wx


# ---------------------------------------------------------------------------
# 特性 3：Telegram 超长截断
# ---------------------------------------------------------------------------


class TestTelegramTruncation:
    def test_long_summary_truncated_with_ellipsis(self) -> None:
        article = {
            **SAMPLE_ARTICLE,
            "summary": "字" * (MAX_TELEGRAM_SUMMARY_CHARS + 50),
        }
        text = json_to_telegram(article)
        assert "字…" in text
        escaped_body_lines = text.split("\n")[1]
        assert len(escaped_body_lines) < MAX_TELEGRAM_SUMMARY_CHARS + 60

    def test_short_summary_not_truncated(self) -> None:
        text = json_to_telegram(SAMPLE_ARTICLE)
        assert "Dify 是一个开源 LLM 应用开发平台。" in text

    def test_long_title_truncated(self) -> None:
        article = {**SAMPLE_ARTICLE, "title": "T" * (MAX_TELEGRAM_TITLE_CHARS + 30)}
        text = json_to_telegram(article)
        title_line = text.split("\n")[0]
        assert "T…" in title_line
        assert len(title_line) < MAX_TELEGRAM_TITLE_CHARS + 60

    def test_truncated_message_keeps_url(self) -> None:
        article = {**SAMPLE_ARTICLE, "summary": "字" * 3000}
        text = json_to_telegram(article)
        # 独立 URL 行处于正文上下文，"." 等特殊字符须全量转义（回归：
        # 未转义会被 Telegram API 拒绝 can't parse entities）
        assert text.rstrip().endswith("https://github\\.com/langgenius/dify")


# ---------------------------------------------------------------------------
# 特性 2 + 3：分组简报与 Telegram 超限适配
# ---------------------------------------------------------------------------


class TestGroupedDigest:
    def test_grouped_by_category(self, tmp_path: Path) -> None:
        _write_article(
            tmp_path,
            "2026-04-11-app1.json",
            title="app-one",
            category="AI Application",
            innovation_score=4,
        )
        _write_article(
            tmp_path,
            "2026-04-11-agent1.json",
            title="agent-one",
            category="AI Agent",
            innovation_score=5,
        )
        digest = generate_daily_digest(tmp_path, date="2026-04-11")
        md = digest["markdown"]
        assert "## 🗂 AI Agent" in md
        assert "## 🗂 AI Application" in md
        # 组间按组内最高分降序：AI Agent (1.0) 在前
        assert md.index("🗂 AI Agent") < md.index("🗂 AI Application")
        # 分组模式下条目标题降为 3 级
        assert "### [agent-one]" in md

    def test_per_group_top_n_limits_each_group(self, tmp_path: Path) -> None:
        for i in range(1, 4):
            _write_article(
                tmp_path,
                f"2026-04-11-app{i}.json",
                title=f"app-{i}",
                category="AI Application",
                innovation_score=i,
            )
        digest = generate_daily_digest(tmp_path, date="2026-04-11", per_group_top_n=2)
        body = digest["markdown"]
        assert "app-3" in body
        assert "app-2" in body
        assert "app-1" not in body

    def test_missing_category_grouped_as_uncategorized(self, tmp_path: Path) -> None:
        _write_article(tmp_path, "2026-04-11-x.json", title="no-cat", category="")
        digest = generate_daily_digest(tmp_path, date="2026-04-11")
        assert "🗂 未分类" in digest["markdown"]

    def test_flat_mode_still_available(self, tmp_path: Path) -> None:
        for i, cat in enumerate(["AI Agent", "AI Application"]):
            _write_article(
                tmp_path,
                f"2026-04-11-{i}.json",
                title=f"item-{i}",
                category=cat,
                innovation_score=4,
            )
        digest = generate_daily_digest(tmp_path, date="2026-04-11", grouped=False)
        assert "🗂" not in digest["markdown"]
        assert "## [item-0]" in digest["markdown"]

    def test_telegram_over_limit_drops_tail_blocks(self, tmp_path: Path) -> None:
        for i in range(1, 8):
            _write_article(
                tmp_path,
                f"2026-04-11-{i}.json",
                title=f"title-{i}",
                category=f"Cat-{i}",
                innovation_score=i,
                summary="长" * 600,
            )
        digest = generate_daily_digest(tmp_path, date="2026-04-11", brief=False)
        telegram = digest["telegram"]
        assert len(telegram) <= 4096
        assert "已省略" in telegram
        # 高分组保留在消息内（Telegram 中 "-" 转义为 "\-"）
        assert "title\\-7" in telegram
        # 低分组（Cat-1，0.2 分）被丢弃
        assert "Cat\\-1" not in telegram

    def test_invalid_per_group_top_n_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="per_group_top_n"):
            generate_daily_digest(tmp_path, date="2026-04-11", per_group_top_n=0)


# ---------------------------------------------------------------------------
# 特性 4：基于 index.json 的快速预览
# ---------------------------------------------------------------------------


def _write_index(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return index_path


class TestDigestFromIndex:
    def test_filters_by_date_and_sorts_score_desc(self, tmp_path: Path) -> None:
        entries = [
            {
                "file": "2026-04-11-a.json",
                "title": "zero",
                "category": "AI Agent",
                "innovation_score": None,
            },
            {
                "file": "2026-04-11-b.json",
                "title": "high",
                "category": "AI Agent",
                "innovation_score": 5,
            },
            {
                "file": "2026-04-11-c.json",
                "title": "mid",
                "category": "AI Application",
                "innovation_score": 3,
            },
            {
                "file": "2026-04-10-old.json",
                "title": "other-day",
                "category": "AI Agent",
                "innovation_score": 5,
            },
        ]
        index_path = _write_index(tmp_path, entries)
        result = digest_from_index(date="2026-04-11", index_path=index_path)
        # None 评分排最后（与 Bot 检索约定一致），其它日期不参与
        assert [e["title"] for e in result] == ["high", "mid", "zero"]

    def test_top_n_limits_result(self, tmp_path: Path) -> None:
        entries = [
            {"file": f"2026-04-11-{i}.json", "title": f"t{i}", "innovation_score": i}
            for i in range(1, 6)
        ]
        index_path = _write_index(tmp_path, entries)
        result = digest_from_index(date="2026-04-11", top_n=2, index_path=index_path)
        assert [e["title"] for e in result] == ["t5", "t4"]

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        index_path = _write_index(tmp_path, [])
        assert digest_from_index(date="2026-04-11", index_path=index_path) == []

    def test_missing_index_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            digest_from_index(date="2026-04-11", index_path=tmp_path / "none.json")

    def test_corrupt_index_returns_empty(self, tmp_path: Path) -> None:
        index_path = tmp_path / "index.json"
        index_path.write_text("{ broken", encoding="utf-8")
        assert digest_from_index(date="2026-04-11", index_path=index_path) == []

    def test_non_list_index_returns_empty(self, tmp_path: Path) -> None:
        index_path = tmp_path / "index.json"
        index_path.write_text('{"not": "a list"}', encoding="utf-8")
        assert digest_from_index(date="2026-04-11", index_path=index_path) == []

    def test_invalid_date_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            digest_from_index(date="04-11-2026", index_path=tmp_path / "i.json")

    def test_invalid_top_n_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="top_n"):
            digest_from_index(
                date="2026-04-11", top_n=0, index_path=tmp_path / "i.json"
            )

    def test_returns_original_fields(self, tmp_path: Path) -> None:
        entry = {
            "file": "2026-04-11-a.json",
            "title": "t",
            "category": "AI Agent",
            "innovation_score": 4,
            "stars": 123,
        }
        index_path = _write_index(tmp_path, [entry])
        result = digest_from_index(date="2026-04-11", index_path=index_path)
        assert result == [entry]
