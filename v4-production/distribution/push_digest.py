"""简报推送 CLI 入口 — 供 CI / 本地手动推送日报或周报。

用法::

    # 日报：某日知识简报（默认今天 UTC）
    python v4-production/distribution/push_digest.py [--date YYYY-MM-DD]

    # 周报：日期窗口 [since, 今天] 的跨日简报
    python v4-production/distribution/push_digest.py --since YYYY-MM-DD

    # 公共参数
    #   --channels telegram[,weixin]   渠道列表，默认 telegram
    #   --knowledge-dir PATH           知识条目目录，默认主仓库 knowledge/articles

``--date`` 与 ``--since`` 互斥。渠道凭证走环境变量
（``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` / ``WEIXIN_WEBHOOK_URL``，
本地运行时 ``publisher`` 会自动读取 ``.env``）。

目录名 ``v4-production`` 含连字符不能作 Python 包名，这里把自身父目录
加入 ``sys.path`` 后导入 ``distribution`` 包（与 ``tests/conftest.py``
同法）。某渠道失败不影响其它渠道，但任一失败以退出码 1 结束，便于
CI 在 continue-on-error 下仍给出可见告警。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from distribution.formatter import generate_range_digest  # noqa: E402
from distribution.publisher import (  # noqa: E402
    PUBLISHER_CLASSES,
    PublishResult,
    publish_daily_digest,
)

logger = logging.getLogger(__name__)


async def _publish_range(
    digest: dict[str, str], channels: list[str]
) -> list[PublishResult]:
    """把已生成的周报文本并发发送到各渠道（渠道级失败不互相阻断）。"""

    async def _send(channel: str) -> PublishResult:
        publisher = PUBLISHER_CLASSES[channel]()
        try:
            return await publisher.send_digest(digest[channel])
        except Exception as e:
            logger.error(
                "[Publisher:%s] 发布异常: %s: %s", channel, type(e).__name__, e
            )
            return PublishResult(
                channel=channel, success=False, error=f"{type(e).__name__}: {e}"
            )

    return list(await asyncio.gather(*(_send(ch) for ch in channels)))


def main() -> int:
    """解析参数、推送日报/周报并汇报各渠道结果。

    Returns:
        0 表示全部启用渠道推送成功；1 表示至少一个渠道失败。
    """
    parser = argparse.ArgumentParser(description="生成并推送知识库日报/周报")
    parser.add_argument(
        "--date", default=None, help="日报日期 YYYY-MM-DD，默认今天（UTC）"
    )
    parser.add_argument(
        "--since",
        default=None,
        help="周报起始日期 YYYY-MM-DD（窗口含端点，至今天），与 --date 互斥",
    )
    parser.add_argument(
        "--channels",
        default="telegram",
        help="逗号分隔的渠道列表：telegram / weixin，默认 telegram",
    )
    parser.add_argument(
        "--knowledge-dir",
        default=None,
        help="知识条目目录，默认主仓库 knowledge/articles",
    )
    args = parser.parse_args()

    if args.date is not None and args.since is not None:
        parser.error("--date 与 --since 互斥，只能二选一")

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-8s | %(name)s | %(message)s"
    )
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    if args.since is not None:
        digest = generate_range_digest(
            knowledge_dir=args.knowledge_dir, since=args.since
        )
        # 空窗口时 formatter 返回「📭 … 暂无新增」占位，静默跳过不发送
        if digest["telegram"].startswith("📭"):
            print("[weekly] 窗口内无新增条目，跳过推送")
            return 0
        results = asyncio.run(_publish_range(digest, channels))
    else:
        results = asyncio.run(
            publish_daily_digest(
                knowledge_dir=args.knowledge_dir,
                date=args.date,
                channels=channels,
            )
        )
    any_failed = False
    for result in results:
        status = "OK" if result.success else f"FAIL: {result.error}"
        print(f"[{result.channel}] {status}")
        any_failed = any_failed or not result.success
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
