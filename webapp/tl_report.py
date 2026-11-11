#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线池 + 穿三线全量跟踪 · 报告段落生成（供 daily_run.py 与独立报告复用）。

独立用法：python3 tl_report.py
  → 生成 daily/三线池与跟踪_<数据日>.md
  → 同时把同样段落合并进 daily/日报_<数据日>.md（幂等，按标记替换）
"""
import csv, glob, json, os, sys
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WEB = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(WEB)
MARK = '## ⭐ 三线池'


def _j(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d if d is not None else {}


def load_all():
    return (_j(os.path.join(WEB, 'tlpool.json')),
            _j(os.path.join(WEB, 'tltrack', 'stats.json')),
            _j(os.path.join(WEB, 'events.json')))


def _sh_ma20():
    """上证 > MA20 的市场环境（与 server.Data.mkt 同口径），date -> bool。"""
    p = os.path.join(_REPO, 'backtest/indices/sh000001.json')
    try:
        k = json.load(open(p))
    except Exception:
        return {}
    dates, close = [r[0] for r in k], [r[2] for r in k]
    out = {}
    for i, d in enumerate(dates):
        if i >= 19:
            out[d] = close[i] > sum(close[i - 19:i + 1]) / 20
    return out


def _d1d3_segments():
    """从 tltrack/signals.csv 现算「隔日短打」分桶（当天收盘买、次日/2~3日了结口径）。

    fwd1 = 次日收盘卖（close→close）；fwd3 = 持有 2~3 日的第3日收盘卖。
    返回 [(子集名, d1_agg, d3_agg, d5_agg), ...]，agg={n,avg,win}。
    """
    p = os.path.join(WEB, 'tltrack', 'signals.csv')
    try:
        rows = list(csv.DictReader(open(p)))
    except Exception:
        return []

    def num(x):
        try:
            return float(x)
        except Exception:
            return None

    def agg(rs, k):
        a = [num(r[k]) for r in rs if num(r[k]) is not None]
        if not a:
            return None
        return {'n': len(a), 'avg': sum(a) / len(a),
                'win': sum(1 for x in a if x > 0) / len(a) * 100}

    brk = lambda r: r.get('brk20') == '1'
    up89 = lambda r: (num(r.get('ma89_slope')) or -99) > 0
    sh20 = _sh_ma20()
    shup = lambda r: sh20.get(r.get('date')) is True
    shdn = lambda r: sh20.get(r.get('date')) is False
    segs = [
        ('全部三线穿阳', rows),
        ('破20日高', [r for r in rows if brk(r)]),
        ('破20高+MA89上行', [r for r in rows if brk(r) and up89(r)]),
        ('破20高+MA89上行+上证>MA20', [r for r in rows if brk(r) and up89(r) and shup(r)]),
        ('破20高+MA89上行+上证<=MA20', [r for r in rows if brk(r) and up89(r) and shdn(r)]),
    ]
    return [(name, agg(rs, 'fwd1'), agg(rs, 'fwd3'), agg(rs, 'fwd5')) for name, rs in segs]


def sections(tp, ts, evlib, out=None, inline=False):
    """返回三线池与全量跟踪的 markdown 行列表。"""
    out = out or {}
    lines = []
    tlp, trk = out.get('tlpool') or {}, out.get('tltrack') or {}
    if tp.get('pool'):
        mk = tp.get('market') or {}
        pool = tp['pool']
        na = sum(1 for x in pool if x['grade'] == 'A')
        lines.append('## ⭐ 三线池 · 穿三线 Top5（操作重点）')
        lines.append('- 数据日 **%s** · 全市场三线穿阳 **%d** 只，其中「收盘破20日高」**%d** 只，'
                     'A级（破20高 + MA89上行）**%d** 只' % (
                         tp.get('as_of'), tp.get('n_candidates'), tp.get('n_confirmed'), na))
        _mkt = tp.get('mkt') or '-'
        lines.append('- 市场环境：**上证%sMA20**（%s）· 情绪 **%s %.1f°** · 涨停 %s 家 · '
                     '成交 %s 亿 · regime=%s（仅参考）· %s' % (
                         '>' if _mkt == 'up' else '<=', _mkt,
                         mk.get('emotion', ''), mk.get('emotion_temp') or 0,
                         mk.get('zt_count', '-'), mk.get('amount_yi', '-'),
                         tp.get('regime'), (mk.get('indices') or '')[:100]))
        lines.append('- 事件主线：%s' % '；'.join(
            '%s（%s）' % (e['tag'], e['title'][:52]) for e in (evlib.get('events') or [])[:6]))
        lines.append('- 口径：评分 = 技术形态62 + 市场适配14（含**上证>MA20软过滤**4）+ 事件主线24；'
                     '**硬门槛 = 收盘破20日高**；评级 A=破20高+MA89上行 / B=破20高但MA89仍下行 / '
                     'C=未破20高（只进观察区）。**卖出：A级买信号日/次日 → 持2~3日了结（胜率55~56%%），'
                     '最多5日；破EMA50三日仅兜底、不推荐长持**。事件库更新于 %s。' %
                     (tp.get('rules', {}).get('event_lib_updated') or '-'))
        lines.append('| 序 | 代码 | 名称 | 评级 | 评分 | 收盘 | 回踩低吸区(首选) | 突破确认价 | 硬止损 | 目标1 / 目标2 | 盈亏比 | 仓位建议 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|---|---|')
        for i, x in enumerate(pool):
            e = x['entry']
            lines.append('| %d | %s | %s | **%s** | %.1f | %.2f | %.2f~%.2f | %.2f | %.2f | %.2f / %.2f | %.1f:1 | %s |' % (
                i + 1, x['code'], x['name'], x['grade'], x['score'], x['close'],
                e['pullback'][0], e['pullback'][1], e['trigger'], e['hard_stop'],
                e['target1'], e['target2'], e['rr'], e['size']))
        lines.append('')
        lines.append('### 介入点明细')
        for i, x in enumerate(pool):
            e = x['entry']
            lines.append('- **%s %s（%s级 · %.1f分）**：%s' % (
                x['name'], x['code'], x['grade'], x['score'], x['grade_note']))
            lines.append('  - 首选 **%.2f~%.2f**（三线密集带 EXPMA12/50/MA89：%.2f/%.2f/%.2f）回踩不破且缩量企稳分批低吸；'
                         '次选次日站上 **%.2f** 且量能不缩做突破确认；**高开 >%.2f 不追**（回测：大阳次日追入期望为负）' % (
                             e['pullback'][0], e['pullback'][1], e['e12'], e['e50'], e['ma89'],
                             e['trigger'], e['chase_cap']))
            lines.append('  - 止损：**%s**（当前 EMA50 = %.2f）；风控硬止损 %.2f。目标 %.2f / %.2f；%s。%s' % (
                e['stop_rule'], e['stop_ref'], e['hard_stop'], e['target1'], e['target2'],
                e['size'], e['hold_note']))
            lines.append('  - 依据：%s' % x['reason'])
        lines.append('')
        if tp.get('watch'):
            lines.append('### 观察区（事件强但尚未破20日高 · 不占前5额度）')
            lines.append('| 代码 | 名称 | 评分 | 收盘 | 需站上的20日高 | 核心事件/理由 |')
            lines.append('|---|---|---|---|---|---|')
            for x in tp['watch'][:5]:
                lines.append('| %s | %s | %.1f | %.2f | %.2f | %s |' % (
                    x['code'], x['name'], x['score'], x['close'],
                    x['entry']['breakout_level'], x['reason'][:78]))
            lines.append('')
        lines.append('- ⚠️ **风险**：%s' % (mk.get('risk') or tp.get('rules', {}).get('risk', '')))
        if na < len(pool):
            lines.append('- ⚠️ 本批仅 %d 只 A 级；其余 %d 只 MA89 仍下行，历史同组合（下行regime+破20高+MA89下行）'
                         '5日 **-1.76%%**、胜率 35.7%%，属追高陷阱区，建议**等 MA89 转正或回踩三线带确认**再动手，'
                         '仓位按试仓处理。' % (na, len(pool) - na))
        lines.append('- 事件/行业热度为量化代理 + 人工录入催化剂，非公告逐条核验；结果为客观数据筛选，不构成投资建议。')
        lines.append('')
    if ts:
        lines.append('## 📈 穿三线全量跟踪（≥30交易日 · 供回测）')
        lines.append('- 文件：`webapp/tltrack/signals.json`（主档）、`signals.csv`（扁平回测表，每行一个信号）、'
                     '`daily/YYYY-MM-DD.json`（每日盯市快照）、`stats.json`（分周期/分桶统计）')
        lines.append('- 全历史 **%s** 个三线穿阳信号 · 已成熟（≥30交易日）**%s** · 跟踪中 **%s** · '
                     '每日快照 %s 天 · 实测起点 live_from=%s（此前为历史K线回填）' % (
                         ts.get('n_signals'), ts.get('n_matured'), ts.get('n_tracking'),
                         trk.get('snapshots') or len(glob.glob(os.path.join(WEB, 'tltrack', 'daily', '*.json'))),
                         ts.get('live_from')))
        hz = ts.get('horizons') or {}
        lines.append('| 周期 | 样本 | 均值% | 中位% | 胜率% | 等权小盘基准% | 超额pp |')
        lines.append('|---|---|---|---|---|---|---|')
        for k in ('d1', 'd3', 'd5', 'd10', 'd20', 'd30'):
            v = hz.get(k)
            if not v:
                continue
            lines.append('| %s日 | %d | %+.2f | %+.2f | %.1f | %+.2f | %+.2f |' % (
                k[1:], v['n'], v['avg'] or 0, v['median'] or 0, v['win'] or 0,
                v['bench_avg'] or 0, v['excess'] or 0))
        lines.append('')
        segs = _d1d3_segments()
        if segs:
            def _cell(a):
                return '—' if not a else '%.1f%% / %+.2f%% (n=%d)' % (a['win'], a['avg'], a['n'])
            lines.append('- **隔日短打口径（当天收盘买入 · 次日或 2~3 日了结，操作重点）**：')
            lines.append('| 子集 | d1 次日收盘卖：胜率 / 均值 | d3 持有2~3日：胜率 / 均值 | d5：胜率 / 均值 |')
            lines.append('|---|---|---|---|')
            for name, a1, a3, a5 in segs:
                lines.append('| %s | %s | %s | %s |' % (name, _cell(a1), _cell(a3), _cell(a5)))
            lines.append('- 读法：**严格次日了结胜率多在 50% 以下，靠盈亏比 + 超额取胜；要胜率站上 50% 必须'
                         '「破20高 + MA89上行」且持到第 2~3 日**。加**上证>MA20**过滤后最优（d3 胜率约 55.6%、'
                         'd5 均值约 +1.98%）；上证<=MA20 时 d3 仅约 49%。**次日开盘卖最差（约35%）**；'
                         '固定止盈（如+4%/-3%）反而更差；Top5 池只对 A 级用隔日短打，B/C 级不适用。')
            lines.append('')
        cm = ts.get('combos') or {}
        if cm:
            lines.append('- 关键组合（5日前向，样本来自全历史）：')
            lines.append('| 组合条件 | 样本 | 均值% | 胜率% | 超额pp | 20日% |')
            lines.append('|---|---|---|---|---|---|')
            for k, v in cm.items():
                a = v['d5']
                lines.append('| %s | %d | %+.2f | %.1f | %+.2f | %+.2f |' % (
                    k, v['n'], a['avg'] or 0, a['win'] or 0, a['excess'] or 0, v['d20']['avg'] or 0))
            lines.append('')
        d5 = hz.get('d5') or {}
        lines.append('- 结论：三线形态整体 5 日 **%+.2f%%**（基准 %+.2f%%），10日后转负，'
                     '**信号有效期仅1~3天 → 卖出应持2~3日了结，不要破EMA50死拿（回测最差25%%胜率）**；'
                     '真正有正期望的是「破20日高 + MA89上行」，再叠加「上证>MA20」胜率最高。'
                     '这也是三线池把 MA89上行 给到 12 分权重的原因。' % (
                         d5.get('avg') or 0, d5.get('bench_avg') or 0))
        lines.append('- 每个信号从次日起逐日记录收盘/收益/基准/超额/EMA50破位天数，至少跟踪 30 个交易日，'
                     '最长留 60 日路径；每日收盘跑批自动追加，可随时用 `signals.csv` 复算或扩展回测。')
        lines.append('')
    if not inline:
        lines.append('> 结果为客观数据筛选，不构成投资建议。')
    return lines


def write_standalone(tp=None, ts=None, ev=None):
    """只写独立报告文件（供 daily_run.py 每日调用，不改动日报）。"""
    tp = tp if tp is not None else _j(os.path.join(WEB, 'tlpool.json'))
    ts = ts if ts is not None else _j(os.path.join(WEB, 'tltrack', 'stats.json'))
    ev = ev if ev is not None else _j(os.path.join(WEB, 'events.json'))
    if not tp.get('pool'):
        return None
    body = ['# 三线池 & 穿三线全量跟踪 · %s' % tp['as_of'], ''] + sections(tp, ts, ev)
    p = os.path.join(BASE, 'daily', '三线池与跟踪_%s.md' % tp['as_of'])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, 'w').write('\n'.join(body))
    return p


def standalone():
    tp, ts, ev = load_all()
    if not tp.get('pool'):
        print('tlpool.json 无数据，请先运行 tlpool.py'); return 1
    p = write_standalone(tp, ts, ev)
    if not p:
        return 1
    d = tp['as_of']
    print(p)
    # 手动执行时，把同样段落合并进当日日报（幂等：以 MARK 为界替换旧 ⑦ 段）
    dp = os.path.join(BASE, 'daily', '日报_%s.md' % d)
    if os.path.exists(dp):
        old = open(dp).read()
        head = '\n'.join(ln for ln in old.split(MARK)[0].splitlines()
                         if not ln.startswith('> 结果为客观数据筛选')).rstrip()
        disc = [ln for ln in old.splitlines() if ln.startswith('> 结果为客观数据筛选')]
        merged = head + '\n\n' + '\n'.join(sections(tp, ts, ev, inline=True))
        if disc:
            merged += '\n' + disc[-1] + '\n'
        open(dp, 'w').write(merged)
    print(dp if os.path.exists(dp) else '(当日日报不存在，跳过合并)')
    return 0


if __name__ == '__main__':
    sys.exit(standalone())
