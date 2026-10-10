# 内置字体说明

本目录内的 `NotoSansCJKsc-Regular.otf` 是 **Noto Sans CJK SC Regular**（未做任何修改）的副本，
随插件一起分发，仅用于把打卡 / 积分 / 商店卡片渲染成图片。

- 版权：Copyright © 2014–2021 Adobe (<http://www.adobe.com/>)
- 来源：<https://github.com/notofonts/noto-cjk/tree/main/Sans/OTF/SimplifiedChinese>
- 许可：SIL Open Font License 1.1，全文见同目录 `OFL.txt`
- 该字体不单独售卖，也不额外收费。

如果不需要内置字体：删掉 `NotoSansCJKsc-Regular.otf`，并在插件配置页把 `render_font_path`
指向系统里的任一中文字体（例如 `C:\Windows\Fonts\msyh.ttc`），或把字体放到
`resources/font.otf`。字体缺失时指令会自动降级为纯文本。
