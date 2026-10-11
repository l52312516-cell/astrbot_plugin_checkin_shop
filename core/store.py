"""插件本地数据存储。

单个 JSON 文件 + 全局 ``asyncio.Lock``：

* 所有写入都经过 :meth:`Store.mutate`，先复制内存快照、在副本上改、原子落盘，
  改失败就丢弃，磁盘上永远是上一份完整数据；
* 落盘方式为"写临时文件 → ``os.replace``"，避免进程被中断时留下半截 JSON；
* 文件损坏时备份成 ``shop.json.corrupt.<时间戳>`` 后用默认值重建。

积分、库存等关键数值全部走这里，因此同一 AstrBot 进程内的并发指令不会超卖或丢分。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from typing import Any, Callable

# 上架规范：日志记录器必须且只能从 astrbot.api 导入，严禁使用内置 logging 模块。
# 单元测试环境没有 AstrBot，由 tests/conftest.py 提供 astrbot.api 的最小替身。
from astrbot.api import logger

STORE_FILE_NAME = "shop.json"
SCHEMA_VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "points_scope": "platform_user",
    "leaderboard_scope": "platform",
    "leaderboard_size": 20,
    "confirm_timeout_seconds": 60,
    "reset_hour": 0,
    "shop_title": "积分商店",
    "shop_subtitle": "引用确认消息并回复 是/否 即可完成兑换",
    # 只影响提示语/卡片里"指令怎么写"，不改变 AstrBot 真正接受的唤醒前缀
    "display_prefix": "/",
}

DEFAULT_REWARDS: dict[str, Any] = {
    "mode": "fixed",
    "fixed": 10,
    "random_min": 5,
    "random_max": 20,
    "custom_list": [5, 10, 20],
    "custom_weights": [],
    "streak_bonus_enabled": True,
    "streak_bonus_every": 7,
    "streak_bonus_points": 50,
}


def default_data() -> dict[str, Any]:
    """返回一份全新的默认数据。"""
    return {
        "version": SCHEMA_VERSION,
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "rewards": copy.deepcopy(DEFAULT_REWARDS),
        "users": {},
        "products": [],
        "redemptions": [],
        # 账单流水：每一次积分变动都追加一条（打卡/兑换/取消返还/手动调整）
        "ledger": [],
        # 上一次把插件配置页同步进运行数据时的配置值（见 main.py 的 _effective）
        "config_snapshot": {},
    }


def _merge_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """用默认值补齐字典里缺失的键（不覆盖已有值）。"""
    for key, value in defaults.items():
        if key not in target or target[key] is None:
            target[key] = copy.deepcopy(value)
    return target


def migrate(data: Any) -> dict[str, Any]:
    """把任意磁盘内容整理成可用的数据结构。

    Args:
        data: ``json.load`` 的结果，可能是任意类型。

    Returns:
        结构完整的字典副本。
    """
    if not isinstance(data, dict):
        raise ValueError("存储根节点必须是 JSON 对象")
    fresh = default_data()
    result: dict[str, Any] = {}
    result["version"] = SCHEMA_VERSION
    settings = data.get("settings")
    result["settings"] = _merge_defaults(
        dict(settings) if isinstance(settings, dict) else {}, DEFAULT_SETTINGS
    )
    rewards = data.get("rewards")
    result["rewards"] = _merge_defaults(
        dict(rewards) if isinstance(rewards, dict) else {}, DEFAULT_REWARDS
    )
    users = data.get("users")
    result["users"] = {str(k): dict(v) for k, v in users.items() if isinstance(v, dict)} if isinstance(users, dict) else {}
    products = data.get("products")
    result["products"] = [dict(item) for item in products if isinstance(item, dict)] if isinstance(products, list) else []
    redemptions = data.get("redemptions")
    result["redemptions"] = (
        [dict(item) for item in redemptions if isinstance(item, dict)] if isinstance(redemptions, list) else []
    )
    ledger = data.get("ledger")
    result["ledger"] = [dict(item) for item in ledger if isinstance(item, dict)] if isinstance(ledger, list) else []
    snapshot = data.get("config_snapshot")
    result["config_snapshot"] = dict(snapshot) if isinstance(snapshot, dict) else {}
    for user in result["users"].values():
        _merge_defaults(
            user,
            {
                "points": 0,
                "total_earned": 0,
                "total_spent": 0,
                "checkin_count": 0,
                "streak": 0,
                "last_checkin_date": "",
                "history": [],
                "nickname": "",
                "email": "",
                "updated_at": 0.0,
            },
        )
        if not isinstance(user.get("history"), list):
            user["history"] = []
    for product in result["products"]:
        _merge_defaults(
            product,
            {
                "id": "",
                "name": "",
                "cost": 0,
                "description": "",
                "category": "默认",
                "stock": None,
                "per_user_limit": None,
                "enabled": True,
                "image_path": "",
                "sort_order": 0,
                "created_at": 0.0,
                "redeemed": 0,
            },
        )
    # 只有默认数据才算真正的新库；显式返回，避免调用方误判。
    if not result["users"] and not result["products"] and not result["redemptions"]:
        result["version"] = fresh["version"]
    return result


class Store:
    """插件数据的读写入口。

    Args:
        directory: 数据目录，不存在时自动创建。
        filename: 数据文件名。
    """

    def __init__(self, directory: str, filename: str = STORE_FILE_NAME) -> None:
        self.directory = str(directory)
        self.path = os.path.join(self.directory, filename)
        self.render_dir = os.path.join(self.directory, "render")
        self.image_dir = os.path.join(self.directory, "products")
        self._data: dict[str, Any] | None = None
        self._lock: asyncio.Lock | None = None
        self._opened = False
        self._signature: tuple[float, int] | None = None

    # ------------------------------------------------------------------ 基础设施

    def _get_lock(self) -> asyncio.Lock:
        """懒创建写锁（避免在没有事件循环时构造 ``asyncio.Lock``）。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def ensure_directories(self) -> None:
        """创建数据目录、渲染目录和商品图片目录。"""
        for path in (self.directory, self.render_dir, self.image_dir):
            os.makedirs(path, exist_ok=True)

    async def open(self, seed: dict[str, Any] | None = None) -> dict[str, Any]:
        """加载数据文件，必要时用 ``seed`` 初始化。

        Args:
            seed: 首次创建数据文件时的种子内容（来自插件配置页）。

        Returns:
            加载后的数据字典。
        """
        async with self._get_lock():
            # 读文件 + JSON 解析（必要时还要初始化落盘）都是同步阻塞操作，
            # 放到线程里做，避免在事件循环里卡住其它会话。
            return await asyncio.to_thread(self._open_locked, seed)

    def _open_locked(self, seed: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ensure_directories()
        if self._data is not None:
            return self._data
        data: dict[str, Any] | None = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = migrate(json.load(handle))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                backup = f"{self.path}.corrupt.{int(time.time())}"
                try:
                    os.replace(self.path, backup)
                except OSError:
                    backup = self.path
                logger.error(
                    "[打卡商店] 数据文件无法解析（%s），已备份为 %s 并重建默认数据",
                    exc,
                    backup,
                )
                data = None
        if data is None:
            data = migrate(seed or {})
            logger.info("[打卡商店] 初始化数据文件 %s", self.path)
            self._write(data)
        self._data = data
        self._opened = True
        self._remember_signature()
        return self._data

    def _remember_signature(self) -> None:
        try:
            stat = os.stat(self.path)
            self._signature = (stat.st_mtime, stat.st_size)
        except OSError:
            self._signature = None

    def _write(self, data: dict[str, Any]) -> None:
        """原子写入。先写临时文件再 ``os.replace``。"""
        self.ensure_directories()
        tmp_path = f"{self.path}.tmp"
        merged = copy.deepcopy(data)
        merged["version"] = SCHEMA_VERSION
        merged["updated_at"] = time.time()
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)
        self._signature = None
        self._remember_signature()

    # ------------------------------------------------------------------ 读写

    async def data(self, seed: dict[str, Any] | None = None) -> dict[str, Any]:
        """读取内存中的数据（首次调用会加载文件）。"""
        if self._data is None:
            return await self.open(seed)
        return self._data

    async def mutate(
        self,
        handler: Callable[[dict[str, Any]], Any],
        seed: dict[str, Any] | None = None,
    ) -> Any:
        """在写锁内修改数据并原子落盘。

        Args:
            handler: 接收数据快照的函数；返回值为本次操作的业务结果。
                抛异常时数据保持原样。
            seed: 首次加载时的种子内容。

        Returns:
                ``handler`` 的返回值。

        Raises:
            Exception: 原样抛出 ``handler`` 的异常。
        """
        async with self._get_lock():
            # 这里必须用不再取锁的 _open_locked：asyncio.Lock 不可重入，
            # 调 open() 会二次抢同一把锁，在"数据还没加载"时永久卡死。
            data = self._open_locked(seed) if self._data is None else self._data
            snapshot = copy.deepcopy(data)
            result = handler(snapshot)
            self._warn_if_externally_changed()
            # 整库 JSON 序列化 + fsync 是同步阻塞操作（库越大越慢），在事件循环里会
            # 拖住同进程的其它会话；丢到线程里，写锁仍然只由本协程持有。
            await asyncio.to_thread(self._write, snapshot)
            self._data = snapshot
            return result

    def _warn_if_externally_changed(self) -> None:
        """检测数据文件是否被别的进程改过；只告警，不自动合并。"""
        if self._signature is None:
            return
        try:
            stat = os.stat(self.path)
        except OSError:
            return
        if (stat.st_mtime, stat.st_size) != self._signature:
            logger.warning(
                "[打卡商店] %s 在运行期间被外部修改，本次保存以内存数据为准；"
                "如需外部改动生效请重载插件",
                self.path,
            )

    async def flush(self) -> None:
        """把内存数据立即写回磁盘（用于后台批量操作）。"""
        if self._data is None:
            return
        async with self._get_lock():
            self._write(self._data)

    def replace_memory(self, data: dict[str, Any]) -> None:
        """直接替换内存数据（仅供测试与迁移使用）。"""
        self._data = migrate(data)

    def snapshot(self) -> dict[str, Any]:
        """同步读取当前内存数据的引用（只读用途，例如算收件地址）。

        Returns:
            内存数据；尚未加载时返回空字典。调用方不应修改返回值。
        """
        return self._data if isinstance(self._data, dict) else {}
