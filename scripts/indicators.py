#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MACD / KDJ 指标计算（基于 etf_data.json 的走势序列）

背景
----
走势接口只返回逐日 `daily_return`（小数，0.0125 = +1.25%），**不含 OHLC**。
因此本脚本：

1. 用收益率链**还原收盘价序列**：以最新真实价（实时 `current`，缺失则用 1.0）为锚点，
   向前逐日反推 C_{t-1} = C_t / (1 + r_t)。
2. **自动剔除假期填充日**（同一份数据被复制到多个非交易日）。
   判定方法：把某日全部标的的收益率取 6 位小数排序后做指纹，
   指纹与之前某日完全相同 → 该日为填充日。国庆 10-01~10-07 实测必然命中。
3. 计算 MACD(12,26,9) 与 KDJ(9,3,3)。

口径说明（务必在报告中披露）
--------------------------
- **MACD**：EMA 是价格的线性变换，价格序列整体缩放不影响 DIF/DEA/柱的**符号与零轴穿越**。
  还原序列锚定在真实收盘价上，所以数值量级也接近真实值，**可直接用于判断绿柱/红柱**。
- **KDJ**：标准算法需要最高价 H、最低价 L。本脚本默认只有收盘价 C，
  故 RSV 用 `(C - LLV(C,9)) / (HHV(C,9) - LLV(C,9)) * 100` **以收盘价替代 H/L**。

  ⚠️ **偏差方向（2026-10-20 实测，与直觉相反）**：收盘价近似会把 K 推向 0 / 100 两个极端，
  **是「放大」而不是「平滑」**。实测三组：
  - 单边上涨 30 日：近似 K=100.0，真实 K=88.0（高估 12）
  - 单边下跌 30 日：近似 K=0.0，真实 K=11.0（低估 11）
  - 区间震荡 30 日：近似 K85.1 / J121.7，真实 K77.4 / J107.0（J 高估 14.7，会误判超标）

  因此对「K<80、J<110」这类**上限**筛选：
  **强势/震荡品种会被系统性高估 → 误杀；弱势品种会被系统性低估 → 误放。**
  结论：**没有真实 OHLC 时，KDJ 只能用于相对排序，不能用于阈值判定。**
  要判定必须用 `--ohlc` 传入真实开高低收。

  ✅ **真实 OHLC 现在可以自取**，不用再找用户要：
     python fetch_ohlc.py --from-data etf_data.json --top 400 --csv ohlc.csv
     python indicators.py --in etf_data.json --screen --ohlc ohlc.csv
  2026-10-20 全市场 271 只实测：近似与真实判定分歧 18 只（6.6%），
  其中近似误放 16 只、误杀 2 只 —— 所以**不能跳过这一步**。

用法
----
    python indicators.py --in etf_data.json --symbols SH512800,SZ159529
    python indicators.py --in etf_data.json --screen          # 全市场筛选
    python indicators.py --in etf_data.json --screen --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------- 数据准备

def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_fill_dates(data: list[dict]) -> set[str]:
    """识别假期填充日：不同交易日的收益率分布指纹完全相同。"""
    bydate: dict[str, list[float]] = {}
    for x in data:
        for t in (x.get("trend") or []):
            v = t.get("daily_return")
            if v is not None:
                bydate.setdefault(t["trade_date"], []).append(round(v, 6))

    seen: dict[tuple, str] = {}
    fills: set[str] = set()
    for dt in sorted(bydate):
        v = bydate[dt]
        if len(v) < 50:            # 样本太少的日期不参与判定
            continue
        fp = (len(v), tuple(sorted(v)))
        if fp in seen:
            fills.add(dt)          # 与更早某日完全相同 → 复制填充
        else:
            seen[fp] = dt
    return fills


def load_live_fallback(cache_dir: str, day: str) -> dict[str, dict]:
    """从当日快照缓存里补齐缺失的实时价。

    为什么需要：`no_live_quote` 的标的没有 `current`，锚点会退化成 1.0，
    MACD 数值量级就和别的标的不可比。当日缓存 `cache/live_YYYY-MM-DD.json`
    按天累积多个 snapshot，取**最后一个含该 symbol 的 snapshot** 即可补齐。
    """
    p = Path(cache_dir) / f"live_{day}.json"
    if not p.exists():
        return {}
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for snap in c.get("snapshots") or []:
        for row in snap.get("rows") or []:
            s = row.get("symbol")
            if s and row.get("current") is not None:
                out[s] = row          # 后面的 snapshot 覆盖前面的
    return out


def load_ohlc(path: str) -> dict[str, list[tuple[str, float, float, float, float]]]:
    """读取用户提供的 OHLC CSV。

    表头不区分大小写，可识别的列名：
      symbol/code/代码, date/日期, open/开盘, high/最高, low/最低, close/收盘
    返回 {symbol: [(date, open, high, low, close), ...]}（按日期升序）。
    """
    import csv
    alias = {
        "symbol": "symbol", "code": "symbol", "代码": "symbol", "基金代码": "symbol",
        "date": "date", "日期": "date", "trade_date": "date",
        "open": "open", "开盘": "open", "开盘价": "open",
        "high": "high", "最高": "high", "最高价": "high",
        "low": "low", "最低": "low", "最低价": "low",
        "close": "close", "收盘": "close", "收盘价": "close",
    }
    out: dict[str, list] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames:
            return out
        cols = {}
        for raw in rd.fieldnames:
            key = alias.get(raw.strip().lower()) or alias.get(raw.strip())
            if key:
                cols[key] = raw
        need = {"symbol", "date", "open", "high", "low", "close"}
        if not need.issubset(cols):
            raise SystemExit(f"CSV 缺列：{sorted(need - set(cols))}；实际表头 {rd.fieldnames}")
        for row in rd:
            try:
                sym = str(row[cols["symbol"]]).strip().upper()
                d = str(row[cols["date"]]).strip().replace("/", "-")
                if len(d) == 8 and d.isdigit():
                    d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                o, h, l, c = (float(row[cols[k]]) for k in ("open", "high", "low", "close"))
            except (TypeError, ValueError, KeyError):
                continue
            out.setdefault(sym, []).append((d, o, h, l, c))
    for s in out:
        out[s].sort(key=lambda r: r[0])
    return out


def close_series(item: dict, fills: set[str], today_pct: float | None) -> list[tuple[str, float]]:
    """还原收盘价序列。today_pct 为今日实时涨跌幅（%），追加为最后一根。"""
    rows = [(t["trade_date"], t["daily_return"])
            for t in (item.get("trend") or [])
            if t.get("daily_return") is not None and t["trade_date"] not in fills]
    rows.sort(key=lambda r: r[0])
    if not rows:
        return []

    # 锚点：今日实时价优先，否则用序列末值归一化
    if today_pct is not None:
        rows = rows + [("TODAY", today_pct / 100.0)]
        anchor = item.get("current") or 1.0
    else:
        anchor = item.get("current") or 1.0

    # 从后往前反推
    closes: list[float] = []
    c = float(anchor)
    for dt, r in reversed(rows):
        closes.append(c)
        c = c / (1.0 + r)
    closes.reverse()
    dates = [r[0] for r in rows]
    return list(zip(dates, closes))


# ---------------------------------------------------------------- 指标

def ema(vals: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out: list[float] = []
    prev = None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(closes: list[float], fast=12, slow=26, signal=9) -> dict:
    if len(closes) < slow + signal:
        return {"ok": False, "reason": f"样本不足（{len(closes)} < {slow + signal}）"}
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    bar = [2 * (a - b) for a, b in zip(dif, dea)]
    return {
        "ok": True,
        "dif": dif[-1], "dea": dea[-1], "bar": bar[-1],
        "dif_prev": dif[-2], "dea_prev": dea[-2], "bar_prev": bar[-2],
        "bar_series": bar[-5:],
        "dif_series": dif[-5:],
    }


def kdj(closes: list[float], highs: list[float] | None = None,
        lows: list[float] | None = None, n=9, k_n=3, d_n=3) -> dict:
    """KDJ(9,3,3)。

    给了 highs / lows 时走**标准算法**（RSV 用真实 H/L）；
    没给则退化为收盘价近似（RSV 用 C 替代 H/L），此时结果偏平滑、偏保守。
    """
    if len(closes) < n + 3:
        return {"ok": False, "reason": f"样本不足（{len(closes)} < {n + 3}）"}
    if highs is not None and lows is not None:
        if len(highs) != len(closes) or len(lows) != len(closes):
            return {"ok": False, "reason": "H/L 与 C 长度不一致"}
        exact = True
    else:
        highs = lows = closes
        exact = False

    k, d = 50.0, 50.0
    K = D = J = None
    ks, js = [], []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1):i + 1])
        hi = max(highs[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0
        k = (k_n - 1) / k_n * k + 1 / k_n * rsv
        d = (d_n - 1) / d_n * d + 1 / d_n * k
        j = 3 * k - 2 * d
        K, D, J = k, d, j
        ks.append(k)
        js.append(j)
    return {"ok": True, "k": K, "d": D, "j": J,
            "k_prev": ks[-2], "j_prev": js[-2], "exact": exact}


# ---------------------------------------------------------------- 判定

def judge(m: dict, kd: dict) -> dict:
    """用户口径：MACD 绿柱转红契机 + K<80 且 J<110。"""
    if not m.get("ok") or not kd.get("ok"):
        return {"pass": False, "why": "指标不可用"}

    bar, bar_prev = m["bar"], m["bar_prev"]
    dif, dea = m["dif"], m["dea"]

    green = bar < 0                      # 绿柱
    shrinking = green and bar > bar_prev  # 绿柱缩短
    # 连续缩短天数
    s = m["bar_series"]
    streak = 0
    for i in range(len(s) - 1, 0, -1):
        if s[i] < 0 and s[i] > s[i - 1]:
            streak += 1
        else:
            break
    red = bar > 0 and bar_prev <= 0      # 今日刚翻红
    k_ok = kd["k"] < 80
    j_ok = kd["j"] < 110

    if red:
        stage = "今日刚翻红"
    elif green and not shrinking:
        stage = "绿柱仍在扩大"
    elif shrinking:
        stage = f"绿柱缩短第 {streak} 日"
    else:
        stage = "红柱"

    return {
        "pass": bool((red or shrinking) and k_ok and j_ok),
        "stage": stage,
        "green": green, "shrinking": shrinking, "red": red,
        "streak": streak,
        "dif": dif, "dea": dea, "bar": bar, "bar_prev": bar_prev,
        "gap": dif - dea,
        "k": kd["k"], "d": kd["d"], "j": kd["j"],
        "k_ok": k_ok, "j_ok": j_ok,
    }


def fmt(v, nd=3):
    return "—" if v is None else f"{v:+.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--screen", action="store_true")
    ap.add_argument("--min-amount", type=float, default=0.5, help="筛选时最小成交额（亿）")
    ap.add_argument("--json", default="")
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--day", default="")
    ap.add_argument("--ohlc", default="", help="用户提供的 OHLC CSV，给了就走标准 KDJ 算法")
    args = ap.parse_args()

    d = load(args.inp)
    data = d.get("data") or []
    fills = find_fill_dates(data)
    print(f"# 填充日剔除: {sorted(fills) if fills else '无'}", file=sys.stderr)

    # 用当日快照缓存补齐 no_live_quote 标的的实时价
    cache_dir = args.cache_dir or str(Path(args.inp).resolve().parent / "cache")
    day = args.day or (d.get("live_snapshot") and None) or ""
    if not day:
        from datetime import datetime
        day = datetime.now().strftime("%Y-%m-%d")
    live = load_live_fallback(cache_dir, day)
    if live:
        patched = 0
        for x in data:
            if x.get("current") is None and x["symbol"] in live:
                r = live[x["symbol"]]
                x["current"] = r.get("current")
                if x.get("no_live_quote") and r.get("percent") is not None:
                    x["percent"] = r["percent"]
                    x["_pct_from_cache"] = True
                patched += 1
        print(f"# 快照补齐 {patched} 只（day={day}）", file=sys.stderr)

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.screen:
        pool = [x for x in data
                if x.get("percent") is not None
                and (x.get("amount") or 0) / 1e8 >= args.min_amount]
    else:
        m = {x["symbol"]: x for x in data}
        pool = [m[s] for s in syms if s in m]

    out = []
    ohlc = load_ohlc(args.ohlc) if args.ohlc else {}
    if ohlc:
        print(f"# OHLC 覆盖 {len(ohlc)} 只（{args.ohlc}）", file=sys.stderr)

    for x in pool:
        # no_live_quote 且缓存也没补到 → percent 是上一交易日的值，不能当作今日追加
        if x.get("no_live_quote") and not x.get("_pct_from_cache"):
            pct = None
        else:
            pct = x.get("percent")

        bars = ohlc.get(x["symbol"])
        if bars:
            # ---- 有真实 OHLC：MACD 用真实收盘，KDJ 用真实 H/L/C ----
            dates = [b[0] for b in bars]
            closes = [b[4] for b in bars]
            highs = [b[2] for b in bars]
            lows = [b[3] for b in bars]
            # 若 OHLC 未含今日且今日有实时价，补一根 H=L=C=现价 的临时 K 线
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            if dates and dates[-1] < today and x.get("current"):
                c0 = float(x["current"])
                closes = closes + [c0]
                highs = highs + [c0]
                lows = lows + [c0]
                dates = dates + [today]
            src = "ohlc"
        else:
            cs = close_series(x, fills, pct)
            closes = [c for _, c in cs]
            highs = lows = None
            src = "rebuilt"

        if len(closes) < 35:
            continue
        m = macd(closes)
        kd = kdj(closes, highs, lows)
        r = judge(m, kd)
        r.update({
            "symbol": x["symbol"], "name": x.get("name", ""),
            "today": pct, "bars": len(closes), "src": src,
            "kdj_exact": bool(kd.get("exact")),
            "amount": (x.get("amount") or 0) / 1e8,
            "premium": x.get("premium_rate"),
        })
        out.append(r)

    out.sort(key=lambda r: (-r.get("pass", False), -(r.get("streak") or 0), r.get("bar") or 0))

    hdr = f"{'代码':<10}{'名称':<24}{'今日':>7}{'DIF':>8}{'DEA':>8}{'柱':>8}{'柱变':>7}{'K':>6}{'D':>6}{'J':>6}  状态"
    print(hdr)
    print("-" * len(hdr))
    for r in out:
        mark = "★" if r.get("pass") else " "
        t = r["today"]
        ts = f"{t:+.2f}" if t is not None else "   —"
        print(f"{r['symbol']:<10}{r['name'][:22]:<24}"
              f"{ts:>7}"
              f"{fmt(r.get('dif')):>8}{fmt(r.get('dea')):>8}{fmt(r.get('bar')):>8}"
              f"{fmt((r.get('bar') or 0) - (r.get('bar_prev') or 0)):>7}"
              f"{(r.get('k') or 0):>6.1f}{(r.get('d') or 0):>6.1f}{(r.get('j') or 0):>6.1f}"
              f"  {mark}{r.get('stage','')}"
              f"{'' if r.get('kdj_exact') else '  [KDJ≈]'}")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
