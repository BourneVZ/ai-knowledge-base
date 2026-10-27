"""publisher.py — 多渠道内容发布器。

异步发布知识库简报到 Telegram 与企业微信群机器人。消息格式化由
``formatter`` 纯函数层完成，本模块只负责发送、重试与结果记录；
发布前的 ``approved`` 状态检查也归本层（待接入状态流转后补充）。

凭证从主仓库根 ``.env`` 注入（python-dotenv，可选依赖），渠道缺凭证
时返回 ``PublishResult(success=False)`` 而非抛异常，便于无凭证
dry-run。渠道集合为 ``{"telegram", "weixin"}``（飞书已放弃，见
formatter 模块说明）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from distribution.formatter import (
    DEFAULT_TOP_N,
    REPO_ROOT,
    generate_daily_digest,
)

logger = logging.getLogger(__name__)

CHANNEL_TELEGRAM = "telegram"
"""Telegram 渠道名（与 digest 结果 key 一致）。"""

CHANNEL_WEIXIN = "weixin"
"""企业微信渠道名（与 digest 结果 key 一致）。"""

TELEGRAM_API_BASE = "https://api.telegram.org"
"""Telegram Bot API 地址前缀。"""

REQUEST_TIMEOUT_SECONDS = 30
"""单次 HTTP 请求总超时（秒），遵守仓库网络请求规范。"""

MAX_RETRIES = 3
"""请求最大尝试次数（含首次）。"""

RETRY_BACKOFF_SECONDS = 1.0
"""重试指数退避基数：第 n 次失败后等待 ``基数 * 2**(n-1)`` 秒。"""

WEIXIN_MAX_CONTENT_BYTES = 4000
"""企业微信 markdown content 分段字节上限（官方 4096，留余量）。"""

ENV_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_TELEGRAM_PROXY = "TELEGRAM_PROXY"
ENV_WEIXIN_WEBHOOK_URL = "WEIXIN_WEBHOOK_URL"


def _load_dotenv() -> None:
    """从主仓库根加载 ``.env``（路径固定，不依赖运行目录）。

    优先使用 python-dotenv；未安装时记录警告而非静默跳过，避免环境
    变量未注入导致发布失败却无提示。
    """
    env_path = REPO_ROOT / ".env"
    try:
        from dotenv import load_dotenv as _ld
    except ImportError:
        logger.warning(
            "未安装 python-dotenv，无法加载 %s（请执行 pip install python-dotenv）",
            env_path,
        )
        return
    if env_path.is_file():
        _ld(dotenv_path=env_path, override=False)
        logger.debug("已加载环境变量文件: %s", env_path)
    else:
        logger.info(".env 文件不存在，仅使用进程环境变量: %s", env_path)


_load_dotenv()


# ---------------------------------------------------------------------------
# 结果与抽象基类
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    """单次发布的结果。

    Attributes:
        channel: 渠道名（``telegram`` / ``weixin``）。
        success: 是否全部发送成功（微信分段时要求每段都成功）。
        message_id: 渠道返回的消息标识；Telegram 为 message_id，
            微信无对应字段，用 ``"N 段"`` 表示分段数，失败时为 ``None``。
        error: 失败原因描述；成功时为 ``None``。
        timestamp: 结果产生时间（ISO 8601，本地时区）。
    """

    channel: str
    success: bool
    message_id: str | None = None
    error: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now().astimezone().isoformat()
    )


class BasePublisher(ABC):
    """发布器抽象基类，约定统一的双接口与共享 HTTP 层。

    ``target`` 为渠道目标的统一占位：Telegram 为 chat_id，企业微信为
    webhook URL（覆盖构造时的默认值），均为 ``None`` 时用渠道默认目标。
    """

    channel: str = ""

    @abstractmethod
    async def send_message(
        self, content: str, target: str | None = None
    ) -> PublishResult:
        """发送单条已格式化消息。

        Args:
            content: 渠道格式的消息文本。
            target: 渠道目标（chat_id / webhook URL）；``None`` 用默认。

        Returns:
            发布结果。
        """

    @abstractmethod
    async def send_digest(
        self, digest_text: str, target: str | None = None
    ) -> PublishResult:
        """发送整条简报文本。

        Args:
            digest_text: 渠道格式的简报全文。
            target: 渠道目标；``None`` 用默认。

        Returns:
            发布结果。
        """

    def _failure(self, error: str) -> PublishResult:
        """构造当前渠道的失败结果并记录日志。"""
        logger.error("[Publisher:%s] 发布失败: %s", self.channel, error)
        return PublishResult(channel=self.channel, success=False, error=error)

    async def _post_json(
        self, url: str, payload: dict[str, Any], proxy: str | None = None
    ) -> dict[str, Any]:
        """带超时与指数退避重试的 JSON POST（子类共享，便于测试 mock）。

         网络异常与 5xx 响应重试（最多 ``MAX_RETRIES`` 次），4xx 与正常
        响应直接返回由调用方按渠道协议判定成败。

        Args:
            url: 请求地址（含凭证，仅传入不落日志）。
            payload: JSON 请求体。
            proxy: HTTP 代理地址（如 ``http://127.0.0.1:7890``）；
                ``None`` 直连。仅 aiohttp 原生支持的 http 代理。

        Returns:
            渠道响应反序列化后的 JSON 对象。

        Raises:
            aiohttp.ClientError: 重试耗尽后的最后一次网络/5xx 错误。
            asyncio.TimeoutError: 重试耗尽后仍超时。
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload, proxy=proxy) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status >= 500:
                            raise aiohttp.ClientResponseError(
                                resp.request_info,
                                resp.history,
                                status=resp.status,
                                message=f"HTTP {resp.status}",
                            )
                        if isinstance(data, dict):
                            return data
                        raise aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                            message="响应不是 JSON 对象",
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt >= MAX_RETRIES:
                    break
                wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "[Publisher:%s] 请求失败（第 %d/%d 次），%.1fs 后重试: %s",
                    self.channel,
                    attempt,
                    MAX_RETRIES,
                    wait,
                    type(e).__name__,
                )
                await asyncio.sleep(wait)
        if last_error is None:
            raise RuntimeError("重试循环异常退出且无错误记录")
        raise last_error


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TelegramPublisher(BasePublisher):
    """通过 Telegram Bot API 异步发送 MarkdownV2 消息。

    凭证：``TELEGRAM_BOT_TOKEN`` 与 ``TELEGRAM_CHAT_ID``（.env 或环境
    变量）。可选 ``TELEGRAM_PROXY``（http 代理，如
    ``http://127.0.0.1:7890``）——api.telegram.org 在国内网络需代理
    才可达。简报文本由 formatter 保证不超过单条 4096 上限，本类不再
    分段。
    """

    channel = CHANNEL_TELEGRAM

    def __init__(
        self,
        token: str | None = None,
        default_chat_id: str | None = None,
        proxy: str | None = None,
    ):
        """初始化发布器并读取凭证。

        Args:
            token: Bot token；``None`` 时读 ``TELEGRAM_BOT_TOKEN``。
            default_chat_id: 默认 chat_id；``None`` 时读
                ``TELEGRAM_CHAT_ID``。
            proxy: HTTP 代理地址；``None`` 时读 ``TELEGRAM_PROXY``
                （未设置则直连）。
        """
        self.token = (
            token if token is not None else os.environ.get(ENV_TELEGRAM_BOT_TOKEN, "")
        )
        self.default_chat_id = (
            default_chat_id
            if default_chat_id is not None
            else os.environ.get(ENV_TELEGRAM_CHAT_ID, "")
        )
        self.proxy = (
            proxy
            if proxy is not None
            else (os.environ.get(ENV_TELEGRAM_PROXY, "").strip() or None)
        )

    async def send_message(
        self, content: str, target: str | None = None
    ) -> PublishResult:
        """发送单条 MarkdownV2 消息。

        Args:
            content: MarkdownV2 文本。
            target: chat_id；``None`` 用 ``TELEGRAM_CHAT_ID``。

        Returns:
            发布结果（含 Telegram message_id）。
        """
        chat_id = target or self.default_chat_id
        if not self.token:
            return self._failure(f"环境变量 {ENV_TELEGRAM_BOT_TOKEN} 未配置")
        if not chat_id:
            return self._failure(f"环境变量 {ENV_TELEGRAM_CHAT_ID} 未配置")

        payload = {
            "chat_id": chat_id,
            "text": content,
            "parse_mode": "MarkdownV2",
        }
        try:
            data = await self._post_json(
                f"{TELEGRAM_API_BASE}/bot{self.token}/sendMessage",
                payload,
                proxy=self.proxy,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(
                "[Publisher:telegram] 请求失败 chat_id=%s: %s",
                chat_id,
                type(e).__name__,
            )
            return self._failure(f"网络错误: {type(e).__name__}")

        if data.get("ok"):
            result = data.get("result")
            message_id = (
                str(result.get("message_id", "")) if isinstance(result, dict) else ""
            )
            logger.info("[Publisher:telegram] 发送成功 message_id=%s", message_id)
            return PublishResult(
                channel=self.channel, success=True, message_id=message_id
            )
        return self._failure(str(data.get("description", "未知错误")))

    async def send_digest(
        self, digest_text: str, target: str | None = None
    ) -> PublishResult:
        """发送简报（Telegram 无分段需求，直接单条发送）。"""
        return await self.send_message(digest_text, target)


# ---------------------------------------------------------------------------
# 企业微信
# ---------------------------------------------------------------------------


class WeixinPublisher(BasePublisher):
    """通过企业微信群机器人 webhook 异步发送 markdown 消息。

    凭证：``WEIXIN_WEBHOOK_URL``（.env 或环境变量）。官方 content 上限
    4096 字节，简报超限时按行边界分段、逐段顺序发送，任一段失败即整体
    失败（错误信息含段序号）。
    """

    channel = CHANNEL_WEIXIN

    def __init__(self, webhook_url: str | None = None) -> None:
        """初始化发布器并读取凭证。

        Args:
            webhook_url: 群机器人 webhook 地址；``None`` 时读
                ``WEIXIN_WEBHOOK_URL``。
        """
        self.webhook_url = (
            webhook_url
            if webhook_url is not None
            else os.environ.get(ENV_WEIXIN_WEBHOOK_URL, "")
        )

    async def send_message(
        self, content: str, target: str | None = None
    ) -> PublishResult:
        """发送 markdown 消息（超限自动分段）。

        Args:
            content: 企业微信 markdown 文本。
            target: webhook URL；``None`` 用 ``WEIXIN_WEBHOOK_URL``。

        Returns:
            发布结果；成功时 ``message_id`` 为 ``"N 段"``。
        """
        url = target or self.webhook_url
        if not url:
            return self._failure(f"环境变量 {ENV_WEIXIN_WEBHOOK_URL} 未配置")

        chunks = split_weixin_content(content)
        sent = 0
        errors: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            payload = {"msgtype": "markdown", "markdown": {"content": chunk}}
            try:
                data = await self._post_json(url, payload)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(
                    "[Publisher:weixin] 第 %d/%d 段请求失败: %s",
                    index,
                    len(chunks),
                    type(e).__name__,
                )
                errors.append(f"第{index}段网络错误: {type(e).__name__}")
                continue
            if data.get("errcode") == 0:
                sent += 1
            else:
                errors.append(
                    f"第{index}段 errcode={data.get('errcode')}: {data.get('errmsg')}"
                )

        if errors:
            return PublishResult(
                channel=self.channel,
                success=False,
                message_id=f"{sent}/{len(chunks)} 段" if sent else None,
                error="; ".join(errors),
            )
        logger.info("[Publisher:weixin] 发送成功（%d 段）", len(chunks))
        return PublishResult(
            channel=self.channel, success=True, message_id=f"{len(chunks)} 段"
        )

    async def send_digest(
        self, digest_text: str, target: str | None = None
    ) -> PublishResult:
        """发送简报（复用分段发送逻辑）。"""
        return await self.send_message(digest_text, target)


def split_weixin_content(
    text: str, max_bytes: int = WEIXIN_MAX_CONTENT_BYTES
) -> list[str]:
    """按字节上限将微信 markdown 文本切分为可发送的段。

    优先按行边界切分以保持 markdown 结构；单行自身超限时退化为字符级
    硬切。空文本返回单段空串，保证调用方恒有可发送内容。

    Args:
        text: 完整 markdown 文本。
        max_bytes: 每段字节上限。

    Returns:
        段列表，每段 UTF-8 字节数不超过 ``max_bytes``。

    Raises:
        ValueError: ``max_bytes`` 非正。
    """
    if max_bytes <= 0:
        raise ValueError(f"max_bytes 必须为正整数，收到: {max_bytes}")

    def _bytes(value: str) -> int:
        return len(value.encode("utf-8"))

    if _bytes(text) <= max_bytes:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_bytes = 0
    for line in text.split("\n"):
        line_bytes = _bytes(line)
        if line_bytes > max_bytes:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines, current_bytes = [], 0
            piece = ""
            for char in line:
                if _bytes(piece + char) > max_bytes:
                    chunks.append(piece)
                    piece = char
                else:
                    piece += char
            current_lines = [piece]
            current_bytes = _bytes(piece)
            continue
        # 换行符只在块内已有行时才产生，避免逐行 +1 高估块大小
        increment = line_bytes + (1 if current_lines else 0)
        if current_bytes + increment > max_bytes:
            chunks.append("\n".join(current_lines))
            current_lines, current_bytes = [], 0
            increment = line_bytes
        current_lines.append(line)
        current_bytes += increment
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


PUBLISHER_CLASSES: dict[str, type[BasePublisher]] = {
    CHANNEL_TELEGRAM: TelegramPublisher,
    CHANNEL_WEIXIN: WeixinPublisher,
}
"""渠道名到发布器类的注册表（测试可替换）。"""


async def publish_daily_digest(
    knowledge_dir: str | Path | None = None,
    date: str | None = None,
    channels: list[str] | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> list[PublishResult]:
    """生成每日简报并发布到所有启用渠道（统一异步入口）。

    调用 ``generate_daily_digest`` 生成三渠道文本，按 ``channels``
    实例化发布器并发发发送；某渠道缺凭证或失败只影响该渠道的
    ``PublishResult``，不中断其它渠道。

    Args:
        knowledge_dir: 知识条目目录；``None`` 用主仓库默认路径。
        date: ``YYYY-MM-DD``；``None`` 取当前 UTC 日期。
        channels: 渠道名列表；``None`` 默认 ``["telegram", "weixin"]``。
        top_n: 平铺模式取前 N 篇（透传给 digest）。

    Returns:
        与 ``channels`` 等长的发布结果列表（顺序一致）。

    Raises:
        ValueError: 存在未注册的渠道名，或 digest 参数非法。
    """
    enabled = channels if channels is not None else [CHANNEL_TELEGRAM, CHANNEL_WEIXIN]
    unknown = [ch for ch in enabled if ch not in PUBLISHER_CLASSES]
    if unknown:
        raise ValueError(f"未知渠道: {unknown}，可选: {sorted(PUBLISHER_CLASSES)}")

    digest = generate_daily_digest(knowledge_dir=knowledge_dir, date=date, top_n=top_n)

    async def _publish(channel: str) -> PublishResult:
        publisher = PUBLISHER_CLASSES[channel]()
        try:
            return await publisher.send_digest(digest[channel])
        except Exception as e:
            # 渠道级兜底：未预期异常记录后转为失败结果，不中断其它渠道
            logger.error(
                "[Publisher:%s] 发布异常: %s: %s", channel, type(e).__name__, e
            )
            return PublishResult(
                channel=channel, success=False, error=f"{type(e).__name__}: {e}"
            )

    results = await asyncio.gather(*(_publish(ch) for ch in enabled))
    logger.info(
        "[Publisher] 简报发布完成：%s",
        ", ".join(f"{r.channel}={'OK' if r.success else 'FAIL'}" for r in results),
    )
    return list(results)
