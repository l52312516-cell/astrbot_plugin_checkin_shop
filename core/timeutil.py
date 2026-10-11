"""打卡所需的逻辑日期计算。

"今天"以 *逻辑日期* 为准：把本地时间往前推 ``reset_hour`` 小时，再取日历日期。
``reset_hour=0`` 时就是自然日；``reset_hour=4`` 时凌晨 0:00–3:59 仍算前一天。

时区使用固定的 UTC+8（``PLUGIN_TIMEZONE``）。原因是打卡是"给人看的日历日"，
必须与用户所在时区一致：容器里的 ``TZ`` 往往是 UTC，若按进程本地时区取日期，
北京时间的 10-11 07:00 会被算成 10-10，于是"第二天早上打卡"会被判成同一天，
用户会收到"今天已经打过卡啦"。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

DAY_SECONDS = 86400.0

#: 逻辑日期使用的固定时区：UTC+8（中国标准时间，即 Asia/Shanghai）。
PLUGIN_TIMEZONE = timezone(timedelta(hours=8))


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


def _moment(ts: float) -> datetime:
    """把时间戳转成 UTC+8 的墙上时间。"""
    return datetime.fromtimestamp(float(ts), tz=PLUGIN_TIMEZONE)


def logical_date(ts: float, reset_hour: int = 0) -> date:
    """返回时间戳对应的逻辑日期。

    Args:
        ts: Unix 时间戳（秒）。
        reset_hour: 每日重置小时数，0..23。

    Returns:
        逻辑日期。
    """
    moment = _moment(ts) - timedelta(hours=normalize_reset_hour(reset_hour))
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


def next_reset_ts(ts: float, reset_hour: int = 0) -> float:
    """返回下一个逻辑日期开始的时间戳，用于提示"下次可打卡时间"。

    Args:
        ts: 当前 Unix 时间戳（秒）。
        reset_hour: 每日重置小时数，0..23。

    Returns:
        下一次重置时刻的时间戳（一定大于 ``ts``）。
    """
    hour = normalize_reset_hour(reset_hour)
    moment = _moment(ts) - timedelta(hours=hour)
    next_day = datetime.combine(
        moment.date() + timedelta(days=1), datetime.min.time(), tzinfo=PLUGIN_TIMEZONE
    )
    return (next_day + timedelta(hours=hour)).timestamp()


def next_midnight_ts(ts: float, reset_hour: int = 0) -> float:
    """兼容旧名字：返回下一个逻辑日期开始的时间戳。"""
    return next_reset_ts(ts, reset_hour)


def seconds_until_reset(ts: float, reset_hour: int = 0) -> float:
    """返回距离下一次逻辑日期开始的秒数（非负）。"""
    return max(0.0, next_reset_ts(ts, reset_hour) - float(ts))


def format_moment(ts: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把时间戳格式化成 UTC+8 的墙上时间文本。

    Args:
        ts: Unix 时间戳（秒）。
        fmt: ``strftime`` 格式串。

    Returns:
        格式化后的文本；时间戳非法时返回空串。
    """
    try:
        stamp = float(ts)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return _moment(stamp).strftime(fmt)


def describe_next_reset(ts: float, reset_hour: int = 0) -> str:
    """生成"下次可打卡时间"的中文提示，例如 ``明天 00:00（还有 2 小时 5 分钟）``。

    Args:
        ts: 当前 Unix 时间戳（秒）。
        reset_hour: 每日重置小时数，0..23。

    Returns:
        可直接拼进回复文案的提示片段。
    """
    stamp = float(ts)
    nxt = next_reset_ts(stamp, reset_hour)
    remaining = max(0, int(round(nxt - stamp)))
    hours, rest = divmod(remaining, 3600)
    minutes = rest // 60
    if hours and minutes:
        left = f"{hours} 小时 {minutes} 分钟"
    elif hours:
        left = f"{hours} 小时"
    elif minutes:
        left = f"{minutes} 分钟"
    else:
        left = "不到 1 分钟"

    reset_moment = datetime.fromtimestamp(nxt, tz=PLUGIN_TIMEZONE)
    now_moment = datetime.fromtimestamp(stamp, tz=PLUGIN_TIMEZONE)
    clock = reset_moment.strftime("%H:%M")
    # 下一次重置必然落在 24 小时以内，所以只会是"今天"或"明天"。
    if reset_moment.date() == now_moment.date():
        when = f"今天 {clock}"
    else:
        when = f"明天 {clock}"
    return f"{when}（还有 {left}）"
