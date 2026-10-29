"""简报推送 CLI 入口 — 供 CI / 本地手动推送某日知识简报。

用法::

    python v4-production/distribution/push_digest.py \
        [--date YYYY-MM-DD] [--channels telegram[,weixin]] [--knowledge-dir PATH]

``--date`` 默认今天（UTC，与 organizer 的文件命名口径一致）；渠道凭证走
环境变量（``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` /
``WEIXIN_WEBHOOK_URL``，本地运行时 ``publisher`` 会自动读取 ``.env``）。

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

from distribution.publisher import publish_daily_digest  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> int:
    """解析参数、推送简报并汇报各渠道结果。

    Returns:
        0 表示全部启用渠道推送成功；1 表示至少一个渠道失败。
    """
    parser = argparse.ArgumentParser(description="生成并推送知识库每日简报")
    parser.add_argument(
        "--date", default=None, help="YYYY-MM-DD，默认今天（UTC）"
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

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-8s | %(name)s | %(message)s"
    )
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
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
