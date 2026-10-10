"""AstrBot 打卡积分商店插件。

指令：
* ``打卡`` / ``签到``：每日打卡获得积分
* ``商店``：渲染可兑换 / 不可兑换商品图片
* ``我的积分`` / ``积分``：个人积分卡片
* ``积分榜``：平台积分排行
* ``兑换商品 <名称>`` / ``兑换 <名称>``：发起兑换，机器人回复确认提示
* 用户引用确认消息回复 ``是`` / ``否`` 完成兑换

后台：WebUI 插件页（``pages/shop``）+ 插件配置页。
"""

from __future__ import annotations

import os
import re
import time
from functools import partial
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - 兜底成普通字符串
    GreedyStr = str  # type: ignore[assignment,misc]

try:
    from astrbot.core.utils.session_waiter import SessionController, session_waiter
except ImportError:  # pragma: no cover - 极老版本没有该模块
    # 兑换确认走 on_confirm_message 监听器，不依赖会话等待器；这里只是保留名字，
    # 便于后续需要"进入多轮对话"时直接使用。
    SessionController = None  # type: ignore[assignment]
    session_waiter = None  # type: ignore[assignment]

try:  # 插件以包的方式加载
    from .core import business, dashboard_api, render
    from .core.store import Store, default_data
except ImportError:  # AstrBot 把 main.py 当顶层模块加载
    from core import business, dashboard_api, render  # type: ignore[no-redef]
    from core.store import Store, default_data  # type: ignore[no-redef]

PLUGIN_ID = "astrbot_plugin_checkin_shop"
PLUGIN_VERSION = "1.0.0"
DEFAULT_DATA_DIR = os.path.join("data", "plugin_data", PLUGIN_ID)

_CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)")


def plugin_data_dir() -> str:
    """解析插件数据目录，优先使用 AstrBot 的 plugin_data。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_ID)
    except Exception:  # pragma: no cover - 单测 / 老版本
        return os.path.abspath(DEFAULT_DATA_DIR)


@register(
    PLUGIN_ID,
    "AstrBot plugin community",
    "每日打卡得积分，积分可在后台配置的商店里兑换商品",
    PLUGIN_VERSION,
)
class CheckinShopPlugin(Star):
    """打卡积分商店主类。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = Store(plugin_data_dir())
        self._pendings: dict[str, business.PendingConfirm] = {}
        self._handled_tokens: set[str] = set()
        self._bot_ids: dict[str, str] = {}
        self._api = dashboard_api.DashboardAPI(self)
        self._registered = False

    # ------------------------------------------------------------------ 基础

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取插件配置（AstrBotConfig 或普通 dict）。"""
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def _seed(self) -> dict[str, Any]:
        """首次创建数据文件时用配置页的值作为种子。"""
        data = default_data()
        settings = data["settings"]
        rewards = data["rewards"]
        for key in (
            "points_scope",
            "leaderboard_scope",
            "confirm_timeout_seconds",
            "shop_title",
            "shop_subtitle",
        ):
            value = self._cfg(key)
            if value not in (None, ""):
                settings[key] = value
        leaderboard_size = self._cfg("leaderboard_size")
        if leaderboard_size:
            settings["leaderboard_size"] = int(leaderboard_size)
        reset_hour = self._cfg("reset_hour")
        if reset_hour is not None:
            settings["reset_hour"] = reset_hour
        for key, target in (
            ("checkin_mode", "mode"),
            ("checkin_fixed", "fixed"),
            ("checkin_random_min", "random_min"),
            ("checkin_random_max", "random_max"),
            ("checkin_custom_points", "custom_list"),
            ("checkin_custom_weights", "custom_weights"),
            ("streak_bonus_enabled", "streak_bonus_enabled"),
            ("streak_bonus_every", "streak_bonus_every"),
            ("streak_bonus_points", "streak_bonus_points"),
        ):
            value = self._cfg(key)
            if value not in (None, ""):
                rewards[target] = value
        return data

    def _palette(self) -> render.CardPalette:
        """构造卡片尺寸配置。"""
        return render.CardPalette(
            width=int(self._cfg("render_width", 0) or render.DEFAULT_WIDTH),
            font_size_base=int(self._cfg("render_font_size_base", 0) or 0),
            font_path=str(self._cfg("render_font_path", "") or ""),
        )

    # ------------------------------------------------------------------ 事件工具

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _user_key(self, event: AstrMessageEvent) -> str:
        platform = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform = str(getter() or "")
            except Exception:
                platform = ""
        if not platform:
            try:
                platform = str(event.get_platform_id() or "")
            except Exception:
                platform = ""
        return business.user_key_for(platform, str(event.get_sender_id() or ""))

    def _sender_name(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or "")
        except Exception:
            return ""

    def _bot_id(self, event: AstrMessageEvent) -> str:
        """尽力解析机器人自身 ID，用于引用来源校验。"""
        session = self._session_key(event)
        known = self._bot_ids.get(session)
        if known:
            return known
        candidate = ""
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                candidate = str(getter() or "")
            except Exception:
                candidate = ""
        if candidate:
            self._bot_ids[session] = candidate
        return candidate

    def _quote_info(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """取出被引用消息的 ``(正文, 消息ID, 发送者ID)``。

        AstrBot 会把引用解析成 ``Reply`` 组件；部分平台还会在原始 payload 里保留
        OneBot 的 ``reply`` 段，这里优先使用原始 payload 的 ID（适配器可能在
        ``get_msg`` 展开后替换 ID）。
        """
        text, message_id, sender_id = "", "", ""
        for component in event.get_messages() or []:
            if getattr(component, "type", "") != "Reply":
                continue
            text = str(
                getattr(component, "message_str", "")
                or getattr(component, "text", "")
                or ""
            )
            message_id = str(getattr(component, "id", "") or "")
            sender_id = str(getattr(component, "sender_id", "") or "")
            break
        if not (text and message_id):
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            payload = raw if isinstance(raw, dict) else getattr(raw, "raw_event", None)
            if isinstance(payload, dict):
                segments = payload.get("message")
                if isinstance(segments, list):
                    for segment in segments:
                        if isinstance(segment, dict) and segment.get("type") == "reply":
                            raw_id = (segment.get("data") or {}).get("id")
                            if raw_id not in (None, ""):
                                message_id = str(raw_id)
                                break
                elif isinstance(segments, str):
                    matched = _CQ_REPLY_RE.search(segments)
                    if matched:
                        message_id = matched.group(1)
        return text, message_id, sender_id

    async def _send(self, event: AstrMessageEvent, text: str, card: Any = None):
        """发送文本 +（可选）图片。

        Args:
            event: 当前消息事件。
            text: 文本内容；为空时不发送文本。
            card: 零参出图回调（同步函数，返回图片路径）；抛异常时自动降级为纯文本。
        """
        if card is not None:
            try:
                path = card()
                if path:
                    yield event.image_result(str(path))
            except Exception:
                logger.exception("[打卡商店] 图片渲染失败，已降级为文本")
        if text:
            yield event.plain_result(text)

    def _image_resolver(self):
        """商品图片读取回调。"""
        if not render.PIL_AVAILABLE:
            return None

        def resolve(relative: str):
            try:
                safe = str(relative or "").replace("\\", "/").lstrip("/")
                parts = [part for part in safe.split("/") if part]
                if not parts or any(part in (".", "..") for part in parts):
                    return None
                path = os.path.join(self.store.directory, *parts)
                if not os.path.exists(path):
                    return None
                return render.Image.open(path)
            except Exception:
                return None

        return resolve

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件激活：加载数据、注册后台 API。"""
        try:
            await self.store.open(self._seed())
        except Exception:
            logger.exception("[打卡商店] 数据加载失败")
        if not self._registered:
            self._api.register()
            self._registered = True
        if not render.available():
            logger.warning(
                "[打卡商店] 未能加载字体/Pillow，商店等指令将降级为纯文本；"
                "请检查 requirements.txt 与 resources/NotoSansCJKsc-Regular.otf"
            )

    async def terminate(self) -> None:
        """插件停用：落盘并释放资源。"""
        try:
            await self.store.flush()
        except Exception:
            logger.exception("[打卡商店] 停用时保存数据失败")
        self._pendings.clear()

    # ------------------------------------------------------------------ 指令

    @filter.command("打卡", alias={"签到"})
    async def cmd_checkin(self, event: AstrMessageEvent):
        """打卡：每天一次，按后台配置发放积分。"""
        user_key = self._user_key(event)
        now = time.time()
        outcome = await self.store.mutate(
            lambda data: business.perform_checkin(
                data, user_key, self._sender_name(event), now
            ),
            self._seed(),
        )
        if not outcome.success:
            yield event.plain_result(f"{outcome.message}\n当前积分：{outcome.total}")
            return
        bonus_line = f"（含连签奖励 {outcome.bonus}）" if outcome.bonus else ""
        yield event.plain_result(
            f"打卡成功！本次获得 {outcome.points} 积分{bonus_line}\n"
            f"连签 {outcome.streak} 天 · 当前积分 {outcome.total}\n"
            "发送 /商店 看看能换点什么吧～"
        )
        card = await self._points_card(user_key, self._sender_name(event))
        async for result in self._send(event, "", card):
            yield result

    async def _points_card(self, user_key: str, fallback_name: str = ""):
        """返回"我的积分"卡片渲染回调（闭包，供 :meth:`_send` 调用）。"""
        data = await self.store.data(self._seed())

        def render_card() -> str:
            user = (data.get("users") or {}).get(user_key) or {}
            name = str(
                user.get("nickname") or fallback_name or user.get("user_id") or user_key
            )
            return render.render_points(
                user,
                name,
                business.user_history(data, user_key, 7),
                self.store.render_dir,
                self._palette(),
            )

        return render_card

    @filter.command("商店")
    async def cmd_shop(self, event: AstrMessageEvent):
        """商店：渲染可兑换 / 不可兑换商品图片。"""
        user_key = self._user_key(event)
        data = await self.store.data(self._seed())
        view = business.build_shop_view(data, user_key)
        if not view.items:
            yield event.plain_result(
                "商店暂时还没有商品，请管理员在 AstrBot 后台的插件页添加。\n"
                f"你当前有 {view.points} 积分。"
            )
            return
        text = (
            f"{view.title}：当前 {view.points} 积分，"
            f"可兑换 {len(view.redeemable)} 件 / 暂不可兑换 {len(view.locked)} 件"
        )

        def card() -> str:
            return render.render_shop(
                view,
                self.store.render_dir,
                self._image_resolver(),
                self._palette(),
            )

        async for result in self._send(event, text, card):
            yield result

    @filter.command("我的积分", alias={"积分"})
    async def cmd_points(self, event: AstrMessageEvent):
        """我的积分：个人积分卡片。"""
        user_key = self._user_key(event)
        name = self._sender_name(event)
        data = await self.store.data(self._seed())
        user = (data.get("users") or {}).get(user_key) or {}
        text = (
            f"当前积分：{int(user.get('points') or 0)}\n"
            f"连签 {int(user.get('streak') or 0)} 天 · "
            f"累计打卡 {int(user.get('checkin_count') or 0)} 次\n"
            "发送 /打卡 赚积分，/商店 查看商品"
        )
        card = await self._points_card(user_key, name)
        async for result in self._send(event, text, card):
            yield result

    @filter.command("积分榜", alias={"排行榜"})
    async def cmd_rank(self, event: AstrMessageEvent):
        """积分榜：同平台积分排行。"""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        data = await self.store.data(self._seed())
        settings = data.get("settings") or {}
        rows = business.leaderboard(
            data, platform, int(settings.get("leaderboard_size") or 20)
        )
        scope = "全平台" if str(settings.get("leaderboard_scope")) == "global" else "本平台"
        if not rows:
            yield event.plain_result("还没有人打卡，发送 /打卡 抢占第一名！")
            return
        lines = [f"积分榜（{scope}，共 {len(rows)} 人）"]
        for index, row in enumerate(rows[:10], 1):
            lines.append(
                f"{index}. {row['nickname']} — {row['points']} 积分（连签 {row['streak']} 天）"
            )

        def card() -> str:
            return render.render_board(
                rows,
                self.store.render_dir,
                "积分榜",
                f"{scope} · 共 {len(rows)} 人",
                self._palette(),
            )

        async for result in self._send(event, "\n".join(lines), card):
            yield result

    @filter.command("兑换商品", alias={"兑换"})
    async def cmd_redeem(self, event: AstrMessageEvent, 商品名: GreedyStr = ""):
        """兑换商品：校验后给出确认提示，等待用户引用回复 是/否。

        用法：``/兑换商品 可乐``、``/兑换商品 冰镇 可乐``、``/兑换 可乐``。
        """
        keyword = str(商品名 or "").strip()
        user_key = self._user_key(event)
        session = self._session_key(event)
        data = await self.store.data(self._seed())
        candidates, error = business.find_products(data, keyword)
        if error:
            yield event.plain_result(error)
            return
        if len(candidates) > 1:
            names = "、".join(item.name for item in candidates[:8])
            yield event.plain_result(
                f"匹配到多个商品：{names}\n请把名称写得更完整，例如 /兑换商品 {candidates[0].name}"
            )
            return
        product = candidates[0]
        state = business.product_state(data, product, user_key)
        if not state.redeemable:
            points = int((data.get("users") or {}).get(user_key, {}).get("points") or 0)
            hint = (
                f"还差 {state.deficit} 积分"
                if state.reason == business.REASON_POINTS
                else state.reason
            )
            yield event.plain_result(
                f"暂时不能兑换「{product.name}」：{hint}\n当前积分 {points}"
            )
            return
        pending = business.build_pending(data, product, session, user_key, time.time())
        self._prune_pendings()
        self._pendings[pending.token] = pending
        timeout = max(
            10, int((data.get("settings") or {}).get("confirm_timeout_seconds") or 60)
        )
        yield event.plain_result(business.prompt_text(pending, timeout))

    def _prune_pendings(self) -> None:
        """清理过期/过多的待确认请求与已处理编号。"""
        now = time.time()
        for key, pending in list(self._pendings.items()):
            if pending.expires_at <= now:
                self._pendings.pop(key, None)
        if len(self._pendings) > 200:
            ordered = sorted(self._pendings, key=lambda item: self._pendings[item].created_at)
            for key in ordered[:100]:
                self._pendings.pop(key, None)
        if len(self._handled_tokens) > 500:
            self._handled_tokens.clear()

    # ------------------------------------------------------------------ 兑换确认

    @filter.event_message_type(EventMessageType.ALL)
    async def on_confirm_message(self, event: AstrMessageEvent):
        """捕获用户对确认提示的引用回复（是/否）并真正完成兑换。

        只有在"该会话 + 该用户"存在未过期的待确认请求、且回复能匹配上确认编号时才
        会接管消息；其余消息原样放行，不影响其他指令与大模型对话。
        """
        user_key = self._user_key(event)
        session = self._session_key(event)
        try:
            message_text = str(event.get_message_str() or "")
        except Exception:
            message_text = ""
        # 只关心明确的"是/否"，其余消息一律放行给其他指令/大模型
        if business.parse_decision(message_text) == "unknown":
            return
        pending, timed_out = business.pick_pending(
            self._pendings, user_key, session, time.time()
        )
        if pending is None:
            if timed_out:
                yield event.plain_result(
                    "上一次兑换请求已超时，请重新发送 /兑换商品 <名称>"
                )
            else:
                yield event.plain_result(
                    "当前没有等待确认的兑换请求，请先发送 /兑换商品 <名称>"
                )
            return
        quote_text, quote_id, quote_sender = self._quote_info(event)
        decision = business.decide_confirmation(
            pending,
            message_text,
            quote_text,
            quote_id,
            quote_sender,
            self._bot_id(event),
            (),
            timed_out,
        )
        if not decision.handled:
            return
        if pending.token in self._handled_tokens:
            return
        if not decision.ok and not decision.cancelled:
            yield event.plain_result(decision.message)
            return
        self._handled_tokens.add(pending.token)
        self._pendings.pop(pending.token, None)
        if decision.cancelled:
            yield event.plain_result(f"已取消兑换「{pending.product_name}」。")
            return
        _ok, message, _balance, _record = await self.store.mutate(
            lambda data: business.perform_redemption(data, pending, time.time()),
            self._seed(),
        )
        yield event.plain_result(message)
