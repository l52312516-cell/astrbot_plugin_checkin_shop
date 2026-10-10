"""打卡、积分与兑换的业务逻辑（不依赖 AstrBot，可直接单测）。"""

from __future__ import annotations

import copy
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

try:  # 作为包被导入（测试 / AstrBot 包加载）
    from .store import Store
    from .timeutil import logical_date_str, normalize_reset_hour, previous_day
except ImportError:  # pragma: no cover - AstrBot 以顶层模块方式加载 core/
    from store import Store  # type: ignore[no-redef]
    from timeutil import (  # type: ignore[no-redef]
        logical_date_str,
        normalize_reset_hour,
        previous_day,
    )

REWARD_MODES = ("fixed", "random", "custom")
REDEMPTION_STATUSES = ("待发放", "已发放", "已取消")

# 不可兑换的原因
REASON_DISABLED = "已下架"
REASON_SOLD_OUT = "已售完"
REASON_LIMIT = "已达限购"
REASON_POINTS = "积分不足"


def _as_int(value: Any, default: int = 0) -> int:
    """把任意值转成 int，失败时返回默认值。"""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    """转成可空的 int；``None``/空串表示"不限"，非法值返回 ``None``。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Product:
    """商品。"""

    id: str
    name: str
    cost: int = 0
    description: str = ""
    category: str = "默认"
    stock: int | None = None
    per_user_limit: int | None = None
    enabled: bool = True
    image_path: str = ""
    sort_order: int = 0
    created_at: float = 0.0
    redeemed: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Product":
        """从存储字典构造商品对象。"""
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            cost=max(0, _as_int(raw.get("cost"))),
            description=str(raw.get("description") or ""),
            category=str(raw.get("category") or "默认"),
            stock=_as_optional_int(raw.get("stock")),
            per_user_limit=_as_optional_int(raw.get("per_user_limit")),
            enabled=bool(raw.get("enabled", True)),
            image_path=str(raw.get("image_path") or ""),
            sort_order=_as_int(raw.get("sort_order")),
            created_at=_as_float(raw.get("created_at")),
            redeemed=max(0, _as_int(raw.get("redeemed"))),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换成存储字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "cost": self.cost,
            "description": self.description,
            "category": self.category,
            "stock": self.stock,
            "per_user_limit": self.per_user_limit,
            "enabled": self.enabled,
            "image_path": self.image_path,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
            "redeemed": self.redeemed,
        }


@dataclass
class ProductState:
    """某个商品对某个用户的兑换可行性。"""

    product: Product
    affordable: bool
    redeemable: bool
    reason: str = ""
    deficit: int = 0
    user_redeemed: int = 0
    remaining_quota: int | None = None


@dataclass
class ShopView:
    """商店视图：全部商品 + 可兑换/不可兑换分组。"""

    user_key: str
    user_name: str
    points: int
    title: str
    subtitle: str
    items: list[ProductState] = field(default_factory=list)

    @property
    def redeemable(self) -> list[ProductState]:
        return [item for item in self.items if item.redeemable]

    @property
    def locked(self) -> list[ProductState]:
        return [item for item in self.items if not item.redeemable]


@dataclass
class CheckinOutcome:
    """打卡结果。"""

    success: bool
    message: str
    points: int = 0
    streak: int = 0
    total: int = 0
    date: str = ""
    bonus: int = 0


@dataclass
class PendingConfirm:
    """等待用户引用回复确认的兑换请求。"""

    token: str
    session_key: str
    user_key: str
    product_id: str
    product_name: str
    cost: int
    created_at: float
    expires_at: float


@dataclass
class ConfirmOutcome:
    """确认兑换的处理结果。"""

    handled: bool
    ok: bool
    message: str
    product_name: str = ""
    cost: int = 0
    balance: int = 0
    cancelled: bool = False
    timed_out: bool = False


def user_key_for(platform: str, user_id: str) -> str:
    """生成用户主键。"""
    platform = (platform or "unknown").strip() or "unknown"
    user_id = str(user_id or "").strip()
    return f"{platform}:{user_id}"


def _ensure_user(data: dict[str, Any], user_key: str) -> dict[str, Any]:
    """取出（必要时创建）用户记录。"""
    users = data.setdefault("users", {})
    user = users.get(user_key)
    if not isinstance(user, dict):
        user = {}
        users[user_key] = user
    user.setdefault("platform", user_key.split(":", 1)[0])
    user.setdefault("user_id", user_key.split(":", 1)[-1])
    user.setdefault("nickname", "")
    user.setdefault("points", 0)
    user.setdefault("total_earned", 0)
    user.setdefault("total_spent", 0)
    user.setdefault("checkin_count", 0)
    user.setdefault("streak", 0)
    user.setdefault("last_checkin_date", "")
    user.setdefault("history", [])
    return user


def _remember_identity(user: dict[str, Any], nickname: str) -> None:
    """记录发送者昵称，优先保留非空值。"""
    nickname = (nickname or "").strip()
    if nickname:
        user["nickname"] = nickname


def all_products(data: dict[str, Any]) -> list[Product]:
    """按展示顺序返回全部商品（含下架）。"""
    products = [Product.from_dict(item) for item in data.get("products", [])]
    products.sort(key=lambda item: (item.sort_order, item.cost, item.name))
    return products


def product_state(data: dict[str, Any], product: Product, user_key: str) -> ProductState:
    """计算某个商品对某个用户是否可兑换。"""
    user = data.get("users", {}).get(user_key) or {}
    points = _as_int(user.get("points"))
    user_redeemed = 0
    for record in data.get("redemptions", []):
        if (
            isinstance(record, dict)
            and record.get("user_key") == user_key
            and record.get("product_id") == product.id
            and record.get("status") != "已取消"
        ):
            user_redeemed += 1

    remaining_quota: int | None = None
    if product.per_user_limit is not None:
        remaining_quota = max(0, product.per_user_limit - user_redeemed)

    affordable = points >= product.cost
    reason = ""
    if not product.enabled:
        reason = REASON_DISABLED
    elif product.stock is not None and product.stock <= 0:
        reason = REASON_SOLD_OUT
    elif remaining_quota is not None and remaining_quota <= 0:
        reason = REASON_LIMIT
    elif not affordable:
        reason = REASON_POINTS
    return ProductState(
        product=product,
        affordable=affordable,
        redeemable=reason == "",
        reason=reason,
        deficit=max(0, product.cost - points),
        user_redeemed=user_redeemed,
        remaining_quota=remaining_quota,
    )


def build_shop_view(
    data: dict[str, Any],
    user_key: str,
    include_disabled: bool = False,
) -> ShopView:
    """构造商店视图（用户在群里只看到上架商品，后台可看全部）。"""
    settings = data.get("settings", {})
    user = data.get("users", {}).get(user_key) or {}
    items = [
        product_state(data, product, user_key)
        for product in all_products(data)
        if include_disabled or product.enabled
    ]
    return ShopView(
        user_key=user_key,
        user_name=str(user.get("nickname") or user.get("user_id") or user_key),
        points=_as_int(user.get("points")),
        title=str(settings.get("shop_title") or "积分商店"),
        subtitle=str(settings.get("shop_subtitle") or ""),
        items=items,
    )


def find_products(data: dict[str, Any], name: str) -> tuple[list[Product], str]:
    """按名称模糊搜索商品。

    Args:
        data: 数据字典。
        name: 用户输入的商品名（可带空格）。

    Returns:
        ``(候选列表, 错误说明)``。错误说明非空时表示请求本身不合法。
    """
    keyword = (name or "").strip()
    if not keyword:
        return [], "请带上商品名，例如：/兑换商品 可乐"
    products = [item for item in all_products(data) if item.enabled]
    exact = [item for item in products if item.name == keyword]
    if exact:
        return exact, ""
    compact = keyword.replace(" ", "").lower()
    folded = [item for item in products if item.name.replace(" ", "").lower() == compact]
    if folded:
        return folded, ""
    partial = [
        item for item in products if compact and compact in item.name.replace(" ", "").lower()
    ]
    if partial:
        return partial, ""
    return [], f"没有找到商品「{keyword}」，发送 /商店 看看有哪些吧"


def calculate_checkin_points(
    rewards: dict[str, Any],
    streak: int,
    rng: Callable[[int, int], int] = random.randint,
) -> tuple[int, int]:
    """按规则计算本次打卡积分。

    Args:
        rewards: 打卡奖励配置。
        streak: 连签天数（含本次）。
        rng: 随机数函数，便于测试注入。

    Returns:
        ``(本次基础积分, 连签奖励)``。
    """
    mode = str(rewards.get("mode") or "fixed")
    if mode not in REWARD_MODES:
        mode = "fixed"
    if mode == "random":
        low = _as_int(rewards.get("random_min"), 5)
        high = _as_int(rewards.get("random_max"), low)
        low, high = max(0, low), max(0, high)
        if high < low:
            low, high = high, low
        base = max(0, _as_int(rng(low, high)))
    elif mode == "custom":
        values = [_as_int(item) for item in rewards.get("custom_list") or []]
        values = [max(0, item) for item in values]
        if not values:
            base = max(0, _as_int(rewards.get("fixed"), 0))
        else:
            weights = [_as_int(item) for item in rewards.get("custom_weights") or []]
            if len(weights) == len(values) and sum(max(0, w) for w in weights) > 0:
                safe_weights = [max(0, w) for w in weights]
                base = rng_choice(values, safe_weights, rng)
            else:
                base = values[int(rng(0, len(values) - 1))]
    else:
        base = max(0, _as_int(rewards.get("fixed"), 0))

    bonus = 0
    if rewards.get("streak_bonus_enabled"):
        every = max(0, _as_int(rewards.get("streak_bonus_every"), 0))
        if every > 0 and streak > 0 and streak % every == 0:
            bonus = max(0, _as_int(rewards.get("streak_bonus_points"), 0))
    return base, bonus


def rng_choice(
    values: list[int],
    weights: list[int],
    rng: Callable[[int, int], int],
) -> int:
    """按权重随机抽一个值。

    Args:
        values: 候选积分。
        weights: 与 ``values`` 等长的权重（非负）。
        rng: 随机数函数。

    Returns:
        抽中的积分值。
    """
    total = sum(weights)
    if total <= 0:
        return values[0]
    point = rng(1, total)
    acc = 0
    for value, weight in zip(values, weights):
        acc += weight
        if point <= acc:
            return value
    return values[-1]


def perform_checkin(
    data: dict[str, Any],
    user_key: str,
    nickname: str,
    now: float,
    rng: Callable[[int, int], int] = random.randint,
) -> CheckinOutcome:
    """在数据快照上执行一次打卡。

    Args:
        data: 数据快照（会被就地修改）。
        user_key: 用户主键。
        nickname: 发送者昵称。
        now: 当前时间戳。
        rng: 随机数函数。

    Returns:
        打卡结果。
    """
    settings = data.setdefault("settings", {})
    rewards = data.setdefault("rewards", {})
    user = _ensure_user(data, user_key)
    _remember_identity(user, nickname)

    reset_hour = normalize_reset_hour(settings.get("reset_hour"), 0)
    today = logical_date_str(now, reset_hour)
    last = str(user.get("last_checkin_date") or "")
    if last == today:
        return CheckinOutcome(
            success=False,
            message="今天已经打过卡啦，明天再来吧～",
            streak=_as_int(user.get("streak")),
            total=_as_int(user.get("points")),
            date=today,
        )

    streak = _as_int(user.get("streak")) + 1 if last and last == previous_day(today) else 1
    base, bonus = calculate_checkin_points(rewards, streak, rng)
    gained = base + bonus
    user["points"] = _as_int(user.get("points")) + gained
    user["total_earned"] = _as_int(user.get("total_earned")) + gained
    user["checkin_count"] = _as_int(user.get("checkin_count")) + 1
    user["streak"] = streak
    user["last_checkin_date"] = today
    user["updated_at"] = now
    history = user.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        user["history"] = history
    history.append(
        {
            "date": today,
            "points": gained,
            "base": base,
            "bonus": bonus,
            "streak": streak,
            "time": now,
        }
    )
    if len(history) > 400:
        del history[: len(history) - 400]
    return CheckinOutcome(
        success=True,
        message="打卡成功",
        points=gained,
        streak=streak,
        total=_as_int(user.get("points")),
        date=today,
        bonus=bonus,
    )


def build_pending(
    data: dict[str, Any],
    product: Product,
    session_key: str,
    user_key: str,
    now: float,
) -> PendingConfirm:
    """为一次兑换生成待确认请求。"""
    settings = data.get("settings", {})
    timeout = max(10, _as_int(settings.get("confirm_timeout_seconds"), 60))
    return PendingConfirm(
        token="SHOP-" + uuid.uuid4().hex[:6].upper(),
        session_key=session_key,
        user_key=user_key,
        product_id=product.id,
        product_name=product.name,
        cost=product.cost,
        created_at=now,
        expires_at=now + timeout,
    )


def prompt_text(pending: PendingConfirm, timeout_seconds: int) -> str:
    """生成确认提示文案。"""
    return (
        f"是否确认兑换「{pending.product_name}」商品？\n"
        f"需要 {pending.cost} 积分。\n"
        f"请引用本条消息回复 是/否（{int(timeout_seconds)} 秒内有效，编号 {pending.token}）"
    )


def pick_pending(
    pendings: dict[str, PendingConfirm],
    user_key: str,
    session_key: str,
    now: float,
) -> tuple[PendingConfirm | None, bool]:
    """挑出当前用户在当前会话里的有效待确认请求。

    Args:
        pendings: 待确认请求表。
        user_key: 用户主键。
        session_key: 会话键。
        now: 当前时间戳。

    Returns:
        ``(请求或 None, 是否因为超时被丢弃)``。
    """
    found: PendingConfirm | None = None
    timed_out = False
    for key, pending in list(pendings.items()):
        if pending.user_key != user_key or pending.session_key != session_key:
            continue
        if pending.expires_at <= now:
            pendings.pop(key, None)
            timed_out = True
            continue
        if found is None or pending.created_at >= found.created_at:
            found = pending
    return found, timed_out


def match_confirm_reply(
    pending: PendingConfirm,
    quote_text: str,
    quote_id: str,
    quote_sender_id: str,
    bot_id: str,
    pending_ids: Iterable[str] = (),
) -> bool:
    """判断用户的回复是否引用了本次确认提示。

    分层匹配（任一层通过即可）：编号 token → 机器人消息 ID → 引用发送者是机器人。
    """
    text = (quote_text or "").strip()
    if pending.token and pending.token in text:
        return True
    quoted_id = str(quote_id or "").strip()
    if quoted_id and quoted_id in {str(item) for item in pending_ids if item}:
        return True
    if not text and not quoted_id:
        # 平台完全没有给出引用信息（既没有正文也没有 ID）时无法校验，按未匹配处理
        return False
    sender = str(quote_sender_id or "").strip()
    if sender and str(bot_id or "").strip() and sender == str(bot_id).strip():
        return True
    return False


def parse_decision(text: str) -> str:
    """把用户回复归一化成 yes / no / unknown。"""
    compact = (text or "").strip().replace(" ", "").lower()
    if compact in {"是", "y", "yes", "确认", "好", "好的", "确定", "ok"}:
        return "yes"
    if compact in {"否", "n", "no", "取消", "不要", "算了", "不"}:
        return "no"
    return "unknown"


def decide_confirmation(
    pending: PendingConfirm | None,
    text: str,
    quote_text: str,
    quote_id: str,
    quote_sender_id: str,
    bot_id: str,
    pending_ids: Iterable[str] = (),
    timed_out: bool = False,
) -> ConfirmOutcome:
    """判断这次回复是否要处理，以及用户选了什么。

    Args:
        pending: 当前待确认请求，None 表示没有。
        text: 用户本次发送的正文。
        quote_text: 被引用消息正文。
        quote_id: 被引用消息 ID。
        quote_sender_id: 被引用消息发送者。
        bot_id: 机器人自身 ID。
        pending_ids: 本次会话记录过的机器人消息 ID。
        timed_out: 是否已有请求刚过期被清理。

    Returns:
        处理结果；``handled=False`` 表示这条消息与本插件无关，应放行。
    """
    stripped = (text or "").strip()
    decision = parse_decision(stripped)
    if pending is None:
        if decision == "unknown":
            return ConfirmOutcome(handled=False, ok=False, message="")
        if timed_out:
            return ConfirmOutcome(
                handled=True, ok=False, timed_out=True, message="上一次兑换请求已超时，请重新发送 /兑换商品 <名称>"
            )
        return ConfirmOutcome(
            handled=True, ok=False, message="当前没有等待确认的兑换请求，请先发送 /兑换商品 <名称>"
        )
    if decision == "unknown":
        return ConfirmOutcome(handled=False, ok=False, message="")
    if not match_confirm_reply(pending, quote_text, quote_id, quote_sender_id, bot_id, pending_ids):
        return ConfirmOutcome(
            handled=True,
            ok=False,
            product_name=pending.product_name,
            message=(
                "引用的消息不是本次兑换确认（编号不匹配）。\n"
                f"请引用上一条确认消息回复 是/否，或重新发送 /兑换商品 {pending.product_name}"
            ),
        )
    return ConfirmOutcome(
        handled=True,
        ok=decision == "yes",
        product_name=pending.product_name,
        cost=pending.cost,
        cancelled=decision == "no",
        message="已确认兑换" if decision == "yes" else "已取消本次兑换",
    )


def perform_redemption(
    data: dict[str, Any],
    pending: PendingConfirm,
    now: float,
) -> tuple[bool, str, int, str]:
    """在数据快照上执行兑换（扣分、扣库存、写记录）。

    Args:
        data: 数据快照（会被就地修改）。
        pending: 待确认请求。
        now: 当前时间戳。

    Returns:
        ``(是否成功, 提示文案, 扣分后的余额, 记录 ID)``。
    """
    user = _ensure_user(data, pending.user_key)
    product_raw = next(
        (item for item in data.get("products", []) if item.get("id") == pending.product_id),
        None,
    )
    if product_raw is None:
        return False, "这个商品已经被删除了，本次兑换已取消", _as_int(user.get("points")), ""
    product = Product.from_dict(product_raw)
    if not product.enabled:
        return False, f"「{product.name}」已下架，本次兑换已取消", _as_int(user.get("points")), ""
    if product.stock is not None and product.stock <= 0:
        return False, f"「{product.name}」库存不足，本次兑换已取消", _as_int(user.get("points")), ""
    state = product_state(data, product, pending.user_key)
    if state.remaining_quota is not None and state.remaining_quota <= 0:
        return False, f"「{product.name}」已达限购数量，本次兑换已取消", _as_int(user.get("points")), ""
    points = _as_int(user.get("points"))
    if points < product.cost:
        return (
            False,
            f"积分不足：需要 {product.cost}，当前只有 {points}",
            points,
            "",
        )

    user["points"] = points - product.cost
    user["total_spent"] = _as_int(user.get("total_spent")) + product.cost
    user["updated_at"] = now
    if product.stock is not None:
        product_raw["stock"] = max(0, _as_int(product_raw.get("stock")) - 1)
    product_raw["redeemed"] = _as_int(product_raw.get("redeemed")) + 1

    record_id = "r_" + uuid.uuid4().hex[:10]
    data.setdefault("redemptions", []).append(
        {
            "id": record_id,
            "user_key": pending.user_key,
            "user_id": str(user.get("user_id") or ""),
            "nickname": str(user.get("nickname") or ""),
            "product_id": product.id,
            "product_name": product.name,
            "cost": product.cost,
            "status": "待发放",
            "created_at": now,
            "updated_at": now,
            "note": "",
        }
    )
    message = (
        f"兑换成功：{product.name}\n"
        f"消耗 {product.cost} 积分，当前余额 {user['points']} 积分\n"
        "管理员会在后台标记发放状态。"
    )
    return True, message, _as_int(user["points"]), record_id


def leaderboard(
    data: dict[str, Any],
    platform: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按积分降序返回榜单数据。"""
    scope = str(data.get("settings", {}).get("leaderboard_scope") or "platform")
    rows: list[dict[str, Any]] = []
    for key, user in (data.get("users") or {}).items():
        if not isinstance(user, dict):
            continue
        key_platform = key.split(":", 1)[0]
        if scope != "global" and platform and key_platform != platform:
            continue
        rows.append(
            {
                "user_key": key,
                "user_id": str(user.get("user_id") or key.split(":", 1)[-1]),
                "nickname": str(user.get("nickname") or user.get("user_id") or key),
                "points": _as_int(user.get("points")),
                "streak": _as_int(user.get("streak")),
                "checkin_count": _as_int(user.get("checkin_count")),
            }
        )
    rows.sort(key=lambda item: (-item["points"], item["nickname"]))
    return rows[: max(1, _as_int(limit, 20))]


def user_history(data: dict[str, Any], user_key: str, limit: int = 7) -> list[dict[str, Any]]:
    """返回用户最近若干条打卡记录（新的在前）。"""
    history = (data.get("users", {}).get(user_key) or {}).get("history") or []
    items = [item for item in history if isinstance(item, dict)]
    return list(reversed(items))[: max(1, int(limit))]


# ---------------------------------------------------------------------- 后台操作


def create_product(payload: dict[str, Any], now: float) -> tuple[dict[str, Any] | None, str]:
    """校验并构造一个新产品字典。"""
    normalized, error = normalize_product(payload)
    if error:
        return None, error
    normalized["id"] = "p_" + uuid.uuid4().hex[:8]
    normalized["created_at"] = now
    normalized["redeemed"] = 0
    return normalized, ""


def normalize_product(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """校验商品字段。

    Args:
        payload: 页面/接口传入的商品字段。

    Returns:
        ``(规范化后的字段, 错误说明)``。
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        return {}, "商品名称不能为空"
    if len(name) > 40:
        return {}, "商品名称不能超过 40 个字符"
    cost = _as_int(payload.get("cost"), -1)
    if cost < 0:
        return {}, "商品价格必须是非负整数"
    stock_raw = payload.get("stock")
    if stock_raw not in (None, "") and _as_int(stock_raw, -1) < 0:
        return {}, "库存必须是非负整数或留空表示不限量"
    limit_raw = payload.get("per_user_limit")
    if limit_raw not in (None, "") and _as_int(limit_raw, -1) < 0:
        return {}, "限购数量必须是非负整数或留空表示不限购"
    stock = _as_optional_int(stock_raw)
    limit = _as_optional_int(limit_raw)
    return (
        {
            "name": name,
            "cost": cost,
            "description": str(payload.get("description") or "").strip()[:200],
            "category": str(payload.get("category") or "默认").strip()[:20] or "默认",
            "stock": stock,
            "per_user_limit": limit,
            "enabled": bool(payload.get("enabled", True)),
            "sort_order": _as_int(payload.get("sort_order")),
            "image_path": str(payload.get("image_path") or "").strip(),
        },
        "",
    )


def upsert_product(
    data: dict[str, Any],
    payload: dict[str, Any],
    now: float,
) -> tuple[dict[str, Any] | None, str]:
    """新增或更新商品。

    Returns:
        ``(保存后的商品字典, 错误说明)``。
    """
    product_id = str(payload.get("id") or "").strip()
    normalized, error = normalize_product(payload)
    if error:
        return None, error
    products = data.setdefault("products", [])
    names = {str(item.get("name")) for item in products if item.get("id") != product_id}
    if normalized["name"] in names:
        return None, f"已经存在名为「{normalized['name']}」的商品"
    if product_id:
        target = next((item for item in products if item.get("id") == product_id), None)
        if target is None:
            return None, "商品不存在，请刷新页面后重试"
        target.update(normalized)
        return target, ""
    created, error = create_product(normalized, now)
    if created is None:
        return None, error
    products.append(created)
    return created, ""


def delete_product(data: dict[str, Any], product_id: str) -> tuple[bool, str]:
    """删除商品；存在未完成兑换记录时只下架。"""
    products = data.setdefault("products", [])
    target = next((item for item in products if item.get("id") == product_id), None)
    if target is None:
        return False, "商品不存在"
    pending = [
        item
        for item in data.get("redemptions", [])
        if isinstance(item, dict)
        and item.get("product_id") == product_id
        and item.get("status") == "待发放"
    ]
    name = str(target.get("name") or "")
    if pending:
        target["enabled"] = False
        return True, f"「{name}」还有 {len(pending)} 条待发放记录，已改为下架而不是删除"
    products.remove(target)
    return True, f"已删除「{name}」"


def reorder_products(data: dict[str, Any], order: Iterable[str]) -> int:
    """按给定 ID 顺序重排商品。"""
    products = data.setdefault("products", [])
    changed = 0
    for position, product_id in enumerate(order):
        target = next((item for item in products if item.get("id") == str(product_id)), None)
        if target is None:
            continue
        target["sort_order"] = (position + 1) * 10
        changed += 1
    return changed


def adjust_user_points(
    data: dict[str, Any],
    user_key: str,
    delta: int | None,
    absolute: int | None,
    note: str,
    now: float,
) -> tuple[bool, str, int]:
    """管理员调整用户积分。

    Args:
        data: 数据快照。
        user_key: 用户主键。
        delta: 增量（与 ``absolute`` 二选一）。
        absolute: 目标值。
        note: 备注，写入历史。
        now: 当前时间戳。

    Returns:
        ``(是否成功, 提示文案, 调整后积分)``。
    """
    user = _ensure_user(data, user_key)
    before = _as_int(user.get("points"))
    if absolute is not None:
        target = int(absolute)
    else:
        target = before + int(delta or 0)
    if target < 0:
        return False, "调整后积分不能为负数", before
    user["points"] = target
    gained = target - before
    if gained > 0:
        user["total_earned"] = _as_int(user.get("total_earned")) + gained
    else:
        user["total_spent"] = _as_int(user.get("total_spent")) + abs(gained)
    user["updated_at"] = now
    history = user.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        user["history"] = history
    history.append(
        {
            "date": logical_date_str(now, normalize_reset_hour(data.get("settings", {}).get("reset_hour"), 0)),
            "points": gained,
            "base": 0,
            "bonus": 0,
            "streak": _as_int(user.get("streak")),
            "time": now,
            "note": f"管理员调整：{note}" if note else "管理员调整",
        }
    )
    return True, f"积分已从 {before} 调整为 {target}", target


def transition_redemption(
    data: dict[str, Any],
    record_id: str,
    status: str,
    note: str,
    restore: bool,
    now: float,
) -> tuple[bool, str]:
    """更新兑换记录状态，取消时可选返还积分与回补库存。"""
    if status not in REDEMPTION_STATUSES:
        return False, "状态必须是 待发放 / 已发放 / 已取消"
    record = next(
        (item for item in data.get("redemptions", []) if item.get("id") == record_id),
        None,
    )
    if record is None:
        return False, "兑换记录不存在"
    previous = str(record.get("status") or "")
    record["status"] = status
    record["note"] = note or record.get("note", "")
    record["updated_at"] = now
    if status == "已取消" and previous != "已取消" and restore:
        user = _ensure_user(data, str(record.get("user_key") or ""))
        cost = _as_int(record.get("cost"))
        user["points"] = _as_int(user.get("points")) + cost
        user["total_spent"] = max(0, _as_int(user.get("total_spent")) - cost)
        user["updated_at"] = now
        product = next(
            (item for item in data.get("products", []) if item.get("id") == record.get("product_id")),
            None,
        )
        if product is not None:
            if product.get("stock") is not None:
                product["stock"] = _as_int(product.get("stock")) + 1
            product["redeemed"] = max(0, _as_int(product.get("redeemed")) - 1)
        record["note"] = (note or "已取消并返还积分") + "（积分已返还）"
    return True, f"记录状态已更新为 {status}"


def shop_statistics(data: dict[str, Any]) -> dict[str, Any]:
    """后台概览统计。"""
    users = data.get("users") or {}
    products = data.get("products") or []
    redemptions = data.get("redemptions") or []
    today = logical_date_str(time.time(), normalize_reset_hour(data.get("settings", {}).get("reset_hour"), 0))
    return {
        "user_count": len(users),
        "product_count": len(products),
        "enabled_product_count": len([item for item in products if item.get("enabled", True)]),
        "total_points": sum(_as_int(item.get("points")) for item in users.values() if isinstance(item, dict)),
        "checkin_today": len(
            [
                item
                for item in users.values()
                if isinstance(item, dict) and str(item.get("last_checkin_date") or "") == today
            ]
        ),
        "redemption_count": len(redemptions),
        "pending_redemption_count": len(
            [item for item in redemptions if isinstance(item, dict) and item.get("status") == "待发放"]
        ),
    }


def list_users(
    data: dict[str, Any],
    keyword: str = "",
    platform: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """后台用户列表（分页 + 关键字）。"""
    rows: list[dict[str, Any]] = []
    keyword = (keyword or "").strip().lower()
    for key, user in (data.get("users") or {}).items():
        if not isinstance(user, dict):
            continue
        if platform and not key.startswith(f"{platform}:"):
            continue
        nickname = str(user.get("nickname") or "")
        user_id = str(user.get("user_id") or "")
        if keyword and keyword not in f"{nickname} {user_id} {key}".lower():
            continue
        rows.append(
            {
                "user_key": key,
                "platform": key.split(":", 1)[0],
                "user_id": user_id,
                "nickname": nickname,
                "points": _as_int(user.get("points")),
                "streak": _as_int(user.get("streak")),
                "checkin_count": _as_int(user.get("checkin_count")),
                "last_checkin_date": str(user.get("last_checkin_date") or ""),
            }
        )
    rows.sort(key=lambda item: (-item["points"], item["user_key"]))
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))
    start = (page - 1) * page_size
    return {
        "items": rows[start : start + page_size],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
    }


def list_redemptions(
    data: dict[str, Any],
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """后台兑换记录（分页 + 状态筛选）。"""
    rows = [
        dict(item)
        for item in data.get("redemptions", [])
        if isinstance(item, dict) and (not status or item.get("status") == status)
    ]
    rows.sort(key=lambda item: -_as_float(item.get("created_at")))
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))
    start = (page - 1) * page_size
    return {
        "items": rows[start : start + page_size],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
    }


def public_settings(data: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """页面首屏需要的设置/规则快照。"""
    settings = copy.deepcopy(data.get("settings") or {})
    rewards = copy.deepcopy(data.get("rewards") or {})
    return {
        "settings": settings,
        "rewards": rewards,
        "statistics": stats if stats is not None else shop_statistics(data),
    }


__all__ = [
    "CheckinOutcome",
    "ConfirmOutcome",
    "PendingConfirm",
    "Product",
    "ProductState",
    "REDEMPTION_STATUSES",
    "REWARD_MODES",
    "ShopView",
    "Store",
    "adjust_user_points",
    "all_products",
    "build_pending",
    "build_shop_view",
    "calculate_checkin_points",
    "decide_confirmation",
    "delete_product",
    "find_products",
    "leaderboard",
    "list_redemptions",
    "list_users",
    "match_confirm_reply",
    "normalize_product",
    "parse_decision",
    "perform_checkin",
    "perform_redemption",
    "pick_pending",
    "product_state",
    "prompt_text",
    "public_settings",
    "reorder_products",
    "shop_statistics",
    "transition_redemption",
    "upsert_product",
    "user_history",
    "user_key_for",
]
