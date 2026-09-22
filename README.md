# etf-analysis — A股 ETF 分析技能

调用数据源获取 A 股 ETF 走势（盘中额外取实时行情），结合你的持仓生成交易分析。

**分工很明确**：Python 只负责把「最近 N 个自然日」的数据稳定拿回来，分析交给技能。
**观察窗口由你决定** —— 脚本不会擅自扩大窗口，也不会替你改天数。
**没有中间层** —— 脚本产出 JSON，技能直接读 JSON 分析，不存在提示词模板/渲染步骤。

## 特点

- **零第三方依赖** — 只用 Python 标准库，任何 Python 3.9+ 环境直接可用，无需 pip install
- **可移植** — 不含任何硬编码绝对路径，所有本地状态按 `Path.home()` 动态解析
- **观察窗口就是自然日** — `--days N` = 最近 N 个自然日（跨月连续），
  你传多少就是多少，脚本不做自动扩容、不做跨月拼接
- **一个脚本** — `fetch_etf.py` 采集，输出自描述 JSON（含涨跌家数、派生指标、警告）
- **走势缓存** — 走势数据当日拉取一次即落盘复用（跨天或 16:00 落库后自动刷新），
  连续对话不重复调接口
- **按时间语义决定是否取实时** — 16:01 后当日行情已在走势数据里，实时接口直接跳过，
  当日涨跌幅由走势序列补出
- **API Key 认证** — 走势接口用 API Key，存一次即可长期复用，无需反复登录取 token
- **四条网络通路自动兜底** — urllib/curl × 代理/直连，任一成功即停

## 安装

需要 Python 3.9 或更高版本。

```bash
python install.py
```

脚本会做两项部署前检查（Python 版本、源码完整性）并安装到
`~/.workbuddy-ai/skills/etf-analysis`。

安装到其他位置：

```bash
python install.py --dest /path/to/skills
```

只做检查、不安装：

```bash
python install.py --check
```

自检安装结果（可选）：

```bash
python install.py --doctor
```

> 安装脚本**刻意不检查数据源连通性**。连通性属于运行时状态，受代理、防火墙、
> 对端服务重启影响，与安装是否成功没有因果关系；采集脚本本身已具备友好报错
> 能力，因此把连通性检查放进安装流程只会产生虚假失败信号。

## 手动安装

不想用安装脚本的话，把 `SKILL.md` 和 `scripts/` 复制到技能目录即可，
不需要执行任何安装命令。

```
<技能目录>/etf-analysis/
├── SKILL.md
└── scripts/{fetch_etf.py, apikey_auth.py}
```

## 使用

### 首次：配置 API Key

走势接口需要 API Key。登录站点后到 **用户菜单 → 「API Key 管理」** 生成。

**最简单的方式** —— 把 Key 直接跟在命令后面，一行搞定：

```bash
python scripts/apikey_auth.py etf_xxxxx
```

脚本会校验格式并保存，之后 `fetch_etf.py` 自动读取。

其他方式：

```bash
python scripts/apikey_auth.py                 # 交互式（在终端里跑，按提示粘贴）
echo etf_xxxxx | python scripts/apikey_auth.py  # 管道传入
export ETF_API_KEY=etf_xxxxx                  # 环境变量（CI/脚本场景）
python scripts/fetch_etf.py --setup           # 从采集脚本进入配置入口
```

> **在终端里手动运行**时才适合用不带参数的交互式方式；如果是通过管道、脚本或
> 自动化调用，请用「直接跟在命令后」或「管道传入」——这两种不依赖交互输入。

> 粘贴时不用管多余的引号、空格或 `Bearer ` 前缀 —— 脚本会自动清洗；
> 如果复制不全，它会直接告诉你差多少位。

配置好后 `fetch_etf.py` 自动读取。查看/清除：

```bash
python scripts/apikey_auth.py --status
python scripts/apikey_auth.py --clear
```

### 采集数据

```bash
# 默认最近 15 个自然日
python scripts/fetch_etf.py --out etf_data.json

# 要 30 天
python scripts/fetch_etf.py --out etf_data.json --days 30

# 要 60 天
python scripts/fetch_etf.py --out etf_data.json --days 60
```

生成分析：

```bash
# 采集（默认最近 15 个自然日）
python scripts/fetch_etf.py --out etf_data.json

# 然后直接读 etf_data.json 分析即可 —— 没有中间步骤
```

在 WorkBuddy 中直接说「用 ETF 分析技能分析我的持仓」，会走完整流程
（采集 → 读取 JSON → 输出分析）。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--days N` | 走势窗口天数（**自然日**），默认 15 |
| `--out PATH` | 输出 JSON（必填，建议用绝对路径） |
| `--setup` | 进入 API Key 配置入口（不采集数据，需在终端里运行） |
| `--api-key "etf_xxx"` | 手动传 API Key |
| `--save-api-key` | 把 Key 存进配置，后续自动读取 |
| `--use-proxy` / `--no-proxy` | 强制走/不走系统代理。默认自动依次尝试 urllib/curl × 代理/直连 四条通路 |
| `--refresh-live` | 强制重新拉取实时数据。⚠️ 默认不要用：16:00 后行情已定格，加它只会在网络受限时超时 |
| `--live-ttl MIN` | 盘中实时数据新鲜度阈值，默认 10 分钟 |
| `--use-live-cache` | 盘中直接复用当日最后一条快照，不重新拉取 |
| `--refresh-trend` | 忽略走势缓存强制重拉。⚠️ 不要凭猜测加，脚本会自己判断缓存是否过期 |
| `--save-cache` / `--no-save-cache` | 走势数据是否落盘（**默认开启**，同一天内重复调用直接命中缓存） |
| `--skip-live` / `--skip-trend` | 只取单个数据源 |
| `--cookie "<cookie>"` | 实时接口失败时手动提供 |

## 数据源

本技能依赖两个外部数据接口（实时行情 + 走势数据），具体地址、认证方式与返回
结构统一记录在 **`ENDPOINTS.md`**，本文不再重复。

> ⚠️ `ENDPOINTS.md` 含真实接口地址，请勿提交到公开仓库或对外分享。

接口行为要点（不含地址）：

- 实时行情：可选 cookie，失败时可手动提供
- 走势数据：走 **API Key** 认证（`Authorization: Bearer etf_...`），Key 永久有效
- 走势接口的路径里带 `month` 是服务端的历史命名；它同时接受 `days` 与 `month`，
  且 `days` 优先（同时传会让 `month` 被静默忽略）。脚本**只传 `days`**，
  不支持整月模式
- `days` 是**自然日**（跨月连续），默认 15

### 什么时候会去取实时行情

走势接口每天 **16:00** 才落库当日行情，所以：

| 时段 | 实时接口 |
|---|---|
| < 16:01 | 必要（盘中行情在变；15:00~16:00 走势尚未落库，跳过会丢当天记录） |
| ≥ 16:01 | **跳过** —— 当日行情已在走势数据里 |

16:01 后：

- `percent`（当日涨跌幅）**有** —— 脚本从走势序列补出，并标 `percent_from_trend: true`
- `current` / `amount` / `premium_rate` **没有** —— 走势数据里根本没有这些字段，
  这是预期行为，不是采集失败。此时 `liquidity_grade` / `tradable` 为 `null`
  （表示"未知"，不是"流动性差"）

## 数据存放位置

```
~/.workbuddy-ai/etf-plugin/
├── config.json              # api_key / cookie
└── cache/
    ├── trend_d15.json          # 走势数据，按 --days 窗口缓存
    └── live_YYYY-MM-DD.json    # 实时快照，按天累积
```

删除缓存目录即可重置所有本地状态，不影响技能本身。

## 环境要求

- Python 3.9+
- 能访问上述两个数据源（国内网络通常直连即可）
