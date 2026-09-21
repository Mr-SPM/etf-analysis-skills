# etf-analysis — A股 ETF 分析技能

调用两个数据源获取 A 股 ETF 行情与走势，结合你的持仓生成交易分析。

## 特点

- **零第三方依赖** — 只用 Python 标准库，任何 Python 3.9+ 环境直接可用，无需 pip install
- **可移植** — 不含任何硬编码绝对路径，所有本地状态按 `Path.home()` 动态解析
- **快照缓存** — 实时行情按天累积、走势数据按窗口缓存，连续对话不重复调接口
- **API Key 认证** — 走势接口用 API Key，存一次即可长期复用，无需反复登录取 token

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

不想用安装脚本的话，把 `SKILL.md`、`prompts/`、`scripts/` 三个部分复制到
技能目录即可，不需要执行任何安装命令。

```
<技能目录>/etf-analysis/
├── SKILL.md
├── prompts/prompt_template.md
└── scripts/{fetch_etf.py, build_prompt.py, apikey_auth.py}
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

# 看某个整月
python scripts/fetch_etf.py --out etf_data.json --month 2026-08
```

生成分析提示词（需先有持仓信息）：

```bash
python scripts/build_prompt.py --data etf_data.json \
  --portfolio '515880:15000:+0.15,513400:23000:-1.08,159768:10000:+5.40' \
  --out prompt.md
```

在 WorkBuddy 中直接说「用 ETF 分析技能分析我的持仓」，会走完整流程。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--days N` | 走势窗口天数（自然日），默认 15 |
| `--month YYYY-MM` | 指定整月；**会覆盖 `--days`** |
| `--setup` | 进入 API Key 配置入口（不采集数据，需在终端里运行） |
| `--api-key "etf_xxx"` | 手动传 API Key |
| `--save-api-key` | 把 Key 存进配置，后续自动读取 |
| `--no-stitch` | 数据不足时不自动扩大窗口 |
| `--use-proxy` / `--no-proxy` | 强制走/不走系统代理。默认自动回退重试 |
| `--refresh-live` | 强制重新拉取实时数据（跳过「月度已落库则跳过实时」与 TTL 判断） |
| `--live-ttl MIN` | 实时数据新鲜度阈值，默认 10 分钟 |
| `--refresh-month` | 忽略走势缓存，强制重拉 |
| `--save-cache` | 把走势数据写入缓存 |
| `--skip-live` / `--skip-month` | 只取单个数据源 |
| `--cookie "<cookie>"` | 实时接口失败时手动提供 |

## 数据源

本技能依赖两个外部数据接口（实时行情 + 走势数据），具体地址、认证方式与返回
结构统一记录在 **`ENDPOINTS.md`**，本文不再重复。

> ⚠️ `ENDPOINTS.md` 含真实接口地址，请勿提交到公开仓库或对外分享。

接口行为要点（不含地址）：

- 实时行情：可选 cookie，失败时可手动提供
- 走势数据：走 **API Key** 认证（`Authorization: Bearer etf_...`），Key 永久有效
- `days` 优先于 `month`，`days` 是**自然日**（跨月连续），默认 15

## 数据存放位置

```
~/.workbuddy-ai/etf-plugin/
├── config.json              # api_key / cookie
└── cache/
    ├── month_d15.json          # 走势数据，按 --days 窗口缓存
    ├── month_2026-08.json      # 走势数据，按 --month 缓存
    └── live_YYYY-MM-DD.json    # 实时快照，按天累积
```

删除缓存目录即可重置所有本地状态，不影响技能本身。

## 环境要求

- Python 3.9+
- 能访问上述两个数据源（国内网络通常直连即可）
