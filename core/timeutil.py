"""打卡所需的逻辑日期计算。

"今天"以 *逻辑日期* 为准：把本地时间往前推 ``reset_hour`` 小时，再取日历日期。
``reset_hour=0`` 时就是自然日；``reset_hour=4`` 时凌晨 0:00–3:59 仍算前一天。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

DAY_SECONDS = 86400.0


def normalize_reset_hour(value: object, default: int = 0) -> int:
    """把配置里的重置小时数收敛到 0..23。

    Args:
        value: 原始配置值，可能是字符串、浮点数或非法值。
        default: 无法解析时使用的默认值。

    Returns:
        0 到 23 之间的整数小时。
    """
    try:
        hour = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if hour < 0 or hour > 23:
        return default
    return hour


def logical_date(ts: float, reset_hour: int = 0) -> date:
    """返回时间戳对应的逻辑日期。

    Args:
        ts: Unix 时间戳（秒）。
        reset_hour: 每日重置小时数，0..23。

    Returns:
        逻辑日期。
    """
    moment = datetime.fromtimestamp(float(ts)) - timedelta(hours=normalize_reset_hour(reset_hour))
    return moment.date()


def logical_date_str(ts: float, reset_hour: int = 0) -> str:
    """返回 ``YYYY-MM-DD`` 形式的逻辑日期字符串。"""
    return logical_date(ts, reset_hour).isoformat()


def previous_day(latest: str) -> str:
    """返回 ``YYYY-MM-DD`` 字符串的前一天。

    Args:
        latest: ``YYYY-MM-DD`` 形式的日期字符串。

    Returns:
        前一天的 ``YYYY-MM-DD`` 字符串；输入非法时返回空串。
    """
    try:
        return (date.fromisoformat(latest) - timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return ""


def days_ago(latest: str, days: int) -> str:
    """返回 ``YYYY-MM-DD`` 字符串往前 ``days`` 天的日期。"""
    try:
        return (date.fromisoformat(latest) - timedelta(days=int(days))).isoformat()
    except (TypeError, ValueError):
        return ""


def next_midnight_ts(ts: float, reset_hour: int = 0) -> float:
    """返回下一个逻辑日期开始的时间戳，用于提示"还剩多久重置"。"""
    moment = datetime.fromtimestamp(float(ts)) - timedelta(hours=normalize_reset_hour(reset_hour))
    tomorrow = datetime.combine(moment.date() + timedelta(days=1), datetime.min.time())
    return tomorrow.timestamp() + normalize_reset_hour(reset_hour) * 3600.0


def seconds_until_reset(ts: float, reset_hour: int = 0) -> float:
    """返回距离下一次逻辑日期开始的秒数（非负）。"""
    return max(0.0, next_midnight_ts(ts, reset_hour) - float(ts))
