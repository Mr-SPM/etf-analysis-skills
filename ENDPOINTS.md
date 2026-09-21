# 数据源接口清单

> 本文件包含真实接口地址，**不要提交到公开仓库，也不要对外分享**。
> 当前接口未做鉴权加固，暴露地址等于开放数据消费权限。

## 实时行情

| 项 | 值 |
|---|---|
| 地址 | `https://my-stock.918968.xyz/all` |
| 方法 | `GET` |
| 认证 | 无需登录；cookie 可选，失败时可手动提供 |
| 返回 | 1600+ 只 ETF 实时行情 |
| 字段 | `symbol`, `name`, `current`, `percent`, `chg`, `amount`, `volume`, `premium_rate`, `unit_nav`, `acc_unit_nav`, `market_capital`, `followers`, `current_year_percent`, `total_shares`, `etf_types` |

## 月度走势（需认证）

| 项 | 值 |
|---|---|
| 地址 | `https://stock2.1168168.xyz/api/etf/month?month=YYYY-MM&days=15` |
| 方法 | `GET` |
| 认证 | `Authorization: Bearer <Clerk session JWT>`（**必需**） |
| 返回 | `{success, data: { <bucket>: [ {symbol, name, series:[...]} ] }}` |
| series 字段 | `trade_date`, `cumulative_return`, `daily_return` |
| 备注 | JWT 有效期仅 **60 秒**（`exp - iat = 60`），必须取到后立刻使用 |

## 月度走势（公开兜底）

| 项 | 值 |
|---|---|
| 地址 | `https://stock2.1168168.xyz/api/etf/series?month=YYYY-MM&days=15` |
| 方法 | `GET` |
| 认证 | **无需认证** |
| 返回 | 结构与 `month` 完全一致 |

`month` 接口返回 401 时，采集脚本会自动降级到 `series`，数据等价。

## 其他

| 用途 | 值 |
|---|---|
| 站点 Referer | `https://stock2.1168168.xyz/`（接口对此敏感，脚本已带上） |
| Clerk Publishable Key | `pk_live_Y2xlcmsuMTE2ODE2OC54eXok` |
| Clerk Frontend API | `https://clerk.1168168.xyz` |
| 登录入口 | `https://stock2.1168168.xyz/` |
| 取 token 方式 | 登录后在浏览器控制台执行 `await window.Clerk.session.getToken()` |

## 代码中对应的常量

`scripts/fetch_etf.py` 顶部集中定义了这些地址，改地址只需改这一处：

```python
LIVE_URL   = "https://my-stock.918968.xyz/all"
MONTH_URL  = "https://stock2.1168168.xyz/api/etf/month"
SERIES_URL = "https://stock2.1168168.xyz/api/etf/series"
```
