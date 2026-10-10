"""使用 Pillow 在本地渲染商店 / 积分 / 榜单卡片。

选择本地渲染的原因：AstrBot 的网络 t2i 服务需要 Playwright/联网，失败时老插件只能
降级成纯文本；这里自带 Noto Sans CJK 字体，离线也能出图，且渲染失败时上层可以
整体降级为纯文本。
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from typing import Any, Callable, Iterable

try:  # 作为包被导入（测试）
    from .business import ProductState, ShopView
except ImportError:  # pragma: no cover - AstrBot 以顶层模块方式加载 core/
    from business import ProductState, ShopView  # type: ignore[no-redef]

logger = logging.getLogger("astrbot")

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - requirements.txt 里已声明
    PIL_AVAILABLE = False
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

RESOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "resources")
FONT_FILENAME = "NotoSansCJKsc-Regular.otf"

COLOR_BG = "#f4f6fb"
COLOR_CARD = "#ffffff"
COLOR_TEXT = "#1f2937"
COLOR_MUTED = "#6b7280"
COLOR_ACCENT = "#2563eb"
COLOR_OK = "#059669"
COLOR_LOCK = "#b45309"
COLOR_GRID = "#e5e7eb"
CATEGORY_COLORS = ("#2563eb", "#7c3aed", "#059669", "#d97706", "#db2777", "#0891b2")

_font_cache: dict[tuple[str, int], Any] = {}

# 默认宽度：QQ 群里 1080~1120 像素宽的卡片显示效果最好
DEFAULT_WIDTH = 1080


def available() -> bool:
    """返回当前环境是否具备出图能力。"""
    if not PIL_AVAILABLE:
        return False
    try:
        font_for(20)
    except Exception:
        return False
    return True


def font_for(size: int, font_path: str = "") -> Any:
    """按尺寸取字体（带缓存）。

    Args:
        size: 字号。
        font_path: 自定义字体文件路径，为空时使用内置 Noto Sans CJK SC。

    Returns:
        ``PIL.ImageFont`` 实例。

    Raises:
        RuntimeError: Pillow 不可用或字体文件缺失。
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("未安装 Pillow，无法渲染图片（请安装插件 requirements.txt）")
    path = font_path or os.path.join(RESOURCE_DIR, FONT_FILENAME)
    key = (os.path.abspath(path), int(size))
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    if not os.path.exists(path):
        raise RuntimeError(f"字体文件不存在：{path}")
    face = ImageFont.truetype(path, int(size))
    _font_cache[key] = face
    return face


def text_width(face: Any, text: str) -> int:
    """测量文本宽度（像素）。"""
    return int(face.getlength(text))


def wrap_text(face: Any, text: str, max_width: int, max_lines: int = 3) -> list[str]:
    """按像素宽度逐字换行，超出 ``max_lines`` 时打省略号。

    Args:
        face: 字体。
        text: 原始文本（可含换行）。
        max_width: 每行最大像素宽度。
        max_lines: 最多返回多少行。

    Returns:
        行列表；空文本返回空列表。
    """
    text = str(text or "").replace("\r", "")
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
        elif text_width(face, current + char) > max_width and current:
            lines.append(current)
            current = char
        else:
            current += char
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) >= max_lines and len("".join(lines)) < len(text.replace("\n", "")):
        last = lines[-1]
        suffix = "…"
        while last and text_width(face, last + suffix) > max_width:
            last = last[:-1]
        lines[-1] = last + suffix
    return lines or [""]


def ellipsize(face: Any, text: str, max_width: int) -> str:
    """单行截断。"""
    text = str(text or "")
    if text_width(face, text) <= max_width:
        return text
    while text and text_width(face, text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def output_path(kind: str, payload: str, render_dir: str) -> str:
    """生成渲染输出路径（按小时分桶，便于缓存复用）。

    Args:
        kind: 卡片类型，例如 ``shop``。
        payload: 参与哈希的业务内容。
        render_dir: 渲染输出目录。

    Returns:
        可直接交给 ``event.image_result`` 的文件路径。
    """
    os.makedirs(render_dir, exist_ok=True)
    bucket = int(time.time() // 1800)
    digest = hashlib.md5(f"{kind}|{payload}|{bucket}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(render_dir, f"{kind}_{digest}.png")


def save_card(image: Any, path: str) -> str:
    """把图片写入磁盘并返回路径。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    image.save(path, format="PNG")
    return path


def _new_canvas(width: int, height: int) -> tuple[Any, Any]:
    image = Image.new("RGB", (width, height), COLOR_BG)
    return image, ImageDraw.Draw(image)


def _rounded(draw: Any, box: tuple[int, int, int, int], fill: str, radius: int = 18) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _copy_to_canvas(
    canvas: Any,
    source: Any,
    position: tuple[int, int],
    box: tuple[int, int],
) -> None:
    """把商品图片裁剪缩放后贴到卡片的方框里。"""
    x, y = position
    width, height = box
    try:
        thumb = source.convert("RGB")
        ratio = max(width / thumb.width, height / thumb.height)
        resized = thumb.resize(
            (max(1, int(thumb.width * ratio)), max(1, int(thumb.height * ratio)))
        )
        left = (resized.width - width) // 2
        top = (resized.height - height) // 2
        cropped = resized.crop((left, top, left + width, top + height))
        canvas.paste(cropped, (x, y))
    except Exception:  # 单张商品图坏了不应该影响整张卡片
        logger.debug("[打卡商店] 商品图片无法读取，使用占位色块", exc_info=True)


def _placeholder(canvas: Any, draw: Any, box: tuple[int, int, int, int], name: str, face: Any) -> None:
    x1, y1, x2, y2 = box
    color = CATEGORY_COLORS[sum(ord(ch) for ch in (name or "?")) % len(CATEGORY_COLORS)]
    _rounded(draw, box, color, radius=14)
    initial = (name or "?").strip()[:1] or "?"
    width = text_width(face, initial)
    draw.text(
        ((x1 + x2 - width) // 2, (y1 + y2 - int(face.size * 1.2)) // 2),
        initial,
        font=face,
        fill="#ffffff",
    )


class CardPalette:
    """卡片尺寸/字号集中定义，方便调整观感。"""

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        font_size_base: int = 0,
        font_path: str = "",
    ) -> None:
        self.width = max(640, int(width))
        self.pad = 36
        self.base = int(font_size_base) if font_size_base else max(20, self.width // 40)
        self.title = int(self.base * 1.45)
        self.section = int(self.base * 1.15)
        self.body = self.base
        self.small = int(self.base * 0.82)
        self.font_path = font_path

    @property
    def content_width(self) -> int:
        return self.width - self.pad * 2


def _draw_header(
    palette: CardPalette,
    canvas: Any,
    draw: Any,
    title: str,
    badge: str,
    subtitle: str = "",
) -> int:
    """绘制卡片头部，返回下一块的起始 y 坐标。"""
    y = palette.pad
    title_face = font_for(palette.title, palette.font_path)
    badge_face = font_for(palette.small, palette.font_path)
    draw.text((palette.pad, y), title, font=title_face, fill=COLOR_TEXT)

    badge_width = text_width(badge_face, badge) + 34
    badge_box = (
        palette.width - palette.pad - badge_width,
        y + 4,
        palette.width - palette.pad,
        y + 4 + int(palette.small * 2.0),
    )
    _rounded(draw, badge_box, COLOR_ACCENT, radius=14)
    draw.text(
        (badge_box[0] + 17, badge_box[1] + int(palette.small * 0.42)),
        badge,
        font=badge_face,
        fill="#ffffff",
    )
    y += int(palette.title * 1.35)
    if subtitle:
        sub_face = font_for(palette.small, palette.font_path)
        for line in wrap_text(sub_face, subtitle, palette.content_width, max_lines=2):
            draw.text((palette.pad, y), line, font=sub_face, fill=COLOR_MUTED)
            y += int(palette.small * 1.5)
    return y + 6


def _draw_section(palette: CardPalette, draw: Any, y: int, text: str, color: str) -> int:
    face = font_for(palette.section, palette.font_path)
    draw.text((palette.pad, y), text, font=face, fill=color)
    y += int(palette.section * 1.5)
    draw.line(
        (palette.pad, y - 10, palette.width - palette.pad, y - 10),
        fill=COLOR_GRID,
        width=2,
    )
    return y


def render_shop(
    view: ShopView,
    render_dir: str,
    image_resolver: Callable[[str], Any] | None = None,
    palette: CardPalette | None = None,
    show_images: bool = True,
    font_path: str = "",
) -> str:
    """渲染商店卡片。

    Args:
        view: 商店视图（含用户积分与商品分组）。
        render_dir: 输出目录。
        image_resolver: 把 ``product.image_path`` 解析成 PIL Image 的回调。
        palette: 尺寸/字号配置。
        show_images: 是否绘制商品缩略图。
        font_path: 自定义字体路径。

    Returns:
        生成的 PNG 路径。
    """
    palette = palette or CardPalette()
    if font_path:
        palette.font_path = font_path

    body_face = font_for(palette.body, palette.font_path)
    small_face = font_for(palette.small, palette.font_path)
    name_face = font_for(int(palette.body * 1.05), palette.font_path)
    pad = palette.pad
    row_gap = 14

    def row_height(item: ProductState) -> int:
        height = 104
        if item.product.description:
            height += 8
        return height

    # 注意：内置的 Noto Sans CJK 不含彩色 emoji，标题里用几何字符 + 颜色区分，
    # 否则会渲染成"豆腐块"。
    sections: list[tuple[str, str, list[ProductState]]] = []
    sections.append((f"● 可兑换（{len(view.redeemable)}）", COLOR_OK, view.redeemable))
    sections.append((f"○ 暂不可兑换（{len(view.locked)}）", COLOR_LOCK, view.locked))

    total_height = palette.pad * 2 + int(palette.title * 1.35) + int(palette.small * 1.5) + 20
    for _, _, items in sections:
        total_height += int(palette.section * 1.5) + 10
        total_height += sum(row_height(item) + row_gap for item in items) or 60
    total_height += palette.pad

    canvas, draw = _new_canvas(palette.width, max(320, total_height))
    y = _draw_header(
        palette,
        canvas,
        draw,
        view.title or "积分商店",
        f"我的积分 {view.points}",
        view.subtitle or f"{view.user_name} · 回复 /兑换商品 <名称> 进行兑换",
    )

    for title, color, items in sections:
        y = _draw_section(palette, draw, y, title, color)
        if not items:
            draw.text((pad, y + 10), "暂无", font=small_face, fill=COLOR_MUTED)
            y += 60
            continue
        for index, item in enumerate(items, 1):
            product = item.product
            height = row_height(item)
            box = (pad, y, palette.width - pad, y + height)
            _rounded(draw, box, COLOR_CARD, radius=16)
            inner_x = pad + 20
            if show_images:
                thumb_box = (inner_x, y + 12, inner_x + 80, y + 92)
                image = None
                if image_resolver is not None and product.image_path:
                    try:
                        image = image_resolver(product.image_path)
                    except Exception:
                        image = None
                if image is not None:
                    _copy_to_canvas(canvas, image, (inner_x, y + 12), (80, 80))
                else:
                    _placeholder(canvas, draw, thumb_box, product.name, name_face)
                inner_x += 96

            index_face = font_for(palette.small, palette.font_path)
            draw.text(
                (inner_x, y + 16),
                f"{index:02d}",
                font=index_face,
                fill=COLOR_MUTED,
            )
            text_x = inner_x + 44
            price_text = f"{product.cost} 积分"
            price_width = text_width(name_face, price_text) + 30
            name = ellipsize(name_face, product.name, palette.width - pad - 40 - text_x - price_width)
            draw.text((text_x, y + 14), name, font=name_face, fill=COLOR_TEXT)

            meta_bits = [f"分类 {product.category}"] if product.category else []
            if product.stock is None:
                meta_bits.append("不限量")
            else:
                meta_bits.append(f"剩余 {product.stock} 件")
            if product.per_user_limit is not None:
                meta_bits.append(f"限购 {product.per_user_limit}（已兑 {item.user_redeemed}）")
            meta = " · ".join(meta_bits)
            draw.text((text_x, y + 48), ellipsize(small_face, meta, palette.width - pad - 40 - text_x), font=small_face, fill=COLOR_MUTED)

            if product.description:
                draw.text(
                    (text_x, y + 76),
                    ellipsize(small_face, product.description, palette.width - pad - 40 - text_x),
                    font=small_face,
                    fill=COLOR_MUTED,
                )

            price_color = COLOR_OK if item.redeemable else COLOR_LOCK
            draw.text(
                (palette.width - pad - 20 - text_width(name_face, price_text), y + 14),
                price_text,
                font=name_face,
                fill=price_color,
            )
            if item.redeemable:
                hint = "可兑换"
            elif item.reason == "积分不足":
                hint = f"还差 {item.deficit} 分"
            else:
                hint = item.reason
            draw.text(
                (
                    palette.width - pad - 20 - text_width(small_face, hint),
                    y + 48,
                ),
                hint,
                font=small_face,
                fill=price_color,
            )
            y += height + row_gap
        y += 8

    payload = f"{view.user_key}|{view.points}|" + "|".join(
        f"{item.product.id}:{item.product.cost}:{item.product.stock}:{item.redeemable}:{item.reason}"
        for item in view.items
    )
    path = output_path("shop", payload, render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(canvas, path)


def render_points(
    user: dict[str, Any],
    display_name: str,
    history: Iterable[dict[str, Any]],
    render_dir: str,
    palette: CardPalette | None = None,
    font_path: str = "",
) -> str:
    """渲染"我的积分"卡片。"""
    palette = palette or CardPalette()
    if font_path:
        palette.font_path = font_path
    history = list(history)
    label_face = font_for(palette.small, palette.font_path)
    value_face = font_for(int(palette.body * 1.1), palette.font_path)
    big_face = font_for(int(palette.title * 1.6), palette.font_path)

    panel_height = 150
    total_height = (
        palette.pad * 2
        + int(palette.title * 1.35)
        + int(palette.small * 1.5)
        + 20
        + panel_height
        + int(palette.section * 1.5)
        + 10
        + max(60, len(history) * 44 + 20)
        + palette.pad
    )
    canvas, draw = _new_canvas(palette.width, total_height)
    y = _draw_header(
        palette,
        canvas,
        draw,
        "我的积分",
        display_name,
        "打卡可以累积积分，积分可以在商店兑换商品",
    )

    box = (palette.pad, y, palette.width - palette.pad, y + panel_height)
    _rounded(draw, box, COLOR_CARD, radius=18)
    draw.text((palette.pad + 26, y + 26), str(int(user.get("points") or 0)), font=big_face, fill=COLOR_ACCENT)
    draw.text((palette.pad + 26, y + 104), "当前积分", font=label_face, fill=COLOR_MUTED)

    stats = (
        ("累计获得", int(user.get("total_earned") or 0)),
        ("累计消耗", int(user.get("total_spent") or 0)),
        ("打卡次数", int(user.get("checkin_count") or 0)),
        ("连签天数", int(user.get("streak") or 0)),
    )
    column = (palette.width - palette.pad * 2 - 300) // len(stats)
    for index, (label, value) in enumerate(stats):
        x = palette.pad + 300 + index * column
        draw.text((x, y + 34), label, font=label_face, fill=COLOR_MUTED)
        draw.text((x, y + 68), str(value), font=value_face, fill=COLOR_TEXT)
    y += panel_height + 18

    y = _draw_section(palette, draw, y, "最近打卡", COLOR_ACCENT)
    if not history:
        draw.text((palette.pad, y + 10), "还没有打卡记录，发送 /打卡 试试～", font=label_face, fill=COLOR_MUTED)
    for item in history[:10]:
        date = str(item.get("date") or "")
        points = int(item.get("points") or 0)
        note = str(item.get("note") or "")
        draw.text((palette.pad + 6, y), date, font=label_face, fill=COLOR_TEXT)
        gained = f"+{points}" if points >= 0 else str(points)
        draw.text((palette.pad + 220, y), gained, font=label_face, fill=COLOR_OK if points >= 0 else COLOR_LOCK)
        detail = note or f"连签 {int(item.get('streak') or 0)} 天"
        draw.text((palette.pad + 380, y), detail, font=label_face, fill=COLOR_MUTED)
        y += 44

    payload = f"{display_name}|{user.get('points')}|{user.get('streak')}|{len(history)}"
    path = output_path("points", payload, render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(canvas, path)


def render_board(
    rows: Iterable[dict[str, Any]],
    render_dir: str,
    title: str = "积分榜",
    scope_hint: str = "",
    palette: CardPalette | None = None,
    font_path: str = "",
) -> str:
    """渲染积分榜卡片。"""
    palette = palette or CardPalette()
    if font_path:
        palette.font_path = font_path
    rows = list(rows)
    name_face = font_for(int(palette.body * 1.02), palette.font_path)
    small_face = font_for(palette.small, palette.font_path)
    medal_colors = {1: "#d97706", 2: "#6b7280", 3: "#b45309"}

    total_height = (
        palette.pad * 2
        + int(palette.title * 1.35)
        + int(palette.small * 1.5)
        + 20
        + max(80, len(rows) * 72 + 20)
        + palette.pad
    )
    canvas, draw = _new_canvas(palette.width, total_height)
    y = _draw_header(palette, canvas, draw, title, f"{len(rows)} 人", scope_hint)
    y = _draw_section(palette, draw, y, "排名", COLOR_ACCENT)
    if not rows:
        draw.text((palette.pad, y + 10), "还没有人打卡，发送 /打卡 抢占第一名！", font=small_face, fill=COLOR_MUTED)
    text_x = palette.pad + 112
    for index, row in enumerate(rows, 1):
        box = (palette.pad, y, palette.width - palette.pad, y + 62)
        if index <= 3:
            _rounded(draw, box, "#fff7ed" if index == 1 else COLOR_CARD, radius=14)
        rank_color = medal_colors.get(index, COLOR_MUTED)
        draw.text((palette.pad + 20, y + 10), f"{index:02d}", font=name_face, fill=rank_color)
        nickname = ellipsize(name_face, str(row.get("nickname") or row.get("user_id") or "未知"), palette.content_width - 480)
        draw.text((text_x, y + 8), nickname, font=name_face, fill=COLOR_TEXT)
        points_text = f"{int(row.get('points') or 0)} 积分"
        draw.text(
            (palette.width - palette.pad - 24 - text_width(name_face, points_text), y + 10),
            points_text,
            font=name_face,
            fill=COLOR_ACCENT,
        )
        draw.text(
            (text_x, y + 36),
            f"连签 {int(row.get('streak') or 0)} 天 · 累计打卡 {int(row.get('checkin_count') or 0)} 次",
            font=small_face,
            fill=COLOR_MUTED,
        )
        y += 72

    payload = "|".join(f"{row.get('user_key')}:{row.get('points')}" for row in rows)
    path = output_path("board", payload or "empty", render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(canvas, path)


def shop_view_payload(view: ShopView) -> str:
    """商店视图的文本化（用于渲染失败时的降级输出）。"""
    lines = [
        f"{view.title}（{view.user_name} · 当前 {view.points} 积分）",
        "",
        f"● 可兑换（{len(view.redeemable)}）",
    ]
    for item in view.redeemable:
        lines.append(
            f"· {item.product.name} — {item.product.cost} 积分"
            + (f"（剩余 {item.product.stock} 件）" if item.product.stock is not None else "")
        )
    if not view.redeemable:
        lines.append("· 暂无")
    lines.append("")
    lines.append(f"○ 暂不可兑换（{len(view.locked)}）")
    for item in view.locked:
        extra = f"还差 {item.deficit} 分" if item.reason == "积分不足" else item.reason
        lines.append(f"· {item.product.name} — {item.product.cost} 积分（{extra}）")
    if not view.locked:
        lines.append("· 暂无")
    return "\n".join(lines)


def temp_render_dir() -> str:
    """测试用渲染目录（放在系统临时目录，避免污染仓库）。"""
    path = os.path.join(tempfile.gettempdir(), "astrbot_plugin_checkin_shop_render")
    os.makedirs(path, exist_ok=True)
    return path
