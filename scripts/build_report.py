# -*- coding: utf-8 -*-
"""从 fetch_etf.py 产出的 etf_data.json 生成自包含 HTML 分析报告。

用法:
    python build_report.py --in  <etf_data.json> --out <report.html> [--title "..."] [--portfolio <portfolio.json>]

设计约束:
- 只读 fetch_etf.py 的 JSON，不重新取数、不重算脚本已给出的指标（仅排序/筛选/聚合）。
- 零外部依赖：不引用 CDN，样式与图表全部内联，离线可看。
- 浅色主题，A股习惯配色（红涨绿跌）。
- trend_days < 交易日中位数的标的自动打「仅N日」标记，提示样本不足。
"""
import argparse
import datetime
import html
import json
import os
from collections import defaultdict

THEMES = [
    '科创人工智能', '人工智能', '科创芯片设计', '芯片设计', '科创芯片', '半导体设备',
    '科创半导体', '半导体', '科创200', '科创50', '科创100', '纳指科技', '纳斯达克',
    '港股医疗', '医疗', '医药', '创新药', '地产', '房地产', '黄金股', '黄金', '油气',
    '石油', '工程机械', '物流', '船舶', '军工', '通信', '恒生消费', '港股通汽车',
    '港股汽车', '汽车', '畜牧', '农业', '粮食', '光伏', '新能源', '电池', '储能',
    '机器人', '证券', '银行', '保险', '煤炭', '钢铁', '有色', '化工', '游戏', '传媒',
    '云计算', '数字经济', '电力', '公用', '红利', '沪深300', '中证500', '中证1000',
    '创业板', '上证50', '北证', '教育', '旅游', '食品饮料', '白酒', '家电', '建材',
    '环保', '稀土', '卫星', '低空',
]


def pct_cls(v):
    if v is None:
        return 'na'
    return 'up' if v > 0 else ('down' if v < 0 else 'flat')


def num(v, n=2):
    return '—' if v is None else f"{v:+.{n}f}"


def plain(v, n=2):
    return '—' if v is None else f"{v:.{n}f}"


def build(src, out, title=None, portfolio=None):
    d = json.load(open(src, encoding='utf-8'))
    data = d['data']
    latest = d.get('data_latest_date')
    days = d.get('days')
    tdays = d.get('trading_days')
    title = title or f"A股 ETF 市场分析 · {latest}"

    # 主题聚合（按 THEMES 顺序匹配，命中即 break，避免重叠主题重复计数）
    agg = defaultdict(lambda: {'n': 0, 'wr': [], 'mom': [], 'pct': []})
    for x in data:
        for t in THEMES:
            if t in x['name']:
                a = agg[t]
                a['n'] += 1
                if x.get('window_return') is not None:
                    a['wr'].append(x['window_return'])
                if x.get('momentum_5trading_days') is not None:
                    a['mom'].append(x['momentum_5trading_days'])
                if x.get('percent') is not None:
                    a['pct'].append(x['percent'])
                break
    theme_rows = [{'name': t, 'n': a['n'],
                   'wr': (sum(a['wr']) / len(a['wr'])) if a['wr'] else None,
                   'mom': (sum(a['mom']) / len(a['mom'])) if a['mom'] else None,
                   'pct': (sum(a['pct']) / len(a['pct'])) if a['pct'] else None}
                  for t, a in agg.items() if a['n'] >= 2]
    theme_rows = [r for r in theme_rows if r['wr'] is not None]
    theme_rows.sort(key=lambda r: -r['wr'])

    # 榜单
    withpct = [x for x in data if x.get('percent') is not None]
    withvol = [x for x in data if x.get('volatility') is not None]
    today_top = sorted(withpct, key=lambda x: -x['percent'])[:10]
    today_bot = sorted(withpct, key=lambda x: x['percent'])[:10]
    withwr = [x for x in data if x.get('window_return') is not None]
    win_top = sorted(withwr, key=lambda x: -x['window_return'])[:10]
    win_bot = sorted(withwr, key=lambda x: x['window_return'])[:10]
    mom_top = sorted([x for x in data if x.get('momentum_5trading_days') is not None],
                     key=lambda x: -x['momentum_5trading_days'])[:10]
    vol_top = sorted(withvol, key=lambda x: -x['volatility'])[:10]

    def row_html(x, show='pct'):
        main = x.get('percent') if show == 'pct' else x.get('window_return')
        td = x.get('trend_days')
        flag = '' if (td and tdays and td >= tdays) else f'<span class="tag warn">仅{td}日</span>'
        return f"""<tr>
      <td class="sym">{x['symbol']}</td>
      <td class="nm">{html.escape(x['name'])}{flag}</td>
      <td class="num {pct_cls(main)}">{num(main)}</td>
      <td class="num {pct_cls(x.get('window_return'))}">{num(x.get('window_return'))}</td>
      <td class="num {pct_cls(x.get('momentum_5trading_days'))}">{num(x.get('momentum_5trading_days'))}</td>
      <td class="num">{plain(x.get('volatility'))}</td>
      <td class="num {pct_cls(x.get('max_daily_drop'))}">{num(x.get('max_daily_drop'))}</td>
    </tr>"""

    def table(rows, show='pct', label='当日'):
        head = (f"<thead><tr><th>代码</th><th>名称</th><th>{label}</th><th>窗口累计</th>"
                f"<th>近5日动量</th><th>波动率</th><th>最大单日跌</th></tr></thead>")
        return f'<table class="tbl">{head}<tbody>' + ''.join(row_html(x, show) for x in rows) + '</tbody></table>'

    maxabs_wr = max((abs(r['wr']) for r in theme_rows), default=1) or 1
    maxabs_pct = max((abs(r['pct']) for r in theme_rows if r['pct'] is not None), default=1) or 1
    theme_today = sorted(theme_rows,
                         key=lambda r: -(r['pct'] if r['pct'] is not None else -99))

    def bar(r, key='wr'):
        v = r.get(key)
        if v is None:
            return ''
        m = maxabs_wr if key == 'wr' else maxabs_pct
        w = abs(v) / m * 100
        return f"""<div class="barrow">
      <div class="bl">{html.escape(r['name'])}<span class="cnt">{r['n']}只</span></div>
      <div class="btrack"><div class="bfill {pct_cls(v)}" style="width:{w:.1f}%"></div></div>
      <div class="bv {pct_cls(v)}">{num(v,1)}%</div>
      <div class="bm">窗口{num(r['wr'],1)}% · 5日{num(r['mom'],1)}%</div>
    </div>"""

    b = d['breadth']
    tb = b['total']
    pct_of = lambda v: v / tb * 100 if tb else 0

    theme_tbl = ''.join(
        f"""<tr><td class="nm">{html.escape(r['name'])}</td><td class="num">{r['n']}</td>
    <td class="num {pct_cls(r['wr'])}">{num(r['wr'])}</td>
    <td class="num {pct_cls(r['mom'])}">{num(r['mom'])}</td>
    <td class="num {pct_cls(r['pct'])}">{num(r['pct'])}</td></tr>""" for r in theme_rows)

    # 持仓区块（可选）
    portfolio_html = ''
    if portfolio and os.path.exists(portfolio):
        pf = json.load(open(portfolio, encoding='utf-8'))
        meta = pf if isinstance(pf, dict) else {}
        items = meta.get('positions') if isinstance(meta.get('positions'), list) else (
            pf if isinstance(pf, list) else [])
        rows = []
        for item in items:
            sym = item.get('symbol')
            m = [x for x in data if x['symbol'] == sym]
            if not m:
                continue
            x = m[0]
            if item.get('amount') is not None:
                c1 = f"{item['amount']:,.2f}"
            elif item.get('shares') is not None:
                c1 = f"{item['shares']:,.0f}"
            else:
                c1 = '—'
            c2 = item.get('entry_date') or (
                f"{item['cost']:.3f}" if item.get('cost') is not None else '—')
            rows.append(f"""<tr><td class="sym">{sym}</td><td class="nm">{html.escape(x['name'])}</td>
            <td class="num">{c1}</td><td class="num">{c2}</td>
            <td class="num {pct_cls(x.get('percent'))}">{num(x.get('percent'))}</td>
            <td class="num {pct_cls(x.get('window_return'))}">{num(x.get('window_return'))}</td>
            <td class="num {pct_cls(x.get('momentum_5trading_days'))}">{num(x.get('momentum_5trading_days'))}</td>
            <td class="num">{plain(x.get('volatility'))}</td>
            <td class="num {pct_cls(x.get('max_daily_drop'))}">{num(x.get('max_daily_drop'))}</td></tr>""")
        if rows:
            tot = meta.get('total_asset')
            head_extra = ''
            if tot:
                held = sum((it.get('amount') or 0) for it in items)
                head_extra = (
                    f'<div class="sub" style="margin:-6px 0 12px">总可用资产 '
                    f'<b>{tot:,.2f}</b> 元 · 持仓 <b>{held:,.2f}</b> 元（{held / tot * 100:.2f}%） · '
                    f'现金 <b>{tot - held:,.2f}</b> 元（{(tot - held) / tot * 100:.2f}%）</div>')
            portfolio_html = f"""<div class="card"><h2>持仓明细</h2>{head_extra}
            <table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>持仓金额</th><th>建仓日期</th>
            <th>当日</th><th>窗口累计</th><th>近5日动量</th><th>波动率</th><th>最大单日跌</th></tr></thead>
            <tbody>{''.join(rows)}</tbody></table></div>"""

    warn_live = ''
    if not any(x.get('amount') is not None for x in data):
        warn_live = ('<li><b>流动性维度缺失：</b>本次数据不含成交额，'
                     '<b>无法评估流动性风险</b>，下单前请自行查看盘口。</li>'
                     '<li><b>溢价风险未知：</b>溢价率字段缺失，跨境类标的可能存在溢价，'
                     '务必在下单前单独确认实时溢价率。</li>')

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    css = """
:root{--bg:#f5f6f8;--card:#fff;--line:#e5e7eb;--tx:#1f2329;--tx2:#5c6570;--tx3:#8b949e;
--up:#d92b2b;--dn:#0f9d58;--acc:#2b6cb0;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-size:14px;line-height:1.6;
font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:26px;margin:0 0 8px;letter-spacing:-.4px}
.sub{color:var(--tx2);font-size:13px}.sub b{color:var(--tx)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:18px}
h2{font-size:17px;margin:0 0 14px;padding-left:10px;border-left:3px solid var(--acc);line-height:1.2}
h3{font-size:14px;margin:20px 0 10px;color:var(--tx2);font-weight:600}
h3:first-of-type{margin-top:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:#fafbfc;border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.kpi .k{font-size:12px;color:var(--tx3);margin-bottom:4px}
.kpi .v{font-size:22px;font-weight:700;letter-spacing:-.5px}
.kpi .v.up{color:var(--up)}.kpi .v.down{color:var(--dn)}
.bar{display:flex;height:12px;border-radius:6px;overflow:hidden;margin:4px 0 8px}
.bar div{height:100%}
.b-up{background:var(--up)}.b-dn{background:var(--dn)}.b-fl{background:#c9ced6}.b-nd{background:#e8ebef}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--tx2)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.barrow{display:grid;grid-template-columns:118px 1fr 62px 128px;align-items:center;gap:10px;padding:4px 0;font-size:13px}
.bl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cnt{color:var(--tx3);font-size:11px;margin-left:5px}
.btrack{background:#eef0f3;border-radius:4px;height:16px;overflow:hidden}
.bfill{height:100%;border-radius:4px;min-width:2px}
.bfill.up{background:linear-gradient(90deg,#e8695f,#d92b2b)}
.bfill.down{background:linear-gradient(90deg,#4fb98a,#0f9d58)}
.bv{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.bm{color:var(--tx3);font-size:11.5px;text-align:right;font-variant-numeric:tabular-nums}
table.tbl{width:100%;border-collapse:collapse;font-size:13px}
table.tbl th{text-align:left;font-weight:600;color:var(--tx3);font-size:12px;padding:7px 8px;
border-bottom:1px solid var(--line);white-space:nowrap}
table.tbl td{padding:7px 8px;border-bottom:1px solid #f2f4f6}
table.tbl tr:last-child td{border-bottom:none}
table.tbl tr:hover td{background:#fafbfc}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.sym{color:var(--tx2);font-family:ui-monospace,Consolas,monospace;font-size:12px}
.nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:190px}
.up{color:var(--up)}.down{color:var(--dn)}.flat{color:var(--tx2)}.na{color:var(--tx3)}
.tag{font-size:10px;padding:1px 5px;border-radius:4px;margin-left:5px;vertical-align:1px}
.tag.warn{background:#fff4e5;color:#b26a00}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:820px){.cols{grid-template-columns:1fr}}
ul.pt{margin:0;padding-left:18px}ul.pt li{margin-bottom:7px}
.note{background:#fff8e6;border:1px solid #ffe1a8;border-radius:9px;padding:12px 15px;font-size:13px;color:#6b4e00;margin-top:14px}
.hl{background:#eef4fb;border:1px solid #cfe0f3;border-radius:9px;padding:12px 15px;font-size:13px;margin-top:14px}
.disc{color:var(--tx3);font-size:12px;line-height:1.7}
footer{text-align:center;color:var(--tx3);font-size:12px;margin-top:30px}
"""

    out_html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{css}</style></head><body><div class="wrap">
<header><h1>{html.escape(title)}</h1>
<div class="sub">观察窗口 <b>{days}</b> 个自然日（{tdays} 个交易日） · 数据截止 <b>{latest}</b> ·
样本 <b>{d['total']}</b> 只 ETF · 生成于 {now}</div></header>

<div class="card"><h2>一、市场概览</h2>
<div class="kpis">
<div class="kpi"><div class="k">上涨</div><div class="v up">{b['up']}</div></div>
<div class="kpi"><div class="k">下跌</div><div class="v down">{b['down']}</div></div>
<div class="kpi"><div class="k">平盘</div><div class="v">{b['flat']}</div></div>
<div class="kpi"><div class="k">无行情</div><div class="v">{b['no_data']}</div></div>
<div class="kpi"><div class="k">平均涨跌幅</div><div class="v {pct_cls(d['avg_percent'])}">{num(d['avg_percent'])}%</div></div>
</div>
<div class="bar">
<div class="b-up" style="width:{pct_of(b['up']):.1f}%"></div>
<div class="b-dn" style="width:{pct_of(b['down']):.1f}%"></div>
<div class="b-fl" style="width:{pct_of(b['flat']):.1f}%"></div>
<div class="b-nd" style="width:{pct_of(b['no_data']):.1f}%"></div>
</div>
<div class="legend">
<span><i class="b-up"></i>上涨 {b['up']}（{pct_of(b['up']):.0f}%）</span>
<span><i class="b-dn"></i>下跌 {b['down']}（{pct_of(b['down']):.0f}%）</span>
<span><i class="b-fl"></i>平盘 {b['flat']}（{pct_of(b['flat']):.0f}%）</span>
<span><i class="b-nd"></i>无行情 {b['no_data']}（{pct_of(b['no_data']):.0f}%）</span>
</div>
<div class="hl"><b>判断：</b>涨跌家数比 {b['up']} : {b['down']}，平均涨跌幅
<b>{num(d['avg_percent'])}%</b>——普涨/普跌/分化需结合主题分布判断。</div>
</div>

<div class="card"><h2>二、主题强弱（今日轮动 + 窗口累计）</h2>
<h3>今日轮动 · 领先 Top 15</h3>{''.join(bar(r, 'pct') for r in theme_today[:15])}
<h3>今日轮动 · 落后 Bottom 15</h3>{''.join(bar(r, 'pct') for r in theme_today[-15:][::-1])}
<h3>窗口累计 · 领先 Top 15</h3>{''.join(bar(r, 'wr') for r in theme_rows[:15])}
<h3>窗口累计 · 落后 Bottom 15</h3>{''.join(bar(r, 'wr') for r in theme_rows[-15:][::-1])}
</div>

<div class="card"><h2>三、强势方向</h2><div class="cols">
<div><h3>当日涨幅榜</h3>{table(today_top,'pct','当日')}</div>
<div><h3>窗口累计涨幅榜</h3>{table(win_top,'wr','当日')}</div></div>
<div class="note"><b>数据说明：</b>「仅N日」标记表示该 ETF 在窗口内只有 N 个交易日数据
（多为新上市品种），样本不足，累计涨幅与动量<b>不具备可比性</b>，请勿据此决策。</div></div>

<div class="card"><h2>四、弱势方向</h2><div class="cols">
<div><h3>当日跌幅榜</h3>{table(today_bot,'pct','当日')}</div>
<div><h3>窗口累计落后榜</h3>{table(win_bot,'wr','当日')}</div></div></div>

<div class="card"><h2>五、动量与波动</h2><div class="cols">
<div><h3>近 5 交易日动量最强</h3>{table(mom_top,'wr','当日')}</div>
<div><h3>波动率最高（风险最高）</h3>{table(vol_top,'wr','当日')}</div></div></div>

{portfolio_html}

<div class="card"><h2>六、主题明细（按窗口累计排序）</h2>
<table class="tbl"><thead><tr><th>主题</th><th>数量</th><th>窗口均值</th><th>近5日动量</th><th>当日均值</th></tr></thead>
<tbody>{theme_tbl}</tbody></table></div>

<div class="card"><h2>七、风险提示</h2><ul class="pt">{warn_live}
<li><b>高位板块波动放大：</b>波动率居前的主题回撤风险显著高于市场平均。</li>
<li><b>短期动量与窗口背离：</b>窗口累计为负但近5日动量为正者，多属超跌反弹，趋势尚未确认。</li>
</ul></div>

<div class="card"><h2>八、配置判断</h2>
<div class="hl">需结合主题强弱与动量背离人工撰写——<b>不得仅为满足格式强行推荐</b>；
无明确信号时明确写「暂无明确新增仓位信号」。</div></div>

<div class="card"><h2>九、免责声明</h2><div class="disc">
本报告基于 ETF 历史走势数据的量化统计结果，数据截止 <b>{latest}</b>，
观察窗口 {days} 个自然日（{tdays} 个交易日）。所有内容仅供研究参考，
<b>不构成任何投资建议</b>。市场有风险，投资需谨慎，请独立判断并自行承担决策后果。</div></div>

<footer>WorkBuddy AI · ETF 分析技能</footer>
</div></body></html>"""

    os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
    open(out, 'w', encoding='utf-8').write(out_html)
    print('OK ->', out)
    print(f"themes={len(theme_rows)} today_top={len(today_top)} win_top={len(win_top)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', required=True, help='fetch_etf.py 产出的 etf_data.json')
    ap.add_argument('--out', dest='out', required=True, help='输出 HTML 路径')
    ap.add_argument('--title', default=None, help='报告标题')
    ap.add_argument('--portfolio', default=None, help='可选持仓 JSON')
    a = ap.parse_args()
    build(a.src, a.out, a.title, a.portfolio)


if __name__ == '__main__':
    main()
