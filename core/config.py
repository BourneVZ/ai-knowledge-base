"""项目级采集/审核配置加载 — V2 流水线与 V3 工作流共用。

从项目根目录 ``config.yaml`` 读取配置（缺失/解析失败时回退内置默认）。
提供两个入口：
- :func:`load_config`：返回原始配置 dict。
- :func:`resolve_plan`：根据 ``target_count`` 选择规划档位，返回 plan 参数字典。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)

CONFIG_PATH: Path = Path(__file__).resolve().parent.parent / "config.yaml"
"""配置文件默认路径（项目根目录）。"""

DEFAULT_CONFIG: dict[str, Any] = {
    "collection": {
        "github_search_query": "ai OR llm OR agent in:topics",
        "github_created_within_days": 0,
        "rss_per_source_limit": 3,
        "rss_total_limit": 10,
    },
    "plan": {
        "target_count": 10,
        "tiers": {
            "lite": {
                "github_limit": 5,
                "min_innovation_score": 3,
                "max_iterations": 1,
            },
            "standard": {
                "github_limit": 10,
                "min_innovation_score": 2,
                "max_iterations": 2,
            },
            "full": {
                "github_limit": 20,
                "min_innovation_score": 1,
                "max_iterations": 3,
            },
        },
    },
    "review": {
        "pass_threshold": 7.0,
    },
}
"""内置默认配置，作为 config.yaml 缺失/解析失败时的回退。"""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个 dict，override 覆盖 base 的同名 key。"""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    """加载根目录 ``config.yaml``，缺失/解析失败时回退默认配置。

    Returns:
        配置 dict（已与默认配置合并，保证所有 key 存在）。
    """
    if yaml is None:
        logger.info("PyYAML 未安装，使用内置默认配置")
        return _deep_merge(DEFAULT_CONFIG, {})

    if not CONFIG_PATH.is_file():
        logger.warning("配置文件 %s 不存在，使用内置默认配置", CONFIG_PATH)
        return _deep_merge(DEFAULT_CONFIG, {})

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning("配置文件解析失败: %s，使用内置默认配置", e)
        return _deep_merge(DEFAULT_CONFIG, {})

    if not isinstance(data, dict):
        logger.warning("配置文件格式错误，使用内置默认配置")
        return _deep_merge(DEFAULT_CONFIG, {})

    return _deep_merge(DEFAULT_CONFIG, data)


def resolve_plan() -> dict[str, Any]:
    """根据 ``config.plan.target_count`` 选择规划档位，返回 plan 参数。

    Returns:
        plan 字典，包含 ``strategy``、``github_limit``、
        ``min_innovation_score``、``max_iterations``、``target_count`` 与 ``rationale``。
    """
    cfg = load_config()
    plan_cfg = cfg.get("plan", {})
    target_count = int(plan_cfg.get("target_count", 10))
    tiers = plan_cfg.get("tiers", {})

    if target_count >= 20:
        strategy = "full"
    elif target_count >= 10:
        strategy = "standard"
    else:
        strategy = "lite"

    tier = dict(tiers.get(strategy, {}))
    return {
        "strategy": strategy,
        "github_limit": int(tier.get("github_limit", 10)),
        "min_innovation_score": int(tier.get("min_innovation_score", 2)),
        "max_iterations": int(tier.get("max_iterations", 2)),
        "target_count": target_count,
        "rationale": f"目标 {target_count} 条，采用 {strategy} 档",
    }
