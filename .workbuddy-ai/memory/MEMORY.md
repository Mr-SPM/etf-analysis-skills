# etf-analysis — 项目长期约定

## 架构（已定型，别再往上加层）

```
用户 → SKILL.md → fetch_etf.py → ETF JSON → AI 直接分析
```

- **只有一个采集脚本**：`scripts/fetch_etf.py`（另有 `apikey_auth.py` 配 Key）
- **没有中间层**：不存在 `build_prompt.py` / `prompts/` / 提示词模板 / 渲染步骤
  （2026-09-22 用户明确要求删除）
- 设计原则（用户原话）：**Python 负责把「最近 N 个自然日」的数据稳定拿回来，
  Skill 负责分析；用户自己决定观察窗口，不让程序擅自扩大**
- 不要为了一个简单功能不断增加中间层

## 文件职责

| 文件 | 职责 |
|---|---|
| `SKILL.md` | 唯一的流程 + 分析要求（含原 prompt_template 的分析框架） |
| `scripts/fetch_etf.py` | 采集 + 缓存 + 派生指标 + 统计，输出自描述 JSON |
| `scripts/apikey_auth.py` | API Key 配置 |
| `install.py` | 安装器，**不装进技能目录** |
| `smoke_test.py` | 离线自测，**不装进技能目录** |
| `docs/`、`ENDPOINTS.md` | 说明材料 |

## 命名：一律用 trend，不用 month

接口路径 `/api/etf/month` 里的 `month` 是**服务端历史命名**，语义是走势接口。
代码/文档统一用 `trend`：`TREND_URL`、`trend_d15.json`、`sources.trend`、
`--refresh-trend`、`--skip-trend`、`window_return`。
**只有接口 URL 本身保留 month。**

## 语义红线（踩过坑，别改回去）

- `days` = **自然日**，跨月连续。脚本**只传 days，不支持 month 模式**
  （接口 days 优先，同时传会让 month 被静默忽略）
- 走势数据 16:00 才落库 → 阈值 `TREND_READY = 16:01`
- **16:01 后不使用旧 live 快照**：快照是盘中某刻的值，当"当前数据"会字段齐全但结论错
- 16:01 后 `percent` 由走势补出（标 `percent_from_trend`），
  **必须校验 `trade_date == data_latest_date`**，否则会把上一天的涨跌当今天
- `amount` 缺失 ≠ 流动性差：`liquidity_grade` / `tradable` 置 `None`
- "平盘"（0%）与"当天无记录"必须分开统计（`breadth.flat` vs `breadth.no_data`）
- **不自动扩大窗口**、不做跨月拼接、不做交易日补足

## 平台注意（WorkBuddy）

- `~/.workbuddy-ai/skills/` 下**每个含 SKILL.md 的子目录都会被当成技能加载**。
  所以技能备份**绝不能**放在 `skills/` 里 —— 无论是 `skills/etf-analysis.bak/`
  还是 `skills/.backup/etf-analysis.<ts>/`，都会被当成第二个技能，agent 可能翻到
  那份旧的 SKILL.md 按废弃流程干活（实测发生过）。
  **已改为 `~/.workbuddy-ai/skill-backups/`（skills/ 之外），且只保留最近一份。**
  验证方法：`find ~/.workbuddy-ai/skills -name SKILL.md` 应该只有 1 个结果。
- **安装副本是双向的**：运行时对已安装文件的修改要回写项目，否则下次安装被覆盖；
  反之项目改动也必须重装/同步，否则运行的是旧代码
- 属于"仓库自己"的东西（`.workbuddy-ai/`、`docs/`、`*.bak`、`smoke_test.py`、
  `.gitignore`）都在 `install.py` 的 ignore 里，不要装进技能目录
- `~/.workbuddy-ai/etf-plugin/` 是**运行时数据目录**（config.json / cache / 输出），
  与技能安装目录是两回事，别混淆

## 环境

- 网络通路可用性**随时间波动**：`stock2.1168168.xyz` 时而 4 条通路全挂。
  不要写死单一路径；脚本内置 urllib/curl × 代理/直连 四条
- 缓存：`~/.workbuddy-ai/etf-plugin/cache/trend_d15.json`
- Python：`C:\Users\admin\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe`
