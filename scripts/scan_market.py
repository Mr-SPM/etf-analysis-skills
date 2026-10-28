#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场一条命令跑完：报价快照 → 成交额筛池 → 拉真 OHLC → MACD/KDJ 分类评分

解决「1600 多只逐只请求太麻烦」的问题：把逐只抓取藏进一个漏斗，
你只敲一条命令，只等十几秒，只看最后的表。

漏斗逻辑（关键）：
  1709 只全市场
    ↓ 批量报价（9 个请求，2 秒，无需 cookie）
  按当日成交额排序
    ↓ 砍掉成交额 < --min-amount 的（默认 0.5 亿，约剩 300~400 只）
  候选池逐只拉 K 线（这才是「逐只请求」，但只对候选池做）
    ↓
  MACD 七阶段 + KDJ 评分 + 可买性判定

为什么不全市场逐只拉：全市场 1709 只实测 15 秒也能跑完，但其中 79 只是
腾讯根本不认识的空代码（未上市/预留号段），另有 1000 多只日均成交几百万，
翻红了也没法建仓。漏斗砍掉它们不损失任何可交易机会。

用法：
  python scan_market.py                          # 默认 0.5 亿门槛
  python scan_market.py --min-amount 1 --count 150
  python scan_market.py --all-market             # 强制全市场逐只（约 15 秒）
  python scan_market.py --universe all1709.txt   # 自定义标的池
"""
import argparse
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetch_ohlc as F
import turn_red as T


def load_universe(args):
    """标的池来源：显式文件 > 趋势缓存 > 实时报价全量"""
    if args.universe:
        txt = io.open(args.universe, encoding="utf-8").read()
        syms = [s.strip() for s in txt.replace("\n", ",").split(",") if s.strip()]
        return syms, f"file:{os.path.basename(args.universe)}"

    cache = os.path.join(args.cache_dir, "cache", "trend_d15.json")
    if os.path.exists(cache):
        d = json.load(io.open(cache, encoding="utf-8"))
        syms = [r["symbol"] for r in d.get("rows", []) if r.get("symbol")]
        if syms:
            return syms, f"trend_cache({len(syms)})"
    return [], "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="", help="标的池文件（逗号或换行分隔）")
    ap.add_argument("--cache-dir", default=".",
                    help="趋势缓存所在目录（其下需有 cache/trend_d15.json）")
    ap.add_argument("--min-amount", type=float, default=0.5,
                    help="成交额门槛（亿元），默认 0.5")
    ap.add_argument("--max-pool", type=int, default=450,
                    help="候选池上限，按成交额降序截断")
    ap.add_argument("--count", type=int, default=150, help="每只 K 线根数")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--all-market", action="store_true",
                    help="跳过成交额漏斗，全市场逐只拉")
    ap.add_argument("--out", default="scan.json")
    args = ap.parse_args()

    t0 = time.time()
    syms, src = load_universe(args)
    if not syms:
        sys.exit("标的池为空：请用 --universe 指定，或确认 cache/trend_d15.json 存在")

    # ---- 1. 批量报价 ----
    quotes = F.fetch_quotes(syms)
    t1 = time.time()
    print(f"# 标的池 {len(syms)} 只（源 {src}）→ 报价命中 {len(quotes)} 只，"
          f"{t1 - t0:.1f}s")

    # ---- 2. 成交额漏斗 ----
    if args.all_market:
        pool = [s for s in quotes]
        print(f"# --all-market：不设门槛，候选 {len(pool)} 只")
    else:
        ranked = sorted(
            quotes.values(),
            key=lambda q: -(q.get("amount") or 0),
        )
        pool = [q["symbol"] for q in ranked
                if (q.get("amount") or 0) / 1e8 >= args.min_amount]
        pool = pool[:args.max_pool]
        floor = (ranked[len(pool) - 1].get("amount") or 0) / 1e8 if pool else 0
        print(f"# 成交额 ≥{args.min_amount}亿 → 候选 {len(pool)} 只"
              f"（池底 {floor:.2f}亿），砍掉 {len(quotes) - len(pool)} 只")

    # ---- 3. 候选池逐只拉 OHLC ----
    bars_map, failed = {}, []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(F.fetch_tencent, s, args.count, "qfq"): s for s in pool}
        for fu in as_completed(futs):
            s = futs[fu]
            try:
                r = fu.result()
            except Exception:
                failed.append(s)
                continue
            if r and r.get("bars"):
                bars_map[s] = r
            else:
                failed.append(s)
    t2 = time.time()
    print(f"# K 线抓取 {len(bars_map)}/{len(pool)} 成功（失败 {len(failed)}），"
          f"{t2 - t1:.1f}s")

    # ---- 4. 指标 + 分类（复用 turn_red 的全部逻辑）----
    # --all-market 时不设成交额门槛，评分阶段也要放行，否则前一步白跑了
    amt_gate = 0.0 if args.all_market else args.min_amount
    rows, skipped = T.score_pool(bars_map, quotes, amt_gate, min_bars=40)
    order = T.ORDER
    rows.sort(key=lambda x: (order.get(x["stage"], 9), -x["amount"]))

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "universe": src, "universe_n": len(syms),
        "quotes_n": len(quotes), "pool_n": len(pool),
        "ok_n": len(bars_map), "failed": failed,
        "min_amount": args.min_amount, "count": args.count,
        "elapsed_s": round(time.time() - t0, 1),
        "data": rows,
    }
    json.dump(payload, io.open(args.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    from collections import Counter
    cnt = Counter(r["stage"] for r in rows)
    print(f"# 覆盖 {len(rows)} 只")
    print("# 阶段分布: " + "  ".join(
        f"{k}={v}" for k, v in sorted(cnt.items(), key=lambda kv: order.get(kv[0], 9))))
    print()
    print(f"{'代码':<10}{'名称':<20}{'阶段':<14}{'需涨%':>7}{'柱%':>7}"
          f"{'K':>6}{'J':>6}{'评分':>5}  判定")
    print("-" * 96)
    for r in rows:
        if r["stage"] == "绿柱扩大":
            continue
        rq = f"{r['req_pct']:+.2f}" if r["req_pct"] is not None else "  —"
        print(f"{r['symbol']:<10}{r['name'][:18]:<20}{r['stage']:<14}{rq:>7}"
              f"{r['bar_pct']:>+7.3f}{r['k']:>6.1f}{r['j']:>6.1f}"
              f"{r['score']:>5}  {r['verdict']}")
    print(f"\n-> {args.out}   总耗时 {payload['elapsed_s']}s")


if __name__ == "__main__":
    main()
