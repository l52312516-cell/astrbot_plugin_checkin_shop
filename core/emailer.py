"""兑换发放邮件通知（标准库 SMTP 实现）。

设计要点：

- 只在管理员把兑换记录标记为「已发放」时发送（调用方见 ``main.CheckinShopPlugin.send_redemption_email``）。
- ``smtplib`` 是阻塞库，发送放在线程里执行（``asyncio.to_thread``），不阻塞 AstrBot 事件循环。
- 收件地址解析顺序：用户绑定邮箱 -> 「用户号 + 配置后缀」（QQ 号 + ``@qq.com``）-> 兜底收件人。
- 配置缺失或发送失败都返回 ``(False, 说明)``，不抛异常，由调用方转成给管理员看的提示。

SMTP 配置项全部只存在于插件配置页（``_conf_schema.json``），不写进 ``shop.json``，
避免把授权码复制到运行数据里。
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

# 上架规范：日志记录器必须且只能从 astrbot.api 导入，严禁使用内置 logging 模块。
# 单元测试环境没有 AstrBot，由 tests/conftest.py 提供 astrbot.api 的最小替身。
from astrbot.api import logger

DEFAULT_SMTP_PORT = 465
# 单次 SMTP 会话里每个阻塞操作都可能各等一次 timeout，所以整封信的耗时上限
# 是 socket 超时的数倍。重试会把这个上限再乘上尝试次数，而管理员点「已发放」时
# WebUI 是同步等结果的，因此这里给整次投递（含重试与退避）定一个总预算。
TOTAL_DELIVERY_BUDGET = 20.0
MIN_ATTEMPT_TIMEOUT = 4.0
DELIVERY_TIMEOUT = 15.0
RETRY_ATTEMPTS = 3
RETRY_DELAYS = (0.0, 1.0, 3.0)
ENCRYPTIONS = ("ssl", "starttls", "none")
# 管理员会把加密方式填成各种写法，这里统一成上面三种之一；
# 尤其 "tls" 常被当成"SSL"，但它其实是 STARTTLS（465 端口上跑 SSL 会握手卡死）。
ENCRYPTION_ALIASES = {
    "tls": "ssl",
    "ssl/tls": "ssl",
    "ssl_tls": "ssl",
    "ssltls": "ssl",
    "start_tls": "starttls",
    "start-tls": "starttls",
    "明文": "none",
    "无": "none",
    "no": "none",
    "off": "none",
}
REDEMPTION_SUBJECT = "【积分商店】兑换发放通知"
TEST_SUBJECT = "【积分商店】邮件通知测试"


@dataclass
class MailSettings:
    """插件配置页里的邮件设置。"""

    enabled: bool = False
    host: str = ""
    port: int = DEFAULT_SMTP_PORT
    encryption: str = "ssl"
    username: str = ""
    password: str = ""
    sender: str = ""
    recipient_suffix: str = "@qq.com"
    fallback_to: str = ""

    @classmethod
    def from_config(cls, config: Any) -> "MailSettings":
        """从 ``AstrBotConfig`` / 普通 dict 里读设置，类型不对就退回默认值。"""

        def raw(key: str, default: Any = "") -> Any:
            getter = getattr(config, "get", None)
            if not callable(getter):
                return default
            try:
                value = getter(key)
            except Exception:
                return default
            return default if value is None else value

        def as_bool(value: Any) -> bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
            return bool(value)

        def as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        encryption = str(raw("email_smtp_encryption", "ssl") or "ssl").strip().lower()
        encryption = ENCRYPTION_ALIASES.get(encryption, encryption)
        if encryption not in ENCRYPTIONS:
            encryption = "ssl"
        suffix = str(raw("email_recipient_suffix", "@qq.com"))
        return cls(
            enabled=as_bool(raw("email_enabled", False)),
            host=str(raw("email_smtp_host", "") or "").strip(),
            port=as_int(raw("email_smtp_port", DEFAULT_SMTP_PORT), DEFAULT_SMTP_PORT),
            encryption=encryption,
            username=str(raw("email_smtp_user", "") or "").strip(),
            # 授权码是从邮箱后台复制粘贴来的，常常带首尾空白（甚至 NBSP）。
            # 用户名已经 strip 了，密码也必须 strip，否则 535 认证失败。
            password=str(raw("email_smtp_password", "")).replace("\u00a0", " ").strip(),
            sender=str(raw("email_sender", "") or "").strip(),
            recipient_suffix=suffix.strip(),
            fallback_to=str(raw("email_fallback_to", "") or "").strip(),
        )

    @property
    def from_address(self) -> str:
        """发件人地址：没单独配就用 SMTP 账号。"""
        return self.sender or self.username

    def missing_fields(self) -> list[str]:
        """返回缺失的必填项名称，用于给出可操作的报错。"""
        missing: list[str] = []
        if not self.host:
            missing.append("SMTP 服务器")
        if not self.from_address:
            missing.append("SMTP 账号/发件人")
        return missing

    def sender_label(self) -> str:
        """日志用：只暴露服务器和账号，绝不记录密码。"""
        return f"{self.host}:{self.port} ({self.encryption}) as {self.from_address or '-'}"


def is_email(value: str) -> bool:
    """极简邮箱校验：``本地部分@域名``，域名里至少要有一个点。"""
    text = (value or "").strip()
    if not text or len(text) > 254 or text.count("@") != 1 or " " in text:
        return False
    local, _, domain = text.partition("@")
    if not local or not domain:
        return False
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return False
    return "." in domain


def normalize_suffix(suffix: str) -> str:
    """把配置里的后缀规范成 ``@xxx`` 形式（填 ``qq.com`` 也认）。"""
    text = (suffix or "").strip()
    if not text:
        return ""
    return text if text.startswith("@") else f"@{text}"


def resolve_recipient(
    *,
    user_email: str = "",
    user_key: str = "",
    suffix: str = "@qq.com",
    fallback: str = "",
) -> str:
    """按 用户绑定邮箱 -> 用户号+后缀 -> 兜底收件人 的顺序决定收件地址。

    Args:
        user_email: 用户在 ``shop.json`` 里绑定的邮箱。
        user_key: ``平台:用户ID`` 形式的用户主键；QQ 用户这里就是 QQ 号。
        suffix: 推不出绑定邮箱时给用户号拼的后缀（如 ``@qq.com``）。
        fallback: 兜底收件人（如管理员邮箱）。

    Returns:
        收件地址；都推不出来时返回空串。
    """
    bound = (user_email or "").strip()
    if is_email(bound):
        return bound
    user_id = (user_key or "").strip().partition(":")[2] or (user_key or "").strip()
    suffix = normalize_suffix(suffix)
    if user_id.isdigit() and suffix:
        candidate = f"{user_id}{suffix}"
        if is_email(candidate):
            return candidate
    return (fallback or "").strip() if is_email(fallback or "") else ""


def build_message(
    *,
    settings: MailSettings,
    to_address: str,
    subject: str,
    body: str,
) -> EmailMessage:
    """组装一封纯文本邮件。"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("积分商店", settings.from_address))
    message["To"] = to_address
    message["Date"] = formatdate(localtime=True)
    message.set_content(body, charset="utf-8")
    return message


def redemption_mail_body(
    *,
    nickname: str,
    product_name: str,
    cost: int,
    record_id: str,
    created_at_text: str = "",
    shop_title: str = "积分商店",
) -> str:
    """兑换发放通知的正文。"""
    lines = [
        f"{nickname or '你好'}，你的兑换已经发放完成。",
        "",
        f"商品：{product_name}",
        f"消耗积分：{cost}",
    ]
    if created_at_text:
        lines.append(f"兑换时间：{created_at_text}")
    lines += [
        f"兑换单号：{record_id}",
        "",
        "如果长时间没有收到实物或权益，请直接回复机器人联系管理员。",
        f"—— {shop_title}",
    ]
    return "\n".join(lines)


def test_mail_body(*, shop_title: str = "积分商店") -> str:
    """测试邮件正文。"""
    return "\n".join(
        [
            "这是一封来自打卡积分商店的测试邮件。",
            "",
            "能收到它说明 SMTP 配置可用：把兑换记录标记为「已发放」时，",
            "插件会用同样的方式给用户发送发放通知。",
            "",
            f"—— {shop_title}",
        ]
    )


def _deliver(settings: MailSettings, message: EmailMessage) -> None:
    """阻塞发送（只在本函数内使用 smtplib，调用方用线程包一层）。

    ``server`` 在 try **之外**先置 None、在 try **之内**才真正建连：
    ``smtplib.SMTP.__init__`` 里的 ``connect()`` 会先建好 socket 再读欢迎语，
    如果欢迎语或 STARTTLS 握手阶段抛错，构造函数就带着一个**还活着的 socket**
    抛出来，那个对象立刻变成不可达，只能等 GC —— 反复失败会堆着占用文件描述符
    和 TCP 连接。所以连接本身也要纳入 try/finally。

    Args:
        settings: SMTP 设置。
        message: 已构造好的邮件。
    """
    context = ssl.create_default_context()
    server: smtplib.SMTP | None = None
    try:
        if settings.encryption == "ssl":
            server = smtplib.SMTP_SSL(
                settings.host,
                settings.port,
                timeout=_attempt_timeout(),
                context=context,
            )
        else:
            server = smtplib.SMTP(
                settings.host, settings.port, timeout=_attempt_timeout()
            )
        server.ehlo()
        if settings.encryption == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if settings.username:
            server.login(settings.username, settings.password)
        server.send_message(message)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - 关闭失败不影响发送结果
                pass


def _is_retryable(exc: BaseException) -> bool:
    """判断这个 SMTP 失败是否值得重试。

    重试**只**针对"临时性"故障，认证/收件人被拒这类确定性错误重试没有意义，
    还会白白拖延给管理员的反馈：

    * 网络层：``OSError`` 覆盖 DNS(gaierror)、拒绝连接、TCP 超时、连接被重置；
      ``TimeoutError``/``ssl.SSLError``（TLS 握手被中断）同理；
    * SMTP 层：4xx 是"稍后再试"（421 服务不可用、450/451/452 等），
      5xx 才是永久失败。

    刻意排除 ``SMTPAuthenticationError``（535 授权码错）和
    ``SMTPRecipientsRefused``（收件人不存在）—— 这两类重试只会让管理员多等几秒。
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        # 4xx 临时失败可重试，5xx 不行
        return 400 <= int(getattr(exc, "smtp_code", 0) or 0) < 500
    return isinstance(exc, (OSError, TimeoutError, ssl.SSLError))


def _attempt_timeout() -> float:
    """本次尝试允许的 socket **单次**超时。

    重试会让总耗时变成"单次超时 × 尝试次数"，而管理员点「已发放」时 WebUI 是
    同步等结果的（``dashboard_api`` 直接 await 发信）。所以重试时必须**缩短单次
    超时**，把 ``单次超时 × RETRY_ATTEMPTS`` 压在 ``TOTAL_DELIVERY_BUDGET`` 以内。

    注意这里约束的是**每次网络读写的 socket 超时**，不是整个投递的墙钟上限：
    一次投递要按顺序做 connect/ehlo/login/send_message/quit 等多个阻塞操作，
    每个都可能各耗掉一次超时，再加上 ``RETRY_DELAYS`` 的退避。所以实际墙钟上界
    明显大于 ``TOTAL_DELIVERY_BUDGET``，不能把它当成"20 秒内必定返回"的承诺。

    Returns:
        本次尝试的 socket 超时（秒），至少 ``MIN_ATTEMPT_TIMEOUT``。
    """
    per_attempt = TOTAL_DELIVERY_BUDGET / max(1, RETRY_ATTEMPTS)
    return max(MIN_ATTEMPT_TIMEOUT, min(DELIVERY_TIMEOUT, per_attempt))


async def _deliver_with_retry(settings: MailSettings, message: EmailMessage) -> None:
    """带退避重试的投递。

    单次网络抖动、DNS 解析暂时失败、SMTP 回 4xx 都会让"偶发发不出去"出现；
    重试 3 次（退避 0/1/3 秒）能挡掉绝大多数瞬时故障。

    墙钟时间只做**尽力而为**的约束：``deadline`` 只在"要不要再起一次尝试"时检查，
    挡不住已经在跑的那一次（``smtplib`` 是阻塞的、不可取消）。所以最坏情况是
    "退避 + 每次尝试的多步 socket 超时之和"，远大于 ``TOTAL_DELIVERY_BUDGET``。

    Args:
        settings: SMTP 设置。
        message: 已构造好的邮件。

    Raises:
        Exception: 所有尝试都失败时抛出最后一次的异常（瞬时错误重试后仍失败，
            或遇到认证失败这类不该重试的错误）。
    """
    deadline = time.monotonic() + TOTAL_DELIVERY_BUDGET
    last_error: BaseException | None = None
    for attempt in range(RETRY_ATTEMPTS):
        if attempt:
            if time.monotonic() >= deadline:
                break  # 退避已经越过预算，不再多起一次尝试
            await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
        try:
            await asyncio.to_thread(_deliver, settings, message)
            return
        except Exception as exc:  # noqa: BLE001 - 统一在上层转成提示
            last_error = exc
            if not _is_retryable(exc) or attempt == RETRY_ATTEMPTS - 1:
                raise
            logger.warning(
                "[打卡商店] 邮件发送第 %s 次失败，正在重试：%s: %s",
                attempt + 1,
                type(exc).__name__,
                exc,
            )
    if last_error is not None:
        raise last_error


async def send_mail(
    settings: MailSettings,
    *,
    to_address: str,
    subject: str,
    body: str,
    command_prefix: str = "/",
) -> tuple[bool, str]:
    """发送一封邮件。

    Args:
        settings: SMTP 配置。
        to_address: 收件地址。
        subject: 主题。
        body: 正文。
        command_prefix: 提示语里显示的指令前缀（默认 ``/``）。

    Returns:
        ``(是否发送成功, 给管理员看的结果说明)``。
    """
    if not settings.enabled:
        return False, ""
    missing = settings.missing_fields()
    if missing:
        return False, f"邮件配置不完整（缺少 {'、'.join(missing)}）"
    if not is_email(to_address):
        prefix = str(command_prefix or "").strip() or "/"
        return False, (
            "没有可用的收件地址（配置里填「兜底收件人」，"
            f"或让用户用 {prefix}绑定邮箱 绑定）"
        )
    message = build_message(
        settings=settings, to_address=to_address, subject=subject, body=body
    )
    try:
        await _deliver_with_retry(settings, message)
    except Exception as exc:  # noqa: BLE001 - 网络/认证失败都只提示，不影响发放
        return False, f"邮件发送失败：{type(exc).__name__}: {exc}"
    return True, f"已发送邮件到 {to_address}"
