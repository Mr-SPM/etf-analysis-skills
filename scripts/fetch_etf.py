#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 ETF 数据采集器 —— 合并两个数据源。

认证:
    走势接口需要 API Key (在网站「API Key 管理」页生成)。
    Key 通过 Authorization: Bearer <key> 头发送。
    可用 --api-key 传入, 或用 --save-api-key 保存到 config.json 后自动读取。

数据源 1 (实时):
    https://my-stock.918968.xyz/all?cookie=<urlencoded>
    - 无需登录, cookie 可选; 失败时需手动提供 cookie
    - 返回 1600+ 只 ETF 的实时行情
    - 字段: symbol, name, current, percent, chg, amount, volume,
            premium_rate, unit_nav, market_capital, followers,
            current_year_percent, etf_types, ...

数据源 2 (走势, 需 API Key):
    https://stock2.1168168.xyz/api/etf/month?days=15
    - 认证: Authorization: Bearer etf_xxxxx
    - **days 优先于 month**: 传 days=N 返回最近 N 个自然日 (跨月连续);
      传 month=YYYY-MM 返回该月整月 (会覆盖 days)
    - 返回 {success, data: { <trade_date>: [ {symbol, name, percent} ] }}
    - percent 是百分数 (1.25 = +1.25%), 脚本统一换算成小数
    - 脚本会把按日分组的结构转成每只 ETF 的逐日走势 (trend)

用法:
    python fetch_etf.py --out out.json
    python fetch_etf.py --out out.json --days 30
    python fetch_etf.py --out out.json --month 2026-08
    python fetch_etf.py --out out.json --api-key "etf_xxx" --save-api-key
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
API_KEY_PREFIX = "etf_"
DEFAULT_DAYS = 15
CONFIG_PATH = Path.home() / ".workbuddy-ai" / "etf-plugin" / "config.json"
CACHE_DIR = Path.home() / ".workbuddy-ai" / "etf-plugin" / "cache"

# 网站 API Key 管理页 (供报错时引导用户)
APIKEY_PAGE_HINT = "https://stock2.1168168.xyz/settings/apikey"

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


# 走势数据落库时间: 走势接口每天 16:00 才写入当日数据。
#
# 这个阈值不是"收盘时间", 而是**走势数据可供替代实时数据的时间点**:
#   - 15:00      A 股收盘, 行情已定格, 但走势数据尚未包含今天
#   - 15:00-16:00  尴尬窗口: 行情已定格, 走势数据却还没有今天的数据
#                  -> 此时仍需要实时/快照数据, 否则当天行情会缺失
#   - >= 16:00   走势数据已含当日行情 -> 实时数据非必要, 可直接跳过
#
# 故阈值取 16:01 (16:00 落库 + 1 分钟缓冲, 避开落库瞬间的边界)。
MONTH_READY = datetime.strptime("16:01", "%H:%M").time()


def month_data_ready(now: datetime | None = None) -> bool:
    """走势数据是否已落库当日行情 (>= 16:01)。

    注意含义不是"收盘" —— 而是**走势数据已可替代实时数据**。
    15:00-16:00 之间行情虽已定格, 但走势数据仍缺当日, 故仍需实时数据
    兜住当天的记录。

    注意: 这里不判断周末/节假日 —— 非交易日接口同样返回静态数据,
    行为等价且更省。
    """
    now = now or datetime.now()
    return now.time() >= MONTH_READY


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
    """读取本地配置 (存 API Key 等凭据)。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            sys.stderr.write(f"[warn] 读取配置失败 {CONFIG_PATH}: {exc}\n")
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_api_key(key: str) -> None:
    """把 API Key 写入本地配置, 以便后续自动复用。"""
    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)


def resolve_api_key(explicit: str = "") -> str:
    """确定要用的 API Key。

    优先级: 命令行参数 > 环境变量 > 配置文件。
    环境变量 ETF_API_KEY 让 CI/脚本场景无需落盘。
    """
    if explicit:
        return explicit.strip()
    env = (os.environ.get("ETF_API_KEY") or "").strip()
    if env:
        return env
    return (load_config().get("api_key") or "").strip()


def looks_like_api_key(value: str) -> bool:
    """粗判是否为 API Key 而非月份 (YYYY-MM)。"""
    return bool(value) and value.strip().startswith(API_KEY_PREFIX)


# --------------------------------------------------------------------------
# HTTP 传输层 (零第三方依赖: 有 requests 就用, 没有就用标准库)
# --------------------------------------------------------------------------
def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """构造一个**不走系统代理**的 opener。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_NO_PROXY_OPENER = _no_proxy_opener()


def _requests_no_proxy() -> dict:
    """requests 用的 proxies 参数: 显式禁用所有代理。"""
    return {"http": None, "https": None}


def _raw_get(
    url: str,
    headers: dict,
    timeout: int,
    use_proxy: bool,
) -> tuple[int, bytes | None, str | None]:
    """底层 GET, 返回 (status, body_bytes, error)。只在传输层做, 不解析 JSON。"""
    if requests is not None:
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                proxies=None if use_proxy else _requests_no_proxy(),
            )
            return resp.status_code, resp.content, None
        except Exception as exc:
            return 0, None, f"请求失败: {exc}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        if use_proxy:
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = _NO_PROXY_OPENER.open(req, timeout=timeout)
        with resp:
            return resp.status, resp.read(), None
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也会走到这里, 但要保留 status 供上层判断 401
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, body, f"HTTP {exc.code}: {body[:200].decode('utf-8', 'replace')}"
    except Exception as exc:
        return 0, None, f"请求失败: {exc}"


def _note(msg: str) -> None:
    """向 stderr 打进度。

    为什么必须打: 脚本可能因网络问题阻塞几十秒, 期间 stdout 毫无输出,
    调用方 (agent / 用户) 会以为"卡死了"。把每一步写到 stderr,
    配合 flush=True 立即落盘, 就能看到究竟卡在哪一步。
    """
    sys.stderr.write(f"[etf] {msg}\n")
    sys.stderr.flush()


def http_get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 25,
    use_proxy: bool | None = None,
    label: str = "",
) -> tuple[int, object | None, str | None]:
    """GET 并解析 JSON, 返回 (status_code, json_obj, error)。

    status_code 为 0 表示连接层失败 (DNS/超时/TLS 等), 此时 error 有值。
    同时兼容 requests 与标准库 urllib, 保证在**任何** Python 3.9+ 上都能跑 ——
    这是本技能可以零依赖分发到任意机器的基础。

    timeout 是**整次调用的总预算**, 不是每次尝试的预算 ——
    自动重试会把预算平摊给两种通路, 绝不会因为重试而让总耗时翻倍。

    use_proxy:
      - None (默认): **自动** —— 先按系统代理设置试一次, 连接层失败则换另一种
        方式重试一次。因为该数据源在不同机器/不同时段对"直连 vs 代理"的
        兼容性并不稳定 (实测两种都出现过失败), 写死任何一种都会偶发失败。
      - True / False: 强制走 / 不走代理, 不重试。
    """
    headers = headers or {}
    tag = f"{label} " if label else ""

    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    # 决定尝试顺序
    if use_proxy is None:
        # 系统配了代理就先试代理, 否则先试直连 —— 但两种都会兜底重试
        sys_proxy = bool(
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        attempts = [sys_proxy, not sys_proxy]
    else:
        attempts = [use_proxy]

    # 总预算平摊到每次尝试 (至少留 5 秒, 避免切得太碎)
    per_try = max(5, int(timeout / len(attempts)))

    last_err = "请求失败"
    for idx, proxy_mode in enumerate(attempts, 1):
        way = "代理" if proxy_mode else "直连"
        if len(attempts) > 1:
            _note(f"{tag}请求中 ({way}, 第 {idx}/{len(attempts)} 次, 上限 {per_try}s)...")
        else:
            _note(f"{tag}请求中 ({way}, 上限 {per_try}s)...")

        status, body, err = _raw_get(url, headers, per_try, proxy_mode)
        if status != 0:
            # 拿到 HTTP 响应 (含 4xx/5xx) —— 传输层成功, 交给上层判断
            _note(f"{tag}已响应 HTTP {status}")
            if body is None:
                return status, None, err
            try:
                return status, json.loads(body.decode("utf-8")), None
            except Exception as exc:
                return status, None, f"响应不是合法 JSON: {exc}"

        last_err = err or last_err
        _note(f"{tag}{way}失败: {last_err}")
        if use_proxy is not None:
            break  # 强制模式不重试

    return 0, None, last_err


# --------------------------------------------------------------------------
# 数据源 1: 实时行情
# --------------------------------------------------------------------------
def fetch_live(
    cookie: str = "", timeout: int = 12, use_proxy: bool | None = None
) -> tuple[list[dict], str | None]:
    """返回 (items, error)。成功时 error 为 None。"""
    url = LIVE_URL
    if cookie:
        url = f"{LIVE_URL}?cookie={urllib.parse.quote(cookie, safe='')}"

    headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    # 该接口对 Referer 敏感, 带上模拟前端来源
    headers["Referer"] = "https://stock2.1168168.xyz/"

    status, payload, err = http_get_json(
        url,
        headers=headers,
        timeout=timeout,
        use_proxy=use_proxy,
        label="实时行情",
    )
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
# 数据源 2: 走势数据 (API Key 认证)
# --------------------------------------------------------------------------
def fetch_month(
    month: str = "",
    days: int = DEFAULT_DAYS,
    api_key: str = "",
    timeout: int = 20,
    use_proxy: bool | None = None,
) -> tuple[list[dict], str | None]:
    """获取走势数据, 返回 (items, error)。

    服务端行为 (见 api/etf/month.js):
      - **days 优先于 month**: 传 days=N 时, 返回 [今天-N, 今天] 区间的数据 (可跨月)
      - 只在未传 days 时才用 month, 返回该月整月
      - 因此这里优先传 days, month 作为可选覆盖

    返回结构: {success, data: { "<trade_date>": [ {symbol, name, percent}, ... ] }}
    本函数把「按日期分组」转换成「按 ETF 分组」的逐日走势, 便于后续指标计算:
      [{symbol, name, trend: [{trade_date, daily_return, cumulative_return}]}]
    """
    if not api_key:
        return [], (
            "缺少 API Key。请在网站「API Key 管理」页生成后重试:\n"
            f"  {APIKEY_PAGE_HINT}\n"
            f'  然后运行: python fetch_etf.py --out out.json --api-key "etf_..."'
        )

    headers = {
        "User-Agent": UA,
        "Accept": "application/json, */*",
        "Authorization": f"Bearer {api_key}",
    }

    # days 优先; 未指定 days 时才回退到 month (兼容按月份查询)
    params: dict[str, object] = {}
    if days:
        params["days"] = days
    if month:
        params["month"] = month

    status, payload, err = http_get_json(
        MONTH_URL,
        params=params,
        headers=headers,
        timeout=timeout,
        use_proxy=use_proxy,
        label="走势数据",
    )

    if status == 401:
        return [], (
            "API Key 无效或已被吊销 (401)。请到网站「API Key 管理」页\n"
            f"  {APIKEY_PAGE_HINT}\n"
            "  重新生成 Key, 再运行: --api-key \"etf_...\""
        )

    if status == 403:
        return [], "无权访问 (403)。请确认账号状态正常, 或 API Key 未被吊销。"

    if err and status == 0:
        return [], err

    if status != 200:
        return [], f"HTTP {status}"

    if payload is None:
        return [], "响应不是合法 JSON"

    if isinstance(payload, dict) and not payload.get("success", True):
        return [], payload.get("error") or "接口返回 success=false"

    grouped = (payload or {}).get("data") or {}
    if not isinstance(grouped, dict):
        return [], "响应 data 结构异常 (预期按日期分组的对象)"

    # 按日期分组 -> 按 ETF 聚合, 并按交易日排序
    #
    # 注意: 响应里**每行自带 trade_date**, 外层 key 也是日期。
    # 优先用行内的 (更可靠, 不怕外层 key 缺失或与行错位);
    # 行内没有时才回退到外层 key。
    by_symbol: dict[str, dict] = {}
    for outer_date, rows in grouped.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            if not sym:
                continue

            trade_date = row.get("trade_date") or outer_date

            item = by_symbol.get(sym)
            if item is None:
                item = {"symbol": sym, "name": row.get("name") or sym, "trend": []}
                by_symbol[sym] = item

            item["trend"].append(
                {
                    "trade_date": trade_date,
                    "daily_return": _pct(row.get("percent")),
                }
            )

    items: list[dict] = []
    for item in by_symbol.values():
        # 同一天可能出现重复行 (多个来源), 按 trade_date 去重保留最后一条
        dedup: dict[str, dict] = {}
        for point in item["trend"]:
            dedup[point["trade_date"]] = point
        item["trend"] = [dedup[k] for k in sorted(dedup)]
        _fill_cumulative(item["trend"])
        item["data_type"] = "month"
        item["days"] = len(item["trend"])
        items.append(item)

    return items, None


def _pct(value) -> float | None:
    """把接口返回的百分比统一成小数 (5.2 -> 0.052)。

    服务端 month 接口的 ``percent`` 是百分数 (1.25 表示 +1.25%),
    且**实测为字符串** (如 ``"6.08"``), 故这里用 float() 兜住字符串/数字两种形态。
    统一转成小数, 与既有指标口径一致。
    """
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _fill_cumulative(trend: list[dict]) -> None:
    """用 daily_return 逐日累乘, 生成连续累计收益曲线。

    不能用接口原始的累计值 —— 它按窗口起算, 跨月/跨窗口会跳变。
    """
    acc = 1.0
    for point in trend:
        dr = point.get("daily_return")
        if dr is None:
            point["cumulative_return"] = None
            continue
        acc *= 1.0 + dr
        point["cumulative_return"] = acc - 1.0


# --------------------------------------------------------------------------
# 推导指标 (为分析提供燃料)
# --------------------------------------------------------------------------
def derive_metrics(live: list[dict], month: list[dict]) -> list[dict]:
    """把走势数据挂到实时行情上, 并计算动量/波动等指标。"""
    # 走势索引
    trend_map: dict[str, dict] = {}
    for m in month:
        sym = m.get("symbol")
        if sym:
            trend_map[sym] = m

    merged = []
    for row in live:
        sym = row.get("symbol", "")
        out = dict(row)
        m = trend_map.get(sym)
        if m:
            s = m.get("trend") or []
            out["trend"] = s
            out["trend_days"] = len(s)
            if s:
                rets = [
                    x.get("daily_return")
                    for x in s
                    if isinstance(x.get("daily_return"), (int, float))
                ]

                # 累计收益: _fill_cumulative() 已用日收益逐日累乘重建,
                # 是小数, 统一 ×100 转百分比。
                cum = s[-1].get("cumulative_return")
                if isinstance(cum, (int, float)):
                    out["month_return"] = cum * 100
                # 5 日动量: 用累计收益的两点差, 避免月界跳变
                if len(s) >= 6:
                    c_now = s[-1].get("cumulative_return")
                    c_prev = s[-6].get("cumulative_return")
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


def run_api_key_setup() -> int:
    """调用同目录的 apikey_auth.py 做交互式配置。

    放在单独文件是为了让"配置 Key"这件事可以独立使用;
    这里只是转调, 避免两处逻辑各写一遍。
    """
    import subprocess

    helper = Path(__file__).resolve().parent / "apikey_auth.py"
    if not helper.exists():
        print(
            f"  找不到配置工具: {helper}\n"
            f"  请手动到 {APIKEY_PAGE_HINT} 生成 Key, 再用 --api-key 传入。",
            file=sys.stderr,
        )
        return 1
    try:
        return subprocess.call([sys.executable, str(helper)])
    except Exception as exc:
        print(f"  启动配置工具失败: {exc}", file=sys.stderr)
        return 1


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
        r["tradable"] = bool(liquidity >= 2 and r.get("trend"))

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
    # --out 正常必填; 但 --setup 只做配置、不采集, 所以放宽再手动校验
    ap.add_argument(
        "--out", default="", help="输出 JSON 路径 (除 --setup 外必填)"
    )
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=(
            f"走势窗口天数, 默认 {DEFAULT_DAYS}。"
            "服务端按自然日回溯 (跨月连续), 建议 15 / 30 / 60"
        ),
    )
    ap.add_argument(
        "--month",
        default="",
        help=(
            "按整月查询 YYYY-MM (可选)。"
            "注意: 一旦指定则**覆盖** --days, 返回该月全部数据"
        ),
    )
    ap.add_argument(
        "--api-key",
        default="",
        help=(
            'API Key, 形如 "etf_xxxxx"。'
            "未提供时读取已保存的配置 (见 --save-api-key)"
        ),
    )
    ap.add_argument(
        "--save-api-key",
        action="store_true",
        help="把本次 --api-key 保存到本地配置, 后续无需重复传入",
    )
    ap.add_argument(
        "--setup",
        action="store_true",
        help="配置 API Key (等同运行 apikey_auth.py), 配完即退出; 需在终端里运行",
    )
    ap.add_argument("--cookie", default="", help="实时接口 cookie (可选)")
    ap.add_argument(
        "--use-proxy",
        dest="use_proxy",
        action="store_true",
        default=None,
        help=(
            "强制走系统代理 (HTTP_PROXY/HTTPS_PROXY)。"
            "默认自动: 按系统配置先试一种, 连接失败则换另一种重试"
        ),
    )
    ap.add_argument(
        "--no-proxy",
        dest="use_proxy",
        action="store_false",
        help="强制直连, 不走代理, 失败也不重试",
    )
    ap.add_argument("--skip-live", action="store_true", help="跳过实时接口")
    ap.add_argument("--skip-month", action="store_true", help="跳过走势接口")
    ap.add_argument(
        "--save-creds",
        action="store_true",
        help="把本次 cookie 写入配置文件以便复用",
    )
    ap.add_argument("--top", type=int, default=0, help="仅输出成交额前 N 只(调试用)")
    ap.add_argument(
        "--no-stitch",
        action="store_true",
        help="走势数据不足时不自动向前回补 (仅按 --days 窗口取值)",
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
        help=(
            "强制重新拉取实时数据, 跳过「月度已落库则跳过实时」与 TTL 判断。"
            "注意: 16:00 后月度数据已含当日行情, 加此参数只会在网络受限时"
            "超时失败。默认不要使用。"
        ),
    )
    args = ap.parse_args()

    # --setup: 交互式配置 API Key, 配完即退出 (不采集数据)
    if args.setup:
        return run_api_key_setup()

    if not args.out:
        ap.error("缺少必填参数: --out (输出 JSON 路径)")

    # 快照默认开启 (用户要求当日多次调用累积对比)
    if args.no_snapshot:
        args.snapshot = False
    else:
        args.snapshot = True

    cfg = load_config()
    cookie = args.cookie or cfg.get("cookie", "")
    api_key = resolve_api_key(args.api_key)

    # 缺 Key 时给出**可执行**的一步到位指引, 而不是干等。
    #
    # 注意不能依赖交互提示: 本脚本多数情况下是被 agent / CI 通过管道调用的,
    # stdin 不是终端, input() 要么拿不到输入、要么直接抛异常。
    # 因此这里不做交互询问, 只把"下一步该做什么"写清楚 (含可直接复制的命令),
    # 让 agent 把这段话转达给用户。
    if not api_key and not args.skip_month:
        script_dir = Path(__file__).resolve().parent
        setup_cmd = f'python "{script_dir / "apikey_auth.py"}"'
        sys.stderr.write(
            "\n"
            "  尚未配置 API Key —— 走势数据需要它。\n"
            f"  1. 打开 {APIKEY_PAGE_HINT}\n"
            "     登录后生成 API Key (以 etf_ 开头)\n"
            "  2. 配置 (任选其一):\n"
            f'     {setup_cmd} etf_你的key        # 一行搞定\n'
            f"     {setup_cmd}                     # 交互式, 按提示粘贴\n"
            "     echo etf_你的key | "
            f"{setup_cmd}   # 管道传入\n"
            "\n"
        )
        sys.stderr.flush()

    month = args.month
    # 缓存键: 按 days 查询时用 "d15" 之类的短键, 避免与整月缓存互相污染
    cache_key = month if month else f"d{args.days}"

    result: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": args.days,
        "sources": {},
        "warnings": [],
        "data": [],
    }
    if month:
        result["month"] = month
    if not api_key:
        result["needs_api_key"] = True
        result["api_key_page"] = APIKEY_PAGE_HINT

    live: list[dict] = []
    month_rows: list[dict] = []

    today = datetime.now().strftime("%Y-%m-%d")

    _note(f"开始采集 (days={args.days}" + (f", month={month}" if month else "") + ")")

    # --- 数据源 1: 实时行情 ---
    # 实时行情的取舍逻辑 (按时间段区分, 而不是"尽量少调"):
    #
    #   16:01 前:  实时接口**必要** —— 分两种情况:
    #              a) 盘中 (< 15:00): 当日行情还在变
    #              b) 15:00-16:00: 行情已定格, 但**走势数据尚未落库当日行情**
    #                 (走势数据 16:00 才写入), 此时跳过会丢失当天记录
    #   >= 16:01:  实时接口**不必要, 直接跳过** —— 走势数据已含当日全部行情
    #              (含当日 percent), 再调实时接口是拿一个已经在手边的数据
    #
    # 16:01 后跳过是**正当行为**, 不是降级、不是妥协。若当日已有快照则顺带复用,
    # 作为补充信息 (可选), 但绝不因缺少实时数据而失败。
    skip_live_due_close = not args.refresh_live and month_data_ready()

    if args.skip_live:
        _note("已按 --skip-live 跳过实时行情")
    elif skip_live_due_close:
        _note("已过 16:01, 走势数据已含当日行情 —— 跳过实时接口 (非必要)")

    if not args.skip_live and skip_live_due_close:
        # 走势数据已落库: 优先复用当日快照作为补充, 没有也完全不影响分析
        last_snap, _age = latest_snapshot_age(today)
        if last_snap:
            live = last_snap.get("rows") or []
            result["sources"]["live"] = {
                "ok": True,
                "count": len(live),
                "via": "走势已落库, 复用快照 (实时非必要)",
                "snapshot_time": last_snap.get("time"),
            }
            result["warnings"].append(
                f"已过 16:00, 走势数据已含当日行情, 实时数据非必要, "
                f"复用 {last_snap.get('time')} 的当日快照作为补充。"
            )
        else:
            result["sources"]["live"] = {
                "ok": True,
                "count": 0,
                "via": "走势已落库, 跳过实时 (实时非必要)",
            }
            result["warnings"].append(
                "已过 16:00, 走势数据已含当日行情, 实时接口非必要, 已跳过。"
                "当日行情完整包含在走势数据中, 不影响分析。"
            )

    elif not args.skip_live:
        # 盘中: 实时数据必要, 依次过三道闸门 (任一命中即复用, 不调接口)
        #   1. --use-live-cache 显式要求复用
        #   2. 距上次快照 < --live-ttl 分钟 (默认 10): 连续对话避免重复拉取
        snap_info = None
        last_snap, age_min = latest_snapshot_age(today)

        if args.use_live_cache and last_snap:
            live = last_snap.get("rows") or []
            result["sources"]["live"] = {
                "ok": True,
                "count": len(live),
                "via": "复用快照 (--use-live-cache)",
                "snapshot_time": last_snap.get("time"),
            }
            result["warnings"].append("实时行情未重新拉取: 显式要求复用当日快照。")
        elif last_snap and age_min is not None and age_min < args.live_ttl:
            live = last_snap.get("rows") or []
            result["sources"]["live"] = {
                "ok": True,
                "count": len(live),
                "via": "复用快照 (TTL 内)",
                "snapshot_time": last_snap.get("time"),
            }
            result["warnings"].append(
                f"实时行情未重新拉取: 距上次采集({last_snap.get('time')})仅 "
                f"{age_min:.1f} 分钟 < {args.live_ttl} 分钟阈值, 数据仍新鲜。"
            )

        if not live:
            _note("盘中: 需要实时行情, 开始拉取")
            live, err = fetch_live(cookie, use_proxy=args.use_proxy)
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
        if args.snapshot:
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

    # --- 数据源 2: 走势数据 (month 接口, API Key 认证) ---
    #
    # 服务端 days 优先于 month:
    #   --days 15  -> 最近 15 个自然日 (跨月连续), 默认值
    #   --month    -> 指定整月 (会覆盖 --days)
    if args.skip_month:
        _note("已按 --skip-month 跳过走势数据")
    if not args.skip_month:
        cached = None if args.refresh_month else load_month_cache(cache_key)
        if cached:
            _note(f"走势数据命中缓存 ({cache_key}), 不重新拉取")
            month_rows = cached
            result["sources"]["month"] = {
                "ok": True,
                "count": len(cached),
                "via": f"缓存 ({cache_key}, 未重新拉取)",
            }
        else:
            _note(f"走势数据未命中缓存, 开始拉取 (days={args.days})")
            month_rows, err = fetch_month(
                month=month,
                days=args.days,
                api_key=api_key,
                use_proxy=args.use_proxy,
            )

            if not err:
                if args.save_cache:
                    save_month_cache(cache_key, month_rows, "month")
                result["sources"]["month"] = {
                    "ok": True,
                    "count": len(month_rows),
                    "via": f"month (days={args.days})" if not month else f"month ({month})",
                }
                if args.save_api_key and args.api_key:
                    save_api_key(args.api_key)
                    result["api_key_saved"] = str(CONFIG_PATH)
            else:
                result["sources"]["month"] = {"ok": False, "error": err, "count": 0}
                if not api_key:
                    result["needs_api_key"] = True
                result["warnings"].append(err)

    # --- 数据不足时向前回补: 直接拉长 days 窗口重取 ---
    #
    # month 接口的 days 本身就能跨月, 所以「窗口不够就放大 days 再拉一次」,
    # 不需要按月拼接。
    if month_rows and not args.no_stitch and not month:
        lens = sorted(len(r.get("trend") or []) for r in month_rows)
        cur_days = lens[len(lens) // 2] if lens else 0
        result["trading_days"] = cur_days
        result["trading_days_min"] = lens[0] if lens else 0
        result["trading_days_max"] = lens[-1] if lens else 0

        # 交易日中位数不足期望的一半 -> 数据可能未更新, 扩大窗口重取
        if cur_days and cur_days < max(args.days // 2, 3):
            wider = args.days * 2
            _note(f"交易日仅 {cur_days} 个, 扩大到 days={wider} 重取")
            wide_rows, wide_err = fetch_month(
                days=wider, api_key=api_key, use_proxy=args.use_proxy
            )
            if wide_rows:
                new_lens = sorted(len(r.get("trend") or []) for r in wide_rows)
                new_days = new_lens[len(new_lens) // 2] if new_lens else 0
                if new_days > cur_days:
                    month_rows = wide_rows
                    if args.save_cache:
                        save_month_cache(cache_key, month_rows, "month")
                    result["sources"]["month"]["count"] = len(month_rows)
                    result["warnings"].append(
                        f"走势数据仅 {cur_days} 个交易日, 已扩大到 days={wider} "
                        f"重新获取 ({new_days} 个交易日)。"
                    )

    # --- 合并 ---
    merged = derive_metrics(live, month_rows) if live else month_rows
    merged = score_etfs(merged)

    # 数据充分性检查: 用中位数, 避免个别品种掩盖整体样本不足
    day_counts = sorted(r["trend_days"] for r in merged if r.get("trend_days"))
    if day_counts:
        actual_days = day_counts[len(day_counts) // 2]
        result["trading_days"] = actual_days
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
    _note(f"采集完成: {len(merged)} 条记录")

    if args.save_creds:
        if args.cookie:
            cfg["cookie"] = args.cookie
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
        "days": args.days,
        "sources": result["sources"],
        "warnings": result["warnings"],
    }
    if month:
        summary["month"] = month
    if result.get("needs_api_key"):
        summary["needs_api_key"] = True
        summary["api_key_page"] = APIKEY_PAGE_HINT
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
