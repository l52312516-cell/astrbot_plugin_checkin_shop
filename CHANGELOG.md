# Changelog

## 1.0.0 - 2026-10-10

首个版本。

- 打卡：`/打卡`（别名 `/签到`），每天一次；积分模式支持固定积分、随机区间、自定义列表（可带权重），并支持每连续 N 天的连签奖励。`reset_hour` 可把"新的一天"推迟到指定小时。
- 商店：`/商店` 输出图片，含本人积分、`● 可兑换` 与 `○ 暂不可兑换` 两组，不可兑换时标出原因（`还差 N 分`、已售完、超出限购、已下架）。
- 个人与排行：`/我的积分`（别名 `/积分`）、`/积分榜`（别名 `/排行榜`），同样出图。
- 兑换：`/兑换商品 可乐`（别名 `/兑换 可乐`），商品名可以带空格（`/兑换商品 冰镇 可乐`）。机器人回复 `是否确认兑换「可乐」商品？…请引用本条消息回复 是/否`，用户引用该消息回复 `是`/`否` 后扣分扣库存并生成 `待发放` 记录。确认判定按"引用正文里的编号 → 记录的确认消息 ID → 引用发送者是机器人"三层兜底；确认时会重新校验价格、库存、限购与下架状态。五条指令全部使用标准 `CommandFilter`，因此都出现在 `/help` 与 WebUI 指令管理页中，可重命名、可禁用。
- 后台插件页（`pages/shop`）五个标签页：商品管理（增删改 + 上传图片 + 排序 + 预览商店图）、打卡规则、用户积分（手动调整并记流水）、兑换记录（标记已发放/已取消，取消可退回积分与库存）、展示设置。
- 渲染：本地 Pillow 出图，内置 Noto Sans CJK SC（OFL 1.1），自带 `render/` 缓存；渲染失败自动降级为纯文本回复。卡片不使用 emoji（等宽 CJK 字体无彩色 emoji 字形），分组标题用 `●`/`○` 加颜色区分。
- 存储：`data/plugin_data/astrbot_plugin_checkin_shop/shop.json` 单文件 JSON，整份快照 + 临时文件 + `os.replace` 原子写，配全局锁；文件损坏时改名备份后重建。`_conf_schema.json` 仅在首次初始化时提供默认值。
- 每个平台适配器设置项：`checkin_mode`、`checkin_fixed`、`checkin_random_min/max`、`checkin_custom_points/weights`、`streak_bonus_*`、`reset_hour`、`leaderboard_size/scope`、`points_scope`、`confirm_timeout_seconds`、`shop_title/subtitle`、`render_width/font_size_base/font_path`。
- 测试：`pytest` 覆盖时间逻辑、存储原子性与并发、打卡与兑换分支、Pillow 出图（含字体缺字检查）、以及用最小 AstrBot 桩跑通指令注册与"发起兑换 → 引用确认 → 扣分扣库存"闭环；`tests/test_page.cjs` 用假 bridge + 本机 Edge 驱动插件页并截图。
- 发布：`scripts/build_release.py` 生成可上传的 `astrbot_plugin_checkin_shop-v1.0.0.zip`（`metadata.yaml` 在包根目录），打包后自检元数据、schema、字体与缓存混入。
