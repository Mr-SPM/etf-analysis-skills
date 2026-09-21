# etf-analysis — A股 ETF 分析技能

调用两个数据源获取 A 股 ETF 行情与走势，结合你的持仓生成交易分析。

## 特点

- **零第三方依赖** — 只用 Python 标准库，任何 Python 3.9+ 环境直接可用，无需 pip install
- **可移植** — 不含任何硬编码绝对路径，所有本地状态按 `Path.home()` 动态解析
- **快照缓存** — 实时行情按天累积、月度走势按月缓存，连续对话不重复调接口
- **自动降级** — 认证接口失败时自动改用公开接口，无需人工干预

## 安装

需要 Python 3.9 或更高版本。

```bash
python install.py
```

脚本会自动完成四项检查（Python 版本、源码完整性、数据源连通性、端到端自检）
并安装到 `~/.workbuddy-ai/skills/etf-analysis`。

安装到其他位置：

```bash
python install.py --dest /path/to/skills
```

只做检查、不安装：

```bash
python install.py --check
```

## 手动安装

不想用安装脚本的话，把 `SKILL.md`、`prompts/`、`scripts/` 三个部分复制到
技能目录即可，不需要执行任何安装命令。

```
<技能目录>/etf-analysis/
├── SKILL.md
├── prompts/prompt_template.md
└── scripts/{fetch_etf.py, build_prompt.py, clerk_auth.py}
```

## 使用

采集数据：

```bash
python scripts/fetch_etf.py --out etf_data.json
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
| `--month YYYY-MM` | 指定月份，默认当月 |
| `--days N` | 月度窗口天数，默认 15 |
| `--refresh-live` | 强制重新拉取实时数据（忽略收盘判断与 TTL） |
| `--live-ttl MIN` | 实时数据新鲜度阈值，默认 10 分钟 |
| `--refresh-month` | 忽略月度缓存，强制重拉 |
| `--save-cache` | 把月度走势写入缓存 |
| `--skip-live` / `--skip-month` | 只取单个数据源 |
| `--cookie "<cookie>"` | 实时接口失败时手动提供 |

## 数据源

| 来源 | 接口 | 认证 |
|---|---|---|
| 实时行情 | `my-stock.918968.xyz/all` | 可选 cookie |
| 月度走势 | `stock2.1168168.xyz/api/etf/month` | Clerk JWT（60 秒有效） |
| 月度走势（兜底） | `stock2.1168168.xyz/api/etf/series` | 无需认证 |

认证接口通常用不上 —— 公开的 `series` 接口返回等价数据，脚本会自动降级。

## 数据存放位置

```
~/.workbuddy-ai/etf-plugin/
├── config.json          # cookie / token（用 --save-creds 保存）
└── cache/
    ├── month_YYYY-MM.json     # 月度走势，按月缓存
    └── live_YYYY-MM-DD.json   # 实时快照，按天累积
```

删除缓存目录即可重置所有本地状态，不影响技能本身。

## 环境要求

- Python 3.9+
- 能访问上述两个数据源（国内网络通常直连即可）
