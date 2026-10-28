# 数据源接口清单

> 本文件包含真实接口地址，**不要提交到公开仓库，也不要对外分享**。

## 实时行情

| 项  | 值                                                                                                                                                                                                 |
| -- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 地址 | `https://my-stock.918968.xyz/all`                                                                                                                                                                 |
| 方法 | `GET`                                                                                                                                                                                             |
| 认证 | 无需登录；cookie 可选，失败时可手动提供                                                                                                                                                                           |
| 返回 | 1600+ 只 ETF 实时行情                                                                                                                                                                                  |
| 字段 | `symbol`, `name`, `current`, `percent`, `chg`, `amount`, `volume`, `premium_rate`, `unit_nav`, `acc_unit_nav`, `market_capital`, `followers`, `current_year_percent`, `total_shares`, `etf_types` |

## 走势数据（API Key 认证）

| 项      | 值                                                                                |
| ------ | -------------------------------------------------------------------------------- |
| 地址     | `https://stock2.1168168.xyz/api/etf/month`                                       |
| 方法     | `GET`                                                                            |
| 认证     | `Authorization: Bearer etf_xxxxxxxx...`（**必需**，`etf_` + 64 位十六进制）                |
| 参数     | `days=15` 优先；`month=YYYY-MM` 次之（**传了 days 则 month 被忽略**）。脚本**只传 `days`**，不支持整月模式 |
| 返回     | `{success, data: { "<trade_date>": [ {symbol, name, percent} ] }}`               |
| 字段     | `percent` 为百分数（1.25 = +1.25%）                                                    |
| Key 获取 | 登录站点 → 用户菜单 → 「API Key 管理」→ 生成                                                   |

**`days` 是自然日**（跨月连续），不是交易日。`days=15` 即最近 15 个自然日。
  
脚本默认 `days=15`，用户指定「30 天内」时传 `--days 30`。

> 路径里的 `month` 是服务端的历史命名，语义上这是**走势接口**。
>   
> 代码里对应常量叫 `TREND_URL`。

Key 存明文、永久有效，但可在网站上吊销；吊销后接口返回 401。


## 日线 OHLC（无需认证，2026-10-20 新增）

**这是 MACD / KDJ 的正确数据源，也是实时行情的备用源。**

### 1. 历史日 K（前复权）— 主源

| 项     | 值                                                                                   |
| ----- | ----------------------------------------------------------------------------------- |
| 地址    | `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{count},qfq` |
| 方法    | `GET`，需 `Referer: https://gu.qq.com/`                                               |
| 认证    | **无**                                                                               |
| code  | 小写带前缀：`sz159529` / `sh512800`                                                       |
| count | 取多少根，320 ≈ 15 个月；上限约 640                                                            |
| 返回    | `{"data":{"sz159529":{"qfqday":[[date,open,close,high,low,volume],...]}}}`          |
| 字段顺序  | **date, open, close, high, low, volume**（注意 close 在 high 之前）                        |

- `qfq` → 前复权，返回键为 `qfqday`；不传 → 不复权，键为 `day`。
- 实测 400 只 5.1 秒（12 线程），成功率 395/400；失败的 5 只是 `SZ158xxx` 特殊代码。
- **不含节假日假 K 线**（10-01~10-07 完全不存在），比 `/api/etf/month` 干净。
- 最后一行是**当日盘中实时** K 线，收盘价即最新价。

#### count 上限（2026-10-20 实测）

| 请求 count | 实际返回      | 起始日期       |
| -------- | --------- | ---------- |
| 320      | 321 根     | 2025-06-24 |
| 640      | 641 根     | 2024-02-21 |
| 1000     | **648 根** | 2024-02-02 |
| 1500     | **648 根** | 2024-02-02 |

**上限 648 根（约 2.6 年）。请求超过 648 不报错，静默截断** —— 必须自己校验 `len(bars)`。

#### 日期区间模式

`param={code},day,{start},{end},{count},{adj}`，第 3、4 位填 `YYYY-MM-DD`。

⚠️ **传了 `end` 之后当日 K 线可能不返回**（实测同一请求里 159530 到 10-20，512710 只到 10-19）。
  
**做指标计算不要传 `end`**，用 `count` 取最近 N 根，当日 K 线一定在最后一行。

#### 三域名等价，会限流（重要）

| 域名                     | 路径                                       | 前复权 | 限流表现             |
| ---------------------- | ---------------------------------------- | --- | ---------------- |
| `web.ifzq.gtimg.cn`    | `/appstock/app/fqkline/get`              | ✓   | **高频后 HTTP 501** |
| `ifzq.gtimg.cn`        | `/appstock/app/fqkline/get`              | ✓   | 正常               |
| `proxy.finance.qq.com` | `/ifzqgtimg/appstock/app/newfqkline/get` | ✓   | 正常               |

连续抓 400 只后 `web.ifzq` 会持续 501，另两个域名正常。三者返回格式一致、都支持 `qfq`
  
（验证：510880 首根开盘 前复权 3.002 / 不复权 3.145）。
  
`fetch_ohlc.py` 已实现**自动轮换**：失败一次降权，下次优先用健康域名。

### 2. 批量报价（当日 OHLC + 成交额 + 涨跌幅）— 实时备用源

| 项  | 值                                                    |
| -- | ---------------------------------------------------- |
| 地址 | `https://qt.gtimg.cn/q={codes}`（逗号分隔，**单次上限 200 只**） |
| 编码 | **GBK**，不是 UTF-8                                     |
| 认证 | **无**                                                |
| 返回 | `v_sz159529="51~名称~代码~现价~昨收~今开~成交量~...";` 用 `~` 分割   |

关键下标：`1`名称 `3`现价 `4`昨收 `5`今开 `30`时间戳 `31`涨跌额 `32`涨跌幅
  
`33`最高 `34`最低 `37`成交额(万元) `43`振幅 `47`涨停 `48`跌停

- 1709 只全部约 **2.2 秒**（9 个请求）。
- 实测与 `/api/etf/month` 的现价偏差为 0。
- **当 `/all` 实时接口超时（curl 28）时，这是首选替代**，无需 cookie。

### 3. 新浪日 K（不复权）— 备用

| 项  | 值                                                                                                                                           |
| -- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 地址 | `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={count}` |
| 认证 | **无**                                                                                                                                       |
| 字段 | 具名 JSON：`day/open/high/low/close/volume`，最易解析                                                                                               |
| 限制 | 不复权，`datalen` 上限约 1023                                                                                                                      |

### 4. 雪球 K 线 — 不可用

`https://stock.xueqiu.com/v5/stock/chart/kline.json` 即使带有效 cookie 也返回
  
`{"items":[],"items_size":0}`，**已放弃**。

### 配套脚本

```bash
# 历史 K 线 → CSV（喂给 indicators.py --ohlc）
python fetch_ohlc.py --from-data etf_data.json --top 400 --csv ohlc.csv

# 当日批量报价（替代实时接口）
python fetch_ohlc.py --from-data etf_data.json --all --quotes --out quotes.json

# 取更长的历史（上限 648 根）
python fetch_ohlc.py --symbols SZ159530 --count 640 --out ohlc_long.json

# 取指定日期区间（注意：当日 K 线可能缺失）
python fetch_ohlc.py --symbols SZ159530 --start 2026-09-01 --end 2026-10-20 --out win.json
```

### 5. MACD 翻红预警脚本

```bash
python turn_red.py --ohlc ohlc_500.json --quotes quotes.json --min-amount 1.0 --out turn_red.json
```

输出每只标的的：MACD 阶段（刚翻红/假翻红/明日即翻红/1-2日/3日内/绿柱缩短/绿柱扩大）、
  
**翻红临界价 P*** 与「需涨 %」、柱值占价格比、KDJ 全参数、金叉天数、量比、近3日涨幅、综合评分与判定。

### 6. 全市场扫描 — 一条命令（推荐入口）

```bash
python scan_market.py                                  # 漏斗模式，约 8 秒
python scan_market.py --all-market                     # 全市场逐只，约 16 秒
python scan_market.py --min-amount 1 --count 150 --out scan.json
```

内部流程：批量报价（2 秒）→ 按成交额筛候选池 → 逐只拉 K 线 → MACD/KDJ 分类评分。
  
`score_pool()` 与 `turn_red.py` 共用同一份代码，不存在两套口径。

**全市场逐只到底慢不慢（2026-10-20 实测）：**

| 模式             | 标的数        | 耗时       | 成功        |
| -------------- | ---------- | -------- | --------- |
| 漏斗（成交额≥0.5亿）   | 1709 → 446 | **8 秒**  | 446/446   |
| `--all-market` | 1709       | **16 秒** | 1709/1709 |

结论：**逐只请求不麻烦**。全市场 1709 只 16 秒跑完、0 失败，`web.ifzq` 这次也没限流
  
（此前 501 是瞬时行为，三域名轮换仍保留作保险）。
  
1709 只里有 79 只（`SZ158xxx` / `SH56xxxx` 等）腾讯根本不认识，是趋势接口多给的未上市/预留代码，
  
真实可交易池约 1630 只。

## 其他

| 用途          | 值                                            |
| ----------- | -------------------------------------------- |
| 站点 Referer  | `https://stock2.1168168.xyz/`（接口对此敏感，脚本已带上）  |
| API Key 管理页 | `https://stock2.1168168.xyz/settings/apikey` |
| 登录入口        | `https://stock2.1168168.xyz/`                |

## 代码中对应的常量

`scripts/fetch_etf.py` 顶部集中定义了这些地址，改地址只需改这一处：

```python
LIVE_URL         = "https://my-stock.918968.xyz/all"
TREND_URL        = "https://stock2.1168168.xyz/api/etf/month"
API_KEY_PREFIX   = "etf_"
DEFAULT_DAYS     = 15
APIKEY_PAGE_HINT = "https://stock2.1168168.xyz/settings/apikey"
```

## 5 分钟 K 线（盘中判断「有没有大额扫单」的唯一工具）

```
https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh510300,m5,,1000
```

返回 `data.<code>.m5`，每根 5 分钟：

```
['202610231325', '4.145', '4.155', '4.158', '4.140', '889156.000', {}, '27.166']
   时间(YYYYMMDDHHMM)  开      收      高      低       量(手)       {}   额(万元)
```

- 1000 根约 8 个交易日（每天 48 根）。
- `data.<code>.prec` 是**字符串**，算涨跌幅要先 `float()`。
- 量单位是**手**（x100 股）。510300 全天 2713 万手 x 4.1 元 约 111 亿，与报价接口的 amount 对得上。

### 为什么必须用 5 分钟 K 判断护盘/扫单

日K 只能看到「今天放量」，看不到**是谁在什么时点买的**。5 分钟 K 的成交量历史分位可以直接定位大额扫单。

510300 正常 5 分钟量中位数 19.5 万手，95 分位 93 万手。2026-10-23 出现三根异常：

| 时点    | 量(万手) | 分位    | 同时点正常量 |
| ----- | ----- | ----- | ------ |
| 09:55 | 484   | 98.6% | 15-38  |
| 10:25 | 280   | 97.9% | 12-44  |
| 10:55 | 263   | 97.6% | 6-20   |

**要点**：一定要拿「同时点」的历史量比，不能拿全天中位数比。09:35 和 14:55 天然量大。

### 护盘 vs 反抽的判别（2026-10-22 vs 10-23 实测）

|              | 10-22（护盘）  | 10-23（反抽）         |
| ------------ | ---------- | ----------------- |
| 510300 尾盘量占比 | 51.3%      | 正常                |
| 510050 量能    | 1.66 倍     | **0.60 倍**（萎缩）    |
| 银行           | +0.12%（转红） | **-1.39%（收在最低价）** |
| 结论           | 保指数        | 抄超跌 + 高低切换        |

护盘的特征是「300 + 50 + 银行一起动」；只动 300、50 缩量、银行被砸，是市场自发的超跌反抽。

## 代理陷阱：两个 ifzq 子域的代理行为相反

| 域名                      | 走系统代理           | 直连     |
| ----------------------- | --------------- | ------ |
| `web.ifzq.gtimg.cn`（日K） | 通               | DNS 失败 |
| `ifzq.gtimg.cn`（5分钟K）   | 502 Bad Gateway | 通      |

所以取 5 分钟 K 必须显式绕开代理：

```python
import urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
r = op.open(urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}), timeout=15)
```

## 实时接口故障（2026-10-23）

`fetch_etf.py --refresh-live` 返回 `live: {ok: false, error: "HTTP 500"}`。
  
当日行情只能走腾讯批量报价 `qt.gtimg.cn`；走势接口数据截止到 2026-10-22，不含当日。
  
分析时必须显式说明「当日价格来自腾讯实时，非走势接口」。
