"""可重启结算的虚拟积分玩法；调用方在 Store.mutate 的快照内调用写操作。

金额为整数积分，股票价格为百分之一积分，费率为万分比。计息使用整数微秒，
随机行情由私有种子和整点序号确定；可展示的响应不包含种子、下注明细或操作回执。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import secrets
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .timeutil import PLUGIN_TIMEZONE, logical_date_str, normalize_reset_hour


class EconomyError(ValueError):
    """可向用户展示的校验或业务错误。"""


MAX_INTEGER = 9_007_199_254_740_991
MAX_TIMESTAMP = 253_402_271_999  # UTC+8 9999-12-31，确保可格式化与有限比较。
INTEREST_DENOMINATOR = 10_000 * 86_400 * 1_000_000
STOCKS = {
    "ST01": "星河科技", "ST02": "喵喵食品", "ST03": "云海能源", "ST04": "像素娱乐",
}
_STOCK_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,11}$")
TERMINAL = {"resolved", "cancelled", "void"}
DEFAULT_SETTINGS: dict[str, Any] = {
    "stocks_enabled": False, "savings_enabled": False, "predictions_enabled": False,
    "auto_predictions_enabled": False, "lottery_enabled": False,
    "checkin_cards_enabled": False, "rebate_cards_enabled": False, "multiplier_cards_enabled": False,
    "stock_fee_bps": 100, "stock_hourly_bps": 200, "stock_daily_bps": 1000,
    "stock_daily_trades": 20, "stock_daily_buy_limit": 1000,
    "savings_rate_bps": 5, "savings_principal_cap": 5000,
    "prediction_min": 5, "prediction_max": 50, "prediction_daily_limit": 100,
    "prediction_payout_bps": 9500, "prediction_timeout_days": 7,
    "auto_stock_code": "ST01", "auto_close_minutes": 5,
    "lottery_cost": 10, "lottery_daily_limit": 3,
    "lottery_prizes": [
        {"points": 0, "weight": 45}, {"points": 5, "weight": 20},
        {"points": 10, "weight": 25}, {"points": 40, "weight": 9},
        {"points": 100, "weight": 1},
    ],
    "checkin_card_daily_limit": 1, "rebate_card_daily_limit": 1, "multiplier_card_daily_limit": 1,
}


def default_economy() -> dict[str, Any]:
    return {
        "settings": copy.deepcopy(DEFAULT_SETTINGS), "savings": {},
        "market": {
            "seed": secrets.token_hex(32), "last_hour": None,
            "stocks": {
                code: _default_stock(code, name, 10000)
                for code, name in STOCKS.items()
            },
            "removed_stocks": [],
        },
        "positions": {}, "predictions": {}, "daily_usage": {}, "cards": {}, "operations": {},
    }


def _default_stock(code: str, name: str, price: int) -> dict[str, Any]:
    return {"code": code, "name": name, "price": price, "open_price": price,
            "date": "", "last_updated": 0, "history": [], "buy_enabled": True,
            "enabled": True, "initial_price_cent": price, "hourly_bps": None,
            "daily_bps": None, "fee_bps": None, "revision": 1, "trade_count": 0}


def ensure(data: dict[str, Any]) -> dict[str, Any]:
    """仅补缺省值，保留账户、结算凭据和未知的未来字段。"""
    if not isinstance(data, dict):
        raise EconomyError("运行数据必须是对象")
    economy = data.get("economy")
    if not isinstance(economy, dict):
        economy = default_economy()
        data["economy"] = economy
    for key in ("settings", "savings", "positions", "predictions", "daily_usage", "cards", "operations"):
        if key not in economy:
            economy[key] = {} if key != "settings" else copy.deepcopy(DEFAULT_SETTINGS)
        if not isinstance(economy[key], dict):
            raise EconomyError(f"积分玩法数据 {key} 格式不正确")
    for key, value in DEFAULT_SETTINGS.items():
        economy["settings"].setdefault(key, copy.deepcopy(value))
    _validated_settings({key: economy["settings"][key] for key in DEFAULT_SETTINGS if key != "auto_stock_code"}, DEFAULT_SETTINGS)
    for key in ("savings", "positions", "predictions", "daily_usage", "cards", "operations"):
        if any(not isinstance(record, dict) for record in economy[key].values()):
            raise EconomyError(f"积分玩法数据 {key} 的记录必须是对象")
    for account in economy["savings"].values():
        _integer(account.get("principal", 0), "存款本金")
        for key in ("last_accrual_us", "interest_remainder", "interest_earned"):
            if key in account:
                _integer(account[key], f"存款 {key}")
        if account.get("principal", 0) and "last_accrual_us" not in account:
            raise EconomyError("存款缺少计息游标，禁止重建以免重复发息")
        if account.get("interest_remainder", 0) >= INTEREST_DENOMINATOR:
            raise EconomyError("存款计息余数格式异常")
    if "market" not in economy:
        economy["market"] = default_economy()["market"]
    market = economy["market"]
    if not isinstance(market, dict) or not isinstance(market.get("stocks"), dict):
        raise EconomyError("行情数据格式不正确")
    if "seed" not in market:
        market["seed"] = secrets.token_hex(32)
    market.setdefault("last_hour", None)
    removed = market.setdefault("removed_stocks", [])
    if not isinstance(removed, list) or any(not isinstance(code, str) for code in removed):
        raise EconomyError("已删除股票记录格式异常")
    for code, name in STOCKS.items():
        if code not in removed:
            market["stocks"].setdefault(code, _default_stock(code, name, 10000))
    for code, stock in market["stocks"].items():
        if not isinstance(stock, dict):
            raise EconomyError("股票行情记录必须是对象")
        if not _STOCK_CODE_RE.fullmatch(str(code)) or stock.get("code") != code:
            raise EconomyError("股票代码或行情记录格式异常")
        if not isinstance(stock.get("name"), str) or not 1 <= len(stock["name"].strip()) <= 30:
            raise EconomyError("股票名称格式异常")
        defaults = {"buy_enabled": True, "enabled": True, "initial_price_cent": stock.get("price", 10000),
                    "hourly_bps": None, "daily_bps": None, "fee_bps": None, "revision": 1, "trade_count": 0}
        for key, value in defaults.items():
            stock.setdefault(key, value)
        if type(stock["buy_enabled"]) is not bool or type(stock["enabled"]) is not bool:
            raise EconomyError("股票买入开关格式异常")
        for key in ("hourly_bps", "daily_bps", "fee_bps"):
            if stock[key] is not None:
                _integer(stock[key], f"股票 {code} {key}", 0, 10000)
        _integer(stock["revision"], "股票版本", 1)
        _integer(stock["trade_count"], "股票交易次数")
        _integer(stock.get("price"), "股票价格", 100)
        _integer(stock.get("open_price"), "股票开盘价", 100)
    known = set(market["stocks"])
    for holdings in economy["positions"].values():
        for code, holding in holdings.items():
            if code not in known or not isinstance(holding, dict):
                raise EconomyError("持仓股票或记录格式异常")
            _integer(holding.get("quantity"), "持仓股数")
            _integer(holding.get("cost_total"), "持仓成本")
    if economy["settings"].get("auto_stock_code") not in known:
        economy["settings"]["auto_stock_code"] = next(iter(known), "ST01")
    return economy


def _integer(value: Any, label: str, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise EconomyError(f"{label}必须是 {minimum} 至 {maximum} 的整数")
    return value


def _timestamp(value: Any, label: str = "时间") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EconomyError(f"{label}必须是有效时间戳")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= MAX_TIMESTAMP:
            raise EconomyError(f"{label}必须是有效时间戳")
        return float(number)
    except (InvalidOperation, OverflowError):
        raise EconomyError(f"{label}必须是有效时间戳") from None


def _microseconds(now: float) -> int:
    return int(Decimal(str(_timestamp(now))) * 1_000_000)


def _date(data: dict[str, Any], now: float) -> str:
    return logical_date_str(now, normalize_reset_hour((data.get("settings") or {}).get("reset_hour"), 0))


def _ceil(number: int, denominator: int) -> int:
    return (number + denominator - 1) // denominator


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def price_change_bps(price: int, opening: int) -> int:
    """涨跌幅展示为百分比两位小数，正负均按绝对值四舍五入。"""
    difference = price - opening
    rounded = (abs(difference) * 20000 + opening) // (opening * 2)
    return -rounded if difference < 0 else rounded


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def daily(data: dict[str, Any], user_key: str, now: float) -> dict[str, Any]:
    """独立日额度，不依赖可删除的兑换历史或有长度上限的流水。"""
    economy = ensure(data)
    today = _date(data, now)
    record = economy["daily_usage"].get(user_key)
    if not isinstance(record, dict) or record.get("date") != today:
        record = {"date": today, "products": {}, "families": {}, "stock_trades": 0,
                  "stock_buy_total": 0, "prediction_stake": 0, "lottery_count": 0}
        economy["daily_usage"][user_key] = record
    for key in ("products", "families"):
        record.setdefault(key, {})
    for key in ("stock_trades", "stock_buy_total", "prediction_stake", "lottery_count"):
        record.setdefault(key, 0)
    return record


def _user(data: dict[str, Any], user_key: str) -> dict[str, Any]:
    if not isinstance(user_key, str) or not user_key.strip():
        raise EconomyError("用户标识不能为空")
    users = data.setdefault("users", {})
    user = users.setdefault(user_key, {})
    platform, _, user_id = user_key.partition(":")
    for key, value in {"points": 0, "total_earned": 0, "total_spent": 0,
                       "platform": platform, "user_id": user_id, "nickname": ""}.items():
        user.setdefault(key, value)
    _integer(user["points"], "用户积分")
    return user


def _wallet(data: dict[str, Any], user_key: str, delta: int, now: float, kind: str,
            ref: str, note: str, *, earned: int = 0, spent: int = 0,
            fee: int = 0, profit: int | None = None) -> None:
    user = _user(data, user_key)
    balance = user["points"] + delta
    if not 0 <= balance <= MAX_INTEGER:
        raise EconomyError("积分不足" if balance < 0 else "积分余额超出安全范围")
    user["points"] = balance
    user["total_earned"] = max(0, int(user.get("total_earned") or 0) + earned)
    user["total_spent"] = max(0, int(user.get("total_spent") or 0) + spent)
    user["updated_at"] = now
    # 延迟导入，避免 Store -> economy -> business -> Store 的循环依赖。
    from .business import append_ledger
    entry = append_ledger(data, user_key, kind, delta, now, note=note, ref=ref)
    source = ("股市" if kind.startswith("股票") else "积分通" if kind.startswith("积分通") or kind == "存款利息"
              else "竞猜" if kind.startswith("竞猜") else "刮刮乐" if kind.startswith("刮奖") else kind)
    entry.update(source=source, fee=fee, profit=earned if profit is None else profit)


def _validated_settings(payload: dict[str, Any], current: dict[str, Any], allowed_stock_codes: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EconomyError("玩法设置必须是对象")
    unknown = set(payload) - set(DEFAULT_SETTINGS)
    if unknown:
        raise EconomyError("未知玩法设置：" + "、".join(sorted(unknown)))
    result = copy.deepcopy(current)
    for key, value in payload.items():
        if key.endswith("_enabled"):
            if type(value) is not bool:
                raise EconomyError(f"{key}必须是布尔值")
        elif key == "auto_stock_code":
            allowed = allowed_stock_codes or set(STOCKS)
            if not isinstance(value, str) or value not in allowed:
                raise EconomyError("自动竞猜股票代码必须选择已存在的股票")
        elif key == "lottery_prizes":
            if not isinstance(value, list) or not 1 <= len(value) <= 30:
                raise EconomyError("彩票奖项需要 1 至 30 项")
            for index, row in enumerate(value, 1):
                if not isinstance(row, dict) or set(row) != {"points", "weight"}:
                    raise EconomyError(f"彩票第 {index} 项必须包含 points 和 weight")
                _integer(row["points"], f"彩票第 {index} 项奖金")
                _integer(row["weight"], f"彩票第 {index} 项权重")
            if sum(row["weight"] for row in value) <= 0:
                raise EconomyError("彩票奖项权重不能全部为 0")
        else:
            maximum = 10000 if key.endswith("_bps") else MAX_INTEGER
            minimum = 1 if key in {"prediction_min", "prediction_max", "prediction_timeout_days", "lottery_cost"} else 0
            if key == "auto_close_minutes":
                minimum, maximum = 1, 59
            _integer(value, key, minimum, maximum)
        result[key] = copy.deepcopy(value)
    if result["prediction_min"] > result["prediction_max"]:
        raise EconomyError("竞猜最低投入不能大于最高投入")
    return result


def update_settings(data: dict[str, Any], payload: dict[str, Any], now: float) -> dict[str, Any]:
    economy = ensure(data)
    settings = _validated_settings(payload, economy["settings"], set(economy["market"]["stocks"]))
    settle_due(data, now)  # 已流逝的时间、到期旧局先使用原规则。
    economy["settings"] = settings
    settle_due(data, now)  # 中途启用时，可创建当前尚未封盘的自动局。
    return copy.deepcopy(settings)


def _accrue_savings(data: dict[str, Any], economy: dict[str, Any], now: float) -> bool:
    now_us = _microseconds(now)
    changed = False
    settings = economy["settings"]
    for user_key, account in economy["savings"].items():
        previous = account.get("last_accrual_us", now_us)
        if now_us <= previous:
            continue
        principal = int(account.get("principal", 0))
        eligible = min(principal, settings["savings_principal_cap"])
        numerator = int(account.get("interest_remainder", 0)) + eligible * settings["savings_rate_bps"] * (now_us - previous)
        interest, remainder = divmod(numerator, INTEREST_DENOMINATOR)
        account["last_accrual_us"] = now_us
        account["interest_remainder"] = remainder
        account["interest_earned"] = int(account.get("interest_earned", 0)) + interest
        if interest:
            _wallet(data, user_key, interest, now, "存款利息", f"interest:{user_key}:{now_us}",
                    "活期按实际持有时间计息，利息转入可用积分", earned=interest)
        changed = True
    return changed


def _market_needed(economy: dict[str, Any]) -> bool:
    settings = economy["settings"]
    return bool(settings["stocks_enabled"] or
                (settings["predictions_enabled"] and settings["auto_predictions_enabled"]) or
                any(int(position.get("quantity", 0)) > 0
                    for positions in economy["positions"].values() for position in positions.values()) or
                any(item.get("source") == "auto" and item.get("status") not in TERMINAL
                    for item in economy["predictions"].values()))


def _stock_delta(seed: str, code: str, hour: int, limit: int) -> int:
    # 一个 slot 一个独立随机流；漏时段重放不会受其它玩法随机调用影响。
    generator = random.Random(int(_digest([seed, code, hour]), 16))
    return generator.randint(-limit, limit)


def _advance_market(data: dict[str, Any], economy: dict[str, Any], now: float) -> bool:
    if not _market_needed(economy):
        return False
    market = economy["market"]
    hour = int(now // 3600)
    if market["last_hour"] is None:
        market["last_hour"] = hour
        for stock in market["stocks"].values():
            stock["date"] = _date(data, hour * 3600)
            stock["open_price"] = stock["price"]
            stock["last_updated"] = hour * 3600
            stock["history"] = [{"hour": hour, "price": stock["price"]}]
        return True
    changed = False
    for slot in range(int(market["last_hour"]) + 1, hour + 1):
        moment = slot * 3600
        for code, stock in market["stocks"].items():
            day = _date(data, moment)
            if stock["date"] != day:
                stock["date"] = day
                stock["open_price"] = stock["price"]
            hourly_bps = stock.get("hourly_bps")
            if hourly_bps is None:
                hourly_bps = economy["settings"]["stock_hourly_bps"]
            daily_bps = stock.get("daily_bps")
            if daily_bps is None:
                daily_bps = economy["settings"]["stock_daily_bps"]
            delta = _stock_delta(market["seed"], code, slot, hourly_bps)
            candidate = (stock["price"] * (10000 + delta) + 5000) // 10000
            cap = daily_bps
            floor = max(100, _ceil(stock["open_price"] * (10000 - cap), 10000))
            ceiling = min(MAX_INTEGER, max(100, stock["open_price"] * (10000 + cap) // 10000))
            stock["price"] = max(floor, min(ceiling, candidate))
            stock["last_updated"] = moment
            stock.setdefault("history", []).append({"hour": slot, "price": stock["price"]})
            del stock["history"][:-169]
        market["last_hour"] = slot
        for prediction in list(economy["predictions"].values()):
            if (prediction.get("source") == "auto" and prediction.get("status") not in TERMINAL
                    and prediction["target_hour"] == slot):
                end_price = market["stocks"][prediction["stock_code"]]["price"]
                prediction["end_price"] = end_price
                winner = "涨" if end_price > prediction["start_price"] else "跌" if end_price < prediction["start_price"] else "平"
                _finish_prediction(data, prediction, winner, moment)
        changed = True
    return changed


def _new_prediction(economy: dict[str, Any], prediction_id: str, title: str,
                    options: list[str], close_at: float, now: float, **extra: Any) -> dict[str, Any]:
    item = {"id": prediction_id, "title": title, "options": options, "status": "open",
            "created_at": now, "close_at": close_at, "closes_at": close_at,
            "resolved_at": None, "winner": None, "source": "manual", "bets": {},
            "payout_bps": economy["settings"]["prediction_payout_bps"],
            "timeout_at": close_at + economy["settings"]["prediction_timeout_days"] * 86400,
            "total_stake": 0, "participants": 0, "paid_total": 0, **extra}
    economy["predictions"][prediction_id] = item
    return item


def _create_auto_current(data: dict[str, Any], economy: dict[str, Any], now: float) -> bool:
    settings = economy["settings"]
    if not (settings["predictions_enabled"] and settings["auto_predictions_enabled"]):
        return False
    hour = int(now // 3600)
    close_at = (hour + 1) * 3600 - settings["auto_close_minutes"] * 60
    code = settings["auto_stock_code"]
    prediction_id = f"auto_{code}_{hour}"
    if now >= close_at or prediction_id in economy["predictions"]:
        return False
    stock = economy["market"]["stocks"][code]
    start = datetime.fromtimestamp(hour * 3600, tz=PLUGIN_TIMEZONE).strftime("%m-%d %H:00")
    _new_prediction(economy, prediction_id, f"{stock['name']} {start} 下一整点涨跌", ["涨", "跌", "平"], close_at, now,
                    source="auto", stock_code=code, start_price=stock["price"], target_hour=hour + 1)
    return True


def _finish_prediction(data: dict[str, Any], item: dict[str, Any], winner: str | None,
                       now: float, *, cancelled: bool = False, reason: str = "") -> None:
    if item["status"] in TERMINAL:
        return
    bets = item["bets"]
    winners = [(key, bet) for key, bet in bets.items() if bet["option"] == winner]
    refund = cancelled or not winners
    if refund:
        for user_key, bet in sorted(bets.items()):
            amount = bet["amount"]
            _wallet(data, user_key, amount, now, "竞猜返还", item["id"],
                    reason or ("竞猜取消，原额退回" if cancelled else "无人押中，原额退回"), spent=-amount)
            bet["refunded"] = True
            bet["payout"] = amount
        item["status"] = "cancelled" if cancelled else "void"
        item["paid_total"] = sum(bet["amount"] for bet in bets.values())
    else:
        total_pool = sum(bet["amount"] for bet in bets.values())
        pool = total_pool * item["payout_bps"] // 10000
        weight = sum(bet["amount"] for _, bet in winners)
        allocated = {key: pool * bet["amount"] // weight for key, bet in winners}
        remainder_order = sorted(winners, key=lambda pair: (-(pool * pair[1]["amount"] % weight), pair[0]))
        for key, _ in remainder_order[:pool - sum(allocated.values())]:
            allocated[key] += 1
        for user_key, bet in sorted(winners):
            amount = allocated[user_key]
            if amount:
                _wallet(data, user_key, amount, now, "竞猜派彩", item["id"],
                        f"竞猜「{item['title']}」押中 {winner}；本局总奖池 {total_pool}，可派彩 {pool}，留存 {total_pool - pool} 积分",
                        earned=amount, profit=amount - bet["amount"])
            bet["payout"] = amount
        item["status"] = "resolved"
        item["paid_total"] = pool
    item["winner"] = winner
    item["resolved_at"] = now
    item["reason"] = reason


def settle_due(data: dict[str, Any], now: float) -> bool:
    """补齐离线利息、逐小时行情和既有局；仅给当前未封盘小时建自动局。"""
    _timestamp(now)
    missing = not isinstance(data.get("economy"), dict)
    economy = ensure(data)
    changed = _accrue_savings(data, economy, now)
    changed = _advance_market(data, economy, now) or changed
    for item in list(economy["predictions"].values()):
        if item["status"] in TERMINAL:
            continue
        if now >= item["close_at"] and item["status"] == "open":
            item["status"] = "closed"
            changed = True
        if item.get("source") != "auto" and now >= item["timeout_at"]:
            _finish_prediction(data, item, None, now, cancelled=True, reason="封盘后逾期未判，自动原额退回")
            changed = True
    changed = _create_auto_current(data, economy, now) or changed
    return changed or missing


def _public_prediction(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "options", "status", "created_at", "close_at", "closes_at", "resolved_at",
            "winner", "source", "total_stake", "participants", "payout_bps", "paid_total", "timeout_at",
            "stock_code", "start_price", "end_price", "target_hour", "reason")
    return {key: copy.deepcopy(item[key]) for key in keys if key in item}


def _public_stocks(economy: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"code": stock["code"], "name": stock["name"], "price": stock["price"],
             "price_cent": stock["price"], "price_text": _money(stock["price"]),
             "open_price": stock["open_price"], "open_price_cent": stock["open_price"],
             "change_bps": price_change_bps(stock["price"], stock["open_price"]),
             "last_updated": stock["last_updated"], "buy_enabled": stock.get("buy_enabled", True),
             "enabled": stock.get("enabled", True), "initial_price_cent": stock.get("initial_price_cent", stock["price"]),
             "hourly_bps": stock.get("hourly_bps"), "daily_bps": stock.get("daily_bps"),
             "fee_bps": stock.get("fee_bps"), "revision": stock.get("revision", 1),
             "trade_count": stock.get("trade_count", 0),
             # 只公开最近行情快照，供聊天/后台画趋势图；不包含随机种子或交易明细。
             "history": [{"hour": row["hour"], "price": row["price"], "price_cent": row["price"]}
                         for row in stock.get("history", [])[-169:]
                         if isinstance(row, dict) and isinstance(row.get("hour"), int)
                         and isinstance(row.get("price"), int)]}
            for stock in economy["market"]["stocks"].values()]


def _stock_definition(data: dict[str, Any], payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EconomyError("股票参数必须是对象")
    code = str(payload.get("code") or (existing or {}).get("code") or "").strip().upper()
    if not _STOCK_CODE_RE.fullmatch(code):
        raise EconomyError("股票代码须为 2～12 位大写字母、数字或下划线，且以字母开头")
    name = str(payload.get("name") if "name" in payload else (existing or {}).get("name") or "").strip()
    if not 1 <= len(name) <= 30:
        raise EconomyError("股票名称须为 1～30 个字符")
    def optional_bps(key: str) -> int | None:
        value = payload.get(key, (existing or {}).get(key))
        if value in (None, ""):
            return None
        return _integer(value, key, 0, 10000)
    buy_enabled = payload.get("buy_enabled", (existing or {}).get("buy_enabled", True))
    if type(buy_enabled) is not bool:
        raise EconomyError("buy_enabled 必须是布尔值")
    initial = payload.get("initial_price_cent")
    if existing is not None:
        if initial not in (None, ""):
            raise EconomyError("已有股票不能修改初始价格；请停牌后删除并新建代码")
        initial_cent = int(existing.get("initial_price_cent", existing["price"]))
    else:
        if initial in (None, ""):
            initial = payload.get("initial_price", 100)
            initial_cent = _integer(initial, "初始价格", 1, MAX_INTEGER // 100) * 100
        else:
            initial_cent = _integer(initial, "初始价格（百分之一积分）", 100, MAX_INTEGER)
    return {"code": code, "name": name, "buy_enabled": buy_enabled,
            "hourly_bps": optional_bps("hourly_bps"), "daily_bps": optional_bps("daily_bps"),
            "fee_bps": optional_bps("fee_bps"), "initial_price_cent": initial_cent}


def upsert_stock(data: dict[str, Any], payload: dict[str, Any], now: float) -> dict[str, Any]:
    economy = ensure(data)
    stocks = economy["market"]["stocks"]
    code = str(payload.get("code") or "").strip().upper() if isinstance(payload, dict) else ""
    existing = stocks.get(code)
    if existing is None and len(stocks) >= 30:
        raise EconomyError("最多只能配置 30 只股票")
    definition = _stock_definition(data, payload, existing)
    if existing is None:
        removed = economy["market"].setdefault("removed_stocks", [])
        if code in removed:
            removed.remove(code)
        stock = _default_stock(definition["code"], definition["name"], definition["initial_price_cent"])
        stock.update({key: definition[key] for key in ("buy_enabled", "hourly_bps", "daily_bps", "fee_bps")})
        stock["revision"] = 1
        stocks[definition["code"]] = stock
    else:
        changed = any(existing.get(key) != definition[key] for key in ("name", "buy_enabled", "hourly_bps", "daily_bps", "fee_bps"))
        for key in ("name", "buy_enabled", "hourly_bps", "daily_bps", "fee_bps"):
            existing[key] = definition[key]
        if changed:
            existing["revision"] = int(existing.get("revision", 1)) + 1
        stock = existing
    return copy.deepcopy(_public_stocks(economy)[list(stocks).index(stock["code"])])


def delete_stock(data: dict[str, Any], payload: dict[str, Any], now: float) -> str:
    economy = ensure(data)
    code = str(payload.get("code") or "").strip().upper() if isinstance(payload, dict) else ""
    stock = economy["market"]["stocks"].get(code)
    if stock is None:
        raise EconomyError("股票不存在")
    if len(economy["market"]["stocks"]) <= 1:
        raise EconomyError("至少保留一只股票，不能删除最后一只")
    if any(int(rows.get(code, {}).get("quantity", 0)) > 0 for rows in economy["positions"].values()):
        raise EconomyError("该股票仍有持仓，不能删除；请先卖出或停牌")
    if stock.get("trade_count", 0):
        raise EconomyError("该股票已有成交记录，不能删除；请改为停牌")
    if any(item.get("stock_code") == code and item.get("status") not in TERMINAL for item in economy["predictions"].values()):
        raise EconomyError("该股票仍被竞猜使用，不能删除；请先处理竞猜")
    economy["market"]["stocks"].pop(code)
    economy["market"].setdefault("removed_stocks", []).append(code)
    if economy["settings"].get("auto_stock_code") == code:
        economy["settings"]["auto_stock_code"] = next(iter(economy["market"]["stocks"]), "ST01")
    return f"已删除股票 {code}"


def overview(data: dict[str, Any], now: float) -> dict[str, Any]:
    settle_due(data, now)
    economy = ensure(data)
    predictions = sorted(economy["predictions"].values(), key=lambda item: (item["created_at"], item["id"]), reverse=True)
    return {"settings": copy.deepcopy(economy["settings"]), "stocks": _public_stocks(economy),
            "predictions": [_public_prediction(item) for item in predictions],
            "statistics": {
                "savings_principal": sum(item.get("principal", 0) for item in economy["savings"].values()),
                "stock_holders": sum(any(item.get("quantity", 0) > 0 for item in positions.values()) for positions in economy["positions"].values()),
                "open_predictions": sum(item["status"] == "open" for item in predictions),
                "unsettled_predictions": sum(item["status"] not in TERMINAL for item in predictions),
                "locked_prediction": sum(item["total_stake"] for item in predictions if item["status"] not in TERMINAL),
                "lottery_tickets": sum(item.get("action") == "lottery_buy" for item in economy["operations"].values()),
            }}


def user_assets(data: dict[str, Any], user_key: str, now: float) -> dict[str, Any]:
    settle_due(data, now)
    economy = ensure(data)
    user = _user(data, user_key)
    account = economy["savings"].get(user_key, {})
    interest_per_microsecond = min(account.get("principal", 0), economy["settings"]["savings_principal_cap"]) * economy["settings"]["savings_rate_bps"]
    next_interest = (now + _ceil(INTEREST_DENOMINATOR - account.get("interest_remainder", 0), interest_per_microsecond) / 1_000_000
                     if interest_per_microsecond else None)
    positions = []
    for code, holding in economy["positions"].get(user_key, {}).items():
        stock = economy["market"]["stocks"][code]
        positions.append({"code": code, "name": stock["name"], "quantity": holding["quantity"],
                          "price": stock["price"], "price_cent": stock["price"],
                          "market_value": stock["price"] * holding["quantity"] // 100,
                          "cost_total": holding["cost_total"]})
    return {"wallet": user["points"], "savings": {
        "principal": account.get("principal", 0), "interest_earned": account.get("interest_earned", 0),
        "interest_remainder": account.get("interest_remainder", 0),
        "rate_bps": economy["settings"]["savings_rate_bps"],
        "next_settlement_at": next_interest,
        "last_accrual_at": account.get("last_accrual_us", _microseconds(now)) / 1_000_000,
        "principal_cap": economy["settings"]["savings_principal_cap"],
    }, "positions": positions,
        "stock_realized_profit": int(user.get("stock_realized_profit", 0)),
        "locked_prediction": sum(item["bets"].get(user_key, {}).get("amount", 0)
                                 for item in economy["predictions"].values() if item["status"] not in TERMINAL),
        "cards": copy.deepcopy(economy["cards"].get(user_key, {})), "daily_usage": copy.deepcopy(daily(data, user_key, now))}


def create_prediction(data: dict[str, Any], payload: dict[str, Any], now: float) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EconomyError("竞猜参数必须是对象")
    economy = ensure(data)
    if not economy["settings"]["predictions_enabled"]:
        raise EconomyError("竞猜功能未开启")
    title = payload.get("title")
    options = payload.get("options")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
        raise EconomyError("竞猜题目需要 1 至 100 个字符")
    if not isinstance(options, list) or not 2 <= len(options) <= 6:
        raise EconomyError("竞猜需要 2 至 6 个选项")
    if any(not isinstance(option, str) or not 1 <= len(option.strip()) <= 40 for option in options):
        raise EconomyError("每个竞猜选项需要 1 至 40 个字符")
    options = [option.strip() for option in options]
    if len(set(options)) != len(options):
        raise EconomyError("竞猜选项不能重复")
    close_at = _timestamp(payload.get("close_at", payload.get("closes_at")), "封盘时间")
    if close_at <= now:
        raise EconomyError("封盘时间必须晚于当前时间")
    item = _new_prediction(economy, "pr_" + uuid.uuid4().hex[:16], title.strip(), options, close_at, now)
    return _public_prediction(item)


def resolve_prediction(data: dict[str, Any], payload: dict[str, Any], now: float) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EconomyError("竞猜结算参数必须是对象")
    economy = ensure(data)
    prediction_id = str(payload.get("prediction_id", payload.get("id", "")))
    item = economy["predictions"].get(prediction_id)
    if not item:
        raise EconomyError("竞猜不存在")
    cancel = payload.get("cancel", False)
    if type(cancel) is not bool:
        raise EconomyError("取消标记必须是布尔值")
    if item.get("source") != "auto" and item["status"] not in TERMINAL and now >= item["timeout_at"]:
        _finish_prediction(data, item, None, now, cancelled=True, reason="封盘后逾期未判，自动原额退回")
        return _public_prediction(item)
    winner = payload.get("option", payload.get("winner"))
    if not cancel and winner not in item["options"]:
        raise EconomyError("获胜选项不存在")
    if item["status"] in TERMINAL:
        if (cancel and item["status"] == "cancelled") or (not cancel and item.get("winner") == winner):
            return _public_prediction(item)
        raise EconomyError("竞猜已经结算，不能更改结果")
    if item.get("source") == "auto" and not cancel:
        raise EconomyError("自动竞猜由下一整点行情判定，不能手工开奖")
    if not cancel and now < item["close_at"]:
        raise EconomyError("竞猜尚未封盘")
    _finish_prediction(data, item, None if cancel else winner, now, cancelled=cancel)
    return _public_prediction(item)


def _params(action: str, params: dict[str, Any], allowed_stock_codes: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise EconomyError("操作参数必须是对象")
    if action in {"stock_buy", "stock_sell"}:
        if set(params) != {"code", "quantity"}:
            raise EconomyError("股票操作仅接受 code 和 quantity")
        code = params.get("code")
        allowed = allowed_stock_codes or set(STOCKS)
        if not isinstance(code, str) or code not in allowed:
            raise EconomyError("股票代码不存在")
        return {"code": code, "quantity": _integer(params.get("quantity"), "股数", 1)}
    if action in {"savings_deposit", "savings_withdraw"}:
        if set(params) != {"amount"}:
            raise EconomyError("积分通操作仅接受 amount")
        return {"amount": _integer(params.get("amount"), "积分数量", 1)}
    if action == "prediction_bet":
        if set(params) != {"prediction_id", "option", "amount"}:
            raise EconomyError("竞猜操作仅接受 prediction_id、option 和 amount")
        if not isinstance(params.get("prediction_id"), str) or not isinstance(params.get("option"), str):
            raise EconomyError("请填写竞猜编号和选项")
        return {"prediction_id": params["prediction_id"], "option": params["option"],
                "amount": _integer(params.get("amount"), "投入积分", 1)}
    if action == "lottery_buy":
        if params:
            raise EconomyError("每次只能购买一张刮刮乐")
        return {}
    raise EconomyError("未知积分玩法操作")


def prepare_action(data: dict[str, Any], user_key: str, action: str,
                   params: dict[str, Any], now: float) -> dict[str, Any]:
    economy = ensure(data)
    settings = economy["settings"]
    normalized = _params(action, params, set(economy["market"]["stocks"]))
    user = _user(data, user_key)
    usage = daily(data, user_key, now)
    details: dict[str, Any] = {"date": usage["date"]}
    if action in {"stock_buy", "stock_sell"}:
        if action == "stock_buy" and not settings["stocks_enabled"]:
            raise EconomyError("股票买入功能未开启，已有持仓仍可卖出")
        if usage["stock_trades"] >= settings["stock_daily_trades"]:
            raise EconomyError("今日股票交易次数已达上限")
        stock = economy["market"]["stocks"][normalized["code"]]
        if action == "stock_buy" and stock.get("buy_enabled", True) is not True:
            raise EconomyError("该股票已暂停买入，但仍可卖出已有持仓")
        quantity = normalized["quantity"]
        gross = _ceil(stock["price"] * quantity, 100) if action == "stock_buy" else stock["price"] * quantity // 100
        fee_bps = stock.get("fee_bps") if stock.get("fee_bps") is not None else settings["stock_fee_bps"]
        fee = max(1, _ceil(gross * fee_bps, 10000))
        if action == "stock_buy":
            if gross + fee > user["points"]:
                raise EconomyError("积分不足，买入金额需包含手续费")
            if usage["stock_buy_total"] + gross + fee > settings["stock_daily_buy_limit"]:
                raise EconomyError("今日股票买入总支出已达上限（含手续费）")
            holding = economy["positions"].get(user_key, {}).get(normalized["code"], {})
            if holding.get("quantity", 0) + quantity > MAX_INTEGER or holding.get("cost_total", 0) + gross > MAX_INTEGER:
                raise EconomyError("买入后持仓超出安全范围")
        else:
            holding = economy["positions"].get(user_key, {}).get(normalized["code"], {})
            if holding.get("quantity", 0) < quantity:
                raise EconomyError("可卖股数不足，不支持做空")
            if gross < 1 or gross - fee < 0:
                raise EconomyError("卖出所得不足以支付手续费")
        details.update(price_cent=stock["price"], market_hour=economy["market"]["last_hour"],
                       gross=gross, fee=fee, fee_bps=fee_bps, stock_revision=stock.get("revision", 1),
                       daily_trades=settings["stock_daily_trades"], daily_buy_limit=settings["stock_daily_buy_limit"])
        text = (f"{'买入' if action == 'stock_buy' else '卖出'} {stock['name']}（{stock['code']}）{quantity} 股，"
                f"每股 {_money(stock['price'])} 积分，本金 {gross}，手续费 {fee}，"
                f"{'合计支出 ' + str(gross + fee) if action == 'stock_buy' else '实际到账 ' + str(gross - fee)} 积分。")
    elif action in {"savings_deposit", "savings_withdraw"}:
        principal = economy["savings"].get(user_key, {}).get("principal", 0)
        amount = normalized["amount"]
        if action == "savings_deposit":
            if not settings["savings_enabled"]:
                raise EconomyError("积分通存入功能未开启，已有本金仍可取出")
            if amount > user["points"]:
                raise EconomyError("可用积分不足")
            if principal + amount > settings["savings_principal_cap"]:
                raise EconomyError("存入后本金超过积分通上限")
        elif amount > principal:
            raise EconomyError("积分通本金不足")
        details.update(rate_bps=settings["savings_rate_bps"], principal_cap=settings["savings_principal_cap"])
        text = (f"{'存入' if action == 'savings_deposit' else '取出'}积分通 {amount} 积分；"
                f"日利率 {settings['savings_rate_bps'] // 100}.{settings['savings_rate_bps'] % 100:02d}%，按实际持有时间计息，利息进入可用积分。")
    elif action == "prediction_bet":
        if not settings["predictions_enabled"]:
            raise EconomyError("竞猜功能未开启")
        item = economy["predictions"].get(normalized["prediction_id"])
        if not item or item["status"] != "open" or now >= item["close_at"]:
            raise EconomyError("竞猜不存在或已封盘")
        if normalized["option"] not in item["options"]:
            raise EconomyError("竞猜选项不存在")
        if user_key in item["bets"]:
            raise EconomyError("每人每局只能下注一次")
        amount = normalized["amount"]
        if not settings["prediction_min"] <= amount <= settings["prediction_max"]:
            raise EconomyError(f"每次竞猜投入须为 {settings['prediction_min']} 至 {settings['prediction_max']} 积分")
        if usage["prediction_stake"] + amount > settings["prediction_daily_limit"]:
            raise EconomyError("今日竞猜投入已达上限，退款不恢复额度")
        if amount > user["points"]:
            raise EconomyError("可用积分不足")
        details.update(close_at=item["close_at"], payout_bps=item["payout_bps"],
                       minimum=settings["prediction_min"], maximum=settings["prediction_max"],
                       daily_limit=settings["prediction_daily_limit"])
        text = f"竞猜「{item['title']}」选择「{normalized['option']}」，投入 {amount} 积分；中奖者按投入比例分配 {item['payout_bps'] // 100}.{item['payout_bps'] % 100:02d}% 奖池；无人押中或取消原额退回。"
    else:
        if not settings["lottery_enabled"]:
            raise EconomyError("刮刮乐功能未开启")
        if usage["lottery_count"] >= settings["lottery_daily_limit"]:
            raise EconomyError("今日刮刮乐次数已达上限")
        if user["points"] < settings["lottery_cost"]:
            raise EconomyError("可用积分不足")
        if user["points"] - settings["lottery_cost"] + max(row["points"] for row in settings["lottery_prizes"]) > MAX_INTEGER:
            raise EconomyError("开奖后余额可能超出安全范围，请先减少可用积分")
        details.update(cost=settings["lottery_cost"], prizes=settings["lottery_prizes"], daily_limit=settings["lottery_daily_limit"])
        text = f"购买一张积分刮刮乐，消耗 {settings['lottery_cost']} 积分，开奖后奖金自动入账。"
    quote = {"action": action, "params": normalized, "text": text, "details": copy.deepcopy(details)}
    quote["fingerprint"] = _digest({"action": action, "params": normalized, "details": details})
    return quote


def _draw_lottery(prizes: list[dict[str, int]]) -> int:
    ticket = secrets.randbelow(sum(row["weight"] for row in prizes))
    for row in prizes:
        if ticket < row["weight"]:
            return row["points"]
        ticket -= row["weight"]
    raise EconomyError("彩票奖项权重配置无效")


def execute_action(data: dict[str, Any], user_key: str, action: str, params: dict[str, Any],
                   quote: dict[str, Any], operation_id: str, now: float) -> dict[str, Any]:
    """先读持久回执，再核对报价；异常由外层原子事务完整回滚。"""
    economy = ensure(data)
    normalized = _params(action, params, set(economy["market"]["stocks"]))
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
        raise EconomyError("操作编号不能为空且不能超过 200 字符")
    digest = _digest(normalized)
    receipt = economy["operations"].get(operation_id)
    if receipt is not None:
        if (receipt.get("owner") != user_key or receipt.get("action") != action
                or receipt.get("params_hash") != digest):
            raise EconomyError("操作编号已用于其他用户或参数，拒绝重复执行")
        return copy.deepcopy(receipt["result"])
    settle_due(data, now)
    fresh = prepare_action(data, user_key, action, normalized, now)
    if (not isinstance(quote, dict) or quote.get("action") != action or quote.get("params") != normalized
            or quote.get("fingerprint") != fresh["fingerprint"]):
        raise EconomyError("报价或规则已变化，请重新发起并确认")
    settings = economy["settings"]
    usage = daily(data, user_key, now)
    result: dict[str, Any] = {"operation_id": operation_id, "action": action}
    if action in {"stock_buy", "stock_sell"}:
        code, quantity = normalized["code"], normalized["quantity"]
        positions = economy["positions"].setdefault(user_key, {})
        position = positions.setdefault(code, {"quantity": 0, "cost_total": 0})
        gross, fee = fresh["details"]["gross"], fresh["details"]["fee"]
        if action == "stock_buy":
            _wallet(data, user_key, -gross - fee, now, "股票买入", operation_id, fresh["text"], spent=fee, fee=fee)
            position["quantity"] += quantity
            position["cost_total"] += gross
            usage["stock_buy_total"] += gross + fee
        else:
            released = position["cost_total"] * quantity // position["quantity"]
            position["quantity"] -= quantity
            position["cost_total"] -= released
            realized = gross - released
            user = _user(data, user_key)
            user["stock_realized_profit"] = int(user.get("stock_realized_profit", 0)) + realized
            _wallet(data, user_key, gross - fee, now, "股票卖出", operation_id, fresh["text"], spent=fee, fee=fee, profit=realized - fee)
            result["realized_profit"] = realized
        usage["stock_trades"] += 1
        economy["market"]["stocks"][code]["trade_count"] = int(economy["market"]["stocks"][code].get("trade_count", 0)) + 1
        result.update(gross=gross, fee=fee, quantity=quantity, code=code)
        message = fresh["text"]
    elif action in {"savings_deposit", "savings_withdraw"}:
        amount = normalized["amount"]
        account = economy["savings"].setdefault(user_key, {"principal": 0, "last_accrual_us": _microseconds(now),
                                                          "interest_remainder": 0, "interest_earned": 0})
        deposit = action == "savings_deposit"
        account["principal"] += amount if deposit else -amount
        _wallet(data, user_key, -amount if deposit else amount, now, "积分通存入" if deposit else "积分通取出",
                operation_id, fresh["text"])
        result["principal"] = account["principal"]
        message = f"已{'存入' if deposit else '取出'} {amount} 积分，积分通本金 {account['principal']}。"
    elif action == "prediction_bet":
        item = economy["predictions"][normalized["prediction_id"]]
        amount = normalized["amount"]
        _wallet(data, user_key, -amount, now, "竞猜下注", operation_id, fresh["text"], spent=amount)
        item["bets"][user_key] = {"option": normalized["option"], "amount": amount, "created_at": now,
                                  "payout": 0, "refunded": False, "operation_id": operation_id}
        item["total_stake"] += amount
        item["participants"] += 1
        usage["prediction_stake"] += amount
        result.update(prediction_id=item["id"], option=normalized["option"], amount=amount)
        message = f"竞猜投入成功：{item['title']}，选择「{normalized['option']}」，投入 {amount} 积分。"
    else:
        cost = settings["lottery_cost"]
        prize = _draw_lottery(settings["lottery_prizes"])
        _wallet(data, user_key, -cost, now, "刮奖购买", operation_id, "购买一张积分刮刮乐", spent=cost)
        if prize:
            _wallet(data, user_key, prize, now, "刮奖奖金", operation_id, f"刮刮乐中奖 {prize} 积分", earned=prize)
        usage["lottery_count"] += 1
        result.update(cost=cost, prize=prize, ticket_id=operation_id)
        message = f"刮刮乐开奖：{'获得 ' + str(prize) + ' 积分' if prize else '本次未中奖'}，消耗 {cost} 积分。"
    result.update(message=message, balance=_user(data, user_key)["points"])
    economy["operations"][operation_id] = {"owner": user_key, "action": action, "params_hash": digest,
                                           "created_at": now, "result": copy.deepcopy(result)}
    return copy.deepcopy(result)


__all__ = ["EconomyError", "default_economy", "ensure", "daily", "settle_due", "update_settings",
           "overview", "user_assets", "prepare_action", "execute_action", "create_prediction", "resolve_prediction",
           "upsert_stock", "delete_stock", "price_change_bps"]
