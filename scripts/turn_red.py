#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MACD 翻红预警 + KDJ 综合评分

核心能力：精确算出「明天收盘价要涨到多少，MACD 柱才会由绿转红」。

不是用柱值斜率外推（那样误差大），而是直接解方程：
    设明日收盘价为 P，则
      e12' = (1-a)·e12 + a·P ,  a = 2/13
      e26' = (1-b)·e26 + b·P ,  b = 2/27
      DIF' = (1-a)e12 - (1-b)e26 + (a-b)·P
      DEA' = 0.8·DEA + 0.2·DIF'          (n=9 → alpha=2/10)
      bar' = 2(DIF' - DEA') = 1.6(DIF' - DEA)
    bar' = 0  ⟺  DIF' = DEA
    ⟹  P* = (DEA - (1-a)e12 + (1-b)e26) / (a - b)

P*/C - 1 就是「明日需要涨多少才翻红」，为负表示平盘即翻红。

用法：
  python turn_red.py --ohlc ohlc_top400.json --quotes quotes_1020.json \
      --min-amount 1.0 --out turn_red.json
"""
import argparse
import io
import json
import math
import sys

A12 = 2.0 / 13.0
A26 = 2.0 / 27.0
AB = A12 - A26
A9 = 2.0 / 10.0

BOND_KW = ["国债", "信用债", "科创债", "地方债", "城投债", "公司债", "政金债",
           "债ETF", "短融", "货币", "日利", "现金", "理财", "债"]


# ---------------- 指标 ----------------
def ema(vals, n):
    if not vals:
        return []
    a = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + a * (v - out[-1]))
    return out


def macd_full(closes, fast=12, slow=26, sig=9):
    """返回逐日 DIF/DEA/bar 序列，用于判断阶段与转折。"""
    e12 = ema(closes, fast)
    e26 = ema(closes, slow)
    dif = [x - y for x, y in zip(e12, e26)]
    dea = ema(dif, sig)
    bar = [2 * (d - s) for d, s in zip(dif, dea)]
    return {"e12": e12, "e26": e26, "dif": dif, "dea": dea, "bar": bar}


def kdj_full(highs, lows, closes, n=9, kn=3, dn=3):
    k, d = 50.0, 50.0
    K, D, J = [], [], []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1):i + 1])
        hi = max(highs[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0
        k = (kn - 1) / kn * k + 1.0 / kn * rsv
        d = (dn - 1) / dn * d + 1.0 / dn * k
        K.append(k)
        D.append(d)
        J.append(3 * k - 2 * d)
    return {"k": K, "d": D, "j": J}


def cross_price(m):
    """明日收盘价达到多少时 MACD 柱恰好归零。返回 (P*, 是否有效)"""
    e12, e26 = m["e12"][-1], m["e26"][-1]
    dea = m["dea"][-1]
    k1 = (1 - A12) * e12 - (1 - A26) * e26
    p = (dea - k1) / AB
    if not math.isfinite(p) or p <= 0:
        return None, False
    return p, True


def consecutive(flags):
    """从末尾往前数连续为 True 的个数"""
    c = 0
    for f in reversed(flags):
        if f:
            c += 1
        else:
            break
    return c


# ---------------- 分类 ----------------
# 柱值占价格比低于此阈值，视为「贴零轴摩擦」，不构成有效信号
NOISE_PCT = 0.10


def classify(bar, bar_prev, req_pct, bar_pct):
    """MACD 阶段分类。bar_pct = 柱值 / 价格 × 100，用于跨标的可比。"""
    if bar > 0 and bar_prev <= 0:
        return "刚翻红" if bar_pct >= NOISE_PCT else "假翻红"
    if bar > 0:
        return "红柱延续"
    # 绿柱
    shrinking = bar > bar_prev
    if not shrinking:
        return "绿柱扩大"
    if req_pct is None:
        return "绿柱缩短"
    if req_pct <= 0.3:
        return "明日即翻红" if bar_pct >= NOISE_PCT else "临近但柱值过小"
    if req_pct <= 1.5:
        return "1-2日可翻红" if bar_pct >= NOISE_PCT else "临近但柱值过小"
    if req_pct <= 3.0:
        return "3日内可翻红"
    return "绿柱缩短"


# ---------------- 综合评分 ----------------
def score_and_verdict(r):
    """硬性条件 + 加分项 + 警示项 → 可买性判定"""
    hard_fail = []
    if r["j"] >= 110:
        hard_fail.append(f"J={r['j']:.1f}≥110")
    if r["k"] >= 80:
        hard_fail.append(f"K={r['k']:.1f}≥80")
    if r["amount"] < 1.0:
        hard_fail.append(f"成交{r['amount']:.2f}亿<1亿")

    add = 0
    notes = []
    if r["k"] > r["d"]:
        add += 1
        notes.append(f"KDJ金叉{r['kdj_golden_days']}日")
        if 1 <= r["kdj_golden_days"] <= 3:
            add += 1
            notes.append("金叉初期")
    if 15 < r["k"] <= 50:
        add += 2
        notes.append("K在中低位")
    elif 50 < r["k"] < 80:
        add += 1
        notes.append("K偏高但未超")
    if r["shrink_days"] >= 2:
        add += 1
        notes.append(f"绿柱缩短{r['shrink_days']}日")
    if r["vol_ratio"] is not None and r["vol_ratio"] >= 1.2:
        add += 1
        notes.append(f"量比{r['vol_ratio']:.2f}")
    if r["req_pct"] is not None and r["req_pct"] <= 0.5:
        add += 2
        notes.append("翻红门槛极低")

    warns = []
    if r["ret3"] is not None and r["ret3"] >= 7:
        warns.append(f"3日已涨{r['ret3']:.1f}%")
    if r["k"] > 70:
        warns.append(f"K={r['k']:.0f}偏高")
    if r["j"] > 100:
        warns.append(f"J={r['j']:.0f}超100")
    if r["amount"] < 2.0:
        warns.append(f"成交仅{r['amount']:.1f}亿")

    if hard_fail:
        verdict = "排除"
    elif warns and add < 4:
        verdict = "观察"
    elif add >= 5 and not warns:
        verdict = "优先关注"
    elif add >= 3:
        verdict = "可观察"
    else:
        verdict = "暂不参与"

    return {
        "verdict": verdict,
        "score": add,
        "hard_fail": hard_fail,
        "notes": notes,
        "warns": warns,
    }


def score_pool(bars_map, qmap, min_amount=1.0, min_bars=60):
    """对一批 K 线做 MACD 阶段分类 + KDJ 评分，返回 (行列表, 跳过数)。

    bars_map: {symbol: {"bars": [...]}}  qmap: {symbol: 报价}
    独立成函数是为了让 scan_market.py 直接复用，避免两套口径漂移。

    qmap 也接受 scan_market.py 输出那种 list（元素带 symbol），自动转成 dict。
    注意：scan 输出里的 amount 单位是「亿」，这里统一还原成「元」，避免差 1e8 倍。
    """
    if isinstance(qmap, list):
        qmap = {q["symbol"]: dict(q, amount=(q.get("amount") or 0) * 1e8)
                for q in qmap if q.get("symbol")}
    elif not isinstance(qmap, dict):
        qmap = {}
    rows = []
    skipped = 0
    for sym, rec in bars_map.items():
        bars = rec["bars"]
        if len(bars) < min_bars:
            skipped += 1
            continue
        q = qmap.get(sym, {})
        name = q.get("name") or sym
        if any(k in name for k in BOND_KW):
            continue
        amount = (q.get("amount") or 0) / 1e8
        if amount < min_amount:
            continue

        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        vols = [b["volume"] or 0 for b in bars]
        m = macd_full(closes)
        kd = kdj_full(highs, lows, closes)

        bar, bar_prev = m["bar"][-1], m["bar"][-2]
        p_star, ok = cross_price(m)
        cur = q.get("current") or closes[-1]
        req_pct = (p_star / cur - 1) * 100 if (ok and cur) else None

        # 绿柱缩短连续天数
        shrink_flags = [m["bar"][i] > m["bar"][i - 1]
                        for i in range(1, len(m["bar"]))]
        shrink_days = consecutive(shrink_flags) if bar < 0 else 0

        # 量比：今日量 / 前 5 日均量
        v5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 and sum(vols[-6:-1]) > 0 else None
        vol_ratio = (vols[-1] / v5) if v5 else None

        # 近 3 日累计涨幅
        ret3 = (closes[-1] / closes[-4] - 1) * 100 if len(closes) >= 4 else None

        # KDJ 金叉/死叉状态与持续天数
        kd_flags = [kd["k"][i] > kd["d"][i] for i in range(len(kd["k"]))]
        kdj_golden_days = consecutive(kd_flags) if kd_flags[-1] else 0
        kdj_dead_days = consecutive([not f for f in kd_flags]) if not kd_flags[-1] else 0
        kdj_state = "金叉" if kd_flags[-1] else "死叉"
        # J 的变化方向（判断超买是否在加剧）
        j_slope = kd["j"][-1] - kd["j"][-2]

        bar_pct = abs(bar) / cur * 100 if cur else 0.0
        stage = classify(bar, bar_prev, req_pct, bar_pct)
        r = {
            "symbol": sym, "name": name, "amount": amount,
            "today": q.get("percent"),
            "close": cur,
            "dif": m["dif"][-1], "dea": m["dea"][-1],
            "bar": bar, "bar_prev": bar_prev,
            "bar_change": bar - bar_prev,
            "bar_pct": bar_pct,
            "stage": stage,
            "cross_price": p_star, "req_pct": req_pct,
            "k": kd["k"][-1], "d": kd["d"][-1], "j": kd["j"][-1],
            "k_prev": kd["k"][-2], "j_prev": kd["j"][-2],
            "shrink_days": shrink_days,
            "vol_ratio": vol_ratio,
            "ret3": ret3,
            "kdj_state": kdj_state,
            "kdj_golden_days": kdj_golden_days,
            "kdj_dead_days": kdj_dead_days,
            "j_slope": j_slope,
            "bars": len(bars),
        }
        r.update(score_and_verdict(r))
        rows.append(r)

    return rows, skipped


ORDER = {"刚翻红": 0, "明日即翻红": 1, "1-2日可翻红": 2, "3日内可翻红": 3,
         "临近但柱值过小": 4, "假翻红": 5, "绿柱缩短": 6,
         "红柱延续": 7, "绿柱扩大": 8}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ohlc", required=True, help="fetch_ohlc.py 输出的 K 线 JSON")
    ap.add_argument("--quotes", default="", help="fetch_ohlc.py --quotes 输出的报价 JSON")
    ap.add_argument("--min-amount", type=float, default=1.0)
    ap.add_argument("--min-bars", type=int, default=60)
    ap.add_argument("--out", default="turn_red.json")
    args = ap.parse_args()

    o = json.load(io.open(args.ohlc, encoding="utf-8"))
    bars_map = o["data"]
    qmap = {}
    if args.quotes:
        qmap = json.load(io.open(args.quotes, encoding="utf-8")).get("data", {})

    rows, skipped = score_pool(bars_map, qmap, args.min_amount, args.min_bars)
    order = ORDER
    rows.sort(key=lambda x: (order.get(x["stage"], 9), -x["amount"]))

    payload = {
        "generated_at": o.get("generated_at"),
        "total": len(rows), "skipped": skipped,
        "min_amount": args.min_amount,
        "data": rows,
    }
    json.dump(payload, io.open(args.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 控制台输出 ----
    from collections import Counter
    cnt = Counter(r["stage"] for r in rows)
    print(f"# 覆盖 {len(rows)} 只（跳过样本不足 {skipped} 只）")
    print("# 阶段分布: " + "  ".join(f"{k}={v}" for k, v in
                                sorted(cnt.items(), key=lambda kv: order.get(kv[0], 9))))
    print()
    hdr = (f"{'代码':<10}{'名称':<22}{'阶段':<14}{'需涨%':>7}"
           f"{'柱%':>7}{'柱变':>8}{'K':>6}{'D':>6}{'J':>6}{'评分':>5}  判定")
    print(hdr)
    print("-" * 104)
    for r in rows:
        if r["stage"] in ("绿柱扩大",):
            continue
        rq = f"{r['req_pct']:+.2f}" if r["req_pct"] is not None else "  —"
        td = f"{r['today']:+.2f}" if r["today"] is not None else "  —"
        print(f"{r['symbol']:<10}{r['name'][:20]:<22}{r['stage']:<14}{rq:>7}"
              f"{r['bar_pct']:>+7.3f}{r['bar_change']:>+8.4f}"
              f"{r['k']:>6.1f}{r['d']:>6.1f}{r['j']:>6.1f}{r['score']:>5}  {r['verdict']}")
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
