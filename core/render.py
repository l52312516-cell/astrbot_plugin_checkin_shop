"""使用 Pillow 在本地渲染商店 / 积分 / 榜单卡片。

选择本地渲染的原因：AstrBot 的网络 t2i 服务需要 Playwright/联网，失败时老插件只能
降级成纯文本；这里自带 Noto Sans CJK 字体，离线也能出图，且渲染失败时上层可以
整体降级为纯文本。

视觉约定（改样式时请一起维护）：

* 画布宽度默认 1080，四周留白用 ``palette.pad``，底部留白与左右一致；
* 卡片 = 白底圆角面板 + 深色标题 + 浅灰说明文字；正文/次要/极小三级字号；
* 区块标题统一成"左侧色条 + 深色标题 + 可选计数胶囊 + 细分隔线"，四张卡放在
  一起时观感一致（不再用蓝/绿/橙的彩色大标题）；
* 高度允许宽松估算，最后统一裁掉底部多余空白（``_trim``），避免卡片拖一条长尾巴；
* 内置的 Noto Sans CJK 不含彩色 emoji，需要视觉符号时用几何字符 + 颜色区分。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
from typing import Any, Callable, Iterable

try:  # 作为包被导入（测试）
    from .business import HelpView, ProductState, ShopView
except ImportError:  # pragma: no cover - AstrBot 以顶层模块方式加载 core/
    from business import HelpView, ProductState, ShopView  # type: ignore[no-redef]

logger = logging.getLogger("astrbot")

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - requirements.txt 里已声明
    PIL_AVAILABLE = False
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

RESOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "resources")
FONT_FILENAME = "NotoSansCJKsc-Regular.otf"

# ------------------------------------------------------------------ 设计令牌
COLOR_BG = "#f5f7fb"          # 页面底色
COLOR_CARD = "#ffffff"        # 卡片/行底
COLOR_TEXT = "#111827"        # 主文字
COLOR_MUTED = "#6b7280"       # 说明文字
COLOR_FAINT = "#9aa3b2"       # 更弱的元信息
COLOR_ACCENT = "#2563eb"      # 主色
COLOR_OK = "#059669"          # 可兑换 / 正向
COLOR_LOCK = "#b45309"        # 不可兑换 / 需要注意
COLOR_GRID = "#e8ecf3"        # 分隔线
COLOR_BORDER = "#e4e9f2"      # 白色面板/胶囊的描边（浅灰底上白色面板需要描边才立得住）
COLOR_GOLD = "#b45309"
COLOR_SILVER = "#475569"
COLOR_BRONZE = "#c2410c"
GOLD_BG = "#fef3c7"
SILVER_BG = "#e2e8f0"
BRONZE_BG = "#ffedd5"
PLAIN_RANK_BG = "#eef1f6"
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


def _trim(canvas: Any, palette: "CardPalette", bottom: int) -> Any:
    """把画布裁到内容底部（保留统一的底部留白）。

    卡片高度是按内容宽松估算的，不裁掉就会在末尾挂一大片空白；裁切放在最后一步，
    估算偏小也不会切到内容。
    """
    height = int(max(palette.pad * 4, min(canvas.height, int(bottom) + palette.pad)))
    if height >= canvas.height:
        return canvas
    trimmed = canvas.crop((0, 0, canvas.width, height))
    canvas.close()
    return trimmed


def _rounded(
    draw: Any,
    box: tuple[int, int, int, int],
    fill: str,
    radius: int = 18,
    outline: str | None = None,
    width: int = 2,
) -> None:
    if outline:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    else:
        draw.rounded_rectangle(box, radius=radius, fill=fill)


def _vcenter(face: Any, top: int, height: int) -> int:
    """文本在 ``[top, top+height)`` 里垂直居中时的绘制 y。"""
    return int(top + (height - face.size * 1.28) / 2)


def _baseline_offset(draw: Any, text: str, face: Any) -> int:
    """文本相对绘制原点的基线位置（用于不同字号之间的基线对齐）。"""
    try:
        return int(draw.textbbox((0, 0), text, font=face)[3])
    except Exception:  # pragma: no cover - 老版本 Pillow
        return int(face.size * 0.86)


def _text_at_baseline(draw: Any, x: int, baseline: int, text: str, face: Any, fill: str) -> None:
    """把文本画到指定基线上（大号数字与小号单位对齐时很好用）。"""
    draw.text((x, baseline - _baseline_offset(draw, text, face)), text, font=face, fill=fill)


def _rounded_mask(size: tuple[int, int], radius: int) -> Any:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=max(0, radius), fill=255
    )
    return mask


def _copy_to_canvas(
    canvas: Any,
    source: Any,
    position: tuple[int, int],
    box: tuple[int, int],
    radius: int = 14,
) -> None:
    """把商品图片裁剪缩放（圆角）后贴到卡片的方框里。"""
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
        # 和占位色块保持一致：真实商品图也要圆角，否则一行里圆角/直角混着很脏
        canvas.paste(cropped, (x, y), _rounded_mask((width, height), radius))
    except Exception:  # 单张商品图坏了不应该影响整张卡片
        logger.debug("[打卡商店] 商品图片无法读取，使用占位色块", exc_info=True)


def _placeholder(
    canvas: Any,
    draw: Any,
    box: tuple[int, int, int, int],
    name: str,
    radius: int = 14,
    font_path: str = "",
) -> None:
    """没有商品图时的占位色块。

    取名字的前两个字（跳过标点/空白）而不是一个字：像"一周管理员体验卡"这种，
    只取"一"会被看成一根横线。
    """
    x1, y1, x2, y2 = box
    size = max(1, y2 - y1)
    label = re.sub(r"^[\s·、，。,.!！?？\-—_]+", "", str(name or ""))[:2] or "?"
    color = CATEGORY_COLORS[sum(ord(ch) for ch in (name or "?")) % len(CATEGORY_COLORS)]
    _rounded(draw, box, color, radius=radius)
    face = font_for(max(12, int(size * (0.3 if len(label) > 1 else 0.44))), font_path)
    draw.text(
        (x1 + (size - text_width(face, label)) // 2, _vcenter(face, y1, size)),
        label,
        font=face,
        fill="#ffffff",
    )


class CardPalette:
    """卡片尺寸/字号集中定义，方便调整观感。

    ``command_prefix`` 只用于卡面上"提示用户发哪条指令"的那几行文字
    （例如「发送 /打卡 试试～」），不影响任何消息匹配。
    """

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        font_size_base: int = 0,
        font_path: str = "",
        command_prefix: str = "/",
    ) -> None:
        self.width = max(640, int(width))
        self.base = int(font_size_base) if font_size_base else max(20, self.width // 40)
        self.pad = max(20, int(round(self.width * 0.033)))  # 1080 -> 36
        self.title = int(self.base * 1.42)
        self.section = int(self.base * 1.12)
        self.body = self.base
        self.small = int(self.base * 0.8)
        self.tiny = int(self.base * 0.7)
        # 间距节奏与通用尺寸
        self.gap_s = max(6, self.base // 4)
        self.gap = max(10, self.base // 2)
        self.gap_l = max(16, int(self.base * 0.8))
        self.radius = max(10, int(self.base * 0.6))
        self.line = max(1, int(self.base * 0.075))
        self.font_path = font_path
        self.command_prefix = str(command_prefix or "/")

    @property
    def content_width(self) -> int:
        return self.width - self.pad * 2

    def command(self, name: str) -> str:
        """卡面文案里的指令写法（如 ``/打卡``）。"""
        clean = str(name or "").lstrip("/")
        return f"{self.command_prefix}{clean}" if clean else self.command_prefix

    @property
    def cache_tag(self) -> str:
        """参与缓存键的渲染参数。

        宽度、字号、字体或指令前缀变了，出的图就不一样，必须换一个缓存键——
        否则管理员改完设置会在最长 30 分钟里一直看到旧图（"改了没生效"）。
        """
        return (
            f"w{self.width}-b{self.base}-f{self.font_path or 'builtin'}"
            f"-p{self.command_prefix}"
        )


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
    title_height = int(palette.title * 1.28)
    draw.text((palette.pad, y), title, font=title_face, fill=COLOR_TEXT)

    if badge:
        badge_face = font_for(palette.small, palette.font_path)
        height = int(palette.small * 2.05)
        width = text_width(badge_face, badge) + 32
        top = y + (title_height - height) // 2
        box = (palette.width - palette.pad - width, top, palette.width - palette.pad, top + height)
        _rounded(draw, box, COLOR_ACCENT, radius=height // 2)
        draw.text(
            (box[0] + 16, _vcenter(badge_face, top, height)),
            badge,
            font=badge_face,
            fill="#ffffff",
        )
    y += title_height
    if subtitle:
        sub_face = font_for(palette.small, palette.font_path)
        for line in wrap_text(sub_face, subtitle, palette.content_width, max_lines=2):
            draw.text((palette.pad, y), line, font=sub_face, fill=COLOR_MUTED)
            y += int(palette.small * 1.45)
    return y + palette.gap_l


def _draw_pill(
    draw: Any,
    x: int,
    y: int,
    text: str,
    face: Any,
    fg: str,
    bg: str,
    pad_x: int = 13,
) -> int:
    """画一个胶囊标签，返回右边界 x。"""
    height = int(face.size * 1.8)
    width = text_width(face, text) + pad_x * 2
    _rounded(
        draw,
        (x, y, x + width, y + height),
        bg,
        radius=height // 2,
        outline=COLOR_BORDER,
    )
    draw.text((x + pad_x, _vcenter(face, y, height)), text, font=face, fill=fg)
    return x + width


def _draw_section(
    palette: CardPalette,
    draw: Any,
    y: int,
    text: str,
    color: str,
    count: int | None = None,
) -> int:
    """区块标题：左侧色条 + 深色标题（可带计数胶囊）+ 细分隔线。

    Returns:
        下一块内容的起始 y。
    """
    face = font_for(palette.section, palette.font_path)
    bar_w = max(6, palette.base // 4)
    bar_h = int(palette.section * 0.95)
    bar_top = y + int(palette.section * 0.22)
    _rounded(draw, (palette.pad, bar_top, palette.pad + bar_w, bar_top + bar_h), color, radius=3)
    text_x = palette.pad + bar_w + palette.gap
    draw.text((text_x, y), text, font=face, fill=COLOR_TEXT)
    if count is not None:
        small_face = font_for(palette.small, palette.font_path)
        _draw_pill(
            draw,
            text_x + text_width(face, text) + 12,
            y + int(palette.section * 0.12),
            str(count),
            small_face,
            COLOR_MUTED,
            COLOR_CARD,
        )
    line_y = y + int(palette.section * 1.35)
    draw.line(
        (palette.pad, line_y, palette.width - palette.pad, line_y),
        fill=COLOR_GRID,
        width=palette.line,
    )
    return line_y + palette.gap + 4


def _draw_empty(palette: CardPalette, draw: Any, y: int, text: str) -> int:
    """空状态：一块浅色圆角面板，返回下一块内容的起始 y。"""
    face = font_for(palette.small, palette.font_path)
    height = max(52, int(palette.body * 2.2))
    _rounded(
        draw,
        (palette.pad, y, palette.width - palette.pad, y + height),
        COLOR_CARD,
        radius=palette.radius,
        outline=COLOR_BORDER,
    )
    draw.text(
        (palette.pad + 20, _vcenter(face, y, height)),
        text,
        font=face,
        fill=COLOR_MUTED,
    )
    return y + height + palette.gap


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

    name_face = font_for(int(palette.body * 1.08), palette.font_path)
    small_face = font_for(palette.small, palette.font_path)
    tiny_face = font_for(palette.tiny, palette.font_path)
    pad = palette.pad
    thumb = max(64, int(palette.base * 2.85))
    line_body = int(palette.body * 1.5)
    line_small = int(palette.small * 1.55)
    top_pad = max(14, int(palette.body * 0.62))

    def row_height(item: ProductState) -> int:
        lines = 2 + (1 if item.product.description else 0)
        return max(thumb + 2 * top_pad, top_pad * 2 + line_body + line_small * (lines - 1))

    # 注意：内置的 Noto Sans CJK 不含彩色 emoji，标题里用几何字符 + 颜色区分，
    # 否则会渲染成"豆腐块"。
    sections: list[tuple[str, str, list[ProductState]]] = [
        ("可兑换", COLOR_OK, view.redeemable),
        ("暂不可兑换", COLOR_LOCK, view.locked),
    ]

    total_height = palette.pad + int(palette.title * 1.28) + int(palette.small * 1.45)
    total_height += palette.gap_l
    for _title, _color, items in sections:
        total_height += int(palette.section * 1.35) + palette.gap + 4
        total_height += sum(row_height(item) + palette.gap for item in items) or int(
            palette.body * 3
        )
        total_height += palette.gap
    total_height += palette.pad

    canvas, draw = _new_canvas(palette.width, max(360, total_height))
    y = _draw_header(
        palette,
        canvas,
        draw,
        view.title or "积分商店",
        f"我的积分 {view.points}",
        view.subtitle or f"{view.user_name} · 回复 {palette.command('兑换商品 <名称>')} 进行兑换",
    )

    index = 0
    for title, color, items in sections:
        y = _draw_section(palette, draw, y, title, color, len(items))
        if not items:
            y = _draw_empty(
                palette,
                draw,
                y,
                "还没有可兑换的商品，攒够积分再来看看～"
                if color == COLOR_OK
                else "这里还没有商品",
            )
            continue
        for item in items:
            index += 1
            product = item.product
            height = row_height(item)
            _rounded(
                draw,
                (pad, y, palette.width - pad, y + height),
                COLOR_CARD,
                radius=palette.radius + 2,
                outline=COLOR_BORDER,
            )
            inner_x = pad + palette.gap
            if show_images:
                top = y + (height - thumb) // 2
                image = None
                if image_resolver is not None and product.image_path:
                    try:
                        image = image_resolver(product.image_path)
                    except Exception:
                        image = None
                thumb_radius = max(8, palette.radius - 2)
                if image is not None:
                    _copy_to_canvas(
                        canvas, image, (inner_x, top), (thumb, thumb), radius=thumb_radius
                    )
                else:
                    _placeholder(
                        canvas,
                        draw,
                        (inner_x, top, inner_x + thumb, top + thumb),
                        product.name,
                        radius=thumb_radius,
                        font_path=palette.font_path,
                    )
                inner_x += thumb + palette.gap

            # 编号跨区块连续（01、02、03、04…），避免两个区块都从 01 开始
            draw.text((inner_x, y + top_pad), f"{index:02d}", font=tiny_face, fill=COLOR_FAINT)
            text_x = inner_x + text_width(tiny_face, "00") + palette.gap
            right_x = palette.width - pad - palette.gap

            price_text = f"{product.cost} 积分"
            price_color = COLOR_OK if item.redeemable else COLOR_LOCK
            if item.redeemable:
                hint = ""
            elif item.reason == "积分不足":
                hint = f"还差 {item.deficit} 分"
            else:
                hint = item.reason

            right_width = max(
                text_width(name_face, price_text), text_width(small_face, hint) if hint else 0
            )
            name = ellipsize(
                name_face,
                product.name,
                max(80, right_x - right_width - 20 - text_x),
            )
            draw.text((text_x, y + top_pad), name, font=name_face, fill=COLOR_TEXT)
            # 可兑换的行不再重复写"可兑换"（区块标题已经说了），只留价格
            draw.text(
                (right_x - text_width(name_face, price_text), y + top_pad),
                price_text,
                font=name_face,
                fill=price_color,
            )
            if hint:
                draw.text(
                    (right_x - text_width(small_face, hint), y + top_pad + line_body),
                    hint,
                    font=small_face,
                    fill=price_color,
                )

            meta_bits = [f"分类 {product.category}"] if product.category else []
            meta_bits.append("不限量" if product.stock is None else f"剩余 {product.stock} 件")
            if product.per_user_limit is not None:
                meta_bits.append(f"限购 {product.per_user_limit} · 已兑 {item.user_redeemed}")
            meta = " · ".join(meta_bits)
            meta_y = y + top_pad + line_body
            draw.text(
                (text_x, meta_y),
                ellipsize(small_face, meta, right_x - text_x),
                font=small_face,
                fill=COLOR_MUTED,
            )
            if product.description:
                draw.text(
                    (text_x, meta_y + line_small),
                    ellipsize(small_face, product.description, right_x - text_x),
                    font=small_face,
                    fill=COLOR_FAINT,
                )
            y += height + palette.gap

    payload = (
        f"{view.user_key}|{view.points}|"
        + "|".join(
            f"{item.product.id}:{item.product.cost}:{item.product.stock}:{item.redeemable}:{item.reason}"
            for item in view.items
        )
        + f"|{palette.cache_tag}"
    )
    path = output_path("shop", payload, render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(_trim(canvas, palette, y), path)


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
    value_face = font_for(int(palette.body * 1.2), palette.font_path)
    delta_face = font_for(int(palette.body * 1.05), palette.font_path)
    big_face = font_for(int(palette.title * 1.55), palette.font_path)

    panel_height = max(140, int(palette.body * 5.1))
    row_height = max(40, int(palette.body * 1.65))
    rows = len(history[:10])
    total_height = (
        palette.pad
        + int(palette.title * 1.28)
        + int(palette.small * 1.45)
        + palette.gap_l
        + panel_height
        + palette.gap_l
        + int(palette.section * 1.35)
        + palette.gap
        + (row_height * rows if rows else max(52, int(palette.body * 2.2)) + palette.gap)
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

    # 概览面板：左侧大号余额 + 四条统计（列间细分隔线，数字按同一基线对齐）
    box = (palette.pad, y, palette.width - palette.pad, y + panel_height)
    _rounded(draw, box, COLOR_CARD, radius=palette.radius + 2, outline=COLOR_BORDER)
    left_width = int(palette.base * 7.6)
    baseline = y + int(panel_height * 0.62)
    _text_at_baseline(
        draw,
        palette.pad + 26,
        baseline,
        str(int(user.get("points") or 0)),
        big_face,
        COLOR_ACCENT,
    )
    draw.text(
        (palette.pad + 28, baseline + int(palette.small * 0.5)),
        "当前积分",
        font=label_face,
        fill=COLOR_MUTED,
    )

    stats = (
        ("累计获得", int(user.get("total_earned") or 0)),
        ("累计消耗", int(user.get("total_spent") or 0)),
        ("打卡次数", int(user.get("checkin_count") or 0)),
        ("连签天数", int(user.get("streak") or 0)),
    )
    stats_x = palette.pad + left_width
    column = max(1, (palette.width - palette.pad - stats_x) // len(stats))
    value_top = baseline - _baseline_offset(draw, "0", value_face)
    for index, (label, value) in enumerate(stats):
        x = stats_x + index * column + palette.gap
        if index:
            draw.line(
                (x - palette.gap, y + 22, x - palette.gap, y + panel_height - 22),
                fill=COLOR_GRID,
                width=palette.line,
            )
        draw.text(
            (x, value_top - int(label_face.size * 1.7)),
            label,
            font=label_face,
            fill=COLOR_FAINT,
        )
        _text_at_baseline(draw, x, baseline, str(value), value_face, COLOR_TEXT)
    y = box[3] + palette.gap_l

    y = _draw_section(palette, draw, y, "最近打卡", COLOR_ACCENT, len(history))
    if not history:
        y = _draw_empty(
            palette, draw, y, f"还没有打卡记录，发送 {palette.command('打卡')} 试试～"
        )
    note_x = palette.pad + max(180, int(palette.content_width * 0.3))
    for index, item in enumerate(history[:10]):
        date = str(item.get("date") or "")
        points = int(item.get("points") or 0)
        bonus = int(item.get("bonus") or 0)
        note = str(item.get("note") or "")
        if not note:
            note = f"连签 {int(item.get('streak') or 0)} 天"
            if bonus:
                note += f" · 连签奖励 +{bonus}"
        gained = f"+{points}" if points >= 0 else str(points)
        draw.text(
            (palette.pad + 4, _vcenter(label_face, y, row_height)),
            date,
            font=label_face,
            fill=COLOR_TEXT,
        )
        draw.text(
            (note_x, _vcenter(label_face, y, row_height)),
            ellipsize(label_face, note, palette.width - palette.pad - note_x - 140),
            font=label_face,
            fill=COLOR_MUTED,
        )
        draw.text(
            (
                palette.width - palette.pad - 4 - text_width(delta_face, gained),
                _vcenter(delta_face, y, row_height),
            ),
            gained,
            font=delta_face,
            fill=COLOR_OK if points >= 0 else COLOR_LOCK,
        )
        if index < len(history[:10]) - 1:
            line_y = y + row_height - 1
            draw.line(
                (palette.pad + 4, line_y, palette.width - palette.pad - 4, line_y),
                fill=COLOR_GRID,
                width=2,
            )
        y += row_height

    payload = (
        f"{display_name}|{user.get('points')}|{user.get('streak')}|{len(history)}"
        f"|{palette.cache_tag}"
    )
    path = output_path("points", payload, render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(_trim(canvas, palette, y), path)


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
    name_face = font_for(int(palette.body * 1.08), palette.font_path)
    small_face = font_for(palette.small, palette.font_path)
    score_face = font_for(int(palette.body * 1.25), palette.font_path)
    score_size = int(palette.body * 1.25)
    chip = max(40, int(palette.base * 1.6))
    name_leading = int(int(palette.body * 1.08) * 1.25)
    block_height = name_leading + int(palette.small * 1.28)
    row_height = max(chip + 2 * palette.gap, block_height + 2 * palette.gap)
    stats = ((len(rows) * (row_height + palette.gap_s)) if rows else max(52, int(palette.body * 2.2)))

    total_height = (
        palette.pad
        + int(palette.title * 1.28)
        + int(palette.small * 1.45)
        + palette.gap_l
        + stats
        + palette.pad
    )
    canvas, draw = _new_canvas(palette.width, total_height)
    y = _draw_header(palette, canvas, draw, title, f"{len(rows)} 人", scope_hint)
    if not rows:
        y = _draw_empty(
            palette, draw, y, f"还没有人打卡，发送 {palette.command('打卡')} 抢占第一名！"
        )

    for index, row in enumerate(rows, 1):
        _rounded(
            draw,
            (palette.pad, y, palette.width - palette.pad, y + row_height),
            COLOR_CARD,
            radius=palette.radius + 2,
            outline=COLOR_BORDER,
        )
        if index == 1:
            chip_fill, chip_fg = GOLD_BG, COLOR_GOLD
        elif index == 2:
            chip_fill, chip_fg = SILVER_BG, COLOR_SILVER
        elif index == 3:
            chip_fill, chip_fg = BRONZE_BG, COLOR_BRONZE
        else:
            chip_fill, chip_fg = PLAIN_RANK_BG, COLOR_SILVER
        chip_top = y + (row_height - chip) // 2
        chip_x = palette.pad + palette.gap
        _rounded(
            draw,
            (chip_x, chip_top, chip_x + chip, chip_top + chip),
            chip_fill,
            radius=max(8, chip // 3),
        )
        rank_text = str(index)
        draw.text(
            (
                chip_x + (chip - text_width(name_face, rank_text)) // 2,
                _vcenter(name_face, chip_top, chip),
            ),
            rank_text,
            font=name_face,
            fill=chip_fg,
        )

        text_x = chip_x + chip + palette.gap
        points_text = str(int(row.get("points") or 0))
        unit = "积分"
        unit_width = text_width(small_face, unit)
        right_x = palette.width - palette.pad - palette.gap
        name_width = right_x - unit_width - text_width(score_face, points_text) - 30 - text_x
        nickname = ellipsize(
            name_face,
            str(row.get("nickname") or row.get("user_id") or "未知"),
            max(80, name_width),
        )
        name_y = y + (row_height - block_height) // 2
        draw.text((text_x, name_y), nickname, font=name_face, fill=COLOR_TEXT)
        draw.text(
            (text_x, name_y + name_leading),
            f"连签 {int(row.get('streak') or 0)} 天 · 累计打卡 {int(row.get('checkin_count') or 0)} 次",
            font=small_face,
            fill=COLOR_FAINT,
        )
        # 分数：大号数字 + 小号单位，共用一条基线并右对齐
        score_width = text_width(score_face, points_text)
        num_x = right_x - unit_width - 8 - score_width
        baseline = y + (row_height + int(score_size * 0.72)) // 2
        _text_at_baseline(draw, num_x, baseline, points_text, score_face, COLOR_ACCENT)
        _text_at_baseline(draw, right_x - unit_width, baseline, unit, small_face, COLOR_FAINT)
        y += row_height + palette.gap_s

    payload = "|".join(f"{row.get('user_key')}:{row.get('points')}" for row in rows)
    payload = f"{payload or 'empty'}|{palette.cache_tag}"
    path = output_path("board", payload, render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(_trim(canvas, palette, y), path)


def render_help(
    view: HelpView,
    render_dir: str,
    palette: CardPalette | None = None,
    font_path: str = "",
) -> str:
    """渲染 ``/商店帮助`` 卡片。

    Args:
        view: 帮助内容（指令表、当前规则、兑换步骤）。
        render_dir: 输出目录。
        palette: 尺寸/字号配置。
        font_path: 自定义字体路径。

    Returns:
        生成的 PNG 路径。
    """
    palette = palette or CardPalette()
    if font_path:
        palette.font_path = font_path
    command_face = font_for(int(palette.body * 1.02), palette.font_path)
    body_face = font_for(palette.body, palette.font_path)
    small_face = font_for(palette.small, palette.font_path)
    line_body = int(palette.body * 1.6)
    chip_h = int(palette.body * 1.85)

    # 先估计高度（偏宽松没关系，最后会裁到内容底部）
    command_rows = max(1, len(view.commands))
    command_block = command_rows * (max(chip_h, int(small_face.size * 1.45)) + 6)
    rules_block = 0
    for line in view.rules:
        rules_block += line_body * len(wrap_text(body_face, line, palette.content_width - 34, 3)) + 2
    rule_chip = int(palette.body * 1.3)
    steps_block = 0
    for line in view.steps:
        wrapped = wrap_text(body_face, line, palette.content_width - rule_chip - 18, 3)
        steps_block += max(rule_chip, line_body * len(wrapped)) + 6
    total_height = (
        palette.pad
        + int(palette.title * 1.28)
        + int(palette.small * 1.45) * 2
        + palette.gap_l
        + int(palette.section * 1.35)
        + palette.gap
        + command_block
        + palette.gap_l
        + int(palette.section * 1.35)
        + palette.gap
        + rules_block
        + palette.gap_l
        + int(palette.section * 1.35)
        + palette.gap
        + steps_block
        + int(palette.small * 2.4)
        + palette.pad
    )
    canvas, draw = _new_canvas(palette.width, total_height)
    y = _draw_header(
        palette,
        canvas,
        draw,
        view.title,
        "帮助",
        view.subtitle,
    )

    name_column = (
        max(text_width(command_face, name) for name, _ in view.commands) + 30
        if view.commands
        else 0
    )
    y = _draw_section(palette, draw, y, "指令", COLOR_ACCENT, len(view.commands))
    for name, desc in view.commands:
        chip_w = text_width(command_face, name) + 26
        wrapped = wrap_text(small_face, desc, palette.content_width - name_column, 2)
        desc_h = int(small_face.size * 1.45) * len(wrapped)
        # 指令名做成"按键"胶囊：白底描边，在浅灰卡片上才立得住
        _rounded(
            draw,
            (palette.pad, y, palette.pad + chip_w, y + chip_h),
            COLOR_CARD,
            radius=max(8, int(palette.body * 0.42)),
            outline=COLOR_BORDER,
        )
        draw.text(
            (palette.pad + 13, _vcenter(command_face, y, chip_h)),
            name,
            font=command_face,
            fill=COLOR_TEXT,
        )
        desc_y = y + (chip_h - desc_h) // 2
        for piece in wrapped:
            draw.text(
                (palette.pad + name_column, desc_y),
                piece,
                font=small_face,
                fill=COLOR_MUTED,
            )
            desc_y += int(small_face.size * 1.45)
        y += max(chip_h, desc_h) + 6

    y = _draw_section(palette, draw, y + palette.gap_s, "当前规则", COLOR_OK, len(view.rules))
    dot = max(6, palette.base // 5)
    for line in view.rules:
        wrapped = wrap_text(body_face, line, palette.content_width - 34, 3)
        for index, piece in enumerate(wrapped):
            top = y + index * line_body
            if index == 0:
                dot_top = top + int(body_face.size * 0.46)
                draw.ellipse(
                    (palette.pad + 5, dot_top, palette.pad + 5 + dot, dot_top + dot),
                    fill=COLOR_OK,
                )
            draw.text((palette.pad + 34, top), piece, font=body_face, fill=COLOR_TEXT)
        y += line_body * len(wrapped) + 2

    y = _draw_section(palette, draw, y + palette.gap_s, "怎么兑换", COLOR_LOCK, len(view.steps))
    for index, line in enumerate(view.steps, 1):
        wrapped = wrap_text(body_face, line, palette.content_width - rule_chip - 18, 3)
        block_h = line_body * len(wrapped)
        _rounded(
            draw,
            (palette.pad, y, palette.pad + rule_chip, y + rule_chip),
            COLOR_CARD,
            radius=max(6, rule_chip // 3),
            outline=COLOR_BORDER,
        )
        number = str(index)
        draw.text(
            (
                palette.pad + (rule_chip - text_width(small_face, number)) // 2,
                _vcenter(small_face, y, rule_chip),
            ),
            number,
            font=small_face,
            fill=COLOR_LOCK,
        )
        text_y = y + (block_h - int(body_face.size * 1.3) * len(wrapped)) // 2
        for piece in wrapped:
            draw.text(
                (palette.pad + rule_chip + 18, text_y),
                piece,
                font=body_face,
                fill=COLOR_TEXT,
            )
            text_y += line_body
        y += max(rule_chip, block_h) + 6

    if view.footer:
        y += palette.gap_s
        draw.text(
            (palette.pad, y),
            ellipsize(small_face, view.footer, palette.content_width),
            font=small_face,
            fill=COLOR_MUTED,
        )
        y += int(small_face.size * 1.4)

    payload = (
        "|".join(
            [
                view.title,
                ",".join(f"{name}:{desc}" for name, desc in view.commands),
                ",".join(view.rules),
                ",".join(view.steps),
            ]
        )
        + f"|{palette.cache_tag}"
    )
    path = output_path("help", payload, render_dir)
    if os.path.exists(path):
        canvas.close()
        return path
    return save_card(_trim(canvas, palette, y), path)


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
