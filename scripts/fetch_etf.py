#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 ETF 数据采集器 —— 合并两个数据源。

数据源 1 (实时):
    https://my-stock.918968.xyz/all?cookie=<urlencoded>
    - 无需登录, cookie 可选; 失败时需手动提供 cookie
    - 返回 1600+ 只 ETF 的实时行情
    - 字段: symbol, name, current, percent, chg, amount, volume,
            premium_rate, unit_nav, market_capital, followers,
            current_year_percent, etf_types, ...

数据源 2 (月度走势, 需 Clerk 认证):
    https://stock2.1168168.xyz/api/etf/month?month=YYYY-MM
    - 认证: Authorization: Bearer <Clerk session JWT>
    - 返回 {success, data: { <bucket>: [ {symbol, name, series:[...] } ] }}
    - series: [{trade_date, cumulative_return, daily_return}]

用法:
    python fetch_etf.py --out out.json
    python fetch_etf.py --out out.json --month 2026-08 --clerk-token <jwt>
    python fetch_etf.py --out out.json --cookie "<cookie string>"
    python fetch_etf.py --out out.json --skip-month
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # 有则用 (连接池/重试更省心), 无则退回标准库

LIVE_URL = "https://my-stock.918968.xyz/all"
MONTH_URL = "https://stock2.1168168.xyz/api/etf/month"
SERIES_URL = "https://stock2.1168168.xyz/api/etf/series"
DEFAULT_DAYS = 15
CONFIG_PATH = Path.home() / ".workbuddy-ai" / "etf-plugin" / "config.json"
CACHE_DIR = Path.home() / ".workbuddy-ai" / "etf-plugin" / "cache"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# 快照缓存
# --------------------------------------------------------------------------
# 两类数据分开处理:
#   月度走势 (月内不变量) -> 按月缓存一个月一个文件, 命中则直接复用, 省接口调用
#   实时行情 (日内变量)   -> 按天缓存, 当日每次调用**追加一条快照**, 形成日内序列
# --------------------------------------------------------------------------
def _cache_path_month(month: str) -> Path:
    return CACHE_DIR / f"month_{month}.json"


def _cache_path_live(day: str) -> Path:
    return CACHE_DIR / f"live_{day}.json"


def load_month_cache(month: str) -> list[dict] | None:
    """读取按月缓存的走势数据。未命中返回 None。"""
    p = _cache_path_month(month)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        rows = d.get("rows") or []
        return rows or None
    except Exception:
        return None


def save_month_cache(month: str, rows: list[dict], source: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "month": month,
        "source": source,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(rows),
        "rows": rows,
    }
    _cache_path_month(month).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def load_live_snapshots(day: str) -> list[dict]:
    """读取当天已累积的实时快照列表 (每项含 saved_at + rows)。"""
    p = _cache_path_live(day)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d.get("snapshots") or []
    except Exception:
        return []


def append_live_snapshot(day: str, rows: list[dict], label: str = "") -> dict:
    """把本次拉取的实时数据追加为当天的一条快照, 返回该快照的摘要。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    snaps = load_live_snapshots(day)
    stamp = datetime.now()
    snap = {
        "seq": len(snaps) + 1,
        "saved_at": stamp.isoformat(timespec="seconds"),
        "time": stamp.strftime("%H:%M"),
        "label": label,
        "count": len(rows),
        "rows": rows,
    }
    snaps.append(snap)
    payload = {"day": day, "snapshots": snaps}
    _cache_path_live(day).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "seq": snap["seq"],
        "time": snap["time"],
        "count": snap["count"],
        "total_snapshots": len(snaps),
    }


# 收盘时间: A 股 15:00 收盘。留 5 分钟缓冲, 15:05 之后的实时行情视为"已定格"。
MARKET_CLOSE = datetime.strptime("15:05", "%H:%M").time()


def is_market_closed(now: datetime | None = None) -> bool:
    """当日是否已收盘 (超过 15:05)。

    收盘后实时行情不再变化, 继续调接口纯属浪费, 应直接复用当日快照。
    注意: 这里不判断周末/节假日 —— 非交易日接口同样返回静态数据,
    复用快照即可, 行为等价且更省。
    """
    now = now or datetime.now()
    return now.time() >= MARKET_CLOSE


def latest_snapshot_age(day: str) -> tuple[dict | None, float | None]:
    """返回当天最后一条快照及其距今的分钟数。

    用于「10 分钟内不重复拉取」的阈值判断。取不到时间戳时返回 (snap, None)。
    """
    snaps = load_live_snapshots(day)
    if not snaps:
        return None, None
    last = snaps[-1]
    raw = last.get("saved_at")
    if not raw:
        return last, None
    try:
        ts = datetime.fromisoformat(raw)
    except Exception:
        return last, None
    age_min = (datetime.now() - ts).total_seconds() / 60.0
    return last, age_min


def diff_snapshots(first: dict, last: dict, top: int = 10, eps: float = 0.01) -> dict:
    """对比当天第一条与最后一条实时快照, 找出日内变化最大的品种。

    eps: 变动阈值 (百分点)。低于该值的视为「没动」, 不进入榜单。
    没有这个过滤时, 若两次采集间行情完全没变 (非交易时段/间隔过短),
    climbing 与 falling 会返回**完全相同**的 top-N (因为对全 0 的 delta 排序,
    升序降序结果一致), 输出看起来像"涨幅榜=跌幅榜"的错误结论。
    """
    def index(snap):
        return {r.get("symbol"): r for r in (snap.get("rows") or [])}

    a, b = index(first), index(last)
    common = set(a) & set(b)
    if not common:
        return {}

    moves = []
    for sym in common:
        ra, rb = a[sym], b[sym]
        pa, pb = ra.get("percent"), rb.get("percent")
        if not isinstance(pa, (int, float)) or not isinstance(pb, (int, float)):
            continue
        ca, cb = ra.get("current"), rb.get("current")
        if not isinstance(ca, (int, float)) or not isinstance(cb, (int, float)) or not ca:
            continue
        moves.append(
            {
                "symbol": sym,
                "name": rb.get("name") or ra.get("name") or "",
                "percent_first": pa,
                "percent_last": pb,
                "delta_percent": pb - pa,
                "price_first": ca,
                "price_last": cb,
                "delta_price_pct": (cb - ca) / ca * 100,
            }
        )

    # 日内相对首条快照的涨跌变化 (按涨幅增量排序)
    # 先用 eps 过滤出"确实在动"的品种, 再分正负向取 top-N。
    movers = [m for m in moves if abs(m["delta_percent"]) >= eps]
    climbing = sorted(
        (m for m in movers if m["delta_percent"] > 0),
        key=lambda x: x["delta_percent"],
        reverse=True,
    )[:top]
    falling = sorted(
        (m for m in movers if m["delta_percent"] < 0),
        key=lambda x: x["delta_percent"],
    )[:top]

    return {
        "first": {"seq": first.get("seq"), "time": first.get("time")},
        "last": {"seq": last.get("seq"), "time": last.get("time")},
        "compared": len(moves),
        "moved": len(movers),
        "climbing": climbing,
        "falling": falling,
    }



# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write(f"[warn] 读取配置失败 {CONFIG_PATH}: {exc}\n")
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# HTTP 传输层 (零第三方依赖: 有 requests 就用, 没有就用标准库)
# --------------------------------------------------------------------------
def http_get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 25,
) -> tuple[int, object | None, str | None]:
    """GET 并解析 JSON, 返回 (status_code, json_obj, error)。

    status_code 为 0 表示连接层失败 (DNS/超时/TLS 等), 此时 error 有值。
    同时兼容 requests 与标准库 urllib, 保证在**任何** Python 3.9+ 上都能跑 ——
    这是本技能可以零依赖分发到任意机器的基础。
    """
    headers = headers or {}

    if requests is not None:
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as exc:
            return 0, None, f"请求失败: {exc}"
        try:
            return resp.status_code, resp.json(), None
        except Exception as exc:
            return resp.status_code, None, f"响应不是合法 JSON: {exc}"

    # --- 标准库兜底 ---
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也会走到这里, 但要保留 status 供上层判断 401
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return exc.code, None, f"HTTP {exc.code}: {body[:200]}"
    except Exception as exc:
        return 0, None, f"请求失败: {exc}"

    try:
        return status, json.loads(raw.decode("utf-8")), None
    except Exception as exc:
        return status, None, f"响应不是合法 JSON: {exc}"


# --------------------------------------------------------------------------
# 数据源 1: 实时行情
# --------------------------------------------------------------------------
def fetch_live(cookie: str = "", timeout: int = 25) -> tuple[list[dict], str | None]:
    """返回 (items, error)。成功时 error 为 None。"""
    url = LIVE_URL
    if cookie:
        url = f"{LIVE_URL}?cookie={urllib.parse.quote(cookie, safe='')}"

    headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    # 该接口对 Referer 敏感, 带上模拟前端来源
    headers["Referer"] = "https://stock2.1168168.xyz/"

    status, payload, err = http_get_json(url, headers=headers, timeout=timeout)
    if err and status == 0:
        return [], err
    if status != 200:
        return [], f"HTTP {status}"
    if payload is None:
        return [], "响应不是合法 JSON (可能被拦截或需要 cookie)"

    # 复刻前端逻辑
    data = (payload or {}).get("data") or payload
    if isinstance(data, dict):
        items = []
        for v in data.values():
            if isinstance(v, list):
                items.extend(v)
            else:
                items.append(v)
    elif isinstance(data, list):
        items = data
    else:
        return [], "响应结构无法识别"

    if not items:
        return [], "返回数据为空"

    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    stamped = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["trade_date"] = today
        row["data_type"] = "live"
        stamped.append(row)

    return stamped, None


# --------------------------------------------------------------------------
# 数据源 2: 月度走势 (Clerk 认证)
# --------------------------------------------------------------------------
def fetch_month(
    month: str, days: int = DEFAULT_DAYS, clerk_token: str = "", timeout: int = 30
) -> tuple[list[dict], str | None]:
    """返回 (items, error)。items 已按前端逻辑扁平化。

    前端调用方式:  f_({days: ue})  其中 ue 来自 localStorage["etf_analysis_days"], 默认 15
    即 month 接口的窗口参数是 days (不是 month)。
    这里同时传 month 与 days 以对齐前端行为。
    """
    headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    if clerk_token:
        headers["Authorization"] = f"Bearer {clerk_token}"

    status, payload, err = http_get_json(
        MONTH_URL,
        params={"month": month, "days": days},
        headers=headers,
        timeout=timeout,
    )

    if status == 401:
        return [], "未登录或登录已过期 (需要 Clerk token)"

    if err and status == 0:
        return [], err

    if status != 200:
        return [], f"HTTP {status}"

    if payload is None:
        return [], "响应不是合法 JSON"

    if isinstance(payload, dict) and not payload.get("success", True):
        return [], payload.get("error") or "接口返回 success=false"

    # 复刻前端: const grouped = res.data?.data || {}; Object.values(grouped).flat()
    grouped = (payload or {}).get("data") or {}
    items: list[dict] = []
    if isinstance(grouped, dict):
        for v in grouped.values():
            if isinstance(v, list):
                items.extend(v)
            elif isinstance(v, dict):
                items.append(v)
    elif isinstance(grouped, list):
        items = grouped

    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    stamped = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["trade_date"] = today
        row["data_type"] = "month"
        stamped.append(row)

    return stamped, None


def fetch_series(
    month: str, days: int = DEFAULT_DAYS, timeout: int = 40
) -> tuple[list[dict], str | None]:
    """公开兜底源: /api/etf/series 无需认证, 返回同样的 symbol/name/series 结构。

    参数: month (YYYY-MM) + days (默认 15)。

    实测行为 (2026-09-21 验证):
      - series 接口对 days 参数 **不敏感** —— days 传 1/3/5/15/30/60 返回字节数完全一致
      - 历史月份 (如 2026-08) 返回该月全部交易日 (21 天)
      - 当前进行中的月份 (2026-09) 返回该月截止目前的数据 (14 天, 至 09-18)
      - 即: series 语义是「返回该月数据」, 而非「返回最近 N 天」

    仍然把 days 传过去, 是为了对齐前端契约 (前端默认 15),
    同时也兼容将来服务端在该路径上启用 days 截取的可能。
    """
    headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    status, payload, err = http_get_json(
        SERIES_URL,
        params={"month": month, "days": days},
        headers=headers,
        timeout=timeout,
    )

    if err and status == 0:
        return [], err

    if status != 200:
        return [], f"HTTP {status}"

    if payload is None:
        return [], "响应不是合法 JSON"

    items = []
    data = payload.get("data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                items.extend(v)

    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    stamped = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["trade_date"] = today
        row["data_type"] = "series"
        stamped.append(row)

    return stamped, None


def default_month() -> str:
    """无指定时默认取**当月**。

    注意: 不要直接返回上月 —— 那样会永远看不到当月数据,
    月初补数逻辑 (cur_days < min(days, 10) 时拼接上一月) 也就永远不会触发。
    正确做法是取当月, 再由 main() 里的交易日数量检查决定是否回补上一月。
    """
    return datetime.now().strftime("%Y-%m")


def prev_month_of(month: str) -> str:
    """给定 YYYY-MM, 返回上一月。"""
    y, m = (int(x) for x in month.split("-"))
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


# --------------------------------------------------------------------------
# 推导指标 (为分析提供燃料)
# --------------------------------------------------------------------------
def stitch_series(prev_item: dict, cur_item: dict) -> dict:
    """把上一月的走势拼到当月走势前面, 形成连续序列。

    月初数据量太少时 (如 9/3 只有 2~3 天), 动量/波动率指标会失真。
    拼接后可得到跨月的连续窗口, 指标才有参考价值。

    ⚠️ 关键: 接口的 cumulative_return 是**月内累计** (每月从 0 重新起算)。
    直接拼接会导致跨越月界时数值跳变 (如 8月末 +15% 接到 9月初 -1%),
    使动量计算完全失真。因此拼接时必须**以日收益重新累乘**,
    重建连续的累计收益曲线。
    """
    prev_series = (prev_item or {}).get("series") or []
    cur_series = (cur_item or {}).get("series") or []

    # 去重: 以 trade_date 为键, 当月数据覆盖上月同日数据
    merged: dict[str, dict] = {}
    for x in prev_series:
        d = x.get("trade_date")
        if d:
            merged[d] = dict(x)
    for x in cur_series:
        d = x.get("trade_date")
        if d:
            merged[d] = dict(x)

    ordered = [merged[k] for k in sorted(merged)]

    # 重建连续累计收益: 逐日累乘 (1 + daily_return)
    acc = 1.0
    for x in ordered:
        dr = x.get("daily_return")
        if isinstance(dr, (int, float)):
            acc *= 1 + dr
        x["cumulative_return_cont"] = acc - 1  # 连续累计 (小数)

    out = dict(cur_item or {})
    out["series"] = ordered
    out["stitched_from"] = {
        "prev_days": len(prev_series),
        "cur_days": len(cur_series),
    }
    return out


def derive_metrics(live: list[dict], month: list[dict]) -> list[dict]:
    """把月度走势挂到实时行情上, 并计算动量/波动等指标。"""
    # 月度走势索引
    series_map: dict[str, dict] = {}
    for m in month:
        sym = m.get("symbol")
        if sym:
            series_map[sym] = m

    merged = []
    for row in live:
        sym = row.get("symbol", "")
        out = dict(row)
        m = series_map.get(sym)
        if m:
            s = m.get("series") or []
            out["month_series"] = s
            out["month_days"] = len(s)
            if s:
                rets = [
                    x.get("daily_return")
                    for x in s
                    if isinstance(x.get("daily_return"), (int, float))
                ]

                # 累计收益取值优先级:
                # 1) cumulative_return_cont —— 拼接场景下由日收益重建的连续累计
                # 2) cumulative_return —— 单月场景下接口直接给的月内累计
                # 两者都是小数, 统一 ×100 转百分比
                def _cum(x):
                    if "cumulative_return_cont" in x:
                        return x["cumulative_return_cont"]
                    return x.get("cumulative_return")

                cum = _cum(s[-1])
                if isinstance(cum, (int, float)):
                    out["month_return"] = cum * 100
                # 5 日动量 (用连续累计, 避免月界跳变)
                if len(s) >= 6:
                    c_now = _cum(s[-1])
                    c_prev = _cum(s[-6])
                    if isinstance(c_now, (int, float)) and isinstance(c_prev, (int, float)):
                        out["momentum_5d"] = (c_now - c_prev) * 100
                # 波动率 (日收益标准差, 百分化)
                if len(rets) >= 3:
                    mean = sum(rets) / len(rets)
                    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                    out["volatility"] = (var**0.5) * 100
                # 最大单日跌幅
                if rets:
                    out["max_daily_drop"] = min(rets) * 100
                    out["max_daily_gain"] = max(rets) * 100
                # 上涨天数占比
                if rets:
                    out["up_day_ratio"] = (
                        sum(1 for r in rets if r > 0) / len(rets) * 100
                    )
        merged.append(out)
    return merged


def score_etfs(rows: list[dict], min_amount: float = 5e7) -> list[dict]:
    """对 ETF 打分/打标签, 便于分析时筛选。"""
    for r in rows:
        liquidity = 0.0
        amt = r.get("amount") or 0
        if amt >= 1e9:
            liquidity = 3
        elif amt >= 3e8:
            liquidity = 2
        elif amt >= min_amount:
            liquidity = 1

        r["liquidity_grade"] = liquidity
        # 是否适合交易 (流动性够 + 有月度数据)
        r["tradable"] = bool(liquidity >= 2 and r.get("month_series"))

        # 溢价率风险
        prem = r.get("premium_rate")
        if isinstance(prem, (int, float)):
            r["premium_risk"] = (
                "high" if abs(prem) >= 1.0 else "mid" if abs(prem) >= 0.3 else "low"
            )
    return rows


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="A股 ETF 数据采集器")
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    ap.add_argument("--month", default="", help="月度走势月份 YYYY-MM, 默认上月")
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"月度窗口天数, 默认 {DEFAULT_DAYS} (服务端目前忽略, 始终返回整月)",
    )
    ap.add_argument("--cookie", default="", help="实时接口 cookie (可选)")
    ap.add_argument(
        "--clerk-token", default="", help="Clerk session JWT (Bearer token)"
    )
    ap.add_argument("--skip-live", action="store_true", help="跳过实时接口")
    ap.add_argument("--skip-month", action="store_true", help="跳过月度接口")
    ap.add_argument(
        "--save-creds",
        action="store_true",
        help="把本次 cookie / token 写入配置文件以便复用",
    )
    ap.add_argument("--top", type=int, default=0, help="仅输出成交额前 N 只(调试用)")
    ap.add_argument(
        "--no-prev-month",
        action="store_true",
        help="当月交易日不足时不自动拼接上一月数据",
    )
    ap.add_argument(
        "--snapshot",
        action="store_true",
        help="把本次实时数据追加为当日快照 (默认开启, 用于日内累积对比)",
    )
    ap.add_argument(
        "--no-snapshot",
        action="store_true",
        help="关闭当日快照累积",
    )
    ap.add_argument(
        "--use-live-cache",
        action="store_true",
        help="当日已有快照时直接复用, 不重新拉取实时行情",
    )
    ap.add_argument(
        "--save-cache",
        action="store_true",
        help="把月度走势写入按月缓存 (下次同月直接复用)",
    )
    ap.add_argument(
        "--refresh-month",
        action="store_true",
        help="忽略月度缓存, 强制重新拉取",
    )
    ap.add_argument(
        "--live-ttl",
        type=int,
        default=10,
        metavar="MIN",
        help="实时数据新鲜度阈值: 最近快照在 MIN 分钟内则直接复用 (默认 10)",
    )
    ap.add_argument(
        "--refresh-live",
        action="store_true",
        help="强制重新拉取实时数据, 忽略收盘判断与 TTL 阈值",
    )
    args = ap.parse_args()

    # 快照默认开启 (用户要求当日多次调用累积对比)
    if args.no_snapshot:
        args.snapshot = False
    else:
        args.snapshot = True

    cfg = load_config()
    cookie = args.cookie or cfg.get("cookie", "")
    token = args.clerk_token or cfg.get("clerk_token", "")

    month = args.month
    auto_month = not month
    if not month:
        month = default_month()

    result: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "month": month,
        "sources": {},
        "warnings": [],
        "data": [],
    }

    live: list[dict] = []
    month_rows: list[dict] = []

    today = datetime.now().strftime("%Y-%m-%d")

    # --- 数据源 1: 实时行情 ---
    # 拉取前的三道闸门 (依次判断, 任一命中即复用快照, 不调接口):
    #   1. --use-live-cache 显式要求复用
    #   2. 已收盘 (>= 15:05): 行情定格, 再调也无变化
    #   3. 距上次快照 < --live-ttl 分钟 (默认 10): 连续对话场景避免重复拉取
    # 用 --refresh-live 可强制跳过 2、3 两道闸门。
    if not args.skip_live:
        snap_info = None
        reuse_reason = None
        last_snap = None

        if not args.refresh_live:
            last_snap, age_min = latest_snapshot_age(today)

            if args.use_live_cache and last_snap:
                reuse_reason = "--use-live-cache"
                detail = "显式要求复用当日快照"
            elif last_snap and is_market_closed():
                reuse_reason = "已收盘"
                detail = (
                    f"当前已过 15:05 收盘时间, 行情已定格, "
                    f"直接复用 {last_snap.get('time')} 的快照"
                )
            elif last_snap and age_min is not None and age_min < args.live_ttl:
                reuse_reason = "TTL 内"
                detail = (
                    f"距上次采集({last_snap.get('time')})仅 {age_min:.1f} 分钟 "
                    f"< {args.live_ttl} 分钟阈值, 数据仍新鲜"
                )

        if reuse_reason:
            live = last_snap.get("rows") or []
            result["sources"]["live"] = {
                "ok": True,
                "count": len(live),
                "via": f"复用快照 ({reuse_reason})",
                "snapshot_time": last_snap.get("time"),
            }
            result["warnings"].append(f"实时行情未重新拉取: {detail}。")

        if not live:
            live, err = fetch_live(cookie)
            if err:
                result["sources"]["live"] = {"ok": False, "error": err, "count": 0}
                result["warnings"].append(
                    f"实时数据获取失败: {err} —— 请手动提供 cookie 后重试 (--cookie)"
                )
            else:
                result["sources"]["live"] = {"ok": True, "count": len(live)}
                if args.snapshot:
                    snap_info = append_live_snapshot(today, live)
                    result["live_snapshot"] = snap_info
                    result["sources"]["live"]["snapshot"] = (
                        f"#{snap_info['seq']} @ {snap_info['time']}"
                    )

        # 当日多次调用 -> 自动对比首条与最新快照
        if args.snapshot and not args.skip_live:
            snaps = load_live_snapshots(today)
            if len(snaps) >= 2:
                d = diff_snapshots(snaps[0], snaps[-1])
                if d:
                    result["intraday_diff"] = d
            if snaps:
                result["today_snapshots"] = [
                    {"seq": s.get("seq"), "time": s.get("time"), "count": s.get("count")}
                    for s in snaps
                ]

    # --- 数据源 2: 月度走势 (优先按月缓存, 未命中才调接口) ---
    if not args.skip_month:
        cached = None if args.refresh_month else load_month_cache(month)
        if cached:
            month_rows = cached
            result["sources"]["month"] = {
                "ok": True,
                "count": len(cached),
                "via": "按月缓存 (未重新拉取)",
            }
        else:
            # 未命中缓存 -> 先试认证接口, 失败则退回公开 series
            month_rows, err = fetch_month(month, token, args.days)

            if not err:
                if args.save_cache:
                    save_month_cache(month, month_rows, "month")
                result["sources"]["month"] = {
                    "ok": True,
                    "count": len(month_rows),
                    "via": "month (已认证)",
                }
            else:
                # 认证失败的常见原因是 JWT 过期 (Clerk token TTL 仅 60 秒)
                auth_failed = "未登录" in err or "401" in err
                fallback_rows, fb_err = fetch_series(month, args.days)

                if fallback_rows:
                    month_rows = fallback_rows
                    if args.save_cache:
                        save_month_cache(month, fallback_rows, "series")
                    result["sources"]["month"] = {
                        "ok": True,
                        "count": len(fallback_rows),
                        "via": "series (公开兜底)",
                        "auth_error": err if auth_failed else None,
                    }
                    if auth_failed and token:
                        result["warnings"].append(
                            "Clerk token 已过期或无效, 已自动改用公开 series 接口。"
                            "注意 Clerk JWT 有效期仅约 60 秒, 需要立即使用。"
                        )
                    elif auth_failed:
                        result["warnings"].append(
                            "月度接口未认证, 已自动改用公开 series 接口获取走势数据。"
                        )
                else:
                    result["sources"]["month"] = {
                        "ok": False,
                        "error": err,
                        "count": 0,
                    }
                    if auth_failed:
                        result["needs_clerk_token"] = True
                        result["warnings"].append(
                            "月度走势接口需要 Clerk 登录, 且公开 series 兜底也失败 "
                            f"({fb_err})。请在页面控制台执行 "
                            "await window.Clerk.session.getToken() 取得 JWT 后立即重跑。"
                        )
                    else:
                        result["warnings"].append(f"月度走势获取失败: {err}")

    # --- 月初补数: 若当月交易日不足, 拼接上一月数据形成连续窗口 ---
    if month_rows and not args.no_prev_month:
        # 用中位数而非最大值判断: 个股间 series 长度不一致 (min 1 / max 15),
        # 取 max 会被个别数据全的品种掩盖整体样本不足的问题。
        lens = sorted(len(r.get("series") or []) for r in month_rows)
        cur_days = lens[len(lens) // 2] if lens else 0
        result["month_trading_days"] = cur_days
        result["month_days_min"] = lens[0] if lens else 0
        result["month_days_max"] = lens[-1] if lens else 0

        # 触发条件: 当月交易日中位数 < min(days, 10), 即月初或数据未更新
        threshold = min(args.days, 10)
        if cur_days < threshold:
            pm = prev_month_of(month)
            prev_rows, prev_err = fetch_series(pm, args.days)

            if prev_rows:
                prev_map = {r.get("symbol"): r for r in prev_rows}
                stitched = [
                    stitch_series(prev_map.get(r.get("symbol")), r)
                    for r in month_rows
                ]
                # 拼接后按 days 截取最近 N 天
                for r in stitched:
                    s = r.get("series") or []
                    if len(s) > args.days:
                        r["series"] = s[-args.days :]

                month_rows = stitched
                new_days = max(
                    (len(r.get("series") or []) for r in month_rows), default=0
                )
                result["prev_month_stitched"] = {
                    "prev_month": pm,
                    "days_before": cur_days,
                    "days_after": new_days,
                }
                result["sources"]["month_prev"] = {
                    "ok": True,
                    "count": len(prev_rows),
                    "month": pm,
                }
                result["warnings"].append(
                    f"当月仅 {cur_days} 个交易日, 已自动拼接 {pm} 数据, "
                    f"形成 {new_days} 天的连续窗口。"
                )
            else:
                result["sources"]["month_prev"] = {
                    "ok": False,
                    "error": prev_err,
                    "month": pm,
                }
                result["warnings"].append(
                    f"⚠️ 当月仅 {cur_days} 个交易日, 动量与波动率样本不足; "
                    f"尝试补取 {pm} 数据也失败 ({prev_err})。"
                )
        else:
            # 数据充足, 无需拼接 —— 只补天数信息, 保留原有的 via/auth 元数据
            src = result["sources"].setdefault("month", {"ok": True})
            src.setdefault("count", len(month_rows))
            src["days_sufficient"] = True

    # --- 合并 ---
    merged = derive_metrics(live, month_rows) if live else month_rows
    merged = score_etfs(merged)

    # 数据充分性检查: 用中位数, 避免个别品种掩盖整体样本不足
    day_counts = sorted(r["month_days"] for r in merged if r.get("month_days"))
    if day_counts:
        actual_days = day_counts[len(day_counts) // 2]
        result["month_trading_days"] = actual_days
        if actual_days < 10:
            result["warnings"].append(
                f"⚠️ 可用走势数据中位数仅 {actual_days} 个交易日 "
                f"(窗口设定 {args.days} 天), 动量与波动率指标参考价值有限, "
                "建议结合更长周期判断。"
            )

    if args.top and args.top > 0:
        merged = sorted(
            merged, key=lambda x: x.get("amount") or 0, reverse=True
        )[: args.top]

    result["data"] = merged
    result["total"] = len(merged)

    if args.save_creds:
        if args.cookie:
            cfg["cookie"] = args.cookie
        if args.clerk_token:
            cfg["clerk_token"] = args.clerk_token
        save_config(cfg)
        result["credentials_saved"] = str(CONFIG_PATH)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 给调用方 (agent) 的简明摘要
    summary = {
        "out": str(out_path),
        "total": result["total"],
        "month": month,
        "sources": result["sources"],
        "warnings": result["warnings"],
        "needs_clerk_token": result.get("needs_clerk_token", False),
    }
    if result.get("live_snapshot"):
        summary["live_snapshot"] = result["live_snapshot"]
    if result.get("today_snapshots"):
        summary["today_snapshots"] = result["today_snapshots"]
    if result.get("intraday_diff"):
        d = result["intraday_diff"]
        summary["intraday_diff"] = {
            "from": d["first"],
            "to": d["last"],
            "compared": d["compared"],
            "moved": d.get("moved", 0),
            "top_climbing": [
                "%s %s %+.2f%%->%+.2f%%"
                % (x["symbol"], x["name"][:14], x["percent_first"], x["percent_last"])
                for x in d["climbing"][:5]
            ],
            "top_falling": [
                "%s %s %+.2f%%->%+.2f%%"
                % (x["symbol"], x["name"][:14], x["percent_first"], x["percent_last"])
                for x in d["falling"][:5]
            ],
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 两个源都失败 -> 非零退出
    ok_any = any(v.get("ok") for v in result["sources"].values())
    return 0 if ok_any else 1


if __name__ == "__main__":
    sys.exit(main())
