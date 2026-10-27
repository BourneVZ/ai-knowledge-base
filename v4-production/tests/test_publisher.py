"""publisher 单元测试 — 全部网络请求经 mock，不依赖实时网络。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from distribution.publisher import (
    PUBLISHER_CLASSES,
    BasePublisher,
    PublishResult,
    TelegramPublisher,
    WeixinPublisher,
    publish_daily_digest,
    split_weixin_content,
)


def run(coro: Any) -> Any:
    """在同步测试内驱动协程（本仓库不依赖 pytest-asyncio 插件）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# PublishResult
# ---------------------------------------------------------------------------


class TestPublishResult:
    def test_defaults(self) -> None:
        result = PublishResult(channel="telegram", success=True)
        assert result.channel == "telegram"
        assert result.success is True
        assert result.message_id is None
        assert result.error is None
        assert result.timestamp  # 自动生成

    def test_full_fields(self) -> None:
        result = PublishResult(
            channel="weixin", success=False, message_id="1/2 段", error="超限"
        )
        assert result.error == "超限"


# ---------------------------------------------------------------------------
# 微信分段
# ---------------------------------------------------------------------------


class TestSplitWeixinContent:
    def test_short_text_single_chunk(self) -> None:
        assert split_weixin_content("hello") == ["hello"]

    def test_empty_text_single_chunk(self) -> None:
        assert split_weixin_content("") == [""]

    def test_chunks_within_byte_limit(self) -> None:
        text = "\n".join(f"第{i}行：{'中' * 50}" for i in range(100))
        chunks = split_weixin_content(text, max_bytes=400)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 400

    def test_split_at_line_boundary(self) -> None:
        text = "行一\n行二\n行三"
        chunks = split_weixin_content(text, max_bytes=13)  # 每行 7 字节 + \n
        assert chunks == ["行一\n行二", "行三"]

    def test_single_line_too_long_hard_split(self) -> None:
        text = "字" * 100
        chunks = split_weixin_content(text, max_bytes=30)
        assert len(chunks) >= 4
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 30
        assert "".join(chunks) == text

    def test_invalid_max_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            split_weixin_content("x", max_bytes=0)


# ---------------------------------------------------------------------------
# TelegramPublisher
# ---------------------------------------------------------------------------


class TestTelegramPublisher:
    def test_missing_token_returns_failure(self) -> None:
        publisher = TelegramPublisher(token="", default_chat_id="123")
        result = run(publisher.send_message("hi"))
        assert result.success is False
        assert "TELEGRAM_BOT_TOKEN" in (result.error or "")

    def test_missing_chat_id_returns_failure(self) -> None:
        publisher = TelegramPublisher(token="t", default_chat_id="")
        result = run(publisher.send_message("hi"))
        assert result.success is False
        assert "TELEGRAM_CHAT_ID" in (result.error or "")

    def test_success_returns_message_id(self) -> None:
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"ok": True, "result": {"message_id": 42}}
        )
        result = run(publisher.send_message("**hi**"))
        assert result.success is True
        assert result.message_id == "42"
        publisher._post_json.assert_awaited_once()
        call_args = publisher._post_json.call_args
        payload = call_args.args[1]
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["chat_id"] == "123"
        assert payload["text"] == "**hi**"

    def test_api_rejection_returns_description(self) -> None:
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"ok": False, "description": "Bad Request: chat not found"}
        )
        result = run(publisher.send_message("hi"))
        assert result.success is False
        assert "chat not found" in (result.error or "")

    def test_network_error_returns_failure_not_crash(self) -> None:
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            side_effect=aiohttp.ClientConnectionError("boom")
        )
        result = run(publisher.send_message("hi"))
        assert result.success is False
        assert "网络错误" in (result.error or "")

    def test_send_digest_delegates_to_send_message(self) -> None:
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        publisher.send_message = AsyncMock(  # type: ignore[method-assign]
            return_value=PublishResult(channel="telegram", success=True)
        )
        run(publisher.send_digest("digest text"))
        publisher.send_message.assert_awaited_once_with("digest text", None)

    def test_target_overrides_default_chat_id(self) -> None:
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"ok": True, "result": {"message_id": 1}}
        )
        run(publisher.send_message("hi", target="999"))
        assert publisher._post_json.call_args.args[1]["chat_id"] == "999"

    def test_proxy_forwarded_to_post_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_PROXY", "http://127.0.0.1:7890")
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        assert publisher.proxy == "http://127.0.0.1:7890"
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"ok": True, "result": {"message_id": 1}}
        )
        run(publisher.send_message("hi"))
        assert publisher._post_json.call_args.kwargs["proxy"] == (
            "http://127.0.0.1:7890"
        )

    def test_blank_proxy_means_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_PROXY", "  ")
        publisher = TelegramPublisher(token="t", default_chat_id="123")
        assert publisher.proxy is None


# ---------------------------------------------------------------------------
# WeixinPublisher
# ---------------------------------------------------------------------------


class TestWeixinPublisher:
    def test_missing_webhook_returns_failure(self) -> None:
        publisher = WeixinPublisher(webhook_url="")
        result = run(publisher.send_message("hi"))
        assert result.success is False
        assert "WEIXIN_WEBHOOK_URL" in (result.error or "")

    def test_success_single_chunk(self) -> None:
        publisher = WeixinPublisher(webhook_url="https://example/hook")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"errcode": 0, "errmsg": "ok"}
        )
        result = run(publisher.send_message("## 标题\n内容"))
        assert result.success is True
        assert result.message_id == "1 段"

    def test_multi_chunk_all_success(self) -> None:
        publisher = WeixinPublisher(webhook_url="https://example/hook")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"errcode": 0, "errmsg": "ok"}
        )
        long_text = "\n".join(f"第{i}行 {'字' * 60}" for i in range(40))
        result = run(publisher.send_message(long_text))
        assert publisher._post_json.await_count >= 2
        assert result.success is True
        assert "段" in (result.message_id or "")

    def test_partial_failure_reports_segment(self) -> None:
        publisher = WeixinPublisher(webhook_url="https://example/hook")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"errcode": 0, "errmsg": "ok"},
                {"errcode": 45009, "errmsg": "api freq out of limit"},
            ]
        )
        two_chunk_text = "字" * 30 + "\n" + "字" * 30
        with patch(
            "distribution.publisher.split_weixin_content",
            return_value=["chunk-1", "chunk-2"],
        ):
            result = run(publisher.send_message(two_chunk_text))
        assert result.success is False
        assert "第2段" in (result.error or "")
        assert result.message_id == "1/2 段"

    def test_payload_is_markdown_msgtype(self) -> None:
        publisher = WeixinPublisher(webhook_url="https://example/hook")
        publisher._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"errcode": 0, "errmsg": "ok"}
        )
        run(publisher.send_message("内容"))
        payload = publisher._post_json.call_args.args[1]
        assert payload["msgtype"] == "markdown"
        assert payload["markdown"]["content"] == "内容"


# ---------------------------------------------------------------------------
# _post_json 重试逻辑
# ---------------------------------------------------------------------------


class _FakeResponse:
    """模拟 aiohttp 响应的异步上下文管理器。"""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload
        self.request_info = {}  # type: ignore[assignment]
        self.history: list[Any] = []

    async def json(self, content_type: str | None = None) -> dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """按脚本回放响应/异常的假 ClientSession。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any], **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestPostJsonRetry:
    def _publisher(self) -> TelegramPublisher:
        return TelegramPublisher(token="t", default_chat_id="1")

    def test_success_first_try(self) -> None:
        session = _FakeSession([_FakeResponse(200, {"ok": True})])
        with (
            patch("distribution.publisher.aiohttp.ClientSession", return_value=session),
            patch("distribution.publisher.asyncio.sleep", new=AsyncMock()),
        ):
            data = run(self._publisher()._post_json("https://x", {"a": 1}))
        assert data == {"ok": True}
        assert len(session.calls) == 1

    def test_retries_on_5xx_then_succeeds(self) -> None:
        session = _FakeSession(
            [
                _FakeResponse(502, {"error": "bad gateway"}),
                _FakeResponse(200, {"ok": True}),
            ]
        )
        with (
            patch("distribution.publisher.aiohttp.ClientSession", return_value=session),
            patch("distribution.publisher.asyncio.sleep", new=AsyncMock()),
        ):
            data = run(self._publisher()._post_json("https://x", {}))
        assert data == {"ok": True}
        assert len(session.calls) == 2

    def test_no_retry_on_4xx(self) -> None:
        session = _FakeSession([_FakeResponse(400, {"ok": False})])
        with (
            patch("distribution.publisher.aiohttp.ClientSession", return_value=session),
            patch("distribution.publisher.asyncio.sleep", new=AsyncMock()),
        ):
            data = run(self._publisher()._post_json("https://x", {}))
        assert data == {"ok": False}
        assert len(session.calls) == 1

    def test_exhausts_retries_on_network_error(self) -> None:
        session = _FakeSession([aiohttp.ClientConnectionError("down")] * 3)
        with (
            patch("distribution.publisher.aiohttp.ClientSession", return_value=session),
            patch("distribution.publisher.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(aiohttp.ClientConnectionError):
                run(self._publisher()._post_json("https://x", {}))
        assert len(session.calls) == 3


# ---------------------------------------------------------------------------
# publish_daily_digest 统一入口
# ---------------------------------------------------------------------------


class _FakePublisher(BasePublisher):
    """注册表替换用的假发布器，按脚本回放结果。"""

    channel = "fake"

    def __init__(self, outcome: PublishResult) -> None:
        self.outcome = outcome
        self.received: str | None = None

    async def send_message(
        self, content: str, target: str | None = None
    ) -> PublishResult:
        self.received = content
        return self.outcome

    async def send_digest(
        self, digest_text: str, target: str | None = None
    ) -> PublishResult:
        self.received = digest_text
        return self.outcome


class TestPublishDailyDigest:
    def _install_fake(
        self, monkeypatch: pytest.MonkeyPatch, outcome: PublishResult
    ) -> _FakePublisher:
        fake = _FakePublisher(outcome)
        monkeypatch.setitem(PUBLISHER_CLASSES, "telegram", lambda: fake)
        return fake

    def test_publishes_telegram_digest_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "2026-04-11-a.json").write_text(
            json.dumps(
                {
                    "title": "demo/repo",
                    "source": "github",
                    "source_url": "https://github.com/demo/repo",
                    "summary": "摘要",
                    "tags": ["ai"],
                    "category": "AI Agent",
                    "innovation_score": 4,
                    "collected_at": "2026-04-11T00:00:00+00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        fake = self._install_fake(
            monkeypatch, PublishResult(channel="telegram", success=True)
        )
        results = run(
            publish_daily_digest(
                knowledge_dir=tmp_path, date="2026-04-11", channels=["telegram"]
            )
        )
        assert len(results) == 1
        assert results[0].success is True
        assert "demo/repo" in (fake.received or "")

    def test_failure_of_one_channel_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = self._install_fake(
            monkeypatch,
            PublishResult(channel="telegram", success=False, error="未配置凭证"),
        )
        results = run(
            publish_daily_digest(
                knowledge_dir=tmp_path,
                date="2026-04-11",  # 无数据日期 → 📭 占位文案照发
                channels=["telegram"],
            )
        )
        assert results[0].success is False
        assert "📭" in (fake.received or "")

    def test_unknown_channel_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="未知渠道"):
            run(
                publish_daily_digest(
                    knowledge_dir=tmp_path, date="2026-04-11", channels=["feishu"]
                )
            )

    def test_unexpected_publisher_exception_becomes_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _CrashPublisher(_FakePublisher):
            async def send_digest(
                self, digest_text: str, target: str | None = None
            ) -> PublishResult:
                raise RuntimeError("unexpected")

        monkeypatch.setitem(
            PUBLISHER_CLASSES,
            "telegram",
            lambda: _CrashPublisher(PublishResult(channel="telegram", success=True)),
        )
        results = run(
            publish_daily_digest(
                knowledge_dir=tmp_path, date="2026-04-11", channels=["telegram"]
            )
        )
        assert results[0].success is False
        assert "RuntimeError" in (results[0].error or "")
