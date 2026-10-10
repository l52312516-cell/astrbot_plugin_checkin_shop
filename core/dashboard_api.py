"""插件页（WebUI）后端 API。

所有 handler 都是模块级函数，第一个参数是插件实例；:class:`DashboardAPI` 只负责
把路由注册到 ``context.register_web_api``，业务逻辑全部复用 ``core.business``。
"""

from __future__ import annotations

import os
import time
import uuid
from functools import partial
from typing import Any

from astrbot.api import logger

try:  # AstrBot >= 4.16 推荐的插件 Web API
    from astrbot.api.web import (
        PluginUploadFile,
        error_response,
        file_response,
        json_response,
        request,
    )

    WEB_API_AVAILABLE = True
except ImportError:  # pragma: no cover - 老版本
    PluginUploadFile = None  # type: ignore[assignment,misc]
    WEB_API_AVAILABLE = False

    def error_response(message: str, status_code: int = 400, **_):  # type: ignore[misc]
        return {"status": "error", "message": message}, status_code

    def file_response(*_args, **_kwargs):  # type: ignore[misc]
        return {"status": "error", "message": "当前 AstrBot 版本不支持文件下载"}, 400

    def json_response(data: Any = None, status_code: int = 200, **_):  # type: ignore[misc]
        return (data if data is not None else {}), status_code

    request = None  # type: ignore[assignment]

try:
    from ..core import business, render  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - AstrBot 顶层模块加载
    from core import business, render  # type: ignore[no-redef]

PLUGIN_ID = "astrbot_plugin_checkin_shop"
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _payload_int(payload: dict[str, Any], key: str, default: int = 0) -> int:
    """从 JSON body 取整数。"""
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _page_args(payload: dict[str, Any]) -> tuple[int, int]:
    """解析分页参数。"""
    return max(1, _payload_int(payload, "page", 1)), min(
        100, max(1, _payload_int(payload, "page_size", 20))
    )


async def shop_summary(plugin) -> Any:
    """页面首屏：设置 + 打卡规则 + 商品 + 统计。

    设置与规则按"插件配置页优先"合并，保证页面上显示的就是真正生效的值。
    """
    data = await plugin.store.data(plugin._seed())
    settings, rewards, _adopted, _snapshot = plugin._effective(
        data.get("settings") or {},
        data.get("rewards") or {},
        data.get("config_snapshot") or {},
    )
    return json_response(
        {
            "settings": settings,
            "rewards": rewards,
            "statistics": business.shop_statistics(data),
            "products": [item.to_dict() for item in business.all_products(data)],
        }
    )


async def rewards_save(plugin) -> Any:
    """保存打卡积分规则。"""
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求内容必须是 JSON 对象")
    mode = str(payload.get("mode") or "fixed")
    if mode not in business.REWARD_MODES:
        return error_response(f"积分模式必须是 {'/'.join(business.REWARD_MODES)}")
    fixed = _payload_int(payload, "fixed")
    random_min = _payload_int(payload, "random_min")
    random_max = _payload_int(payload, "random_max")
    if fixed < 0 or random_min < 0 or random_max < 0:
        return error_response("积分不能为负数")
    if random_max < random_min:
        return error_response("随机积分上限不能小于下限")
    custom_list = payload.get("custom_list") or []
    if not isinstance(custom_list, list):
        return error_response("自定义积分列表必须是数组")
    try:
        custom_values = [int(item) for item in custom_list]
    except (TypeError, ValueError):
        return error_response("自定义积分列表必须都是整数")
    if any(item < 0 for item in custom_values):
        return error_response("自定义积分不能为负数")
    if len(custom_values) > 30:
        return error_response("自定义积分列表最多 30 项")
    raw_weights = payload.get("custom_weights") or []
    if not isinstance(raw_weights, list):
        return error_response("自定义权重必须是数组")
    try:
        weights = [int(item) for item in raw_weights]
    except (TypeError, ValueError):
        return error_response("自定义权重必须都是整数")
    if any(item < 0 for item in weights):
        return error_response("权重不能为负数")
    if weights and len(weights) != len(custom_values):
        return error_response("权重数量必须与自定义积分数量一致")
    if mode == "custom" and not custom_values:
        return error_response("自定义模式下至少需要一个积分值")
    every = _payload_int(payload, "streak_bonus_every", 7)
    if every < 0:
        return error_response("连签奖励间隔不能为负数")
    bonus_points = _payload_int(payload, "streak_bonus_points")
    if bonus_points < 0:
        return error_response("连签奖励积分不能为负数")

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("rewards", {}).update(
            {
                "mode": mode,
                "fixed": fixed,
                "random_min": random_min,
                "random_max": random_max,
                "custom_list": custom_values,
                "custom_weights": weights,
                "streak_bonus_enabled": bool(payload.get("streak_bonus_enabled", True)),
                "streak_bonus_every": every,
                "streak_bonus_points": bonus_points,
            }
        )
        return {"rewards": data["rewards"]}

    result = await plugin.store.mutate(handler, plugin._seed())
    # 回写插件配置页，避免"插件页改了、配置页还是旧值"的两套配置
    mirrored = plugin.write_config(
        {
            plugin.REWARDS_TO_CONFIG[name]: value
            for name, value in (result.get("rewards") or {}).items()
            if name in plugin.REWARDS_TO_CONFIG
        }
    )
    return json_response({**result, "mirrored": mirrored})


async def settings_save(plugin) -> Any:
    """保存展示与行为设置。"""
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求内容必须是 JSON 对象")
    timeout = _payload_int(payload, "confirm_timeout_seconds", 60)
    if timeout < 10 or timeout > 600:
        return error_response("确认超时必须在 10~600 秒之间")
    size = _payload_int(payload, "leaderboard_size", 20)
    if size < 1 or size > 100:
        return error_response("榜单长度必须在 1~100 之间")
    reset_hour = _payload_int(payload, "reset_hour", 0)
    if reset_hour < 0 or reset_hour > 23:
        return error_response("重置小时必须在 0~23 之间")
    scope = str(payload.get("points_scope") or "platform_user")
    if scope not in ("platform_user", "conversation"):
        return error_response("积分作用域取值非法")
    board_scope = str(payload.get("leaderboard_scope") or "platform")
    if board_scope not in ("platform", "global"):
        return error_response("榜单范围取值非法")

    def handler(data: dict[str, Any]) -> dict[str, Any]:
        data.setdefault("settings", {}).update(
            {
                "points_scope": scope,
                "leaderboard_scope": board_scope,
                "leaderboard_size": size,
                "confirm_timeout_seconds": timeout,
                "reset_hour": reset_hour,
                "shop_title": str(payload.get("shop_title") or "积分商店")[:40],
                "shop_subtitle": str(payload.get("shop_subtitle") or "")[:120],
            }
        )
        return {"settings": data["settings"]}

    result = await plugin.store.mutate(handler, plugin._seed())
    mirrored = plugin.write_config(
        {
            plugin.SETTINGS_TO_CONFIG[name]: value
            for name, value in (result.get("settings") or {}).items()
            if name in plugin.SETTINGS_TO_CONFIG
        }
    )
    return json_response({**result, "mirrored": mirrored})


async def products_save(plugin) -> Any:
    """新增或更新商品。"""
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求内容必须是 JSON 对象")
    now = time.time()

    def handler(data: dict[str, Any]):
        product, message = business.upsert_product(data, payload, now)
        if product is None:
            return None, message
        return product, ""

    product, message = await plugin.store.mutate(handler, plugin._seed())
    if product is None:
        return error_response(message)
    return json_response({"product": product, "message": "已保存"})


async def products_delete(plugin) -> Any:
    """删除商品（有未完成记录时改为下架）。"""
    payload = await request.json(default={})
    product_id = str((payload or {}).get("id") or "").strip()
    if not product_id:
        return error_response("缺少商品 id")

    def handler(data: dict[str, Any]):
        return business.delete_product(data, product_id)

    ok, message = await plugin.store.mutate(handler, plugin._seed())
    if not ok:
        return error_response(message)
    return json_response({"message": message})


async def products_reorder(plugin) -> Any:
    """按页面拖拽顺序更新排序。"""
    payload = await request.json(default={})
    order = (payload or {}).get("order") or []
    if not isinstance(order, list):
        return error_response("order 必须是商品 id 数组")

    def handler(data: dict[str, Any]) -> int:
        return business.reorder_products(data, [str(item) for item in order])

    changed = await plugin.store.mutate(handler, plugin._seed())
    return json_response({"changed": changed})


async def products_image_upload(plugin) -> Any:
    """上传商品图片（字段名固定为 file）。"""
    if not WEB_API_AVAILABLE:
        return error_response("当前 AstrBot 版本不支持插件文件上传")
    files = await request.files()
    upload = files.get("file")
    if not isinstance(upload, PluginUploadFile):
        return error_response("缺少上传文件（字段名 file）")
    # bridge.upload() 只带一个文件字段，商品 id 通过 query 传入
    product_id = str(request.query.get("product_id", "") or "").strip()
    extension = os.path.splitext(str(upload.filename or ""))[1].lower()
    if extension not in ALLOWED_IMAGE_EXT:
        return error_response("只支持 png/jpg/jpeg/webp/gif 图片")
    plugin.store.ensure_directories()
    relative = os.path.join("products", f"{uuid.uuid4().hex}{extension}")
    target = os.path.join(plugin.store.directory, relative)
    try:
        written = await upload.save(target, max_bytes=MAX_IMAGE_BYTES)
    except Exception as exc:
        logger.exception("[打卡商店] 保存商品图片失败")
        return error_response(f"保存图片失败：{exc}")
    normalized = relative.replace(os.sep, "/")

    def handler(data: dict[str, Any]):
        if not product_id:
            return None, ""
        target_product = next(
            (item for item in data.get("products", []) if item.get("id") == product_id), None
        )
        if target_product is None:
            return None, normalized
        target_product["image_path"] = normalized
        return target_product, normalized

    product, _ = await plugin.store.mutate(handler, plugin._seed())
    return json_response(
        {
            "image_path": normalized,
            "bytes": written,
            "product": product,
        }
    )


async def products_image_get(plugin) -> Any:
    """按商品读取图片（页面预览用）。

    通过 bridge 的 ``apiGet`` 拿到 base64 数据后在前端拼成 ``data:`` URL。
    之所以不直接返回文件流：插件页运行在受限 iframe 里，``<img>`` 无法携带
    Dashboard 的鉴权头，而插件页静态资源只能来自插件目录内的 ``pages/``，
    不能指向 ``plugin_data`` 下的商品图片。
    """
    if request is None:
        return error_response("当前 AstrBot 版本不支持该接口")
    product_id = request.query.get("id", "")
    relative = request.query.get("path", "")
    if product_id:
        data = await plugin.store.data(plugin._seed())
        target = next(
            (item for item in data.get("products", []) if item.get("id") == product_id), None
        )
        relative = str((target or {}).get("image_path") or "")
    safe = str(relative or "").replace("\\", "/").lstrip("/")
    parts = [part for part in safe.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return error_response("图片路径非法", status_code=400)
    path = os.path.join(plugin.store.directory, *parts)
    if not os.path.exists(path):
        return error_response("图片不存在", status_code=404)
    if os.path.getsize(path) > MAX_IMAGE_BYTES * 2:
        return error_response("图片过大，无法预览", status_code=413)
    import base64

    extension = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(extension, "application/octet-stream")
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return json_response(
        {
            "image_path": safe,
            "content_type": mime,
            "data_url": f"data:{mime};base64,{encoded}",
        }
    )


async def users_list(plugin) -> Any:
    """用户列表（分页 + 关键字）。"""
    payload = await request.json(default={})
    payload = payload if isinstance(payload, dict) else {}
    page, page_size = _page_args(payload)
    data = await plugin.store.data(plugin._seed())
    return json_response(
        business.list_users(
            data,
            keyword=str(payload.get("keyword") or ""),
            platform=str(payload.get("platform") or ""),
            page=page,
            page_size=page_size,
        )
    )


async def users_adjust(plugin) -> Any:
    """管理员调整用户积分。"""
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求内容必须是 JSON 对象")
    user_key = str(payload.get("user_key") or "").strip()
    if not user_key or ":" not in user_key:
        return error_response("缺少合法的 user_key（形如 qq:12345）")
    absolute = payload.get("absolute")
    delta = payload.get("delta")
    if absolute in (None, "") and delta in (None, ""):
        return error_response("请提供 delta 或 absolute")
    try:
        absolute_value = None if absolute in (None, "") else int(absolute)
        delta_value = None if delta in (None, "") else int(delta)
    except (TypeError, ValueError):
        return error_response("积分必须是整数")
    note = str(payload.get("note") or "")[:100]
    now = time.time()

    def handler(data: dict[str, Any]):
        return business.adjust_user_points(
            data, user_key, delta_value, absolute_value, note, now
        )

    ok, message, points = await plugin.store.mutate(handler, plugin._seed())
    if not ok:
        return error_response(message)
    return json_response({"message": message, "points": points})


async def redemptions_list(plugin) -> Any:
    """兑换记录（分页 + 状态筛选）。"""
    payload = await request.json(default={})
    payload = payload if isinstance(payload, dict) else {}
    page, page_size = _page_args(payload)
    data = await plugin.store.data(plugin._seed())
    return json_response(
        business.list_redemptions(
            data,
            status=str(payload.get("status") or ""),
            page=page,
            page_size=page_size,
        )
    )


async def redemptions_status(plugin) -> Any:
    """更新兑换记录状态；首次标记为已发放时通知用户（会话消息 + SMTP 邮件）。"""
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求内容必须是 JSON 对象")
    record_id = str(payload.get("id") or "").strip()
    if not record_id:
        return error_response("缺少记录 id")
    now = time.time()

    def handler(data: dict[str, Any]):
        return business.transition_redemption(
            data,
            record_id,
            str(payload.get("status") or ""),
            str(payload.get("note") or "")[:100],
            bool(payload.get("restore", True)),
            now,
        )

    ok, message, notify = await plugin.store.mutate(handler, plugin._seed())
    if not ok:
        return error_response(message)
    notice = ""
    email = ""
    if notify is not None:
        notice = await plugin.notify_redemption(notify)
        email = await plugin.send_redemption_email(notify)
    return json_response({"message": message, "notice": notice, "email": email})


async def email_test(plugin) -> Any:
    """发一封测试邮件，用来验证 SMTP 配置。"""
    payload = await request.json(default={})
    payload = payload if isinstance(payload, dict) else {}
    ok, message = await plugin.send_test_email(str(payload.get("to") or ""))
    if not ok:
        return error_response(message or "测试邮件未发出")
    return json_response({"message": message})


async def redemptions_delete(plugin) -> Any:
    """删除兑换记录（只清理历史，不返还积分，详见 business.delete_redemption）。"""
    payload = await request.json(default={})
    if not isinstance(payload, dict):
        return error_response("请求内容必须是 JSON 对象")
    record_id = str(payload.get("id") or "").strip()
    if not record_id:
        return error_response("缺少记录 id")

    def handler(data: dict[str, Any]):
        return business.delete_redemption(data, record_id, bool(payload.get("force", False)))

    ok, message = await plugin.store.mutate(handler, plugin._seed())
    if not ok:
        return error_response(message)
    return json_response({"message": message})


async def preview_card(plugin) -> Any:
    """在后台预览商店卡片（``bridge.download`` 会以 GET 请求下载）。"""
    user_key = "preview:0"
    if request is not None:
        user_key = str(request.query.get("user_key", "preview:0") or "preview:0")
    data = await plugin.store.data(plugin._seed())
    view = business.build_shop_view(data, user_key, include_disabled=True)
    try:
        path = render.render_shop(
            view,
            plugin.store.render_dir,
            None,
            plugin._palette(),
        )
    except Exception as exc:
        logger.exception("[打卡商店] 预览卡片失败")
        return error_response(f"预览失败：{exc}")
    return file_response(path, filename="shop-preview.png", content_type="image/png")


class DashboardAPI:
    """把上面的 handler 注册成插件 Web API。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register(self) -> None:
        """注册全部路由。"""
        routes = (
            (f"/{PLUGIN_ID}/shop/summary", shop_summary, ["GET"], "打卡商店：读取设置与商品"),
            (f"/{PLUGIN_ID}/shop/rewards/save", rewards_save, ["POST"], "打卡商店：保存打卡规则"),
            (f"/{PLUGIN_ID}/shop/settings/save", settings_save, ["POST"], "打卡商店：保存展示设置"),
            (f"/{PLUGIN_ID}/shop/products/save", products_save, ["POST"], "打卡商店：保存商品"),
            (f"/{PLUGIN_ID}/shop/products/delete", products_delete, ["POST"], "打卡商店：删除商品"),
            (f"/{PLUGIN_ID}/shop/products/reorder", products_reorder, ["POST"], "打卡商店：商品排序"),
            (f"/{PLUGIN_ID}/shop/products/image", products_image_get, ["GET"], "打卡商店：读取商品图片"),
            (f"/{PLUGIN_ID}/shop/products/image/upload", products_image_upload, ["POST"], "打卡商店：上传商品图片"),
            (f"/{PLUGIN_ID}/shop/users", users_list, ["POST"], "打卡商店：用户积分列表"),
            (f"/{PLUGIN_ID}/shop/users/adjust", users_adjust, ["POST"], "打卡商店：调整用户积分"),
            (f"/{PLUGIN_ID}/shop/redemptions", redemptions_list, ["POST"], "打卡商店：兑换记录"),
            (f"/{PLUGIN_ID}/shop/redemptions/status", redemptions_status, ["POST"], "打卡商店：更新兑换状态"),
            (f"/{PLUGIN_ID}/shop/redemptions/delete", redemptions_delete, ["POST"], "打卡商店：删除兑换记录"),
            (f"/{PLUGIN_ID}/shop/email/test", email_test, ["POST"], "打卡商店：发送测试邮件"),
            (f"/{PLUGIN_ID}/shop/preview", preview_card, ["GET"], "打卡商店：预览商店卡片"),
        )
        context = self.plugin.context
        for route, handler, methods, desc in routes:
            try:
                context.register_web_api(route, partial(handler, self.plugin), methods, desc)
            except Exception:
                logger.exception("[打卡商店] 注册接口失败：%s", route)
