# 数据源接口清单

> 本文件包含真实接口地址，**不要提交到公开仓库，也不要对外分享**。

## 实时行情

| 项 | 值 |
|---|---|
| 地址 | `https://my-stock.918968.xyz/all` |
| 方法 | `GET` |
| 认证 | 无需登录；cookie 可选，失败时可手动提供 |
| 返回 | 1600+ 只 ETF 实时行情 |
| 字段 | `symbol`, `name`, `current`, `percent`, `chg`, `amount`, `volume`, `premium_rate`, `unit_nav`, `acc_unit_nav`, `market_capital`, `followers`, `current_year_percent`, `total_shares`, `etf_types` |

## 走势数据（API Key 认证）

| 项 | 值 |
|---|---|
| 地址 | `https://stock2.1168168.xyz/api/etf/month` |
| 方法 | `GET` |
| 认证 | `Authorization: Bearer etf_xxxxxxxx...`（**必需**，`etf_` + 64 位十六进制） |
| 参数 | `days=15` 优先；`month=YYYY-MM` 次之（**传了 days 则 month 被忽略**）。脚本**只传 `days`**，不支持整月模式 |
| 返回 | `{success, data: { "<trade_date>": [ {symbol, name, percent} ] }}` |
| 字段 | `percent` 为百分数（1.25 = +1.25%） |
| Key 获取 | 登录站点 → 用户菜单 → 「API Key 管理」→ 生成 |

**`days` 是自然日**（跨月连续），不是交易日。`days=15` 即最近 15 个自然日。
脚本默认 `days=15`，用户指定「30 天内」时传 `--days 30`。

> 路径里的 `month` 是服务端的历史命名，语义上这是**走势接口**。
> 代码里对应常量叫 `TREND_URL`。

Key 存明文、永久有效，但可在网站上吊销；吊销后接口返回 401。

## 其他

| 用途 | 值 |
|---|---|
| 站点 Referer | `https://stock2.1168168.xyz/`（接口对此敏感，脚本已带上） |
| API Key 管理页 | `https://stock2.1168168.xyz/settings/apikey` |
| 登录入口 | `https://stock2.1168168.xyz/` |

## 代码中对应的常量

`scripts/fetch_etf.py` 顶部集中定义了这些地址，改地址只需改这一处：

```python
LIVE_URL         = "https://my-stock.918968.xyz/all"
TREND_URL        = "https://stock2.1168168.xyz/api/etf/month"
API_KEY_PREFIX   = "etf_"
DEFAULT_DAYS     = 15
APIKEY_PAGE_HINT = "https://stock2.1168168.xyz/settings/apikey"
```
