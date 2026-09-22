#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线冒烟测试 —— 不联网, 只验证 fetch_etf.py 的逻辑不变量。

覆盖:
  1. 缓存路径命名 (trend_<key>.json / live_<day>.json)
  2. 缓存结构校验 (旧字段名 series 应判为无效)
  3. 缓存保鲜规则 (跨天 / 16:01 前生成 / 同日已落库)
  4. score_etfs: 无成交额时不能报 0 分 / 不可交易
  4b. premium_risk 阈值 —— 脚本是唯一定义处, 锁住 0.5 / 1.0
  4c. 动量字段名 momentum_5trading_days (5 个交易日, 不是 5 个自然日)
  5. _backfill_today_percent: 必须校验交易日, 不能把上一天的涨跌当今天
  6. _breadth: "平盘" 与 "当天无记录" 必须分开
  7. fetch_trend: 缺 API Key 时直接返回错误, 不发请求
  8. 端到端 (仅当本地已有缓存时): 校验产出 JSON 的自洽性

用法:  python smoke_test.py
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
OUT = HERE / "_tmp_out"

sys.path.insert(0, str(HERE / "scripts"))
import fetch_etf  # noqa: E402

RESULTS = []


def check(label, cond, extra=""):
    RESULTS.append(bool(cond))
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


def trend(seq):
    """seq: [(date, daily_return)] -> trend 列表 (含累计收益)"""
    out, acc = [], 1.0
    for d, r in seq:
        acc *= 1.0 + r
        out.append({"trade_date": d, "daily_return": r, "cumulative_return": acc - 1.0})
    return out


# ---------------------------------------------------------------- 1. 缓存路径
def test_cache_paths():
    print("== 1. 缓存路径命名 ==")
    p = fetch_etf._cache_path_trend("d15")
    check("trend 缓存文件名为 trend_d15.json", p.name == "trend_d15.json", f"实际 {p.name}")
    check("trend 缓存落在 cache/ 下", p.parent.name == "cache", f"实际 {p.parent}")
    p2 = fetch_etf._cache_path_live("2026-09-22")
    check("live 快照文件名为 live_2026-09-22.json",
          p2.name == "live_2026-09-22.json", f"实际 {p2.name}")


# ------------------------------------------------------------ 2. 缓存结构校验
def test_rows_valid():
    print("== 2. 缓存结构校验 ==")
    check("含 trend 字段 -> 有效",
          fetch_etf._trend_rows_look_valid([{"symbol": "A", "trend": []}]))
    check("旧字段 series -> 无效 (当作未命中)",
          not fetch_etf._trend_rows_look_valid([{"symbol": "A", "series": []}]))
    check("空列表 -> 无效", not fetch_etf._trend_rows_look_valid([]))


# ---------------------------------------------------------------- 3. 保鲜规则
def test_freshness():
    print("== 3. 缓存保鲜规则 ==")
    today = datetime(2026, 9, 22, 20, 30)      # 已过 16:01
    fresh, why = fetch_etf.trend_cache_freshness("2026-09-22T20:12:48", today)
    check("同日 + 已过 16:01 -> 新鲜", fresh, why)

    fresh, why = fetch_etf.trend_cache_freshness("2026-09-22T09:00:00", today)
    check("同日但 16:01 前生成 -> 过期", not fresh, why)

    fresh, why = fetch_etf.trend_cache_freshness("2026-09-21T20:12:48", today)
    check("跨天 -> 过期", not fresh, why)

    fresh, why = fetch_etf.trend_cache_freshness("不是时间", today)
    check("无法解析 -> 过期", not fresh, why)

    early = datetime(2026, 9, 22, 9, 0)        # 16:01 前
    fresh, why = fetch_etf.trend_cache_freshness("2026-09-22T08:00:00", early)
    check("同日在 16:01 前 (都还没落库) -> 新鲜", fresh, why)


# ---------------------------------------------------------------- 4. score_etfs
def test_score_etfs():
    print("== 4. score_etfs (无成交额 != 流动性差) ==")
    rows = [
        {"symbol": "A", "amount": 1.5e9, "trend": [1]},   # 高流动性
        {"symbol": "B", "amount": 1.0e7, "trend": [1]},   # 低流动性
        {"symbol": "C", "trend": [1]},                    # 无成交额 (16:01 后)
    ]
    fetch_etf.score_etfs(rows)
    exp = [("A", 3, True, True), ("B", 0, False, True), ("C", None, None, False)]
    for r, (sym, grade, tradable, known) in zip(rows, exp):
        check(
            f"{sym}: grade={grade} tradable={tradable} known={known}",
            r["liquidity_grade"] == grade
            and r["tradable"] == tradable
            and r["liquidity_known"] == known,
            f"实际 grade={r['liquidity_grade']} tradable={r['tradable']} "
            f"known={r['liquidity_known']}",
        )


# --------------------------------------------------- 4b. premium_risk 阈值
def test_premium_risk():
    """溢价风险分级是**脚本单方面定义**的, SKILL 只解释标签。

    这里把阈值锁住 —— 一旦改动, 测试会失败, 提醒同步检查文档描述。
    """
    print("== 4b. premium_risk 阈值 (唯一定义处) ==")
    check("PREMIUM_MID == 0.5", fetch_etf.PREMIUM_MID == 0.5,
          f"实际 {fetch_etf.PREMIUM_MID}")
    check("PREMIUM_HIGH == 1.0", fetch_etf.PREMIUM_HIGH == 1.0,
          f"实际 {fetch_etf.PREMIUM_HIGH}")

    cases = [
        (0.0, "low"), (0.49, "low"),
        (0.5, "mid"), (0.99, "mid"),
        (1.0, "high"), (5.0, "high"),
        (-0.6, "mid"),   # 折价同样有风险, 取绝对值
        (-2.0, "high"),
    ]
    rows = [{"symbol": f"S{i}", "premium_rate": v} for i, (v, _) in enumerate(cases)]
    fetch_etf.score_etfs(rows)
    for r, (v, exp) in zip(rows, cases):
        check(f"premium_rate={v} -> {exp}", r.get("premium_risk") == exp,
              f"实际 {r.get('premium_risk')}")

    # 没有 premium_rate 时不写这个字段 (16:01 后没有实时数据)
    rows2 = [{"symbol": "X"}]
    fetch_etf.score_etfs(rows2)
    check("无 premium_rate -> 不写 premium_risk",
          "premium_risk" not in rows2[0])


# --------------------------------------------------------- 4c. 动量字段命名
def test_momentum_name():
    print("== 4c. 动量字段名 (必须是 5 个交易日, 不是 5 个自然日) ==")
    seq = [(f"2026-09-{10 + i:02d}", 0.01) for i in range(6)]
    out = {}
    fetch_etf._attach_trend_metrics(out, trend(seq))
    check("写出 momentum_5trading_days",
          "momentum_5trading_days" in out, f"实际 {sorted(out)}")
    check("不再有 momentum_5d", "momentum_5d" not in out)
    # 6 个点、每日 +1% -> 两点差应为 5 次复利
    expect = ((1.01**5) - 1) * 100
    check("动量数值 = 5 个交易日复利",
          abs(out["momentum_5trading_days"] - expect) < 1e-6,
          f"实际 {out.get('momentum_5trading_days')} 期望 {expect}")

    # 不足 6 个交易日 -> 不写这个字段
    out2 = {}
    fetch_etf._attach_trend_metrics(out2, trend([("2026-09-21", 0.01)]))
    check("交易日不足 -> 不写动量", "momentum_5trading_days" not in out2)


# ---------------------------------------------------------------- 5. 回填
def test_backfill():
    print("== 5. _backfill_today_percent (必须校验交易日) ==")
    latest = "2026-09-22"
    rows = [
        # 当天有记录 -> 应补出 percent
        {"symbol": "A", "trend": trend([("2026-09-21", 0.01), ("2026-09-22", -0.02)])},
        # 最后一条是上一天 (停牌/无成交) -> 必须保持空
        {"symbol": "B", "trend": trend([("2026-09-20", 0.03), ("2026-09-21", 0.05)])},
        # 已有 percent (盘中取过实时) -> 不该被覆盖
        {"symbol": "C", "percent": 9.99,
         "trend": trend([("2026-09-22", -0.02)])},
        # 完全没有走势 -> 保持空
        {"symbol": "D", "trend": []},
    ]
    filled = fetch_etf._backfill_today_percent(rows, latest)
    check("只补了 1 条", filled == 1, f"实际 {filled}")
    check("A 补出 -2.0%", rows[0]["percent"] == -2.0, f"实际 {rows[0].get('percent')}")
    check("A 带 percent_from_trend 标记", rows[0].get("percent_from_trend") is True)
    check("B 保持空 (上一天不能被当成今天)", rows[1].get("percent") is None,
          f"实际 {rows[1].get('percent')}")
    check("C 未被覆盖", rows[2]["percent"] == 9.99)
    check("D 保持空", rows[3].get("percent") is None)

    # latest 为空时不应补任何东西
    rows2 = [{"symbol": "A", "trend": trend([("2026-09-22", 0.01)])}]
    check("latest_date 为空 -> 不补", fetch_etf._backfill_today_percent(rows2, "") == 0)


# ---------------------------------------------------------------- 6. 涨跌家数
def test_breadth():
    print("== 6. _breadth (平盘 vs 无记录) ==")
    rows = [
        {"percent": 1.2}, {"percent": 0.5},          # up 2
        {"percent": -0.3},                            # down 1
        {"percent": 0.0},                             # flat 1
        {"percent": None}, {"percent": None},         # no_data 2
    ]
    b = fetch_etf._breadth(rows)
    check("up=2 down=1 flat=1 no_data=2",
          b == {"total": 6, "up": 2, "down": 1, "flat": 1, "no_data": 2}, f"实际 {b}")


# ------------------------------------------------------------ 7. 缺 Key 行为
def test_no_api_key():
    print("== 7. 缺 API Key 时直接报错, 不发请求 ==")
    items, err = fetch_etf.fetch_trend(days=15, api_key="")
    check("返回空列表", items == [])
    check("错误信息提示缺 Key", err and "API Key" in err)


# ------------------------------------------------------------------ 8. 端到端
def test_e2e():
    print("== 8. 端到端 (仅当已有缓存时) ==")
    cache = fetch_etf._cache_path_trend("d15")
    if not cache.exists():
        print(f"  [SKIP] 无缓存 {cache.name} —— 跳过 (避免联网)")
        return
    OUT.mkdir(exist_ok=True)
    data = OUT / "etf_data.json"
    r = subprocess.run(
        [PY, str(HERE / "scripts" / "fetch_etf.py"), "--out", str(data)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        check("脚本退出码为 0", False, r.stderr.strip()[:200])
        return
    check("脚本退出码为 0", True)
    d = json.loads(data.read_text(encoding="utf-8"))
    rows = d.get("data") or []
    check("data 非空", bool(rows), f"共 {len(rows)} 条")
    check("sources.trend 存在 (不再是 sources.month)", "trend" in d["sources"])
    check("每条都有 trend_days", all("trend_days" in x for x in rows))
    check("每条都有 window_return (不再是 month_return)",
          all(x.get("window_return") is not None for x in rows))
    check("动量字段名为 momentum_5trading_days (不再是 momentum_5d)",
          any("momentum_5trading_days" in x for x in rows)
          and not any("momentum_5d" in x for x in rows))
    b = d.get("breadth") or {}
    check("breadth 家数自洽",
          b.get("up", 0) + b.get("down", 0) + b.get("flat", 0) + b.get("no_data", 0)
          == b.get("total") == len(rows), f"实际 {b}")
    check("data_latest_date 已给出", bool(d.get("data_latest_date")),
          str(d.get("data_latest_date")))
    # 没有实时数据时, 不应有任何 ETF 拿到成交额
    if not d["sources"].get("live", {}).get("count"):
        check("无实时数据 -> 全部 liquidity_grade=None",
              all(x.get("liquidity_grade") is None for x in rows))
        check("无实时数据 -> 全部 tradable=None",
              all(x.get("tradable") is None for x in rows))
    print(f"  [INFO] 采集到 {len(rows)} 条, 涨跌家数 {b}")


if __name__ == "__main__":
    print("etf-analysis 离线冒烟测试")
    print("=" * 46)
    test_cache_paths()
    test_rows_valid()
    test_freshness()
    test_score_etfs()
    test_premium_risk()
    test_momentum_name()
    test_backfill()
    test_breadth()
    test_no_api_key()
    test_e2e()
    print("=" * 46)
    failed = RESULTS.count(False)
    print(f"共 {len(RESULTS)} 项, 失败 {failed} 项")
    sys.exit(1 if failed else 0)
