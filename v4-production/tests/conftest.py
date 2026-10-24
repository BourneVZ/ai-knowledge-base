"""v4-production 测试公共夹具。

``v4-production`` 目录名含连字符，无法作为 Python 包名，这里将其加入
``sys.path`` 使 ``distribution`` 等子包可作为顶层包导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

V4_ROOT = Path(__file__).resolve().parent.parent
if str(V4_ROOT) not in sys.path:
    sys.path.insert(0, str(V4_ROOT))
