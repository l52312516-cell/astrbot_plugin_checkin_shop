"""打卡积分商店插件的核心模块。

* :mod:`core.store`：JSON 原子存储
* :mod:`core.business`：打卡/积分/兑换业务逻辑（可脱离 AstrBot 单测）
* :mod:`core.render`：Pillow 图片渲染
* :mod:`core.dashboard_api`：WebUI 插件页后端接口
* :mod:`core.timeutil`：逻辑日期（支持自定义重置小时）
"""

from __future__ import annotations

__all__ = ["business", "dashboard_api", "render", "store", "timeutil"]
