"""聊天端的指令参数与展示文案，不接受客户端指定玩家身份。"""
from __future__ import annotations

from datetime import date
import re

from .items import integer
from .timeutil import format_moment


def _parts(section: str, text: str) -> list[str]:
    """读取命令参数，兼容不同 AstrBot 版本传入的完整命令文本。

    正常情况下 CommandFilter 会把 ``积分通`` 从参数中去掉；部分适配器/旧版
    GreedyStr 会把完整的 ``+积分通 存入 200`` 或 ``积分通 存入 200`` 传进来，
    之前这会把命令名当成第一个参数而返回用法错误。
    """
    parts = str(text or "").strip().split()
    if not parts:
        return []
    first = parts[0]
    # 去掉唤醒前缀（+、/、@ 等非字母/数字/CJK字符）后，若正好是当前分区名，
    # 只消费这一段；其余参数保持原样，避免改动竞猜选项文本。
    bare = re.sub(r"^[^\w\u4e00-\u9fff]*", "", first)
    if bare == section:
        return parts[1:]
    return parts


def parse(section: str, text: str) -> tuple[str, dict] | None:
    parts = _parts(section, text)
    if section == "股市" and parts and parts[0] in ("买入", "卖出"):
        if len(parts) != 3:
            raise ValueError("用法：股市 买入/卖出 ST01 股数")
        return ("stock_buy" if parts[0] == "买入" else "stock_sell", {"code": parts[1].upper(), "quantity": integer(parts[2], "股数", 1)})
    if section == "积分通" and parts:
        if len(parts) != 2 or parts[0] not in ("存入", "取出"):
            raise ValueError("用法：积分通 存入/取出 积分数；不带参数查看账户")
        return ("savings_deposit" if parts[0] == "存入" else "savings_withdraw", {"amount": integer(parts[1], "积分数", 1)})
    if section == "竞猜" and parts and parts[0] == "下注":
        if len(parts) < 4:
            raise ValueError("用法：竞猜 下注 题号 选项 积分数")
        return "prediction_bet", {"prediction_id": parts[1], "option": " ".join(parts[2:-1]), "amount": integer(parts[-1], "投入积分", 1)}
    if section == "刮刮乐" and parts:
        if parts != ["购买"]:
            raise ValueError("用法：刮刮乐 购买；不带参数查看奖表")
        return "lottery_buy", {}
    if section == "股市" and parts and parts[0] not in ("行情", "持仓"):
        raise ValueError("用法：股市 行情/持仓，或 股市 买入/卖出 ST01 股数")
    if section == "股市" and len(parts) > 1:
        raise ValueError("查看行情或持仓时无需其他参数")
    return None


def display(section: str, text: str, overview: dict, assets: dict, prefix: str) -> str:
    settings = overview["settings"]
    text = " ".join(_parts(section, text))
    if section == "股市":
        if text.strip() == "持仓":
            lines = ["我的股票持仓"]
            for p in assets["positions"]:
                if p["quantity"]:
                    lines.append(f"{p['code']} {p['name']} · {p['quantity']} 股 · 市值 {p['market_value']} 分 · 本金 {p['cost_total']} 分")
            if len(lines) == 1:
                lines.append("暂无持仓")
        else:
            lines = ["虚构股市" + ("（买入未开启）" if not settings["stocks_enabled"] else "")]
            for s in overview["stocks"]:
                extra = f" · 手续费 {s['fee_bps']/100:g}%" if s.get('fee_bps') is not None else ""
                if s.get('buy_enabled') is False:
                    extra += " · 暂停买入，可卖出"
                lines.append(f"{s['code']} {s['name']} · {s['price_text']} 分/股 · 日涨跌 {s['change_bps']/100:+.2f}%{extra}")
            if overview["stocks"]:
                lines.append("报价时间：" + (format_moment(overview['stocks'][0].get('last_updated')) or "尚未生成行情"))
        example_code = overview["stocks"][0]["code"] if overview.get("stocks") else "股票代码"
        lines += [f"全局默认手续费 {settings['stock_fee_bps']/100:g}%（至少1分）；单股独立费率以行情标注和确认报价为准", f"每日最多 {settings['stock_daily_trades']} 笔，买入含费最多 {settings['stock_daily_buy_limit']} 分", f"{prefix}股市 买入 {example_code} 1", f"{prefix}股市 卖出 {example_code} 1 / {prefix}股市 持仓"]
        return "\n".join(lines)
    if section == "积分通":
        account = assets["savings"]
        next_interest = format_moment(account.get('next_settlement_at')) if account.get('next_settlement_at') else "暂无预计收益"
        return "\n".join(["活期积分通" + ("（暂停新存入）" if not settings['savings_enabled'] else ""), f"可用积分 {assets['wallet']} · 本金 {account['principal']}", f"累计利息 {account['interest_earned']} 分 · 日利率 {account['rate_bps']/100:g}%", f"预计下次整积分收益：{next_interest}", f"本金上限 {account['principal_cap']} 分；按持有时间计息，不足1分累计，利息不复存", f"{prefix}积分通 存入 100", f"{prefix}积分通 取出 100"])
    if section == "竞猜":
        target = text.strip().removeprefix("详情").strip()
        if target == "列表":
            target = ""
        rows = overview["predictions"]
        if target:
            rows = [p for p in rows if p['id'] == target]
            if not rows:
                raise ValueError("竞猜题号不存在")
        else:
            rows = [p for p in rows if p['status'] in ("open", "closed")][:8]
        lines = ["积分竞猜" + ("（暂停新参与）" if not settings['predictions_enabled'] else "")]
        for p in rows:
            status = {"open": "下注中", "closed": "已封盘", "resolved": "已结算", "cancelled": "已作废返本", "void": "无人押中返本"}.get(p['status'], p['status'])
            lines += [f"{p['id']} · {p['title']}（{status}）", "选项：" + " / ".join(p['options']), f"截止：{format_moment(p['close_at'])} 北京时间 · 奖池 {p['total_stake']} 分"]
            if p.get('winner'):
                lines.append("结果：" + p['winner'])
        if not rows:
            lines.append("暂无可参与竞猜")
        lines += [f"每局一次，投入 {settings['prediction_min']}～{settings['prediction_max']} 分，每日合计最多 {settings['prediction_daily_limit']} 分", f"奖池的 {settings['prediction_payout_bps']/100:g}% 按中奖投入分配；回报随奖池变化，不保证固定赔率", f"{prefix}竞猜 下注 题号 选项 10"]
        return "\n".join(lines)
    if section == "刮刮乐":
        total = sum(p['weight'] for p in settings['lottery_prizes'])
        lines = ["积分刮刮乐" + ("（尚未开启）" if not settings['lottery_enabled'] else ""), f"每张 {settings['lottery_cost']} 分，每日最多 {settings['lottery_daily_limit']} 张"]
        lines += [f"{p['points']} 分 · 概率 {p['weight']*100/total:g}%" for p in settings['lottery_prizes']]
        lines += [f"今日已购买 {assets['daily_usage']['lottery_count']} 张", f"{prefix}刮刮乐 购买（确认后开奖）"]
        return "\n".join(lines)
    cards = assets['cards']
    boost = cards.get('boost')
    lines = ["我的道具"]
    if boost:
        lines += [f"签到基础 {boost['multiplier_bps']/10000:g} 倍，每日额外最多 {boost['daily_bonus_cap']} 分", f"生效 {boost['start_date']}；{boost['end_date']} 重置时结束（以北京时间为准）"]
        day = assets.get('business_date')
        if day:
            status = "待生效" if day < boost['start_date'] else "生效中" if day < boost['end_date'] else "已过期"
            remaining = max(0, (date.fromisoformat(boost['end_date']) - date.fromisoformat(max(day, boost['start_date']))).days)
            lines.append(f"{status} · 剩余有效业务日 {remaining} 天（漏签不顺延）")
    else:
        lines.append("暂无签到增益卡")
    until = cards.get('rebate_cooldown_until', 0)
    lines.append("返利卡冷却结束：" + format_moment(until) if until else "暂无余额返利卡冷却")
    lines.append("通过商店兑换道具，自动生效；同类道具共享每日限额")
    return "\n".join(lines)
