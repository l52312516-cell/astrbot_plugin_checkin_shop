"""特殊商品权益：与钱包、库存和兑换记录在同一个 Store 事务内提交。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

from .timeutil import logical_date_str, normalize_reset_hour

TYPES = ("normal", "checkin_boost", "balance_rebate")
DEFAULT_EFFECTS = {
    "normal": {},
    "checkin_boost": {"multiplier_bps": 20000, "days": 7, "daily_bonus_cap": 20},
    "balance_rebate": {"rebate_bps": 1500, "rebate_cap": 350, "cooldown_hours": 168},
}


def integer(value: Any, label: str, minimum: int = 0, maximum: int = 9007199254740991) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        number = int(value.strip())
    else:
        raise ValueError(f"{label}必须是整数")
    if minimum == 0 and number < 0:
        raise ValueError(f"{label}必须是非负整数")
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}必须在 {minimum}～{maximum} 之间")
    return number


def normalize(payload: dict) -> dict:
    kind = payload.get("product_type", "normal")
    if kind not in TYPES:
        raise ValueError("商品类型无效")
    daily_limit = payload.get("per_user_daily_limit")
    daily_limit = None if daily_limit in (None, "") else integer(daily_limit, "每日限购")
    raw = payload.get("effect_config", {})
    if not isinstance(raw, dict):
        raise ValueError("商品效果必须是对象")
    effect = copy.deepcopy(DEFAULT_EFFECTS[kind])
    for key in effect:
        if key in raw:
            effect[key] = raw[key]
    if kind == "checkin_boost":
        effect = {
            "multiplier_bps": integer(effect["multiplier_bps"], "签到倍率", 10000, 1000000),
            "days": integer(effect["days"], "有效天数", 1, 365),
            "daily_bonus_cap": integer(effect["daily_bonus_cap"], "每日额外奖励上限"),
        }
    elif kind == "balance_rebate":
        effect = {
            "rebate_bps": integer(effect["rebate_bps"], "返利比例", 1, 10000),
            "rebate_cap": integer(effect["rebate_cap"], "返利上限"),
            "cooldown_hours": integer(effect["cooldown_hours"], "冷却小时数", 0, 87600),
        }
    return {"product_type": kind, "per_user_daily_limit": daily_limit, "effect_config": effect}


def today(data: dict, now: float) -> str:
    return logical_date_str(now, normalize_reset_hour(data.get("settings", {}).get("reset_hour"), 0))


def fingerprint(data: dict, product: Any) -> str:
    econ_settings = data.get("economy", {}).get("settings", {})
    raw = {
        "type": product.product_type, "cost": product.cost, "effect": product.effect_config,
        "daily": product.per_user_daily_limit, "limit": product.per_user_limit,
        "family": econ_settings.get("checkin_card_daily_limit" if product.product_type == "checkin_boost" else "rebate_card_daily_limit", 1) if product.product_type != "normal" else None,
    }
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def rebate(product: Any, wallet: int) -> int:
    effect = product.effect_config
    return min(effect.get("rebate_cap", 350), (wallet * effect.get("rebate_bps", 1500) + 5000) // 10000)


def eligibility(data: dict, product: Any, user_key: str, now: float) -> str:
    econ = data.get("economy", {})
    usage = econ.get("daily_usage", {}).get(user_key, {})
    if usage.get("date") != today(data, now):
        usage = {}
    if product.per_user_daily_limit is not None and usage.get("products", {}).get(product.id, 0) >= product.per_user_daily_limit:
        return "已达每日限购"
    kind = product.product_type
    if kind == "normal":
        return ""
    settings = econ.get("settings", {})
    key = "checkin_cards_enabled" if kind == "checkin_boost" else "rebate_cards_enabled"
    if settings.get(key) is not True:
        return "该道具玩法尚未开启"
    family_limit = settings.get("checkin_card_daily_limit" if kind == "checkin_boost" else "rebate_card_daily_limit", 1)
    if usage.get("families", {}).get(kind, 0) >= family_limit:
        return "已达同类道具每日限额"
    cards = econ.get("cards", {}).get(user_key, {})
    if kind == "checkin_boost":
        boost = cards.get("boost", {})
        if boost and boost.get("end_date", "") > today(data, now):
            return "已有待生效或生效中的签到卡"
    else:
        cooldown = product.effect_config.get("cooldown_hours", 168) * 3600
        # 冷却使用购买时保存的到期时间，改规则不能缩短已接受的冷却。
        until = cards.get("rebate_cooldown_until", cards.get("rebate_last_at", 0) + cooldown if cards.get("rebate_last_at") else 0)
        if now < until:
            return "余额返利卡冷却中"
        points = int(data.get("users", {}).get(user_key, {}).get("points", 0))
        if rebate(product, points) < product.cost:
            return "预计返还低于售价，暂不可兑换"
        if points - product.cost + rebate(product, points) > 9007199254740991:
            return "返利后余额超出安全范围，请先减少可用积分"
    return ""


def description(product: Any, wallet: int | None = None) -> str:
    effect = product.effect_config
    if product.product_type == "checkin_boost":
        return f"次日起 {effect.get('days', 7)} 个业务日基础签到 {effect.get('multiplier_bps', 20000) / 10000:g} 倍，每日额外最多 {effect.get('daily_bonus_cap', 20)} 分；不含连签奖励，不叠加。"
    if product.product_type == "balance_rebate":
        text = f"按扣费前可用余额 {effect.get('rebate_bps', 1500) / 100:g}% 返还，最多 {effect.get('rebate_cap', 350)} 分；冷却 {effect.get('cooldown_hours', 168)} 小时。"
        if wallet is not None:
            amount = rebate(product, wallet)
            text += f" 本次消耗 {product.cost}、返还 {amount}、净变化 {amount-product.cost:+d} 分。"
        return text
    return ""


def apply_purchase(data: dict, product: Any, user_key: str, now: float, record_id: str, before: int) -> str:
    from . import economy
    from .business import append_ledger
    econ = economy.ensure(data)
    usage = economy.daily(data, user_key, now)
    usage.setdefault("products", {})[product.id] = usage.setdefault("products", {}).get(product.id, 0) + 1
    kind = product.product_type
    if kind == "normal":
        return ""
    usage.setdefault("families", {})[kind] = usage.setdefault("families", {}).get(kind, 0) + 1
    cards = econ.setdefault("cards", {}).setdefault(user_key, {})
    if kind == "checkin_boost":
        start = date.fromisoformat(today(data, now)) + timedelta(days=1)
        end = start + timedelta(days=product.effect_config["days"])
        cards["boost"] = {**copy.deepcopy(product.effect_config), "start_date": start.isoformat(), "end_date": end.isoformat(), "record_id": record_id}
        return f"签到增益已发放：{start.isoformat()} 起生效，至 {end.isoformat()} 重置时结束。"
    amount = rebate(product, before)
    user = data["users"][user_key]
    integer(user["points"] + amount, "返利后余额")
    user["points"] += amount
    user["total_earned"] = int(user.get("total_earned", 0)) + amount
    cards["rebate_last_at"] = now
    cards["rebate_cooldown_until"] = now + product.effect_config["cooldown_hours"] * 3600
    append_ledger(data, user_key, "道具返利", amount, now, note=f"余额返利：基数 {before}，返还 {amount}，净变化 {amount-product.cost:+d}", ref=record_id)
    return f"已自动返还 {amount} 积分，净变化 {amount-product.cost:+d} 积分。"


def checkin_extra(data: dict, user_key: str, base: int, now: float) -> int:
    boost = data.get("economy", {}).get("cards", {}).get(user_key, {}).get("boost", {})
    day = today(data, now)
    if not boost or not boost.get("start_date", "") <= day < boost.get("end_date", ""):
        return 0
    return min(boost["daily_bonus_cap"], (base * (boost["multiplier_bps"] - 10000) + 5000) // 10000)
