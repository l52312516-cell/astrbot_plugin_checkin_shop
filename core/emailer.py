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
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

DEFAULT_SMTP_PORT = 465
DELIVERY_TIMEOUT = 15.0
ENCRYPTIONS = ("ssl", "starttls", "none")
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
        if encryption not in ENCRYPTIONS:
            encryption = "ssl"
        suffix = str(raw("email_recipient_suffix", "@qq.com"))
        return cls(
            enabled=as_bool(raw("email_enabled", False)),
            host=str(raw("email_smtp_host", "") or "").strip(),
            port=as_int(raw("email_smtp_port", DEFAULT_SMTP_PORT), DEFAULT_SMTP_PORT),
            encryption=encryption,
            username=str(raw("email_smtp_user", "") or "").strip(),
            password=str(raw("email_smtp_password", "")),
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
    """阻塞发送（只在本函数内使用 smtplib，调用方用线程包一层）。"""
    context = ssl.create_default_context()
    if settings.encryption == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.host, settings.port, timeout=DELIVERY_TIMEOUT, context=context
        )
    else:
        server = smtplib.SMTP(settings.host, settings.port, timeout=DELIVERY_TIMEOUT)
    try:
        server.ehlo()
        if settings.encryption == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if settings.username:
            server.login(settings.username, settings.password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - 关闭失败不影响发送结果
            pass


async def send_mail(
    settings: MailSettings,
    *,
    to_address: str,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    """发送一封邮件。

    Returns:
        ``(是否发送成功, 给管理员看的结果说明)``。
    """
    if not settings.enabled:
        return False, ""
    missing = settings.missing_fields()
    if missing:
        return False, f"邮件配置不完整（缺少 {'、'.join(missing)}）"
    if not is_email(to_address):
        return False, "没有可用的收件地址（配置里填「兜底收件人」，或让用户用 /绑定邮箱 绑定）"
    message = build_message(
        settings=settings, to_address=to_address, subject=subject, body=body
    )
    try:
        await asyncio.to_thread(_deliver, settings, message)
    except Exception as exc:  # noqa: BLE001 - 网络/认证失败都只提示，不影响发放
        return False, f"邮件发送失败：{type(exc).__name__}: {exc}"
    return True, f"已发送邮件到 {to_address}"
