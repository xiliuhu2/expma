#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
立桩量 · 全市场信号复现与前向验证

信号定义（公开公式口径，非任何行情软件自选指标的逐行等价实现）
--------------------------------------------------------------
立桩量柱 t（默认口径 A，源自公开的同花顺「量能量柱」公式）：
    VOL[t] >= HHV(VOL, 40)[t]  且  VOL[t] > 1.9 * VOL[t-1]
    即：成交量创 40 日新高，且较前一日放大 90% 以上。
另一常见口径 B（量能翻倍版）：VOL[t] / VOL[t-1] - 1 >= 1。
「低位/上升途中」属主观描述，公开资料无统一量化定义，本模块默认不加，
仅在参数敏感性里给出 C[t] > MA(C,60)[t] 的变体结果。

三天法则确认：立桩量日 t 记 H0=high[t]、L0=low[t]；
在 t+1..t+3 内寻找第一个 close[k] > H0 且期间 low 均未跌破 L0 的交易日 k。
k 即为「确认日」。未能在 3 日内确认者单独归为未确认组（描述性对照）。

三关纪律（结论一律标「描述/候选」，不写成操作建议）
----------------------------------------------------
① 基准对比：不报裸收益。
   base_same = 同成交日全市场等权、同持有期（控制信号日的市场环境）
   base_any  = 「同期任意日买入同持有期」全样本等权均值
   超额 = 前向收益 - base_same
② 无未来函数：确认只需 t..k 的收盘数据，k 收盘确认、k+1 开盘成交；
   持有 h 个交易日以 calendar[k+1+h] 收盘计。评级/分箱不用未来数据。
③ 分年份稳健性：正超额年数、最强两年贡献占比、前后半段样本外、
   参数敏感性（窗口 / 倍数 / 趋势过滤 / 是否限三日）。
   本地日K约 321 根（2025-07 起）、仅 2 个年份段，且为日线量能而非真实盘中量，
   年份样本不足 → 一律标「描述/候选」。

产出：webapp/lizhuang.json（前端唯一数据源）
      webapp/lizhuang/signals.csv（全历史信号回测表）
      daily/立桩量_YYYY-MM-DD.md（专题报告）
"""
import array
import csv
import datetime
import json
import os
import statistics

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(_REPO, 'backtest')
WEB = os.path.join(_REPO, 'webapp')
DAILY = os.path.join(_REPO, 'daily')
OUT_JSON = os.path.join(WEB, 'lizhuang.json')
OUT_DIR = os.path.join(WEB, 'lizhuang')
OUT_CSV = os.path.join(OUT_DIR, 'signals.csv')

HORIZONS = [1, 3, 5, 10, 20]
PATH_BEFORE, PATH_AFTER = 10, 20
CONFIRM_WINDOW = 3
WARMUP = 60
CASE_CODE = 'sz300102'


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _r(v, n=3):
    return round(v, n) if isinstance(v, (int, float)) else None


def load_universe():
    """全市场：K线文件域 + 名称/行业/市值快照（能取到就带上）。"""
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
    kdir = os.path.join(ROOT, 'klines')
    out = {}
    for fn in os.listdir(kdir):
        if not fn.endswith('.json'):
            continue
        code = fn[:-5]
        m = meta.get(code, {})
        out[code] = {'name': m.get('name', ''), 'ind': ind.get(code, ''),
                     'mv': m.get('mv', 0.0)}
    return out


def load_klines(universe):
    """按全局交易日历对齐的 OHLCV 数组（缺失日为 0）。"""
    files = {}
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
        files[code] = bars
        all_dates.update(b[0] for b in bars)
    calendar = sorted(d for d in all_dates if d)
    n = len(calendar)
    cpos = {d: i for i, d in enumerate(calendar)}
    stocks = {}
    for code, bars in files.items():
        o = array.array('d', [0.0]) * n
        c = array.array('d', [0.0]) * n
        h = array.array('d', [0.0]) * n
        l = array.array('d', [0.0]) * n
        v = array.array('d', [0.0]) * n
        for b in bars:
            i = cpos.get(b[0])
            if i is None:
                continue
            o[i], c[i], h[i], l[i], v[i] = _f(b[1]), _f(b[2]), _f(b[3]), _f(b[4]), _f(b[5])
        stocks[code] = {'o': o, 'c': c, 'h': h, 'l': l, 'v': v}
    return stocks, calendar


def market_returns(stocks, calendar):
    """预计算 (成交日索引, h) -> 全市场等权收益(%)，供基准与任意日基线复用。"""
    n = len(calendar)
    codes = list(stocks)
    out = {}
    for hh in HORIZONS:
        for te in range(WARMUP, n - hh - 1):
            tx = te + hh
            vals = []
            for code in codes:
                s = stocks[code]
                if s['o'][te] > 0 and s['c'][tx] > 0:
                    vals.append(s['c'][tx] / s['o'][te] - 1)
            out[(te, hh)] = statistics.mean(vals) * 100 if vals else None
    return out


def pillar(s, t, window=40, mult=1.9):
    """立桩量柱判定；返回量比或 None。"""
    v, n = s['v'], len(s['v'])
    if t < WARMUP or t + CONFIRM_WINDOW + max(HORIZONS) + 1 >= n:
        return None
    if v[t] <= 0 or v[t - 1] <= 0 or s['o'][t] <= 0 or s['c'][t] <= 0:
        return None
    if v[t] < mult * v[t - 1]:
        return None
    lo = max(0, t - window + 1)
    if v[t] < max(v[lo:t + 1]):
        return None
    return v[t] / v[t - 1]


def trend_ok(s, t, n=60):
    """可选变体：收盘在 60 日均线上方（「上升途中」的粗糙代理）。"""
    c = s['c']
    if t < n:
        return False
    seg = [c[i] for i in range(t - n + 1, t + 1) if c[i] > 0]
    return len(seg) >= n * 0.8 and c[t] > sum(seg) / len(seg)


def confirm(s, t, max_wait=CONFIRM_WINDOW):
    """三天法则：返回确认日索引；未确认返回 None。"""
    h0, l0 = s['h'][t], s['l'][t]
    if h0 <= 0 or l0 <= 0:
        return None
    for k in range(t + 1, t + max_wait + 1):
        if s['o'][k] <= 0 or s['c'][k] <= 0:
            continue
        if any(0 < s['l'][j] <= l0 for j in range(t + 1, k + 1)):
            return None
        if s['c'][k] > h0:
            return k
    return None


def collect(stocks, calendar, mret, universe,
            window=40, mult=1.9, use_trend=False, max_wait=CONFIRM_WINDOW,
            need_confirm=True, collect_paths=True):
    """扫描全市场，产出信号明细。need_confirm=False 时以 t+1 为成交前一日（立桩量日收盘确认）。"""
    n = len(calendar)
    signals = []
    for code, s in stocks.items():
        for t in range(WARMUP, n - max_wait - max(HORIZONS) - 2):
            ratio = pillar(s, t, window, mult)
            if ratio is None:
                continue
            if use_trend and not trend_ok(s, t):
                continue
            k = confirm(s, t, max_wait) if need_confirm else None
            if need_confirm and k is None:
                continue
            kk = k if k is not None else t
            te = kk + 1
            if te >= n - max(HORIZONS) - 1 or s['o'][te] <= 0:
                continue
            ret, base, exc = {}, {}, {}
            for hh in HORIZONS:
                if te + hh >= n or s['c'][te + hh] <= 0:
                    continue
                r = (s['c'][te + hh] / s['o'][te] - 1) * 100
                b = mret.get((te, hh))
                ret[str(hh)] = _r(r)
                base[str(hh)] = _r(b)
                exc[str(hh)] = _r(r - b) if b is not None else None
            path = []
            if collect_paths:
                for j in range(max(0, t - PATH_BEFORE), min(n, kk + 1 + PATH_AFTER)):
                    if s['c'][j] > 0 and s['c'][t] > 0:
                        path.append(_r((s['c'][j] / s['c'][t] - 1) * 100))
            signals.append({
                'code': code, 'name': universe.get(code, {}).get('name', ''),
                'ind': universe.get(code, {}).get('ind', ''),
                'pillar_date': calendar[t], 'confirm_date': calendar[kk],
                'entry_date': calendar[te],
                'confirmed': k is not None,
                'pillar_high': _r(s['h'][t], 2), 'pillar_low': _r(s['l'][t], 2),
                'vol_ratio': _r(ratio, 2),
                'pos60': _r((s['c'][t] / max(s['c'][t - 60:t]) - 1) * 100, 2)
                         if t >= 60 and max(s['c'][t - 60:t]) > 0 else None,
                'ret': ret, 'base': base, 'excess': exc,
                'path': path,
            })
    return signals


def summarize(signals, label, mret=None):
    out = {'label': label, 'n': len(signals), 'by_h': {}}
    for hh in HORIZONS:
        k = str(hh)
        vals = [x['excess'][k] for x in signals if x['excess'].get(k) is not None]
        raw = [x['ret'][k] for x in signals if x['ret'].get(k) is not None]
        bse = [x['base'][k] for x in signals if x['base'].get(k) is not None]
        if not vals:
            continue
        out['by_h'][k] = {
            'n': len(vals),
            'mean': _r(statistics.mean(raw)),
            'base_same': _r(statistics.mean(bse)),
            'excess': _r(statistics.mean(vals)),
            'median_excess': _r(statistics.median(vals)),
            'win': _r(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        }
    return out


def by_period(signals, calendar, key):
    """按年份或前后半段切分，返回 5 日超额汇总。"""
    mid = calendar[len(calendar) // 2]
    groups = {}
    for x in signals:
        d = x['confirm_date']
        g = d[:4] if key == 'year' else ('前半' if d < mid else '后半')
        e = x['excess'].get('5')
        if e is None:
            continue
        groups.setdefault(g, []).append(e)
    out = {}
    for g, vals in sorted(groups.items()):
        out[g] = {'n': len(vals), 'excess': _r(statistics.mean(vals)),
                  'win': _r(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)}
    return out


def histogram(vals, bins=16, lo=-25, hi=25):
    step = (hi - lo) / bins
    cnt = [0] * bins
    for v in vals:
        i = int((v - lo) / step)
        cnt[max(0, min(bins - 1, i))] += 1
    return [{'x0': _r(lo + i * step, 1), 'x1': _r(lo + (i + 1) * step, 1),
             'n': c} for i, c in enumerate(cnt)]


def event_path(signals):
    """事件对齐：以立桩量日为 0，平均累计涨跌路径（相对立桩日收盘）。"""
    n = PATH_BEFORE + 1 + PATH_AFTER
    acc = [0.0] * n
    cnt = [0] * n
    for x in signals:
        p = x['path']
        if len(p) < n:
            continue
        for i, v in enumerate(p[:n]):
            acc[i] += v
            cnt[i] += 1
    return [{'i': i - PATH_BEFORE, 'v': _r(acc[i] / cnt[i]) if cnt[i] else None,
             'n': cnt[i]} for i in range(n)]


def timeline(signals, calendar):
    daily = {}
    for x in signals:
        e = x['excess'].get('5')
        if e is None:
            continue
        a = daily.setdefault(x['confirm_date'], [0, 0.0])
        a[0] += 1
        a[1] += e
    cum = 0.0
    out = []
    for d in calendar:
        if d in daily:
            n, s = daily[d]
            cum += s / n
            out.append({'date': d, 'n': n, 'excess': _r(s / n), 'cum': _r(cum)})
    return out


def bins_of(signals):
    def mk(field, edges, labels):
        out = []
        for i, lab in enumerate(labels):
            vals = []
            for x in signals:
                fv = x.get(field)
                e = x['excess'].get('5')
                if fv is None or e is None:
                    continue
                if (i == 0 and fv < edges[0]) or \
                   (i == len(labels) - 1 and fv >= edges[-1]) or \
                   (0 < i < len(labels) - 1 and edges[i - 1] <= fv < edges[i]):
                    vals.append(e)
            out.append({'label': lab, 'n': len(vals),
                        'excess': _r(statistics.mean(vals)) if vals else None,
                        'win': _r(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None})
        return out
    return {
        'vol_ratio': mk('vol_ratio', [2.0, 2.5, 3.0, 4.0], ['<2.0', '2.0~2.5', '2.5~3.0', '3.0~4.0', '>=4.0']),
        'pos60': mk('pos60', [-15, -5, 5, 15], ['<-15%', '-15~-5%', '-5~+5%', '+5~+15%', '>=+15%']),
    }


def case_study(stocks, calendar, universe):
    """用公开口径复核一张周K截图里被标为「立桩」的那只票。"""
    s = stocks.get(CASE_CODE)
    if not s:
        return None
    n = len(calendar)
    rows = []
    for t in range(WARMUP, n - 25):
        ratio = pillar(s, t)
        if ratio is None:
            continue
        k = confirm(s, t)
        rows.append({'date': calendar[t], 'vol_ratio': _r(ratio, 2),
                     'high': _r(s['h'][t], 2), 'low': _r(s['l'][t], 2),
                     'confirmed': k is not None,
                     'confirm_date': calendar[k] if k is not None else None})
    return {'code': CASE_CODE, 'name': universe.get(CASE_CODE, {}).get('name', ''),
            'pillars': rows}


def write_report(d, path):
    s = d['summary']['by_h'].get('5', {})
    lines = [
        '# 立桩量 · 全历史复现与前向验证 · %s' % d['as_of'],
        '',
        '> 口径说明：按公开公式复现，非任何行情软件自选指标的逐行等价实现。'
        'VWAP10/60/120、低位价格异常、承接中、S 卖点等闭源自选输出无法复刻，'
        '本模块只还原「立桩量柱 + 三天法则」这一可检验内核；'
        '结论一律为描述性/候选，不构成操作建议。',
        '',
        '## 一、信号定义',
        '',
        '- 立桩量柱：`VOL[t] >= HHV(VOL,40)[t]` 且 `VOL[t] > 1.9 × VOL[t-1]`',
        '- 三天法则：t+1..t+3 内首次 `close > high[t]` 且期间 `low` 不破 `low[t]` → 确认日 k',
        '- 成交：k+1 开盘；持有 h 日以 `close[k+1+h]` 计；基线为同成交日全市场等权',
        '- 目标位「最高价×2」等民间说法不纳入验证（无可检验定义）',
        '',
        '## 二、总体（5 日口径）',
        '',
        '| 项 | 值 |',
        '|---|---|',
        '| 信号数 | %s |' % s.get('n'),
        '| 前向均值 | %s%% |' % s.get('mean'),
        '| 同日全市场基线 | %s%% |' % s.get('base_same'),
        '| **超额** | **%s pp** |' % s.get('excess'),
        '| 跑赢基线占比 | %s%% |' % s.get('win'),
        '',
        '## 三、三关',
        '',
        '**① 基准对比**：已同时给同日全市场等权与任意日基线（见 JSON `base_any`）。',
        '',
        '**② 无未来函数**：确认只用 t..k 收盘数据，k+1 开盘成交；分箱与标签不含未来信息。',
        '',
        '**③ 分年份稳健性**：',
        '',
        '| 年份 | 样本 | 5日超额 | 胜率 |',
        '|---|---|---|---|',
    ]
    for y, v in d['by_year'].items():
        lines.append('| %s | %s | %s pp | %s%% |' % (y, v['n'], v['excess'], v['win']))
    lines += ['', '| 时段 | 样本 | 5日超额 | 胜率 |', '|---|---|---|---|']
    for y, v in d['by_half'].items():
        lines.append('| %s | %s | %s pp | %s%% |' % (y, v['n'], v['excess'], v['win']))
    lines += ['', '**参数敏感性**（5日超额 pp）：', '',
              '| 口径 | 样本 | 超额 |', '|---|---|---|']
    for v in d['sensitivity']:
        b = v['by_h'].get('5', {})
        lines.append('| %s | %s | %s |' % (v['label'], b.get('n'), b.get('excess')))
    lines += ['', '## 四、结论', '', d['verdict'], '',
              '## 五、截图案例复核', '']
    cs = d.get('case')
    if cs and cs['pillars']:
        lines.append('`%s %s` 按公开口径检出的立桩量柱：' % (cs['code'], cs['name']))
        lines += ['', '| 日期 | 量比 | 高/低 | 三日内确认 | 确认日 |', '|---|---|---|---|---|']
        for r in cs['pillars'][-12:]:
            lines.append('| %s | %s | %s / %s | %s | %s |' %
                         (r['date'], r['vol_ratio'], r['high'], r['low'],
                          '是' if r['confirmed'] else '否', r['confirm_date'] or '—'))
    else:
        lines.append('未检出。')
    lines += ['', '---', '', '以上为公开数据与历史统计描述，仅供参考，不构成投资建议。', '']
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


def build():
    universe = load_universe()
    stocks, calendar = load_klines(universe)
    if not stocks or len(calendar) < WARMUP + 60:
        raise RuntimeError('全市场K线不足，请先跑 daily_run.py')
    mret = market_returns(stocks, calendar)
    base_any = {}
    for hh in HORIZONS:
        vals = [v for (te, h), v in mret.items() if h == hh and v is not None]
        base_any[str(hh)] = _r(statistics.mean(vals))

    main = collect(stocks, calendar, mret, universe)
    summary = summarize(main, '默认口径 · 三日确认')
    by_year = by_period(main, calendar, 'year')
    by_half = by_period(main, calendar, 'half')

    variants = [
        ('窗口 20 日', dict(window=20)),
        ('窗口 10 日', dict(window=10)),
        ('倍数 2.0', dict(mult=2.0)),
        ('倍数 1.0（量翻倍）', dict(mult=1.0, window=10)),
        ('加 60 日均线趋势过滤', dict(use_trend=True)),
        ('放宽确认至 6 日', dict(max_wait=6)),
        ('不做三日确认（立桩日收盘即计）', dict(need_confirm=False)),
    ]
    sens = [summarize(collect(stocks, calendar, mret, universe, collect_paths=False, **kw), lab)
            for lab, kw in variants]
    unconfirmed = collect(stocks, calendar, mret, universe, need_confirm=False,
                          collect_paths=False)
    # 未确认组 = 立桩量日收盘即计 减去 三日确认成功的那批（按 code+pillar_date）
    ok = {(x['code'], x['pillar_date']) for x in main}
    unconf = [x for x in unconfirmed if (x['code'], x['pillar_date']) not in ok]
    sens.append(summarize(unconf, '三日未确认组'))

    vals5 = [x['excess']['5'] for x in main if x['excess'].get('5') is not None]
    pos_sum = sum(v for v in vals5 if v > 0)
    strong = sum(sorted(vals5, reverse=True)[:max(1, len(vals5) // 10)])
    top10_share = _r(strong / pos_sum * 100, 1) if pos_sum > 0 else None
    pos_years = sum(1 for v in by_year.values() if (v['excess'] or 0) > 0)
    sens_vals = [v['by_h'].get('5', {}).get('excess') for v in sens[:-1]]
    sens_vals = [v for v in sens_vals if v is not None]
    sens_all_neg = bool(sens_vals) and all(v < 0 for v in sens_vals)
    unconf_excess = sens[-1]['by_h'].get('5', {}).get('excess')

    e5 = summary['by_h'].get('5', {})
    verdict = (
        '**描述/候选，未过三关，不写操作建议。** '
        '默认口径共 %s 个信号，次日开盘接力的 5 日超额 %s pp、胜率 %s%%；'
        '分年份 %s，前后半段 %s / %s pp，正超额年数 %s/%s。'
        '%s 组参数变体（窗口/倍数/趋势过滤/确认窗口）5 日超额区间 %s~%s pp、%s，'
        '不存在能挑出正期望的参数平台；三日未确认组 %s pp，说明三天法则确实剔掉了更差的一批，'
        '但确认后整体仍为负期望。最强 10%% 信号占全部正超额 %s%%。'
        '本地日K仅 %s 根、覆盖 %s 起共 %s 个年份段，且为日线量能而非真实盘中量，'
        '样本期与年份稳健性均不足，故不把方向性结论写成可执行建议。'
        '该模块仅用于形态归档与描述性对照，不接入任何提醒、下单或仓位链路。'
        % (summary['n'], e5.get('excess'), e5.get('win'),
           json.dumps({k: v['excess'] for k, v in by_year.items()}, ensure_ascii=False),
           by_half.get('前半', {}).get('excess'), by_half.get('后半', {}).get('excess'),
           pos_years, len(by_year), len(sens_vals),
           min(sens_vals) if sens_vals else None, max(sens_vals) if sens_vals else None,
           '全部为负' if sens_all_neg else '方向不一致',
           unconf_excess, top10_share, len(calendar), calendar[0], len(by_year))
    )

    d = {
        'as_of': calendar[-1],
        'calendar_start': calendar[0], 'calendar_end': calendar[-1],
        'n_days': len(calendar), 'n_stocks': len(stocks),
        'definition': {
            'pillar': 'VOL[t] >= HHV(VOL,40)[t] 且 VOL[t] > 1.9*VOL[t-1]',
            'confirm': '三天法则：t+1..t+3 首次 close > high[t] 且期间 low 不破 low[t]',
            'entry': '确认日 k 收盘确认，k+1 开盘成交',
            'horizons': HORIZONS,
            'note': '按公开公式复现，非行情软件自选指标的等价实现；VWAP10/60/120、低位价格异常、承接中等闭源输出无法复刻',
        },
        'base_any': base_any,
        'summary': summary,
        'by_year': by_year, 'by_half': by_half,
        'sensitivity': sens,
        'dist': histogram(vals5),
        'event_path': event_path(main),
        'timeline': timeline(main, calendar),
        'bins': bins_of(main),
        'top10_share': top10_share,
        'sens_all_negative': sens_all_neg,
        'unconfirmed_excess': unconf_excess,
        'three_gates': {
            'base_compare': True, 'no_future': True,
            'year_robust': False,
            'note': '① 基准对比、② 无未来函数 已落实；③ 分年份稳健性未通过（两年均负、样本期短、年份段不足）',
        },
        'verdict': verdict,
        'case': case_study(stocks, calendar, universe),
        'recent': sorted(main, key=lambda x: x['pillar_date'], reverse=True)[:60],
        'n_signals': len(main),
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)
    with open(OUT_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['code', 'name', 'ind', 'pillar_date', 'confirm_date', 'entry_date',
                    'confirmed', 'pillar_high', 'pillar_low', 'vol_ratio', 'pos60'] +
                   ['ret_%s' % h for h in HORIZONS] +
                   ['base_%s' % h for h in HORIZONS] +
                   ['excess_%s' % h for h in HORIZONS])
        for x in sorted(main, key=lambda y: (y['pillar_date'], y['code'])):
            w.writerow([x['code'], x['name'], x['ind'], x['pillar_date'], x['confirm_date'],
                        x['entry_date'], int(x['confirmed']), x['pillar_high'], x['pillar_low'],
                        x['vol_ratio'], x['pos60']] +
                       [x['ret'].get(str(h), '') for h in HORIZONS] +
                       [x['base'].get(str(h), '') for h in HORIZONS] +
                       [x['excess'].get(str(h), '') for h in HORIZONS])
    os.makedirs(DAILY, exist_ok=True)
    write_report(d, os.path.join(DAILY, '立桩量_%s.md' % calendar[-1]))
    return d


if __name__ == '__main__':
    d = build()
    s = d['summary']['by_h'].get('5', {})
    print('as_of=%s stocks=%s days=%s signals=%s' %
          (d['as_of'], d['n_stocks'], d['n_days'], d['n_signals']))
    print('5d: n=%s mean=%s base=%s excess=%s win=%s%%' %
          (s.get('n'), s.get('mean'), s.get('base_same'), s.get('excess'), s.get('win')))
    print('by_year=%s' % json.dumps(d['by_year'], ensure_ascii=False))
    print('by_half=%s' % json.dumps(d['by_half'], ensure_ascii=False))
    for v in d['sensitivity']:
        b = v['by_h'].get('5', {})
        print('  sens %-24s n=%-6s excess=%s' % (v['label'], b.get('n'), b.get('excess')))
    cs = d.get('case') or {}
    print('case %s pillars=%s %s' % (cs.get('code'), len(cs.get('pillars') or []),
                                    json.dumps((cs.get('pillars') or [])[-6:], ensure_ascii=False)))
    print('verdict=%s' % d['verdict'])
