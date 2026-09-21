#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把采集到的 ETF 数据 + 持仓输入, 渲染成最终的分析提示词。

用法:
    python build_prompt.py --data out.json --portfolio portfolio.json --out prompt.md
    python build_prompt.py --data out.json --portfolio "512880:20000:1.05,510300:5000:3.9"

持仓格式 (portfolio.json):
    {
      "notes": "我关注医药和科技方向",
      "holdings": [
        {"symbol": "SH512880", "name": "证券ETF", "shares": 20000, "cost": 1.05}
      ],
      "watchlist": ["SH510300", "SZ159915"]
    }

也支持 --portfolio "代码:份额:成本,代码:份额:成本" 的简写形式。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TEMPLATE_NAME = "prompt_template.md"


def find_template() -> Path:
    """优先用户覆盖目录, 其次技能自带模板。"""
    candidates = [
        Path.home() / ".workbuddy-ai" / "etf-plugin" / TEMPLATE_NAME,
        Path(__file__).resolve().parent.parent / "prompts" / TEMPLATE_NAME,
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("找不到提示词模板 prompt_template.md")


def fmt_num(v, digits=2, suffix="", na="—"):
    if v is None:
        return na
    if isinstance(v, (int, float)):
        return f"{v:,.{digits}f}{suffix}"
    return str(v)


def fmt_amount(v, na="—"):
    if not isinstance(v, (int, float)):
        return na
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.0f}"


def table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_无数据_"
    header = "| " + " | ".join(title for _, title in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [header, sep]
    for r in rows:
        cells = []
        for key, _ in cols:
            v = r.get(key)
            if key == "amount":
                cells.append(fmt_amount(v))
            elif key in ("percent", "premium_rate", "month_return",
                         "momentum_5d", "volatility", "max_daily_drop",
                         "up_day_ratio"):
                cells.append(fmt_num(v, 2, "%" if v is not None else ""))
            elif key == "current":
                cells.append(fmt_num(v, 3))
            else:
                cells.append("—" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def parse_portfolio(arg: str) -> dict:
    """支持 JSON 文件路径 或 简写字符串。"""
    p = Path(arg)
    if p.is_file():
        text = p.read_text(encoding="utf-8")
        # 文件内容是 JSON 就按 JSON 解析, 否则按简写字符串解析
        if text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        arg = text.strip()
    if arg.lstrip().startswith("{"):
        return json.loads(arg)
    if ":" not in arg:
        raise ValueError(f"无法解析持仓参数: {arg!r}")

    holdings = []
    for chunk in arg.split(","):
        parts = chunk.strip().split(":")
        if len(parts) < 2:
            continue
        sym = parts[0].strip()
        try:
            shares = float(parts[1])
        except ValueError:
            continue
        cost = None
        if len(parts) >= 3:
            try:
                cost = float(parts[2])
            except ValueError:
                cost = None
        holdings.append({"symbol": sym, "shares": shares, "cost": cost})
    return {"holdings": holdings, "watchlist": [], "notes": ""}


def build_intraday_section(payload: dict) -> str:
    """把当日多次快照的对比结果渲染成可读文本。"""
    diff = payload.get("intraday_diff")
    snaps = payload.get("today_snapshots") or []

    if not diff:
        if snaps:
            return (
                "_当日仅有 %d 次采集 (最新 %s), 尚无对比基准。"
                "稍后再次运行本技能即可自动生成日内变化对比。_"
                % (len(snaps), snaps[-1].get("time", "—"))
            )
        return "_无日内对比数据_"

    f, t = diff.get("first", {}), diff.get("last", {})

    # 过滤掉没有实际变化的品种 —— 全 0 的表格是噪音
    def movers(items, positive=True, eps=0.01):
        out = [
            x
            for x in (items or [])
            if abs(x.get("delta_percent") or 0) >= eps
        ]
        return out

    climbing = movers(diff.get("climbing"), positive=True)
    falling = movers(diff.get("falling"), positive=False)

    header = (
        f"对比区间: 第 {f.get('seq')} 次 ({f.get('time')}) → "
        f"第 {t.get('seq')} 次 ({t.get('time')}), 覆盖 {diff.get('compared')} 只 ETF"
    )

    if not climbing and not falling:
        return (
            f"{header}\n\n"
            "_两次采集间所有品种涨幅均无实质变化 (变化幅度 < 0.01%)，"
            "可能是非交易时段或采集间隔过短，暂无日内轮动信号。_"
        )

    lines = [header, ""]
    if climbing:
        lines += [
            "**日内相对走强** (涨幅较首次采集扩大最多)",
            "",
            "| 代码 | 名称 | 首次涨幅 | 最新涨幅 | 变化 | 价格变化 |",
            "|---|---|---|---|---|---|",
        ]
        for x in climbing:
            lines.append(
                "| {} | {} | {:+.2f}% | {:+.2f}% | {:+.2f}% | {:+.2f}% |".format(
                    x.get("symbol", ""),
                    x.get("name", ""),
                    x.get("percent_first", 0),
                    x.get("percent_last", 0),
                    x.get("delta_percent", 0),
                    x.get("delta_price_pct", 0),
                )
            )
        lines.append("")

    if falling:
        lines += [
            "**日内相对走弱** (涨幅回落最多)",
            "",
            "| 代码 | 名称 | 首次涨幅 | 最新涨幅 | 变化 | 价格变化 |",
            "|---|---|---|---|---|---|",
        ]
        for x in falling:
            lines.append(
                "| {} | {} | {:+.2f}% | {:+.2f}% | {:+.2f}% | {:+.2f}% |".format(
                    x.get("symbol", ""),
                    x.get("name", ""),
                    x.get("percent_first", 0),
                    x.get("percent_last", 0),
                    x.get("delta_percent", 0),
                    x.get("delta_price_pct", 0),
                )
            )
        lines.append("")

    if snaps:
        lines.append(
            "当日采集记录: " + ", ".join(f"#{s.get('seq')}@{s.get('time')}" for s in snaps)
        )
    return "\n".join(lines)


def build_portfolio_section(pf: dict, data_index: dict) -> str:
    holdings = pf.get("holdings") or []
    watchlist = pf.get("watchlist") or []
    notes = pf.get("notes") or ""

    if not holdings and not watchlist:
        return "_用户未提供持仓或关注列表_"

    out = []
    if holdings:
        out.append("**持仓明细**\n")
        out.append("| 代码 | 名称 | 持有份额 | 成本价 | 现价 | 浮动盈亏 | 当日涨幅 | 溢价率 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for h in holdings:
            sym = h.get("symbol", "")
            info = data_index.get(sym, {})
            name = info.get("name") or h.get("name") or "—"
            cur = info.get("current")
            cost = h.get("cost")
            pnl = "—"
            if isinstance(cur, (int, float)) and isinstance(cost, (int, float)) and cost:
                pnl = f"{(cur - cost) / cost * 100:.2f}%"
            out.append(
                "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    sym,
                    name,
                    fmt_num(h.get("shares"), 0),
                    fmt_num(cost, 3),
                    fmt_num(cur, 3),
                    pnl,
                    fmt_num(info.get("percent"), 2, "%" if info.get("percent") is not None else ""),
                    fmt_num(info.get("premium_rate"), 2, "%" if info.get("premium_rate") is not None else ""),
                )
            )
    if watchlist:
        out.append("\n**关注列表**: " + ", ".join(watchlist))
        for sym in watchlist:
            info = data_index.get(sym)
            if info:
                out.append(
                    "- {} {}: 现价 {}, 当日 {}%, 溢价率 {}%, 月内 {}%".format(
                        sym,
                        info.get("name", ""),
                        fmt_num(info.get("current"), 3),
                        fmt_num(info.get("percent"), 2),
                        fmt_num(info.get("premium_rate"), 2),
                        fmt_num(info.get("month_return"), 2),
                    )
                )
    if notes:
        out.append(f"\n**用户补充说明**: {notes}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染 ETF 分析提示词")
    ap.add_argument("--data", required=True, help="fetch_etf.py 产出的 JSON")
    ap.add_argument("--portfolio", default="", help="持仓 JSON 文件或简写字符串")
    ap.add_argument("--notes", default="", help="用户手动输入的附加说明")
    ap.add_argument("--out", required=True, help="输出 markdown 路径")
    args = ap.parse_args()

    payload = json.loads(Path(args.data).read_text(encoding="utf-8"))
    rows = payload.get("data") or []
    data_index = {r.get("symbol"): r for r in rows if r.get("symbol")}

    # --- 源状态 ---
    src = payload.get("sources", {})
    status_lines = []
    for k, label in (("live", "实时行情"), ("month", "月度走势")):
        s = src.get(k)
        if not s:
            continue
        if s.get("ok"):
            status_lines.append(f"- {label}: ✅ 正常, {s.get('count')} 条")
        else:
            status_lines.append(f"- {label}: ❌ 失败 — {s.get('error')}")
    for w in payload.get("warnings") or []:
        status_lines.append(f"- ⚠️ {w}")
    source_status = "\n".join(status_lines) or "_无数据源信息_"

    # --- 各类榜单 ---
    # 剔除货币/债券类极低波动品种来算涨幅榜? 保留原样, 但按成交额过滤
    liquid = [r for r in rows if (r.get("amount") or 0) >= 5e7]
    gainers = sorted(
        [r for r in liquid if r.get("percent") is not None],
        key=lambda x: x["percent"],
        reverse=True,
    )[:10]
    losers = sorted(
        [r for r in liquid if r.get("percent") is not None],
        key=lambda x: x["percent"],
    )[:10]
    by_amount = sorted(
        rows, key=lambda x: x.get("amount") or 0, reverse=True
    )[:10]

    cols_basic = [
        ("symbol", "代码"),
        ("name", "名称"),
        ("current", "现价"),
        ("percent", "涨幅"),
        ("amount", "成交额"),
        ("premium_rate", "溢价率"),
        ("volatility", "波动率"),
    ]

    # 月度榜单独用一套列, 突出月内累计涨幅与动量
    cols_month = [
        ("symbol", "代码"),
        ("name", "名称"),
        ("current", "现价"),
        ("month_return", "月内涨幅"),
        ("momentum_5d", "5日动量"),
        ("volatility", "波动率"),
        ("amount", "成交额"),
        ("premium_rate", "溢价率"),
    ]

    # 月度榜只看可交易的品种 (流动性够)
    with_month = [r for r in rows if r.get("month_return") is not None]
    month_leaders = sorted(
        with_month, key=lambda x: x["month_return"], reverse=True
    )[:10]
    month_laggards = sorted(with_month, key=lambda x: x["month_return"])[:10]

    # --- 市场概览 ---
    total = len(rows)
    up = sum(1 for r in rows if (r.get("percent") or 0) > 0)
    down = sum(1 for r in rows if (r.get("percent") or 0) < 0)
    flat = total - up - down
    avg_pct = (
        sum(r["percent"] for r in rows if r.get("percent") is not None) / total
        if total
        else 0
    )
    total_amt = sum(r.get("amount") or 0 for r in rows)
    overview = (
        f"- ETF 总数: {total}\n"
        f"- 上涨: {up} 只 / 下跌: {down} 只 / 平盘: {flat} 只\n"
        f"- 平均涨跌幅: {avg_pct:.2f}%\n"
        f"- 总成交额: {fmt_amount(total_amt)}\n"
        f"- 数据月份: {payload.get('month', '—')}\n"
        f"- 采集时间: {payload.get('generated_at', '—')}"
    )

    # --- 持仓 ---
    pf = parse_portfolio(args.portfolio) if args.portfolio else {}
    if args.notes:
        pf["notes"] = (pf.get("notes") or "") + (" " + args.notes if pf.get("notes") else args.notes)
    portfolio_section = build_portfolio_section(pf, data_index)

    template = find_template().read_text(encoding="utf-8")
    rendered = (
        template.replace("{{SOURCE_STATUS}}", source_status)
        .replace("{{MARKET_OVERVIEW}}", overview)
        .replace("{{TOP_GAINERS}}", table(gainers, cols_basic))
        .replace("{{TOP_LOSERS}}", table(losers, cols_basic))
        .replace("{{TOP_BY_AMOUNT}}", table(by_amount, cols_basic))
        .replace("{{MONTH_LEADERS}}", table(month_leaders, cols_month))
        .replace("{{MONTH_LAGGARDS}}", table(month_laggards, cols_month))
        .replace("{{USER_PORTFOLIO}}", portfolio_section)
        .replace("{{INTRADAY_DIFF}}", build_intraday_section(payload))
        .replace("{{USER_NOTES}}", args.notes or "_无_")
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
