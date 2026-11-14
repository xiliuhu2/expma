#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「双线裹实体」策略：最近 3 个交易日中，K 线实体（开收之间）同时包住
EXPMA12 与 EXPMA50 的天数 >= 2 → 入选；3 天全中 → A 级（更强），2 天 → B 级。

纪律口径（三关）：
  ① 基准对比：不报裸收益，超额对比「同成交日全市场等权」+「同期任意日基线」两条；
  ② 无未来函数：信号只用 t 及之前的收盘/均线，t 日收盘确认、t+1 开盘成交；
  ③ 分年份稳健性：正超额年数、最强两年贡献、前后半段样本外。

产物（唯一数据源）：webapp/bodywrap.json；webapp/bodywrap/signals.csv；
daily/双线裹实体_<日期>.md。每次跑批重算全历史，跟踪中的信号前向收益自动补齐。
"""
import csv
import json
import os
import statistics
from collections import defaultdict

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(_REPO, 'backtest')
WEB = os.path.join(_REPO, 'webapp')
DAILY = os.path.join(_REPO, 'daily')
OUT_JSON = os.path.join(WEB, 'bodywrap.json')
OUT_DIR = os.path.join(WEB, 'bodywrap')
OUT_CSV = os.path.join(OUT_DIR, 'signals.csv')

HORIZONS = [1, 3, 5, 10, 20]
WINDOW = 3
MIN_HITS = 2
WARMUP = 60
PATH_BEFORE, PATH_AFTER = 10, 20


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _r(v, n=3):
    return round(v, n) if isinstance(v, (int, float)) else None


def ema(vals, n):
    """EXPMA：k=2/(n+1)，与 firstcross.py / server.py 完全一致。"""
    k = 2.0 / (n + 1)
    out = []
    e = vals[0]
    for v in vals:
        e = e + k * (v - e)
        out.append(e)
    return out


def load_universe():
    meta = {}
    p = os.path.join(ROOT, 'snapshot.json')
    if os.path.exists(p):
        try:
            snap = json.load(open(p))
            items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
            for r in items:
                meta[r.get('code', '')] = {
                    'name': r.get('name', ''),
                    'mv': _f(r.get('TotalMV') or 0),
                }
        except Exception:
            pass
    ind = {}
    p = os.path.join(WEB, 'industry.json')
    if os.path.exists(p):
        try:
            ind = json.load(open(p))
        except Exception:
            pass
    out = {}
    p = os.path.join(ROOT, 'klines')
    for fn in os.listdir(p):
        if not fn.endswith('.json'):
            continue
        code = fn[:-5]
        m = meta.get(code, {})
        out[code] = {'name': m.get('name', ''), 'ind': ind.get(code, ''),
                     'mv': m.get('mv', 0.0)}
    return out


def load_stocks(universe):
    """逐票读原始日K并算 EXPMA12/50（不按全局日历补零，避免均线被 0 污染）。"""
    stocks = {}
    all_dates = set()
    for code in universe:
        p = os.path.join(ROOT, 'klines', code + '.json')
        if not os.path.exists(p):
            continue
        try:
            bars = json.load(open(p))
        except Exception:
            continue
        if not isinstance(bars, list) or len(bars) < WARMUP + 30:
            continue
        dates = [b[0] for b in bars]
        o = [_f(b[1]) for b in bars]
        c = [_f(b[2]) for b in bars]
        h = [_f(b[3]) for b in bars]
        l = [_f(b[4]) for b in bars]
        v = [_f(b[5]) for b in bars]
        if any(x <= 0 for x in c[:5]) or any(x <= 0 for x in o[:5]):
            continue
        stocks[code] = {'dates': dates, 'o': o, 'c': c, 'h': h, 'l': l, 'v': v,
                        'e12': ema(c, 12), 'e50': ema(c, 50),
                        'idx': {d: i for i, d in enumerate(dates)}}
        all_dates.update(dates)
    return stocks, sorted(d for d in all_dates if d)


def body_hits(s, t):
    """t 日实体（min(开,收)~max(开,收)）是否同时包住 EXPMA12/50。"""
    lo, hi = min(s['o'][t], s['c'][t]), max(s['o'][t], s['c'][t])
    if hi <= lo or hi <= 0:
        return 0
    e12, e50 = s['e12'][t], s['e50'][t]
    return 1 if (lo - 1e-9 <= e12 <= hi + 1e-9 and
                 lo - 1e-9 <= e50 <= hi + 1e-9) else 0


def market_returns(stocks):
    """(成交日, h) -> 全市场等权收益%：同日基线；并给同期任意日基线。"""
    sums, cnts = defaultdict(float), defaultdict(int)
    for s in stocks.values():
        o, c, dates, n = s['o'], s['c'], s['dates'], len(s['c'])
        for hh in HORIZONS:
            for i in range(1, n):
                j = i + hh
                if j >= n or o[i] <= 0 or c[j] <= 0:
                    continue
                k = (dates[i], hh)
                sums[k] += (c[j] / o[i] - 1) * 100
                cnts[k] += 1
    mret = {k: sums[k] / cnts[k] for k in sums if cnts[k]}
    return mret


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def summarize(rows, key='excess', base_any=None, h=5):
    """rows: 带 ret/base/excess 字典的信号；返回持有期 h 的汇总。"""
    hs = str(h)
    rets = [r['ret'].get(hs) for r in rows]
    bases = [r['base'].get(hs) for r in rows]
    excs = [r['excess'].get(hs) for r in rows]
    valid = [e for e in excs if e is not None]
    m, b, e = _mean(rets), _mean(bases), _mean(excs)
    return {'n': len(valid), 'mean': _r(m), 'base_same': _r(b),
            'base_any': _r(base_any.get(hs) if base_any else None),
            'excess': _r(e), 'median_excess': _r(_median(excs)),
            'win': _r(100 * sum(1 for x in valid if x > 0) / len(valid), 1) if valid else None,
            'ret_win': _r(100 * sum(1 for x in rets if x is not None and x > 0) /
                          max(1, len([x for x in rets if x is not None])), 1)}


def yang_body_hits(s, t):
    """口径二：t 日必须是阳线（收>开），且阳柱实体同时包住 EXPMA12/50。
    不涉及 MA89（89 均线不参与本策略任何判定）。
    """
    o, c = s['o'][t], s['c'][t]
    if o <= 0 or c <= o:
        return 0
    e12, e50 = s['e12'][t], s['e50'][t]
    return 1 if (o - 1e-9 <= e12 <= c + 1e-9 and
                 o - 1e-9 <= e50 <= c + 1e-9) else 0


def collect_strict2(stocks, mret, universe, calendar):
    """口径二信号：连续两个交易日均为阳线且实体包住 EXPMA12/50（不看 MA89）。
    t 日收盘确认、t+1 开盘成交；is_new 标记「首次达成连续两日」（簇首日），
    用于剔除连续 3 天以上造成的重叠样本，作敏感性对照。
    """
    signals = []
    for code, s in stocks.items():
        o, c, e12, e50, dates, n = s['o'], s['c'], s['e12'], s['e50'], s['dates'], len(s['c'])
        hits = [0] * n
        for t in range(WARMUP, n):
            hits[t] = yang_body_hits(s, t)
        meta = universe.get(code, {})
        for t in range(WARMUP, n):
            if not (hits[t - 1] and hits[t]):
                continue
            te = t + 1 if t + 1 < n else None
            if te is not None and o[te] <= 0:
                continue
            ret, base, exc = {}, {}, {}
            if te is not None:
                for hh in HORIZONS:
                    j = te + hh
                    rr = (c[j] / o[te] - 1) * 100 if j < n and c[j] > 0 else None
                    bb = mret.get((dates[te], hh))
                    ret[str(hh)] = _r(rr)
                    base[str(hh)] = _r(bb)
                    exc[str(hh)] = _r(rr - bb) if (rr is not None and bb is not None) else None
            else:
                for hh in HORIZONS:
                    ret[str(hh)] = base[str(hh)] = exc[str(hh)] = None
            is_new = not hits[t - 2]
            signals.append({
                'sel_date': dates[t], 'code': code,
                'name': meta.get('name', ''), 'ind': meta.get('ind', ''),
                'grade': 'S2', 'hits': 2,
                'pattern': '%d%d%d' % (hits[t - 2], hits[t - 1], hits[t]),
                'is_new': is_new,
                'close': _r(c[t], 2), 'e12': _r(e12[t], 3), 'e50': _r(e50[t], 3),
                'bull': True, 'e12_gt_e50': e12[t] > e50[t],
                'vr': _r(s['v'][t] / (sum(s['v'][t - 5:t]) / 5), 2) if sum(s['v'][t - 5:t]) else None,
                'entry_date': dates[te] if te is not None else None,
                'entry_px': _r(o[te], 2) if te is not None else None,
                'ret': ret, 'base': base, 'excess': exc,
                'track_days': max(0, n - 1 - te) if te is not None else 0,
                'status': 'pending' if te is None else (
                    'matured' if ret.get('10') is not None else 'tracking'),
                '_ti': t, '_s': s,
            })
    return signals


def collect(stocks, mret, universe, calendar):
    """全历史信号：选入日、3日命中形态、A/B 级、次日开盘成交、前向收益。"""
    signals = []
    single_excess = defaultdict(list)   # 仅 1 天命中（未入选）对照组
    for code, s in stocks.items():
        o, c, e12, e50, dates, n = s['o'], s['c'], s['e12'], s['e50'], s['dates'], len(s['c'])
        hits = [0] * n
        for t in range(WARMUP, n):
            hits[t] = body_hits(s, t)
        meta = universe.get(code, {})
        for t in range(WARMUP, n):
            w = hits[t - 2] + hits[t - 1] + hits[t]
            if w < 1:
                continue
            te = t + 1 if t + 1 < n else None
            if te is not None and o[te] <= 0:
                continue
            ret, base, exc = {}, {}, {}
            if te is not None:
                for hh in HORIZONS:
                    j = te + hh
                    rr = (c[j] / o[te] - 1) * 100 if j < n and c[j] > 0 else None
                    bb = mret.get((dates[te], hh))
                    ret[str(hh)] = _r(rr)
                    base[str(hh)] = _r(bb)
                    exc[str(hh)] = _r(rr - bb) if (rr is not None and bb is not None) else None
            else:  # 当日刚入选：次日开盘待成交，前向收益留空，跑批时自动补齐
                for hh in HORIZONS:
                    ret[str(hh)] = base[str(hh)] = exc[str(hh)] = None
            if w == 1:  # 未入选对照组，只留 5/10 日超额
                for hh in (5, 10):
                    if exc[str(hh)] is not None:
                        single_excess[hh].append(exc[str(hh)])
                continue
            if w < MIN_HITS:
                continue
            pat = '%d%d%d' % (hits[t - 2], hits[t - 1], hits[t])
            pat4 = '%d%d%d%d' % (hits[t - 3], hits[t - 2], hits[t - 1], hits[t])
            prev_w = hits[t - 3] + hits[t - 2] + hits[t - 1] if t >= WARMUP + 1 else 0
            signals.append({
                'sel_date': dates[t], 'code': code,
                'name': meta.get('name', ''), 'ind': meta.get('ind', ''),
                'grade': 'A' if w == 3 else 'B', 'hits': w, 'pattern': pat, 'pattern4': pat4,
                'is_new': prev_w < MIN_HITS,
                'close': _r(c[t], 2), 'e12': _r(e12[t], 3), 'e50': _r(e50[t], 3),
                'bull': c[t] >= o[t], 'e12_gt_e50': e12[t] > e50[t],
                'vr': _r(s['v'][t] / (sum(s['v'][t - 5:t]) / 5), 2) if sum(s['v'][t - 5:t]) else None,
                'entry_date': dates[te] if te is not None else None,
                'entry_px': _r(o[te], 2) if te is not None else None,
                'ret': ret, 'base': base, 'excess': exc,
                'track_days': max(0, n - 1 - te) if te is not None else 0,
                'status': 'pending' if te is None else (
                    'matured' if ret.get('10') is not None else 'tracking'),
                '_ti': t, '_s': s,
            })
    return signals, single_excess


def by_h_stats(signals, base_any):
    return {str(h): summarize(signals, base_any=base_any, h=h) for h in HORIZONS}


def group_stats(rows, keyfn, base_any, h=5):
    g = defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    return {str(k): summarize(v, base_any=base_any, h=h) for k, v in sorted(g.items(), key=lambda x: str(x[0]))}


def hist(values, lo=-25, hi=25, nb=26):
    step = (hi - lo) / nb
    buckets = [{'x0': _r(lo + i * step, 1), 'x1': _r(lo + (i + 1) * step, 1), 'n': 0} for i in range(nb)]
    for v in values:
        if v is None:
            continue
        i = int((min(max(v, lo), hi - 1e-6) - lo) / step)
        buckets[max(0, min(nb - 1, i))]['n'] += 1
    return buckets


def event_path(signals):
    """事件对齐：相对选入日 t 的收盘累计涨跌（描述性，仅作图，不进任何标签）。"""
    sums, cnts = defaultdict(float), defaultdict(int)
    for r in signals:
        s, t = r['_s'], r['_ti']
        if s['c'][t] <= 0:
            continue
        for i in range(-PATH_BEFORE, PATH_AFTER + 1):
            j = t + i
            if 0 <= j < len(s['c']) and s['c'][j] > 0:
                sums[i] += (s['c'][j] / s['c'][t] - 1) * 100
                cnts[i] += 1
    return [{'i': i, 'v': _r(sums[i] / cnts[i]) if cnts[i] else None, 'n': cnts[i]}
            for i in range(-PATH_BEFORE, PATH_AFTER + 1) if cnts[i]]


def timeline(signals, h, cum_from=None):
    g = defaultdict(list)
    for r in signals:
        g[r['sel_date']].append(r['excess'].get(str(h)))
    cum, out = 0.0, []
    for d in sorted(g):
        vals = [v for v in g[d] if v is not None]
        e = _mean(vals)
        if e is not None:
            cum += e
        out.append({'date': d, 'n': len(g[d]), 'excess': _r(e), 'cum': _r(cum)})
    return out


def build():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(DAILY, exist_ok=True)
    universe = load_universe()
    stocks, calendar = load_stocks(universe)
    if not stocks:
        raise RuntimeError('无可用K线')
    as_of = calendar[-1]
    mret = market_returns(stocks)
    # 同期任意日基线：仅取信号可能发生的时间窗（warmup 之后的成交日）
    win_start = calendar[max(0, WARMUP - 1)] if len(calendar) > WARMUP else calendar[0]
    base_any = {}
    for hh in HORIZONS:
        vals = [v for (d, h), v in mret.items() if h == hh and d >= win_start]
        base_any[str(hh)] = _r(_mean(vals))

    signals, single_excess = collect(stocks, mret, universe, calendar)
    ev_path = event_path(signals)
    for r in signals:
        r.pop('_s', None)
        r.pop('_ti', None)

    by_h = by_h_stats(signals, base_any)
    by_grade = {g: by_h_stats([r for r in signals if r['grade'] == g], base_any)
                for g in ('A', 'B')}
    by_year = group_stats(signals, lambda r: r['sel_date'][:4], base_any)
    half = len(calendar) // 2
    split_date = calendar[half] if half < len(calendar) else calendar[-1]
    by_half = group_stats(signals, lambda r: '前半段' if r['sel_date'] < split_date else '后半段', base_any)
    by_year_by_h = {str(h): group_stats(signals, lambda r: r['sel_date'][:4], base_any, h)
                    for h in HORIZONS}
    by_half_by_h = {str(h): group_stats(signals, lambda r: '前半段' if r['sel_date'] < split_date else '后半段',
                                        base_any, h) for h in HORIZONS}

    # 敏感性：窗口 2/3/4 日、最少命中 2/3 天、仅阳线、仅双线多头、仅新入簇
    sens = []
    for w, mh in ((2, 2), (3, 2), (3, 3), (4, 2), (4, 3), (4, 4)):
        rows = []
        for r in signals:
            if w == 3:
                hh = sum(int(x) for x in r['pattern'])
            elif w == 2:
                hh = sum(int(x) for x in r['pattern'][-2:])
            else:  # w=4：真实第 4 天命中形态
                hh = sum(int(x) for x in r['pattern4'])
            if hh >= mh:
                rows.append(r)
        s = by_h_stats(rows, base_any)
        sens.append({'label': '窗口%d日·≥%d天' % (w, mh), 'by_h': s})
    for label, filt in (('仅阳线', lambda r: r['bull']),
                        ('仅阴线', lambda r: not r['bull']),
                        ('EMA12>EMA50', lambda r: r['e12_gt_e50']),
                        ('EMA12≤EMA50', lambda r: not r['e12_gt_e50']),
                        ('簇首日', lambda r: r['is_new']),
                        ('簇内跟随日', lambda r: not r['is_new'])):
        rows = [r for r in signals if filt(r)]
        sens.append({'label': label, 'by_h': by_h_stats(rows, base_any)})
    sens_all_negative = all((v['by_h']['5']['excess'] or 0) < 0 and
                            (v['by_h']['10']['excess'] or 0) < 0 for v in sens)

    # 三关
    yr_pos = sum(1 for v in by_year.values() if (v['excess'] or 0) > 0)
    yr_n = len(by_year)
    yr5 = by_h['5']['excess'] or 0
    yr10 = by_h['10']['excess'] or 0
    exc5 = [r['excess'].get('5') for r in signals if r['excess'].get('5') is not None]
    pos = [x for x in exc5 if x > 0]
    top10_n = max(1, int(len(exc5) * .1))
    top10_share = _r(100 * sum(sorted(exc5, reverse=True)[:top10_n]) /
                     sum(pos), 1) if pos and sum(pos) > 0 else None
    halves_pos = sum(1 for v in by_half.values() if (v['excess'] or 0) > 0)
    year_robust = (yr_n >= 3 and yr_pos >= yr_n - 1 and halves_pos == len(by_half)
                   and yr5 > 0 and yr10 > 0 and (top10_share or 100) < 60)
    three_gates = {'base_compare': True, 'no_future': True, 'year_robust': year_robust,
                   'positive_years': yr_pos, 'n_years': yr_n, 'top10_share': top10_share,
                   'split_date': split_date}
    verdict = ('三关③未过（本地K线仅 %d 个年份段、信号集中在近期），'
               '全部结论仅标<b>描述/候选</b>，不构成任何买入/等待建议；'
               '参数敏感性挑出的组合需查样本外与平台状，勿直接用于操作。' % yr_n)
    if year_robust:
        verdict = ('三关全过：基准对比成立、无未来函数、分年份与前后半段稳健；'
                   '仍需独立样本外复核，不构成投资建议。')

    # 图表数据（按持有期分别预算，前端切换持有期即时联动）
    dist_by_h, timeline_by_h, bins_by_h = {}, {}, {}
    for h in HORIZONS:
        hs = str(h)
        excs = [r['excess'].get(hs) for r in signals if r['excess'].get(hs) is not None]
        dist_by_h[hs] = hist(excs)
        timeline_by_h[hs] = timeline(signals, h)
        bins_by_h[hs] = {
            'vol_ratio': [
                {'label': 'A级·3天全中', 'n': by_grade['A'][hs]['n'],
                 'win': by_grade['A'][hs]['win'], 'excess': by_grade['A'][hs]['excess']},
                {'label': 'B级·2天', 'n': by_grade['B'][hs]['n'],
                 'win': by_grade['B'][hs]['win'], 'excess': by_grade['B'][hs]['excess']},
            ] + [{'label': '形态 ' + k, **{x: v[x] for x in ('n', 'win', 'excess')}}
                 for k, v in group_stats([r for r in signals if r['grade'] == 'B'],
                                         lambda r: r['pattern'], base_any, h).items()],
            'pos60': [
                {'label': k, 'n': v['n'], 'win': v['win'], 'excess': v['excess']}
                for k, v in group_stats(signals,
                                        lambda r: ('阳线' if r['bull'] else '阴线') + '/' +
                                        ('双线多头' if r['e12_gt_e50'] else '双线空头'),
                                        base_any, h).items()],
        }

    today_rows = [r for r in signals if r['sel_date'] == as_of]
    today_sorted = sorted(today_rows, key=lambda r: (-r['hits'], -(r['vr'] or 0)))
    n_A = sum(1 for r in today_rows if r['grade'] == 'A')
    n_B = len(today_rows) - n_A
    recent = sorted(signals, key=lambda r: (r['sel_date'], r['code']), reverse=True)[:120]
    matured = sum(1 for r in signals if r['status'] == 'matured')
    tracking = len(signals) - matured
    new_today = sum(1 for r in today_rows if r['is_new'])

    d = {
        'as_of': as_of, 'n_stocks': len(stocks), 'n_days': len(calendar),
        'n_signals': len(signals),
        'definition': {'horizons': HORIZONS, 'window': WINDOW, 'min_hits': MIN_HITS,
                       'warmup': WARMUP, 'expma': [12, 50],
                       'rule': '实体[min(开,收), max(开,收)]同时包住 EXPMA12/EXPMA50；3日内≥2天入选，3天全中=A级'},
        'summary': {'by_h': by_h}, 'by_grade': by_grade,
        'base_any': base_any,
        'by_year': by_year, 'by_half': by_half,
        'by_year_by_h': by_year_by_h, 'by_half_by_h': by_half_by_h,
        'top10_share': top10_share, 'three_gates': three_gates,
        'sensitivity': sens, 'sens_all_negative': sens_all_negative,
        'unconfirmed_excess': _r(_mean(single_excess.get(10, []))),
        'single_hit_n': len(single_excess.get(10, [])),
        'dist': dist_by_h['5'], 'dist_by_h': dist_by_h,
        'event_path': ev_path,
        'timeline': timeline_by_h['5'], 'timeline_by_h': timeline_by_h,
        'bins': bins_by_h['5'], 'bins_by_h': bins_by_h,
        'recent': recent,
        'today': {'picks': today_sorted[:60], 'n': len(today_rows), 'n_A': n_A, 'n_B': n_B},
        'tracking': {'n_signals': len(signals), 'matured': matured, 'tracking': tracking,
                     'new_today': new_today,
                     'matured10': sum(1 for r in signals if r['ret'].get('10') is not None)},
        'verdict': verdict,
        'case': {'n_A': n_A, 'n_B': n_B,
                 'picks': [{'code': r['code'], 'name': r['name'], 'grade': r['grade'],
                            'pattern': r['pattern'], 'close': r['close'],
                            'e12': r['e12'], 'e50': r['e50']} for r in today_sorted[:12]]},
        'case_stock': {'code': 'sz002520', 'name': universe.get('sz002520', {}).get('name', ''),
                       'signals': [{'sel_date': r['sel_date'], 'grade': r['grade'],
                                    'pattern': r['pattern'], 'entry_date': r['entry_date'],
                                    'entry_px': r['entry_px'], 'ret5': r['ret'].get('5'),
                                    'ret10': r['ret'].get('10'),
                                    'excess5': r['excess'].get('5'),
                                    'excess10': r['excess'].get('10')}
                                   for r in signals if r['code'] == 'sz002520' and
                                   r['sel_date'] >= calendar[-20]]},
    }
    sig2 = collect_strict2(stocks, mret, universe, calendar)
    ev2 = event_path(sig2)
    for r in sig2:
        r.pop('_s', None)
        r.pop('_ti', None)
    d['strict2'] = build_strict2_pack(sig2, base_any, calendar, split_date, ev2, universe, as_of)
    d['strict2']['n_stocks'] = len(stocks)

    with open(OUT_JSON, 'w') as f:
        json.dump(d, f, ensure_ascii=False)
    write_csv(signals)
    write_csv(sig2, os.path.join(OUT_DIR, 'signals_strict2.csv'))
    report = write_report(d)
    d['report'] = report
    return d


def build_strict2_pack(signals, base_any, calendar, split_date, ev_path, universe, as_of):
    """口径二完整数据包：结构与主口径一致，前端可直接切换数据源。"""
    by_h = by_h_stats(signals, base_any)
    by_grade = {'A': by_h_stats([r for r in signals if r['is_new']], base_any),
                'B': by_h_stats([r for r in signals if not r['is_new']], base_any)}
    by_year = group_stats(signals, lambda r: r['sel_date'][:4], base_any)
    by_half = group_stats(signals, lambda r: '前半段' if r['sel_date'] < split_date else '后半段', base_any)
    by_year_by_h = {str(h): group_stats(signals, lambda r: r['sel_date'][:4], base_any, h)
                    for h in HORIZONS}
    by_half_by_h = {str(h): group_stats(signals,
                                        lambda r: '前半段' if r['sel_date'] < split_date else '后半段',
                                        base_any, h) for h in HORIZONS}

    sens = []
    variants = (
        ('仅首次达成（剔除重叠）', lambda r: r['is_new']),
        ('簇内延续日', lambda r: not r['is_new']),
        ('连续 3 天阳柱', lambda r: r['pattern'] == '111'),
        ('EMA12>EMA50（双线多头）', lambda r: r['e12_gt_e50']),
        ('EMA12≤EMA50（双线空头）', lambda r: not r['e12_gt_e50']),
        ('当日放量 vr≥1.2', lambda r: (r['vr'] or 0) >= 1.2),
        ('当日缩量 vr<1.0', lambda r: (r['vr'] or 0) < 1.0),
    )
    for label, filt in variants:
        rows = [r for r in signals if filt(r)]
        sens.append({'label': label, 'by_h': by_h_stats(rows, base_any)})

    yr_pos = sum(1 for v in by_year.values() if (v['excess'] or 0) > 0)
    yr_n = len(by_year)
    exc5 = [r['excess'].get('5') for r in signals if r['excess'].get('5') is not None]
    pos = [x for x in exc5 if x > 0]
    top10_n = max(1, int(len(exc5) * .1))
    top10_share = _r(100 * sum(sorted(exc5, reverse=True)[:top10_n]) / sum(pos), 1) \
        if pos and sum(pos) > 0 else None
    halves_pos = sum(1 for v in by_half.values() if (v['excess'] or 0) > 0)
    year_robust = (yr_n >= 3 and yr_pos >= yr_n - 1 and halves_pos == len(by_half)
                   and (by_h['5']['excess'] or 0) > 0 and (by_h['10']['excess'] or 0) > 0
                   and (top10_share or 100) < 60)
    three_gates = {'base_compare': True, 'no_future': True, 'year_robust': year_robust,
                   'positive_years': yr_pos, 'n_years': yr_n, 'top10_share': top10_share,
                   'split_date': split_date}
    verdict = ('口径二三关③未过（本地K线仅 %d 个年份段、信号集中在近期），'
               '全部结论仅标<b>描述/候选</b>，不构成任何买入/等待建议；'
               '敏感性挑出的组合需查样本外与平台状，勿直接用于操作。' % yr_n)
    if year_robust:
        verdict = ('口径二三关全过：基准对比成立、无未来函数、分年份与前后半段稳健；'
                   '仍需独立样本外复核，不构成投资建议。')

    dist_by_h, timeline_by_h, bins_by_h = {}, {}, {}
    for h in HORIZONS:
        hs = str(h)
        excs = [r['excess'].get(hs) for r in signals if r['excess'].get(hs) is not None]
        dist_by_h[hs] = hist(excs)
        timeline_by_h[hs] = timeline(signals, h)
        bins_by_h[hs] = {
            'vol_ratio': [
                {'label': '首次达成', 'n': by_grade['A'][hs]['n'], 'win': by_grade['A'][hs]['win'],
                 'excess': by_grade['A'][hs]['excess']},
                {'label': '簇内延续', 'n': by_grade['B'][hs]['n'], 'win': by_grade['B'][hs]['win'],
                 'excess': by_grade['B'][hs]['excess']},
            ] + [{'label': '形态 ' + k, **{x: v[x] for x in ('n', 'win', 'excess')}}
                 for k, v in group_stats(signals, lambda r: r['pattern'], base_any, h).items()],
            'pos60': [
                {'label': k, 'n': v['n'], 'win': v['win'], 'excess': v['excess']}
                for k, v in group_stats(signals,
                                        lambda r: '双线多头' if r['e12_gt_e50'] else '双线空头',
                                        base_any, h).items()],
        }

    today_rows = [r for r in signals if r['sel_date'] == as_of]
    today_sorted = sorted(today_rows, key=lambda r: (-int(r['is_new']), -(r['vr'] or 0)))
    recent = sorted(signals, key=lambda r: (r['sel_date'], r['code']), reverse=True)[:120]
    matured = sum(1 for r in signals if r['status'] == 'matured')
    case_code = 'sh603366'
    return {
        'as_of': as_of, 'n_stocks': None, 'n_days': len(calendar),
        'n_signals': len(signals),
        'definition': {'horizons': HORIZONS, 'window': 2, 'min_hits': 2,
                       'warmup': WARMUP, 'expma': [12, 50], 'ma89': '不涉及',
                       'rule': '连续两个交易日均为阳线（收>开）且阳柱实体同时包住 EXPMA12/EXPMA50；不看 MA89'},
        'summary': {'by_h': by_h}, 'by_grade': by_grade,
        'base_any': base_any,
        'by_year': by_year, 'by_half': by_half,
        'by_year_by_h': by_year_by_h, 'by_half_by_h': by_half_by_h,
        'top10_share': top10_share, 'three_gates': three_gates,
        'sensitivity': sens,
        'unconfirmed_excess': None, 'single_hit_n': 0,
        'dist': dist_by_h['5'], 'dist_by_h': dist_by_h,
        'event_path': ev_path,
        'timeline': timeline_by_h['5'], 'timeline_by_h': timeline_by_h,
        'bins': bins_by_h['5'], 'bins_by_h': bins_by_h,
        'recent': recent,
        'today': {'picks': today_sorted[:60], 'n': len(today_rows),
                  'n_A': sum(1 for r in today_rows if r['is_new']),
                  'n_B': sum(1 for r in today_rows if not r['is_new'])},
        'tracking': {'n_signals': len(signals), 'matured': matured,
                     'tracking': len(signals) - matured,
                     'new_today': sum(1 for r in today_rows if r['is_new']),
                     'matured10': sum(1 for r in signals if r['ret'].get('10') is not None)},
        'verdict': verdict,
        'case': {'n_A': 0, 'n_B': len(today_rows),
                 'picks': [{'code': r['code'], 'name': r['name'], 'grade': 'S2',
                            'pattern': r['pattern'], 'close': r['close'],
                            'e12': r['e12'], 'e50': r['e50']} for r in today_sorted[:12]]},
        'case_stock': {'code': case_code, 'name': universe.get(case_code, {}).get('name', ''),
                       'signals': [{'sel_date': r['sel_date'], 'grade': 'S2',
                                    'pattern': r['pattern'], 'entry_date': r['entry_date'],
                                    'entry_px': r['entry_px'], 'ret5': r['ret'].get('5'),
                                    'ret10': r['ret'].get('10'),
                                    'excess5': r['excess'].get('5'),
                                    'excess10': r['excess'].get('10')}
                                   for r in signals if r['code'] == case_code and
                                   r['sel_date'] >= calendar[-20]]},
    }


def write_csv(signals, path=None):
    cols = ['sel_date', 'code', 'name', 'ind', 'grade', 'hits', 'pattern', 'pattern4', 'is_new',
            'close', 'e12', 'e50', 'bull', 'e12_gt_e50', 'vr',
            'entry_date', 'entry_px', 'track_days', 'status', 'ret10', 'base10', 'excess10']
    cols += ['ret_%d' % h for h in HORIZONS] + ['base_%d' % h for h in HORIZONS] + \
            ['excess_%d' % h for h in HORIZONS]
    with open(path or OUT_CSV, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in signals:
            row = [r['sel_date'], r['code'], r['name'], r['ind'], r['grade'], r['hits'],
                   r['pattern'], r.get('pattern4', ''), int(r['is_new']), r['close'], r['e12'], r['e50'],
                   int(r['bull']), int(r['e12_gt_e50']), r['vr'],
                   r['entry_date'], r['entry_px'], r['track_days'], r['status'],
                   r['ret'].get('10'), r['base'].get('10'), r['excess'].get('10')]
            row += [r['ret'].get(str(h)) for h in HORIZONS]
            row += [r['base'].get(str(h)) for h in HORIZONS]
            row += [r['excess'].get(str(h)) for h in HORIZONS]
            w.writerow(row)


def write_report(d, path=None):
    """一份参数，兼容 daily_run 调用（首参为数据）。"""
    path = path or os.path.join(DAILY, '双线裹实体_%s.md' % d['as_of'])
    h5, h10 = d['summary']['by_h']['5'], d['summary']['by_h']['10']
    g = d['three_gates']
    lines = [
        '# 双线裹实体 · 全历史回测与跟踪 · %s' % d['as_of'], '',
        '## 口径',
        '- 信号：实体 `[min(开,收), max(开,收)]` 同时包住 EXPMA12 与 EXPMA50；最近 3 个交易日 ≥2 天入选（B），3 天全中（A）。阴阳不限。',
        '- 无未来函数：t 日收盘确认（只用 t 及之前的 K 线/均线），**t+1 开盘成交**；前向收益 = 收盘[成交日+h] / 开盘[成交日] − 1。',
        '- 基准：同成交日全市场等权（逐票逐日等权）+ 同期任意日基线；超额 = 前向 − 同日基线。',
        '- 样本：%d 只 / 全局日历 %d 日 / 信号 %d 个；成熟（已有10日前向）%d、跟踪中 %d。' % (
            d['n_stocks'], d['n_days'], d['n_signals'],
            d['tracking']['matured'], d['tracking']['tracking']),
        '', '## 全历史前向（三关口径）',
        '| 持有期 | 样本 | 前向均值% | 同日基线% | 任意日基线% | 超额pp | 超额胜率% | 中位超额pp |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for h in HORIZONS:
        s = d['summary']['by_h'][str(h)]
        lines.append('| %d日 | %s | %s | %s | %s | %s | %s | %s |' % (
            h, s['n'], s['mean'], s['base_same'], d['base_any'].get(str(h)),
            s['excess'], s['win'], s['median_excess']))
    lines += ['', '## 分级 / 分组',
              '| 组别 | 5日样本 | 5日超额pp | 10日超额pp |', '|---|---|---|---|']
    for grade, s in d['by_grade'].items():
        lines.append('| %s级 | %s | %s | %s |' % (
            grade, s['5']['n'], s['5']['excess'], s['10']['excess']))
    for k, v in d['by_year'].items():
        lines.append('| %s年 | %s | %s | — |' % (k, v['n'], v['excess']))
    for k, v in d['by_half'].items():
        lines.append('| %s | %s | %s | — |' % (k, v['n'], v['excess']))
    lines += ['- 仅 1 天命中（未入选对照）：%d 个窗口，10 日超额 %s pp。' % (
        d['single_hit_n'], d['unconfirmed_excess']),
        '- 分年份正超额 %d/%d · 最强 10%% 信号占全部正超额 %s%% · 前后半段切分日 %s。' % (
            g['positive_years'], g['n_years'], d['top10_share'], g['split_date']),
        '', '## 参数敏感性（5日 / 10日超额pp）',
        '| 口径 | 5日样本 | 5日超额 | 10日超额 |', '|---|---|---|---|']
    for v in d['sensitivity']:
        a, b = v['by_h']['5'], v['by_h']['10']
        lines.append('| %s | %s | %s | %s |' % (v['label'], a['n'], a['excess'], b['excess']))
    lines += ['', '## 当日入选（%s，A级 %d / B级 %d）' % (
        d['as_of'], d['today']['n_A'], d['today']['n_B']),
        '| 代码 | 名称 | 级别 | 命中形态 | 收盘 | EMA12 | EMA50 |', '|---|---|---|---|---|---|---|']
    for r in d['today']['picks']:
        lines.append('| %s | %s | %s | %s | %s | %s | %s |' % (
            r['code'], r['name'], r['grade'], r['pattern'], r['close'], r['e12'], r['e50']))
    cs = d.get('case_stock') or {}
    if cs.get('signals'):
        lines += ['', '## 案例复核（截图样本 %s %s）' % (cs['code'], cs['name']),
                  '| 选入日 | 级别 | 命中形态 | 成交日 | 成交价 | 5日% | 5日超额pp | 10日% | 10日超额pp |',
                  '|---|---|---|---|---|---|---|---|---|']
        for r in cs['signals']:
            lines.append('| %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
                r['sel_date'], r['grade'], r['pattern'], r['entry_date'], r['entry_px'],
                r['ret5'], r['excess5'], r['ret10'], r['excess10']))
        lines.append('- 单案例不构成结论；入选后涨幅集中于少数极端值（见最强 10% 贡献）。')
    lines += ['', '## 三关结论',
              '- ① 基准对比：已过（两条基线 + 超额口径）；② 无未来函数：已过（次日开盘成交）；③ 分年份稳健性：%s。' % (
                  '已过' if g['year_robust'] else '未过'),
              '- %s' % d['verdict'].replace('<b>', '').replace('</b>', ''),
              '', '> 结果为客观数据筛选与描述性回测，不构成投资建议。',
              '> 全历史信号与 10 日涨幅明细：webapp/bodywrap/signals.csv（每日跑批自动补齐跟踪中信号）。']
    s2 = d.get('strict2')
    if s2:
        lines += ['', '---', '', '## 口径二 · 连续两日阳柱裹双线（不看 MA89）',
                  '- 信号：**连续两个交易日均为阳线（收>开）**，且阳柱实体 `[开, 收]` 同时包住 EXPMA12 与 EXPMA50；MA89 不参与任何判定。',
                  '- t 日收盘确认、**t+1 开盘成交**；基准（同成交日全市场等权 + 同期任意日）与三关口徑同口径一。',
                  '- 全历史信号 %d 个，成熟 %d / 跟踪中 %d；当日入选 %d 只（首次达成 %d / 簇内延续 %d）。' % (
                      s2['n_signals'], s2['tracking']['matured'], s2['tracking']['tracking'],
                      s2['today']['n'], s2['today']['n_A'], s2['today']['n_B']),
                  '', '| 持有期 | 样本 | 前向均值% | 同日基线% | 超额pp | 超额胜率% |',
                  '|---|---|---|---|---|---|']
        for h in HORIZONS:
            a = s2['summary']['by_h'][str(h)]
            lines.append('| %d日 | %s | %s | %s | %s | %s |' % (
                h, a['n'], a['mean'], a['base_same'], a['excess'], a['win']))
        lines += ['', '| 分组 | 5日样本 | 5日超额pp | 10日超额pp |', '|---|---|---|---|',
                  '| 首次达成 | %s | %s | %s |' % (
                      s2['by_grade']['A']['5']['n'], s2['by_grade']['A']['5']['excess'],
                      s2['by_grade']['A']['10']['excess']),
                  '| 簇内延续 | %s | %s | %s |' % (
                      s2['by_grade']['B']['5']['n'], s2['by_grade']['B']['5']['excess'],
                      s2['by_grade']['B']['10']['excess'])]
        for k, v in s2['by_year'].items():
            lines.append('| %s年 | %s | %s | — |' % (k, v['n'], v['excess']))
        for k, v in s2['by_half'].items():
            lines.append('| %s | %s | %s | — |' % (k, v['n'], v['excess']))
        lines += ['', '### 参数敏感性（5日 / 10日超额pp）',
                  '| 口径 | 5日样本 | 5日超额 | 10日超额 |', '|---|---|---|---|']
        for v in s2['sensitivity']:
            a, b = v['by_h']['5'], v['by_h']['10']
            lines.append('| %s | %s | %s | %s |' % (v['label'], a['n'], a['excess'], b['excess']))
        lines += ['', '### 当日入选（%s，%d 只）' % (d['as_of'], s2['today']['n']),
                  '| 代码 | 名称 | 命中形态 | 收盘 | EMA12 | EMA50 | 5日量比 |',
                  '|---|---|---|---|---|---|---|']
        for r in s2['today']['picks']:
            lines.append('| %s | %s | %s | %s | %s | %s | %s |' % (
                r['code'], r['name'], r['pattern'], r['close'], r['e12'], r['e50'], r.get('vr')))
        cs2 = s2.get('case_stock') or {}
        if cs2.get('signals'):
            lines += ['', '### 案例复核（截图样本 %s %s）' % (cs2['code'], cs2['name']),
                      '| 选入日 | 命中形态 | 成交日 | 成交价 | 5日% | 5日超额pp | 10日% | 10日超额pp |',
                      '|---|---|---|---|---|---|---|---|']
            for r in cs2['signals']:
                lines.append('| %s | %s | %s | %s | %s | %s | %s | %s |' % (
                    r['sel_date'], r['pattern'], r['entry_date'], r['entry_px'],
                    r['ret5'], r['excess5'], r['ret10'], r['excess10']))
            lines.append('- 单案例不构成结论。')
        lines += ['', '- 三关结论：①已过 / ②已过 / ③%s。%s' % (
                      '已过' if s2['three_gates']['year_robust'] else '未过',
                      s2['verdict'].replace('<b>', '').replace('</b>', '')),
                  '- 全历史信号与 10 日涨幅明细：webapp/bodywrap/signals_strict2.csv。']
    open(path, 'w').write('\n'.join(lines) + '\n')
    return path


if __name__ == '__main__':
    d = build()
    s2 = d['strict2']
    print(json.dumps({'as_of': d['as_of'], 'n_signals': d['n_signals'],
                      'today': d['today']['n'], 'n_A': d['today']['n_A'],
                      'n_B': d['today']['n_B'], 'd10_excess': d['summary']['by_h']['10']['excess'],
                      'd5_excess': d['summary']['by_h']['5']['excess'],
                      'year_robust': d['three_gates']['year_robust'],
                      'strict2': {'n_signals': s2['n_signals'], 'today': s2['today']['n'],
                                  'd5_excess': s2['summary']['by_h']['5']['excess'],
                                  'd10_excess': s2['summary']['by_h']['10']['excess'],
                                  'win10': s2['summary']['by_h']['10']['win'],
                                  'year_robust': s2['three_gates']['year_robust']},
                      'report': d['report']}, ensure_ascii=False))
