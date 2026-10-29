"""knowledge_bot 单元测试 — 纯规则逻辑与本地文件，不依赖实时网络。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bot.knowledge_bot import (
    Intent,
    KnowledgeBot,
    KnowledgeSearchEngine,
    Permission,
    PermissionManager,
    SubscriptionManager,
    recognize_intent,
)


def make_article(article_id: str, title: str, **overrides: Any) -> dict[str, Any]:
    """构造一条最小可用的知识条目，字段可按需覆盖。"""
    article: dict[str, Any] = {
        "id": article_id,
        "title": title,
        "source": "github",
        "source_url": f"https://example.com/{article_id}",
        "summary": "",
        "tags": ["ai"],
        "category": "AI Application",
        "status": "draft",
        "collected_at": "2026-07-01T10:00:00+08:00",
        "innovation_score": None,
        "stars": None,
    }
    article.update(overrides)
    return article


@pytest.fixture
def articles_dir(tmp_path: Path) -> Path:
    """四条已知条目 + 索引/损坏文件（用于验证容错跳过）。"""
    entries = [
        make_article(
            "a-1",
            "LangGraph Agent Framework",
            tags=["llm", "agent"],
            innovation_score=5,
            stars=300,
            collected_at="2026-07-01T10:00:00+08:00",
        ),
        make_article(
            "a-2",
            "Fast Vector Database",
            tags=["vector-db", "database"],
            innovation_score=3,
            stars=50,
            collected_at="2026-07-02T09:00:00+08:00",
        ),
        make_article(
            "a-3",
            "Prompt Engineering Guide",
            tags=["prompt", "llm"],
            stars=10,
            collected_at="2026-07-02T18:00:00+08:00",
        ),
        make_article(
            "a-4",
            "RAG Pipeline Toolkit",
            tags=["rag", "llm"],
            innovation_score=5,
            stars=120,
            collected_at="2026-07-03T08:00:00+08:00",
        ),
    ]
    for entry in entries:
        day = str(entry["collected_at"])[:10]
        path = tmp_path / f"{day}-{entry['id']}.json"
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{ not valid", encoding="utf-8")
    (tmp_path / "not-dict.json").write_text("[1, 2]", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# recognize_intent
# ---------------------------------------------------------------------------


class TestRecognizeIntent:
    def test_search_command_with_args(self) -> None:
        assert recognize_intent("/search llm agent") == (Intent.SEARCH, "llm agent")

    def test_today_command_without_args(self) -> None:
        assert recognize_intent("/today") == (Intent.TODAY, "")

    def test_top_command_with_number(self) -> None:
        assert recognize_intent("/top 10") == (Intent.TOP, "10")

    def test_subscribe_command_with_remove_prefix(self) -> None:
        intent, args = recognize_intent("/subscribe 取消 llm")
        assert intent is Intent.SUBSCRIBE
        assert args == "取消 llm"

    def test_help_command(self) -> None:
        assert recognize_intent("/help") == (Intent.HELP, "")

    def test_command_with_bot_suffix(self) -> None:
        intent, args = recognize_intent("/search@my_kb_bot rag")
        assert intent is Intent.SEARCH
        assert args == "rag"

    def test_command_case_insensitive(self) -> None:
        intent, args = recognize_intent("/SEARCH agent")
        assert intent is Intent.SEARCH
        assert args == "agent"

    def test_unknown_command_falls_back_to_nl(self) -> None:
        # 非法命令按自然语言继续匹配，这里没有任何关键词 → UNKNOWN
        assert recognize_intent("/frobnicate") == (Intent.UNKNOWN, "")

    def test_nl_search_with_fillers(self) -> None:
        intent, args = recognize_intent("帮我搜索一下 llm agent")
        assert intent is Intent.SEARCH
        assert args == "llm agent"

    def test_nl_search_query_word(self) -> None:
        intent, args = recognize_intent("查询 prompt engineering")
        assert intent is Intent.SEARCH
        assert args == "prompt engineering"

    def test_nl_today_keywords(self) -> None:
        assert recognize_intent("今天有什么新内容")[0] is Intent.TODAY
        assert recognize_intent("看看今日简报")[0] is Intent.TODAY

    def test_nl_top_keywords(self) -> None:
        intent, args = recognize_intent("来一份热门项目")
        assert intent is Intent.TOP
        assert args == "项目"

    def test_nl_subscribe_over_search_priority(self) -> None:
        # 「查询我的订阅」同时含搜索词（查询）与订阅词，订阅优先
        intent, _args = recognize_intent("查询我的订阅")
        assert intent is Intent.SUBSCRIBE

    def test_nl_subscribe_with_tag(self) -> None:
        intent, args = recognize_intent("订阅 rag")
        assert intent is Intent.SUBSCRIBE
        assert args == "rag"

    def test_nl_help(self) -> None:
        assert recognize_intent("这个机器人怎么用")[0] is Intent.HELP

    def test_ascii_keyword_word_boundary(self) -> None:
        # "topics" 内含 "top"，不应命中 TOP
        intent, _args = recognize_intent("search topics about agents")
        assert intent is Intent.SEARCH

    def test_empty_and_blank(self) -> None:
        assert recognize_intent("") == (Intent.UNKNOWN, "")
        assert recognize_intent("   ") == (Intent.UNKNOWN, "")

    def test_reference_intent_matrix(self) -> None:
        # 交互验证清单：命令与自然语言混排；args 为 None 表示只断言意图
        cases: list[tuple[str, Intent, str | None]] = [
            ("/search MCP", Intent.SEARCH, "MCP"),
            ("/today", Intent.TODAY, ""),
            ("/top", Intent.TOP, ""),
            ("/top 3", Intent.TOP, "3"),
            ("/help", Intent.HELP, ""),
            ("搜索 Agent 文章", Intent.SEARCH, "Agent 文章"),
            ("搜索 MCP 协议", Intent.SEARCH, "MCP 协议"),
            ("搜一下 RAG", Intent.SEARCH, "RAG"),
            ("今天有什么新内容", Intent.TODAY, None),
            ("随便聊聊", Intent.UNKNOWN, ""),
        ]
        for text, expected_intent, expected_args in cases:
            intent, args = recognize_intent(text)
            assert intent is expected_intent, (
                f"{text!r} → {intent.value}，期望 {expected_intent.value}"
            )
            if expected_args is not None:
                assert args == expected_args, (
                    f"{text!r} args={args!r}，期望 {expected_args!r}"
                )


# ---------------------------------------------------------------------------
# PermissionManager
# ---------------------------------------------------------------------------


class TestPermissionManager:
    def test_default_read_for_unknown_user(self, tmp_path: Path) -> None:
        manager = PermissionManager(tmp_path / "perm.json")
        assert manager.get_permission("u1") is Permission.READ

    def test_rank_ordering(self) -> None:
        assert Permission.READ.rank < Permission.WRITE.rank < Permission.DELETE.rank

    def test_check_hierarchy(self, tmp_path: Path) -> None:
        manager = PermissionManager(tmp_path / "perm.json")
        assert manager.check("u1", Permission.READ)
        assert not manager.check("u1", Permission.WRITE)
        manager.set_permission("u1", Permission.WRITE)
        assert manager.check("u1", Permission.WRITE)
        assert not manager.check("u1", Permission.DELETE)
        manager.set_permission("u1", Permission.DELETE)
        assert manager.check("u1", Permission.READ)
        assert manager.check("u1", Permission.WRITE)
        assert manager.check("u1", Permission.DELETE)

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        store = tmp_path / "perm.json"
        manager = PermissionManager(store)
        manager.set_permission("u1", Permission.DELETE)
        reloaded = PermissionManager(store)
        assert reloaded.get_permission("u1") is Permission.DELETE
        assert reloaded.get_permission("u2") is Permission.READ

    def test_corrupted_file_falls_back_to_default(self, tmp_path: Path) -> None:
        store = tmp_path / "perm.json"
        store.write_text("{ broken", encoding="utf-8")
        manager = PermissionManager(store)
        assert manager.get_permission("u1") is Permission.READ

    def test_invalid_permission_value_ignored(self, tmp_path: Path) -> None:
        store = tmp_path / "perm.json"
        store.write_text(
            json.dumps({"users": {"u1": "superuser", "u2": "write"}}),
            encoding="utf-8",
        )
        manager = PermissionManager(store)
        assert manager.get_permission("u1") is Permission.READ
        assert manager.get_permission("u2") is Permission.WRITE


# ---------------------------------------------------------------------------
# SubscriptionManager
# ---------------------------------------------------------------------------


class TestSubscriptionManager:
    def test_add_list_remove(self, tmp_path: Path) -> None:
        manager = SubscriptionManager(tmp_path / "subs.json")
        assert manager.list_subscriptions("u1") == []
        assert manager.add_subscription("u1", "llm")
        assert manager.add_subscription("u1", "agent")
        assert manager.list_subscriptions("u1") == ["llm", "agent"]
        assert manager.remove_subscription("u1", "llm")
        assert manager.list_subscriptions("u1") == ["agent"]

    def test_add_duplicate_returns_false(self, tmp_path: Path) -> None:
        manager = SubscriptionManager(tmp_path / "subs.json")
        assert manager.add_subscription("u1", "llm")
        assert not manager.add_subscription("u1", "llm")

    def test_remove_absent_returns_false(self, tmp_path: Path) -> None:
        manager = SubscriptionManager(tmp_path / "subs.json")
        assert not manager.remove_subscription("u1", "llm")
        assert manager.add_subscription("u1", "llm")
        assert not manager.remove_subscription("u2", "llm")

    def test_tag_normalization(self, tmp_path: Path) -> None:
        manager = SubscriptionManager(tmp_path / "subs.json")
        assert manager.add_subscription("u1", "#LLM")
        assert manager.list_subscriptions("u1") == ["llm"]
        assert not manager.add_subscription("u1", "llm")
        assert manager.remove_subscription("u1", "LLM")

    def test_empty_tag_rejected(self, tmp_path: Path) -> None:
        manager = SubscriptionManager(tmp_path / "subs.json")
        assert not manager.add_subscription("u1", "#")
        assert not manager.add_subscription("u1", "  ")

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        store = tmp_path / "subs.json"
        manager = SubscriptionManager(store)
        manager.add_subscription("u1", "llm")
        manager.add_subscription("u1", "rag")
        reloaded = SubscriptionManager(store)
        assert reloaded.list_subscriptions("u1") == ["llm", "rag"]

    def test_corrupted_file_starts_empty(self, tmp_path: Path) -> None:
        store = tmp_path / "subs.json"
        store.write_text("not json", encoding="utf-8")
        manager = SubscriptionManager(store)
        assert manager.list_subscriptions("u1") == []

    def test_subscribers_of(self, tmp_path: Path) -> None:
        manager = SubscriptionManager(tmp_path / "subs.json")
        manager.add_subscription("u1", "llm")
        manager.add_subscription("u2", "rag")
        manager.add_subscription("u3", "llm")
        assert manager.subscribers_of("llm") == ["u1", "u3"]
        assert manager.subscribers_of("rag") == ["u2"]
        assert manager.subscribers_of("none") == []


# ---------------------------------------------------------------------------
# KnowledgeSearchEngine
# ---------------------------------------------------------------------------


class TestKnowledgeSearchEngine:
    def test_keyword_search(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(keywords=["agent"])
        assert [r["id"] for r in results] == ["a-1"]

    def test_keyword_search_case_insensitive(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(keywords=["GUIDE"])
        assert [r["id"] for r in results] == ["a-3"]

    def test_multiple_keywords_are_and(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        assert [r["id"] for r in engine.search(keywords=["vector", "fast"])] == ["a-2"]
        assert engine.search(keywords=["vector", "agent"]) == []

    def test_tag_search(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        assert [r["id"] for r in engine.search(tags=["llm"])] == [
            "a-4",
            "a-3",
            "a-1",
        ]

    def test_multiple_tags_are_and(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(tags=["prompt", "llm"])
        assert [r["id"] for r in results] == ["a-3"]

    def test_date_range(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(date_from="2026-07-02", date_to="2026-07-02")
        assert {r["id"] for r in results} == {"a-2", "a-3"}

    def test_combined_filters(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(keywords=["rag"], tags=["llm"])
        assert [r["id"] for r in results] == ["a-4"]

    def test_limit(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        assert len(engine.search(tags=["llm"], limit=2)) == 2
        assert len(engine.search(tags=["llm"], limit=10)) == 3

    def test_no_match_returns_empty(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        assert engine.search(keywords=["不存在的关键词xyz"]) == []

    def test_index_and_broken_files_skipped(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(limit=10)  # 无过滤，应恰好加载 4 条真实条目
        assert {r["id"] for r in results} == {"a-1", "a-2", "a-3", "a-4"}

    def test_invalid_date_format_raises(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        with pytest.raises(ValueError, match="date_from"):
            engine.search(date_from="2026/07/01")

    def test_reversed_date_range_raises(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        with pytest.raises(ValueError, match="晚于"):
            engine.search(date_from="2026-07-03", date_to="2026-07-01")

    def test_non_positive_limit_raises(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        with pytest.raises(ValueError, match="limit"):
            engine.search(limit=0)

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        engine = KnowledgeSearchEngine(tmp_path / "nope")
        assert engine.search(keywords=["llm"]) == []
        assert engine.top() == []

    def test_filename_date_fallback(self, tmp_path: Path) -> None:
        # collected_at 缺失时回退文件名日期，日期过滤仍可命中
        entry = make_article("a-9", "No Timestamp Entry")
        entry.pop("collected_at")
        (tmp_path / "2026-07-05-a-9.json").write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
        engine = KnowledgeSearchEngine(tmp_path)
        results = engine.search(date_from="2026-07-05", date_to="2026-07-05")
        assert [r["id"] for r in results] == ["a-9"]

    def test_today_with_explicit_date(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.today(today=date(2026, 7, 3))
        assert [r["id"] for r in results] == ["a-4"]
        assert engine.today(today=date(2026, 7, 10)) == []

    def test_search_agent_with_limit(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        results = engine.search(keywords=["agent"], limit=3)
        assert [r["id"] for r in results] == ["a-1"]

    def test_top_ordering(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        # a-1 与 a-4 同为 5 分按 stars 降序；a-3 无评分排最后
        assert [r["id"] for r in engine.top()] == ["a-1", "a-4", "a-2", "a-3"]
        assert [r["id"] for r in engine.top(n=2)] == ["a-1", "a-4"]

    def test_top_non_positive_raises(self, articles_dir: Path) -> None:
        engine = KnowledgeSearchEngine(articles_dir)
        with pytest.raises(ValueError, match="n "):
            engine.top(n=0)


# ---------------------------------------------------------------------------
# KnowledgeBot.handle_message
# ---------------------------------------------------------------------------


@pytest.fixture
def today_article() -> dict[str, Any]:
    """collected_at 为当前本地时间的条目，保证 /today 可命中。"""
    now = datetime.now().astimezone()
    return make_article(
        "t-1",
        "Fresh MCP Server",
        tags=["mcp"],
        summary="Model Context Protocol（MCP）协议的服务端示例实现",
        innovation_score=4,
        collected_at=now.isoformat(),
    )


@pytest.fixture
def bot(tmp_path: Path, today_article: dict[str, Any]) -> KnowledgeBot:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    entry = make_article(
        "b-1",
        "LangGraph Agent Framework",
        tags=["llm", "agent"],
        innovation_score=5,
        collected_at="2026-07-01T10:00:00+08:00",
    )
    (knowledge_dir / "2026-07-01-b-1.json").write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8"
    )
    rag = make_article(
        "b-2",
        "RAG Pipeline Toolkit",
        tags=["rag"],
        innovation_score=4,
        collected_at="2026-07-02T10:00:00+08:00",
    )
    (knowledge_dir / "2026-07-02-b-2.json").write_text(
        json.dumps(rag, ensure_ascii=False), encoding="utf-8"
    )
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    (knowledge_dir / f"{day}-t-1.json").write_text(
        json.dumps(today_article, ensure_ascii=False), encoding="utf-8"
    )
    return KnowledgeBot(
        knowledge_dir=knowledge_dir,
        subscriptions_path=tmp_path / "subs.json",
        permissions_path=tmp_path / "perm.json",
    )


class TestKnowledgeBot:
    def test_search_command(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/search langgraph")
        assert "LangGraph Agent Framework" in reply
        assert "https://example.com/b-1" in reply

    def test_search_natural_language(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "帮我搜索一下 langgraph")
        assert "LangGraph Agent Framework" in reply

    def test_search_no_result(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/search 不存在的关键词xyz")
        assert "没有匹配" in reply

    def test_search_empty_args_shows_usage(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/search")
        assert "用法" in reply

    def test_search_with_tag_and_date_filters(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message(
            "u1", "/search framework tag:llm from:2026-07-01 to:2026-07-01"
        )
        assert "LangGraph Agent Framework" in reply

    def test_search_invalid_date_shows_error(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/search from:2026-07")
        assert "参数错误" in reply
        assert "用法" in reply

    def test_today_command(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/today")
        assert "Fresh MCP Server" in reply

    def test_top_command(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/top 1")
        assert "LangGraph Agent Framework" in reply

    def test_top_invalid_arg_shows_usage(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/top abc")
        assert "用法" in reply

    def test_help_command(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/help")
        assert "/search" in reply
        assert "/subscribe" in reply

    def test_unknown_message_returns_hint(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "你好呀")
        assert "/help" in reply

    def test_subscribe_denied_without_write(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/subscribe llm")
        assert "权限不足" in reply
        assert "WRITE" in reply
        assert bot.subscriptions.list_subscriptions("u1") == []

    def test_subscribe_after_grant(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        reply = bot.handle_message("u1", "/subscribe llm")
        assert "已订阅" in reply
        assert bot.subscriptions.list_subscriptions("u1") == ["llm"]

    def test_subscribe_duplicate_reply(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        bot.handle_message("u1", "/subscribe llm")
        reply = bot.handle_message("u1", "/subscribe llm")
        assert "已订阅过" in reply

    def test_subscribe_list_when_empty(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        reply = bot.handle_message("u1", "/subscribe")
        assert "还没有订阅" in reply

    def test_subscribe_list_after_add(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        bot.handle_message("u1", "/subscribe llm")
        bot.handle_message("u1", "/subscribe rag")
        reply = bot.handle_message("u1", "/subscribe")
        assert "llm" in reply
        assert "rag" in reply

    def test_unsubscribe(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        bot.handle_message("u1", "/subscribe llm")
        reply = bot.handle_message("u1", "/subscribe 取消 llm")
        assert "已退订" in reply
        assert bot.subscriptions.list_subscriptions("u1") == []

    def test_unsubscribe_absent(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        reply = bot.handle_message("u1", "/subscribe 取消 llm")
        assert "未订阅过" in reply

    def test_unsubscribe_without_tag_shows_usage(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        reply = bot.handle_message("u1", "/subscribe 取消")
        assert "用法" in reply

    def test_delete_permission_covers_write(self, bot: KnowledgeBot) -> None:
        bot.permissions.set_permission("u1", Permission.DELETE)
        reply = bot.handle_message("u1", "/subscribe llm")
        assert "已订阅" in reply

    def test_state_files_persisted(self, bot: KnowledgeBot, tmp_path: Path) -> None:
        bot.permissions.set_permission("u1", Permission.WRITE)
        bot.handle_message("u1", "/subscribe llm")
        # 用相同存储路径重建 bot，状态应恢复
        reborn = KnowledgeBot(
            knowledge_dir=bot.search_engine.knowledge_dir,
            subscriptions_path=tmp_path / "subs.json",
            permissions_path=tmp_path / "perm.json",
        )
        assert reborn.permissions.get_permission("u1") is Permission.WRITE
        assert reborn.subscriptions.list_subscriptions("u1") == ["llm"]

    def test_reference_full_flow(self, bot: KnowledgeBot) -> None:
        # 参考脚本的完整消息流程：帮助 → 命令搜索 → 今日 → 自然语言搜索
        assert "/search" in bot.handle_message("u1", "/help")

        search_reply = bot.handle_message("u1", "/search Agent")
        assert "LangGraph Agent Framework" in search_reply

        today_reply = bot.handle_message("u1", "/today")
        assert "Fresh MCP Server" in today_reply

        nl_reply = bot.handle_message("u1", "搜索 MCP 协议")
        assert "Fresh MCP Server" in nl_reply

        sou_reply = bot.handle_message("u1", "搜一下 RAG")
        assert "RAG Pipeline Toolkit" in sou_reply

    def test_search_reply_format(self, bot: KnowledgeBot) -> None:
        reply = bot.handle_message("u1", "/search agent")
        assert reply.startswith("🔍")
        assert "共 1 条" in reply
        assert "[AI Application]" in reply
        assert "https://example.com/b-1" in reply

    def test_yesterday_articles_not_in_today(self, tmp_path: Path) -> None:
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
        entry = make_article("y-1", "Yesterday Entry", collected_at=yesterday)
        (knowledge_dir / "2026-01-01-y-1.json").write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
        bot = KnowledgeBot(
            knowledge_dir=knowledge_dir,
            subscriptions_path=tmp_path / "subs.json",
            permissions_path=tmp_path / "perm.json",
        )
        reply = bot.handle_message("u1", "/today")
        assert "Yesterday Entry" not in reply
        assert "暂无新增" in reply
