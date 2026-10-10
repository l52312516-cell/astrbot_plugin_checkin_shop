"""AstrBot 打卡积分商店插件。

指令：
* ``打卡`` / ``签到``：每日打卡获得积分
* ``商店``：渲染可兑换 / 不可兑换商品图片
* ``我的积分`` / ``积分``：个人积分卡片
* ``积分榜``：平台积分排行
* ``兑换商品 <名称>`` / ``兑换 <名称>``：发起兑换，机器人回复确认提示
* ``商店帮助`` / ``打卡帮助``：渲染帮助图片（指令、当前规则、兑换流程）
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
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

try:  # 文本 + 图片合并成一条消息用
    from astrbot.api.message_components import Image, Plain
except ImportError:  # pragma: no cover - 老版本路径
    from astrbot.core.message.components import Image, Plain  # type: ignore[no-redef]

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
    from .core import business, dashboard_api, emailer, render
    from .core.store import Store, default_data
except ImportError:  # AstrBot 把 main.py 当顶层模块加载
    from core import business, dashboard_api, emailer, render  # type: ignore[no-redef]
    from core.store import Store, default_data  # type: ignore[no-redef]

PLUGIN_ID = "astrbot_plugin_checkin_shop"
PLUGIN_VERSION = "1.2.0"
DEFAULT_DATA_DIR = os.path.join("data", "plugin_data", PLUGIN_ID)

_CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)")


def _format_ts(value: Any) -> str:
    """把时间戳格式化成邮件里好读的本地时间；无效值返回空串。"""
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))

# 插件配置页（_conf_schema.json）与运行数据里重复出现的字段。
# 两边都能改，规则是"配置页优先"，但升级时如果配置页还是默认值，则保留
# 插件页里已经改过的值（见 ShopPlugin._effective）。
CONFIG_SETTINGS: tuple[tuple[str, str], ...] = (
    ("points_scope", "points_scope"),
    ("leaderboard_scope", "leaderboard_scope"),
    ("leaderboard_size", "leaderboard_size"),
    ("confirm_timeout_seconds", "confirm_timeout_seconds"),
    ("reset_hour", "reset_hour"),
    ("shop_title", "shop_title"),
    ("shop_subtitle", "shop_subtitle"),
    ("display_prefix", "display_prefix"),
)
CONFIG_REWARDS: tuple[tuple[str, str], ...] = (
    ("checkin_mode", "mode"),
    ("checkin_fixed", "fixed"),
    ("checkin_random_min", "random_min"),
    ("checkin_random_max", "random_max"),
    ("checkin_custom_points", "custom_list"),
    ("checkin_custom_weights", "custom_weights"),
    ("streak_bonus_enabled", "streak_bonus_enabled"),
    ("streak_bonus_every", "streak_bonus_every"),
    ("streak_bonus_points", "streak_bonus_points"),
)
# 参与"配置页 <-> 运行数据"合并的全部配置键，用于记录同步快照
CONFIG_KEYS: tuple[str, ...] = tuple(
    key for key, _ in CONFIG_SETTINGS
) + tuple(key for key, _ in CONFIG_REWARDS)
# 反向映射：运行数据字段 -> 配置页字段，供插件页保存后回写配置用
REWARDS_TO_CONFIG = {target: key for key, target in CONFIG_REWARDS}
SETTINGS_TO_CONFIG = {target: key for key, target in CONFIG_SETTINGS}

# 主动通知优先走私聊：OneBot 系平台可以用 <平台>:FriendMessage:<用户ID> 直接私聊用户
# （aiocqhttp 的非群会话 session_id 就是 QQ 号，见 aiocqhttp_message_event）。
# 其他平台不去猜格式，直接退回用户发起兑换时的会话。
PRIVATE_SESSION_TYPES = {"aiocqhttp": "FriendMessage"}

# 只发了指令名、没有唤醒前缀（也没 @ 机器人）的消息。AstrBot 的标准指令受
# wake_prefix 制约，这类消息会被直接忽略、机器人毫无反应——新用户最容易这么发。
# 正则过滤器不受 wake_prefix 制约，所以这里能兜住它并回一句用法说明。
BARE_COMMAND_RE = re.compile(
    r"^[\s]*(打卡|签到|商店|我的积分|积分|积分榜|排行榜|商店帮助|打卡帮助"
    r"|兑换商品|兑换|绑定邮箱|我的邮箱)[\s]*[!！。.、~～,，?？]*$"
)


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

    # 运行数据字段 -> 插件配置页字段；后台接口（core/dashboard_api）用它回写配置
    REWARDS_TO_CONFIG = REWARDS_TO_CONFIG
    SETTINGS_TO_CONFIG = SETTINGS_TO_CONFIG

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

    def _config_values(self) -> dict[str, Any]:
        """取插件配置页里"有值"的那些键（空值表示没配过，不参与合并）。"""
        values: dict[str, Any] = {}
        for key in CONFIG_KEYS:
            value = self._cfg(key)
            if value not in (None, ""):
                values[key] = value
        return values

    def _effective(
        self,
        settings: dict[str, Any],
        rewards: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """合并插件配置页与运行数据，得出真正生效的设置与打卡规则。

        合并规则：

        1. 有同步记录（``snapshot``，即上一次同步时的配置值）时，**只有相对上次同步
           改动过**的配置键才覆盖运行数据。这样配置页既能把值改回默认值（"配置页
           优先"，不会因为值恰好等于默认值而被忽略），也不会在每次加载时无谓地
           覆盖插件页刚保存的值。
        2. 首次同步（新装，或从没有同步记录的旧版本升级）时：配置页里的非默认值
           直接生效；如果配置页还是默认值、而运行数据已被插件页改过，则保留运行
           数据的值并回写配置页（升级保护）。

        Args:
            settings: 运行数据里的设置。
            rewards: 运行数据里的打卡规则。
            snapshot: 上一次同步时的配置值。

        Returns:
            ``(设置, 规则, 需要回写配置页的键值对, 新的同步快照)``。
        """
        defaults = default_data()
        merged_settings = dict(settings)
        merged_rewards = dict(rewards)
        adopted: dict[str, Any] = {}
        current = self._config_values()
        for mapping, target, default_section in (
            (CONFIG_SETTINGS, merged_settings, defaults["settings"]),
            (CONFIG_REWARDS, merged_rewards, defaults["rewards"]),
        ):
            for key, name in mapping:
                value = current.get(key)
                if value is None:
                    continue
                if snapshot:
                    if snapshot.get(key) == value:
                        continue  # 配置页没动过，保留运行数据里的值
                elif value == default_section.get(name) and target.get(name) not in (None, value):
                    # 首次同步 + 配置页还是默认值 + 运行数据已被改过 -> 保留运行数据
                    adopted[key] = target[name]
                    continue
                target[name] = value
        fresh_snapshot = dict(current)
        fresh_snapshot.update(adopted)
        return merged_settings, merged_rewards, adopted, fresh_snapshot

    def _apply_config(
        self,
        data: dict[str, Any],
        adopt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把插件配置页的值写进运行数据快照。

        Args:
            data: 运行数据快照（就地修改）。
            adopt: 传入一个 dict 时，把"需要回写配置页"的键值对填进去。

        Returns:
            ``data`` 本身，方便直接接着做业务处理。
        """
        settings, rewards, adopted, snapshot = self._effective(
            data.get("settings") or {},
            data.get("rewards") or {},
            data.get("config_snapshot") or {},
        )
        data["settings"] = settings
        data["rewards"] = rewards
        data["config_snapshot"] = snapshot
        if adopt is not None:
            adopt.update(adopted)
        return data

    def write_config(self, values: dict[str, Any]) -> bool:
        """把插件页保存的值回写插件配置，让配置页显示同样的内容。

        AstrBot 的配置页保存后会热重载插件，所以这里只写文件、不触发重载；
        插件实例是普通 dict（单测）时不写。

        Args:
            values: ``{配置键: 值}``。

        Returns:
            是否真的写入了配置。
        """
        if not values or not hasattr(self.config, "save_config"):
            return False
        changed = {key: value for key, value in values.items() if self._cfg(key) != value}
        if not changed:
            return False
        try:
            for key, value in changed.items():
                self.config[key] = value
            self.config.save_config()
        except Exception:
            logger.warning("[打卡商店] 回写插件配置失败，配置页可能仍显示旧值", exc_info=True)
            return False
        return True

    def _seed(self) -> dict[str, Any]:
        """首次创建数据文件时的种子：默认数据 + 插件配置页的值。"""
        data = default_data()
        self._apply_config(data)
        return data

    def _display_prefix(self) -> str:
        """提示语/卡片里显示的指令前缀（只影响文案，不参与消息匹配）。

        优先用**运行数据**里的值：插件页保存时会同时写运行数据与插件配置页，
        而配置页只在热重载后才生效，读运行数据才能让插件页的改动立刻反映到文案上。
        运行数据还没建好时退回插件配置页的值（种子数据也用这个）。
        """
        data = self.store.snapshot()
        settings = data.get("settings") if isinstance(data, dict) else None
        if isinstance(settings, dict) and "display_prefix" in settings:
            return business.display_prefix(settings)
        return business.normalize_display_prefix(self._cfg("display_prefix"))

    def _palette(self) -> render.CardPalette:
        """构造卡片尺寸配置。"""
        return render.CardPalette(
            width=int(self._cfg("render_width", 0) or render.DEFAULT_WIDTH),
            font_size_base=int(self._cfg("render_font_size_base", 0) or 0),
            font_path=str(self._cfg("render_font_path", "") or ""),
            command_prefix=self._display_prefix(),
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

    async def _send(
        self,
        event: AstrMessageEvent,
        text: str,
        card: Any = None,
    ):
        """把文本和图片**合并成一条消息**发出。

        Args:
            event: 当前消息事件。
            text: 文本内容；为空时不发文本。
            card: 零参出图回调（同步函数，返回图片路径）；为 None 或渲染失败时只发文本。
        """
        path = ""
        if card is not None:
            try:
                path = str(card() or "")
            except Exception:
                logger.exception("[打卡商店] 图片渲染失败，已降级为文本")
        if path and not text:
            yield event.image_result(path)
            return
        if path:
            yield event.chain_result([Plain(text), Image.fromFileSystem(path)])
            return
        if text:
            yield event.plain_result(text)

    def mail_settings(self) -> emailer.MailSettings:
        """读取插件配置页里的 SMTP 设置。"""
        return emailer.MailSettings.from_config(self.config)

    def user_email(self, user_key: str, data: dict[str, Any] | None = None) -> str:
        """取用户绑定的邮箱（没有就返回空串）。"""
        users = (data if isinstance(data, dict) else self.store.snapshot()).get("users") or {}
        record = users.get(user_key)
        return str((record or {}).get("email") or "").strip() if isinstance(record, dict) else ""

    def email_recipient(self, user_key: str, data: dict[str, Any] | None = None) -> str:
        """算出某个用户的收件地址（可能为空）。"""
        settings = self.mail_settings()
        return emailer.resolve_recipient(
            user_email=self.user_email(user_key, data),
            user_key=user_key,
            suffix=settings.recipient_suffix,
            fallback=settings.fallback_to,
        )

    async def send_redemption_email(self, record: dict[str, Any]) -> str:
        """给兑换用户发"发放成功"邮件。

        Args:
            record: 兑换记录。

        Returns:
            给管理员看的结果说明；未启用邮件通知时返回空串。
        """
        if not record:
            return ""
        settings = self.mail_settings()
        if not settings.enabled:
            return ""
        try:
            data = await self.store.data(self._seed())
        except Exception:
            data = {}
        user_key = str(record.get("user_key") or "")
        recipient = self.email_recipient(user_key, data)
        if not recipient:
            return (
                "未发邮件：该用户没绑定邮箱"
                f"（可用 {self._display_prefix()}绑定邮箱 绑定），也没配置兜底收件人"
            )
        shop_settings = data.get("settings") or {}
        body = emailer.redemption_mail_body(
            nickname=str(record.get("nickname") or ""),
            product_name=str(record.get("product_name") or "商品"),
            cost=int(record.get("cost") or 0),
            record_id=str(record.get("id") or ""),
            created_at_text=_format_ts(record.get("created_at")),
            shop_title=str(shop_settings.get("shop_title") or "积分商店"),
        )
        ok, message = await emailer.send_mail(
            settings,
            to_address=recipient,
            subject=emailer.REDEMPTION_SUBJECT,
            body=body,
            command_prefix=business.display_prefix(shop_settings),
        )
        if not ok:
            logger.warning(
                "[打卡商店] 发放邮件未发出：%s（%s）", message, settings.sender_label()
            )
        return message

    async def send_test_email(self, to_address: str = "") -> tuple[bool, str]:
        """发一封测试邮件（管理员在插件页点「测试邮件」）。

        Args:
            to_address: 收件地址；留空则用配置里的兜底收件人 / SMTP 账号。

        Returns:
            ``(是否发出, 给管理员看的结果说明)``。
        """
        settings = self.mail_settings()
        if not settings.enabled:
            return False, "邮件通知未启用：请先在插件配置页打开「启用兑换发放邮件通知」"
        target = (to_address or "").strip() or settings.fallback_to or settings.username
        try:
            data = await self.store.data(self._seed())
        except Exception:
            data = {}
        shop_settings = data.get("settings") or {}
        ok, message = await emailer.send_mail(
            settings,
            to_address=target,
            subject=emailer.TEST_SUBJECT,
            body=emailer.test_mail_body(
                shop_title=str(shop_settings.get("shop_title") or "积分商店")
            ),
            command_prefix=business.display_prefix(shop_settings),
        )
        if not ok:
            logger.warning("[打卡商店] 测试邮件未发出：%s（%s）", message, settings.sender_label())
        return ok, message

    async def notify_redemption(self, record: dict[str, Any]) -> str:
        """把"发放成功"告诉用户。

        优先私聊用户（仅对能确定会话格式的平台），失败则退回用户发起兑换的那个
        会话（群里兑换的就会发在群里），因此用户总能看到提示。

        Args:
            record: 兑换记录。

        Returns:
            给管理员看的结果说明。
        """
        if not record:
            return ""
        context = getattr(self, "context", None)
        text = business.redemption_notice(record)
        if context is None or not hasattr(context, "send_message"):
            return "插件环境不支持主动发消息，未通知用户"

        user_key = str(record.get("user_key") or "")
        platform, _, user_id = user_key.partition(":")
        candidates: list[str] = []
        message_type = PRIVATE_SESSION_TYPES.get(platform, "")
        if message_type and user_id:
            candidates.append(f"{platform}:{message_type}:{user_id}")
        session = str(record.get("session") or "")
        if session and session not in candidates:
            candidates.append(session)
        if not candidates:
            return "该记录没有可用的会话信息，未通知用户"

        for target in candidates:
            try:
                if await context.send_message(target, MessageChain(chain=[Plain(text)])):
                    return "已通知用户" if target == candidates[0] else "已在该会话通知用户"
            except Exception:
                logger.warning("[打卡商店] 通知 %s 失败，尝试下一个会话", target, exc_info=True)
        return "通知用户失败，请检查平台是否支持主动发消息"

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
        """插件激活：加载数据、把插件配置页的值同步进运行数据、注册后台 API。

        AstrBot 在配置页保存后会热重载插件，因此这里是"配置页改动生效"的
        入口：每次激活都把配置页的打卡规则与展示设置写进运行数据。
        """
        try:
            adopted: dict[str, Any] = {}
            await self.store.mutate(
                lambda data: self._apply_config(data, adopted), self._seed()
            )
            if adopted:
                # 运行数据比配置页新（升级场景）：回写配置页，保持两边一致
                self.write_config(adopted)
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
        # 顺手把插件配置页的规则同步进快照，配置页改完即使没热重载也立刻生效
        try:
            outcome = await self.store.mutate(
                lambda data: business.perform_checkin(
                    self._apply_config(data), user_key, self._sender_name(event), now
                ),
                self._seed(),
            )
        except Exception as exc:
            # 打卡出错时必须说出来：静默失败（"发了指令没反应"）最难排查
            logger.exception(
                "[打卡商店] 打卡失败 user=%s session=%s",
                user_key,
                self._session_key(event),
            )
            yield event.plain_result(
                f"打卡失败：{exc}\n请把这条消息发给管理员，AstrBot 日志里有完整堆栈。"
            )
            return
        if not outcome.success:
            yield event.plain_result(f"{outcome.message}\n当前积分：{outcome.total}")
            return
        bonus_line = f"（含连签奖励 {outcome.bonus}）" if outcome.bonus else ""
        text = (
            f"打卡成功！本次获得 {outcome.points} 积分{bonus_line}\n"
            f"连签 {outcome.streak} 天 · 当前积分 {outcome.total}\n"
            f"发送 {self._display_prefix()}商店 看看能换点什么吧～"
        )
        # 有了这行，'指令没反应' 就能一眼分清是"插件没跑到"还是"发出去了但没到用户手上"
        logger.info(
            "[打卡商店] 打卡成功 user=%s session=%s points=%s streak=%s",
            user_key,
            self._session_key(event),
            outcome.points,
            outcome.streak,
        )
        card = await self._points_card(user_key, self._sender_name(event))
        async for result in self._send(event, text, card):
            yield result

    @filter.regex(BARE_COMMAND_RE)
    async def on_bare_command(self, event: AstrMessageEvent):
        """兜住"没带唤醒前缀的裸指令"，回一句用法说明而不是装死。

        AstrBot 的标准指令只在消息以 wake_prefix 开头（或 @ 了机器人 / 私聊）时才会
        触发，新用户很容易直接发「打卡」，此时命令过滤器根本不匹配、机器人毫无反应。
        正则过滤器不受 wake_prefix 约束，所以这里能给出提示。
        """
        if getattr(event, "is_at_or_wake_command", False):
            return  # 标准指令已经接管，别重复回复
        name = str(event.get_message_str() or "").strip().strip("!！。.、~～,，?？")
        yield event.plain_result(
            business.bare_command_hint(name, self._display_prefix())
        )

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

    @filter.command("商店帮助", alias={"打卡帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """商店帮助：渲染一张说明当前指令与积分规则的帮助图。"""
        data = await self.store.data(self._seed())
        view = business.build_help_view(data)
        prefix = business.display_prefix(data.get("settings"))
        text = (
            f"{view.title}\n"
            f"发送 {prefix}打卡 赚积分，发送 {prefix}商店 查看商品。"
        )

        def card() -> str:
            return render.render_help(
                view,
                self.store.render_dir,
                self._palette(),
            )

        async for result in self._send(event, text, card):
            yield result

    @filter.command("绑定邮箱", alias={"我的邮箱"})
    async def cmd_bind_email(self, event: AstrMessageEvent, 邮箱: GreedyStr = ""):
        """绑定邮箱：设置接收兑换发放邮件的收件地址。

        用法：``绑定邮箱 123@qq.com`` 绑定；``绑定邮箱`` 查看；``绑定邮箱 解绑`` 清除
        （文档里的指令前缀以插件配置页的「显示用的指令前缀」为准）。
        """
        user_key = self._user_key(event)
        value = str(邮箱 or "").strip()
        ok = False
        message = ""
        current = ""

        def handler(data: dict[str, Any]):
            return business.bind_user_email(data, user_key, value, time.time())

        try:
            ok, message, current = await self.store.mutate(handler, self._seed())
        except Exception:
            logger.exception("[打卡商店] 绑定邮箱失败")
            yield event.plain_result("绑定邮箱失败，请稍后再试。")
            return
        if not ok:
            yield event.plain_result(message)
            return
        if current:
            yield event.plain_result(message)
            return
        yield event.plain_result(
            f"{message}\n绑定后兑换奖励发放完成时会给你发邮件（邮件通知由管理员在插件配置页里开启）。"
        )

    @filter.command("我的积分", alias={"积分"})
    async def cmd_points(self, event: AstrMessageEvent):
        """我的积分：个人积分卡片。"""
        user_key = self._user_key(event)
        name = self._sender_name(event)
        data = await self.store.data(self._seed())
        user = (data.get("users") or {}).get(user_key) or {}
        prefix = business.display_prefix(data.get("settings"))
        text = (
            f"当前积分：{int(user.get('points') or 0)}\n"
            f"连签 {int(user.get('streak') or 0)} 天 · "
            f"累计打卡 {int(user.get('checkin_count') or 0)} 次\n"
            f"发送 {prefix}打卡 赚积分，{prefix}商店 查看商品"
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
            yield event.plain_result(
                f"还没有人打卡，发送 {business.display_prefix(settings)}打卡 抢占第一名！"
            )
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
                f"{scope}",
                self._palette(),
            )

        async for result in self._send(event, "\n".join(lines), card):
            yield result

    @filter.command("兑换商品", alias={"兑换"})
    async def cmd_redeem(self, event: AstrMessageEvent, 商品名: GreedyStr = ""):
        """兑换商品：不带参数列编号菜单，带编号（或商品名）给出确认提示。

        用法：``兑换商品``（列出编号）、``兑换 2``（按编号兑换）、
        ``兑换商品 冰镇 可乐``（按名称，仍支持）。
        （文档里的指令前缀以插件配置页的「显示用的指令前缀」为准）。
        """
        keyword = str(商品名 or "").strip()
        user_key = self._user_key(event)
        session = self._session_key(event)
        data = await self.store.data(self._seed())
        if not keyword:
            # 不带编号就是"给我看菜单"：发编号即可兑换，比打字输入商品名省事
            yield event.plain_result(business.catalog_text(data, user_key, self._display_prefix()))
            return
        candidates, error = business.find_products(data, keyword)
        if error:
            yield event.plain_result(error)
            return
        if len(candidates) > 1:
            prefix = business.display_prefix(data.get("settings"))
            numbered = {product.id: number for number, product in business.numbered_products(data)}
            names = "、".join(
                f"{numbered.get(item.id, '')} {item.name}".strip() for item in candidates[:8]
            )
            yield event.plain_result(
                f"匹配到多个商品：{names}\n"
                f"发编号更省事，例如 {prefix}兑换 {numbered.get(candidates[0].id, '1')}"
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
            # 这个用户根本没有待确认的兑换请求：这条"好/不"是普通聊天（或别人的事），
            # 绝不能插一句"当前没有等待确认的兑换请求"——静默放行。
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
            self._display_prefix(),
        )
        if not decision.handled:
            return
        if pending.token in self._handled_tokens:
            return
        if not decision.ok and not decision.cancelled:
            # 超时或引用没对上：只提示、不动数据；过期请求顺手清掉，有效请求留着等正确引用
            if timed_out:
                self._pendings.pop(pending.token, None)
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
