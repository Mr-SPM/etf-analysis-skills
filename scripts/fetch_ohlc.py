#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 日线 OHLC 抓取器 —— 为 MACD / KDJ 提供真实的开高低收

数据源（均无需认证，按顺序回退）：
  1. 腾讯 ifzq  https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
     支持前复权（qfq），单只 320 根约 0.15s，最稳定
  2. 新浪     https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
     不复权，仅作备用

输出：
  --out  JSON（symbol/date/open/high/low/close/volume/amount?）
  --csv  CSV（列：symbol,date,open,high,low,close），可直接喂给 indicators.py --ohlc

用法：
  # 抓指定标的
  python fetch_ohlc.py --symbols SZ159529,SH512800 --out ohlc.json --csv ohlc.csv

  # 抓 etf_data.json 里成交额前 200 的标的
  python fetch_ohlc.py --from-data etf_data.json --top 200 --csv ohlc.csv

  # 抓全部（约 1700 只，8 线程约 2~4 分钟）
  python fetch_ohlc.py --from-data etf_data.json --all --out ohlc_all.json
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{count},{adj}"
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={count}")
QT_URL = "https://qt.gtimg.cn/q={codes}"

# 三个等价的 K 线主机。web.ifzq 高频调用会被限流（HTTP 501），
# 所以按顺序轮换，任一个成功即用。实测三者都正确支持 qfq 与日期区间。
TX_HOSTS = [
    ("web.ifzq.gtimg.cn", "/appstock/app/fqkline/get"),
    ("ifzq.gtimg.cn", "/appstock/app/fqkline/get"),
    ("proxy.finance.qq.com", "/ifzqgtimg/appstock/app/newfqkline/get"),
]

# count 实测上限：648 根（约 2.6 年），请求更多也只返回 648
TX_MAX_BARS = 648

# 主机健康度：失败的临时降权，成功即恢复
_HOST_FAIL = {}

# 腾讯批量报价字段下标（~ 分隔，GBK 编码）
QT_F = {
    "name": 1, "code": 2, "current": 3, "prev_close": 4, "open": 5,
    "volume": 6, "time": 30, "chg": 31, "percent": 32, "high": 33, "low": 34,
    "amount_wan": 37, "turnover": 38, "amplitude": 43,
    "float_mcap": 44, "total_mcap": 45, "limit_up": 47, "limit_down": 48,
}


def _get_gbk(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("gbk", "ignore")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_quotes(symbols: list[str], batch: int = 200) -> dict:
    """腾讯批量报价：一次最多 200 只，返回当日开高低收 + 成交额 + 涨跌幅。

    这既是「实时行情」的替代源，也直接给出当日 OHLC，无需另拉 K 线。
    """
    out = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        codes = ",".join(to_tx(s) for s in chunk)
        try:
            raw = _get_gbk(QT_URL.format(codes=codes))
        except Exception:
            continue
        for line in raw.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            body = line.partition("=")[2].strip().strip('"')
            f = body.split("~")
            if len(f) < 50:
                continue
            code = f[QT_F["code"]]
            if not code:
                continue
            sym = ("SH" if code.startswith(("5", "6", "9")) else
                   "SZ" if code.startswith(("0", "1", "2", "3")) else "BJ") + code
            cur = _f(f[QT_F["current"]])
            prev = _f(f[QT_F["prev_close"]])
            amt_wan = _f(f[QT_F["amount_wan"]])
            out[sym] = {
                "symbol": sym,
                "name": f[QT_F["name"]],
                "current": cur,
                "prev_close": prev,
                "open": _f(f[QT_F["open"]]),
                "high": _f(f[QT_F["high"]]),
                "low": _f(f[QT_F["low"]]),
                "volume": _f(f[QT_F["volume"]]),
                "amount": amt_wan * 1e4 if amt_wan is not None else None,
                "percent": _f(f[QT_F["percent"]]),
                "chg": _f(f[QT_F["chg"]]),
                "turnover": _f(f[QT_F["turnover"]]),
                "amplitude": _f(f[QT_F["amplitude"]]),
                "time": f[QT_F["time"]],
            }
    return out

# A 股节假日（这些日期不应出现在任何真实 K 线里）
HOLIDAYS = {
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def to_tx(symbol: str) -> str:
    """SZ159529 -> sz159529 ; SH512800 -> sh512800"""
    s = symbol.strip().upper()
    if s.startswith(("SH", "SZ", "BJ")):
        return s[:2].lower() + s[2:]
    return s.lower()


def to_std(code: str) -> str:
    """sz159529 -> SZ159529"""
    return code.strip().upper()


def fetch_tencent(symbol: str, count: int, adj: str = "qfq",
                  start: str = "", end: str = "") -> dict | None:
    """拉前复权日 K。start/end 非空时按日期区间取（格式 YYYY-MM-DD）。"""
    code = to_tx(symbol)
    count = min(int(count), TX_MAX_BARS)
    # 按历史失败次数排序，失败少的优先
    hosts = sorted(TX_HOSTS, key=lambda h: _HOST_FAIL.get(h[0], 0))
    for host, path in hosts:
        url = (f"https://{host}{path}?param="
               f"{code},day,{start},{end},{count},{adj}")
        try:
            raw = _get(url)
            j = json.loads(raw)
            d = (j.get("data") or {}).get(code)
            if not isinstance(d, dict):
                _HOST_FAIL[host] = _HOST_FAIL.get(host, 0) + 1
                continue
            rows = d.get("qfqday") if adj == "qfq" else None
            if not rows:
                rows = d.get("day")
            if not rows:
                _HOST_FAIL[host] = _HOST_FAIL.get(host, 0) + 1
                continue
            out = []
            for r in rows:
                # 腾讯格式: [date, open, close, high, low, volume, ...]
                if len(r) < 6:
                    continue
                try:
                    out.append({
                        "date": r[0],
                        "open": float(r[1]),
                        "high": float(r[3]),
                        "low": float(r[4]),
                        "close": float(r[2]),
                        "volume": float(r[5]) if r[5] not in ("", None) else None,
                    })
                except (TypeError, ValueError):
                    continue
            if not out:
                _HOST_FAIL[host] = _HOST_FAIL.get(host, 0) + 1
                continue
            _HOST_FAIL[host] = max(0, _HOST_FAIL.get(host, 0) - 1)
            return {"symbol": to_std(symbol), "source": f"tencent:{host}",
                    "adj": adj, "bars": out}
        except Exception:
            _HOST_FAIL[host] = _HOST_FAIL.get(host, 0) + 1
            continue
    return None


def fetch_sina(symbol: str, count: int) -> dict | None:
    code = to_tx(symbol)
    try:
        raw = _get(SINA_URL.format(code=code, count=count))
        rows = json.loads(raw)
        if not isinstance(rows, list) or not rows:
            return None
        out = []
        for r in rows:
            try:
                out.append({
                    "date": r["day"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]) if r.get("volume") else None,
                })
            except (KeyError, TypeError, ValueError):
                continue
        if not out:
            return None
        return {"symbol": to_std(symbol), "source": "sina", "adj": "none", "bars": out}
    except Exception:
        return None


def clean_bars(bars: list[dict]) -> tuple[list[dict], list[str]]:
    """去重、排序、剔除节假日与非法值。返回 (bars, 问题列表)"""
    issues = []
    seen = {}
    for b in bars:
        d = b["date"]
        if d in HOLIDAYS:
            issues.append(f"剔除节假日假K线 {d}")
            continue
        if not all(b.get(k) is not None for k in ("open", "high", "low", "close")):
            continue
        if b["high"] < b["low"]:
            issues.append(f"{d} high<low，已丢弃")
            continue
        if b["close"] <= 0:
            continue
        seen[d] = b
    out = [seen[k] for k in sorted(seen)]
    return out, issues


def load_symbols(args) -> list[str]:
    if args.symbols:
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not args.from_data:
        raise SystemExit("必须提供 --symbols 或 --from-data")
    d = json.load(io.open(args.from_data, encoding="utf-8"))
    rows = d.get("data") or []
    rows = [x for x in rows if x.get("symbol")]
    if not args.all:
        rows.sort(key=lambda x: -(x.get("amount") or 0))
        rows = rows[: args.top]
    return [x["symbol"] for x in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔，如 SZ159529,SH512800")
    ap.add_argument("--from-data", default="", help="从 fetch_etf.py 的输出 JSON 取标的")
    ap.add_argument("--all", action="store_true", help="取 --from-data 里的全部标的")
    ap.add_argument("--top", type=int, default=200, help="不传 --all 时取成交额前 N 只")
    ap.add_argument("--count", type=int, default=320,
                    help="每只取多少根日K（默认320≈15个月，上限 648≈2.6年）")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD（可选，与 --end 配合取区间）")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（可选）")
    ap.add_argument("--adj", default="qfq", choices=["qfq", "none"], help="复权方式，默认前复权")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--retry", type=int, default=2)
    ap.add_argument("--out", default="ohlc.json")
    ap.add_argument("--csv", default="")
    ap.add_argument("--sleep", type=float, default=0.0, help="每只之间的间隔秒数")
    ap.add_argument("--quotes", action="store_true",
                    help="批量报价模式：只取当日开高低收+成交额+涨跌幅（1700只约2秒）")
    args = ap.parse_args()

    symbols = load_symbols(args)

    # ---- 批量报价模式 ----
    if args.quotes:
        t0 = time.time()
        q = fetch_quotes(symbols)
        el = time.time() - t0
        if args.out:
            json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "mode": "quotes", "count": len(q), "data": q},
                      io.open(args.out, "w", encoding="utf-8"), ensure_ascii=False)
        if args.csv:
            with io.open(args.csv, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "date", "open", "high", "low", "close"])
                for sym, r in q.items():
                    if None in (r["open"], r["high"], r["low"], r["current"]):
                        continue
                    w.writerow([sym, (r["time"] or "")[:8], r["open"], r["high"],
                                r["low"], r["current"]])
        ps = [v["percent"] for v in q.values() if v.get("percent") is not None]
        up = sum(1 for p in ps if p > 0)
        dn = sum(1 for p in ps if p < 0)
        print(f"# 报价 {len(q)}/{len(symbols)}  耗时 {el:.1f}s")
        print(f"# 上涨 {up} / 下跌 {dn} / 平 {len(ps)-up-dn}  均值 {sum(ps)/len(ps):+.2f}%"
              if ps else "# 无有效报价")
        ts = {v["time"] for v in q.values() if v.get("time")}
        print(f"# 快照时间: {sorted(ts)[-1] if ts else '未知'}")
        return

    print(f"# 待抓取 {len(symbols)} 只，源=tencent({args.adj}) 回退=sina，{args.workers} 线程",
          file=sys.stderr)

    results = {}
    fails = []

    def work(sym):
        for attempt in range(args.retry + 1):
            r = fetch_tencent(sym, args.count, args.adj, args.start, args.end)
            if r:
                return r
            if attempt < args.retry:
                time.sleep(0.3 * (attempt + 1))
        r = fetch_sina(sym, args.count)
        if r:
            return r
        return None

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, s): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            done += 1
            try:
                r = f.result()
            except Exception:
                r = None
            if not r:
                fails.append(sym)
                continue
            bars, issues = clean_bars(r["bars"])
            # 指定日期区间时允许短样本（用户可能只要最近 20 天）；
            # 否则要求 ≥40 根，保证 MACD/KDJ 有足够预热。
            min_bars = 5 if (args.start or args.end) else 40
            if len(bars) < min_bars:
                fails.append(sym)
                continue
            r["bars"] = bars
            r["bars_count"] = len(bars)
            r["first_date"] = bars[0]["date"]
            r["last_date"] = bars[-1]["date"]
            r["last_close"] = bars[-1]["close"]
            if issues:
                r["issues"] = issues
            results[sym] = r
            if args.sleep:
                time.sleep(args.sleep)
            if done % 100 == 0:
                print(f"# 进度 {done}/{len(symbols)}", file=sys.stderr)

    el = time.time() - t0
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "adj": args.adj,
        "requested": len(symbols),
        "ok": len(results),
        "failed": fails[:200],
        "failed_count": len(fails),
        "elapsed_sec": round(el, 1),
        "data": results,
    }
    if args.out:
        json.dump(payload, io.open(args.out, "w", encoding="utf-8"), ensure_ascii=False)
    if args.csv:
        with io.open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "date", "open", "high", "low", "close"])
            for sym, r in results.items():
                for b in r["bars"]:
                    w.writerow([sym, b["date"], b["open"], b["high"], b["low"], b["close"]])

    # 汇总
    from collections import Counter
    src = Counter(r["source"] for r in results.values())
    print(f"# 成功 {len(results)}/{len(symbols)}  耗时 {el:.1f}s  源分布 {dict(src)}",
          file=sys.stderr)
    if fails:
        print(f"# 失败 {len(fails)} 只: {fails[:20]}", file=sys.stderr)

    # 完整性检查
    print("\n=== 完整性检查 ===")
    bad_holiday = [s for s, r in results.items()
                   if any(b["date"] in HOLIDAYS for b in r["bars"])]
    print(f"含节假日假K线的标的: {len(bad_holiday)}")
    last_dates = Counter(r["last_date"] for r in results.values())
    print(f"最后交易日分布: {dict(last_dates)}")
    adj_eff = sum(1 for r in results.values() if r["adj"] == "qfq")
    print(f"前复权生效: {adj_eff} / {len(results)}")

    # 抽样打印
    sample = list(results.items())[:3]
    for sym, r in sample:
        print(f"\n{sym} [{r['source']}/{r['adj']}] {r['bars_count']}根 "
              f"{r['first_date']} ~ {r['last_date']} 收盘 {r['last_close']}")
        for b in r["bars"][-3:]:
            print(f"   {b['date']} O{b['open']} H{b['high']} L{b['low']} C{b['close']}")


if __name__ == "__main__":
    main()
