#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 · EXPMA 首次上穿（转势首叉）模块

信号定义（用户口径 2026-11-10）
--------------------------------
沪深300 成份股中，某只票的 EMA12 由「<= EMA50」转为「> EMA50」的当日 = 首次满足
买入条件 = 开始转势。为避免双线反复缠绕的假转势，默认要求此前至少 QUIET 个交易日
持续 EMA12 <= EMA50（冷静期，前端可调 0/5/10/20/40/60）。

三关纪律（结论只标「描述/候选」，不写成操作建议）
--------------------------------------------------
① 基准对比：不报裸收益。给两套基线
   - base_same = 同日全成份等权、同持有期（控制信号日的市场环境）
   - base_any  = 「同期任意日买入同持有期」全样本等权均值
   超额 = 前向收益 - base_same；汇总同时给 vs base_any。
② 无未来函数：信号只用 t 及之前数据（收盘确认）；成交一律 t+1 开盘，
   持有 h 个交易日后以 t+h 收盘计。标签/评级不用未来数据。
③ 分年份稳健性：正超额年数、最强两年贡献占比、时间前后段样本外。
   注意：本地日K仅约 321 根（2025-06 起）、且股票域用「当前」成份股，
   存在历史成份回溯偏差 + 年份样本不足 → 一律标「描述/候选」。

产出：webapp/firstcross.json（前端唯一数据源）
      webapp/firstcross/signals.csv（全历史信号回测表）
      daily/转势首叉_YYYY-MM-DD.md（专题报告，由 write_report 生成）
用法：python3 firstcross.py
"""
import csv
import datetime
import json
import os
import statistics

BASE = os.path.expanduser('~/WorkBuddy/Claw')
ROOT = os.path.join(BASE, 'backtest')
WEB = os.path.join(BASE, 'webapp')
DAILY = os.path.join(BASE, 'daily')
OUT_JSON = os.path.join(WEB, 'firstcross.json')
OUT_DIR = os.path.join(WEB, 'firstcross')
OUT_CSV = os.path.join(OUT_DIR, 'signals.csv')

QUIET_DEFAULT = 20
QUIET_OPTIONS = [0, 5, 10, 20, 40, 60]
HORIZONS = [1, 3, 5, 10, 20]
PATH_BEFORE, PATH_AFTER = 10, 20


def ema(vals, n):
    """与 server.py:ema 完全一致的口径（e[0]=vals[0]）。"""
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def is_st(name):
    n = (name or '').upper().replace(' ', '').replace('　', '')
    return ('ST' in n) or ('退' in (name or ''))


def load_universe():
    """沪深300成份股（当前名单）+ 行业 + 市值快照。"""
    cons = json.load(open(os.path.join(WEB, 'hs300_cons.json')))
    ind = json.load(open(os.path.join(WEB, 'industry.json')))
    snap = json.load(open(os.path.join(ROOT, 'snapshot.json')))
    items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
    meta = {r['code']: {'name': r.get('name', ''), 'mv': _f(r.get('TotalMV') or 0)}
            for r in items}
    out = {}
    for c6, name in cons['map'].items():
        code = ('sh' if c6[0] == '6' else ('bj' if c6[0] in '48' else 'sz')) + c6
        if is_st(name):
            continue
        out[code] = {'name': meta.get(code, {}).get('name') or name,
                     'ind': ind.get(code, '其他'),
                     'mv': meta.get(code, {}).get('mv')}
    return cons, out


def load_klines(universe):
    """按日期索引的 OHLCV；返回 stocks / 全局交易日历。"""
    stocks, all_dates = {}, set()
    for code in universe:
        p = os.path.join(ROOT, 'klines', code + '.json')
        if not os.path.exists(p):
            continue
        bars = json.load(open(p))
        if len(bars) < 70:
            continue
        dates = [b[0] for b in bars]
        o = [_f(b[1]) for b in bars]
        c = [_f(b[2]) for b in bars]
        h = [_f(b[3]) for b in bars]
        v = [_f(b[5]) for b in bars]
        stocks[code] = {
            'dates': dates, 'o': o, 'c': c, 'h': h, 'v': v,
            'e12': ema(c, 12), 'e50': ema(c, 50),
            'idx': {d: i for i, d in enumerate(dates)},
        }
        all_dates.update(dates)
    return stocks, sorted(all_dates)


def bench_daily(stocks, calendar):
    """等权全成份日收益序列（按日期），用于事件对齐与描述性对照。"""
    bucket = {d: [] for d in calendar}
    for s in stocks.values():
        d, c = s['dates'], s['c']
        for t in range(1, len(d)):
            if c[t - 1] > 0 and c[t] > 0:
                bucket[d[t]].append(c[t] / c[t - 1] - 1)
    return {d: (statistics.mean(v) if v else 0.0) for d, v in bucket.items()}


def cross_section(stocks, d_entry, d_exit):
    """同日全成份等权、同持有期：mean(close[d_exit] / open[d_entry] - 1)。"""
    vals = []
    for s in stocks.values():
        i = s['idx'].get(d_entry)
        j = s['idx'].get(d_exit)
        if i is None or j is None or s['o'][i] <= 0:
            continue
        vals.append(s['c'][j] / s['o'][i] - 1)
    return statistics.mean(vals) * 100 if vals else None


def base_any(stocks, calendar):
    """「同期任意日买入同持有期」全样本等权均值（%）。"""
    out = {}
    for h in HORIZONS:
        vals = []
        for t in range(60, len(calendar) - h - 1):
            de, dx = calendar[t + 1], calendar[t + h]
            for s in stocks.values():
                i, j = s['idx'].get(de), s['idx'].get(dx)
                if i is None or j is None or s['o'][i] <= 0:
                    continue
                vals.append(s['c'][j] / s['o'][i] - 1)
        out[str(h)] = round(statistics.mean(vals) * 100, 3) if vals else None
    return out


def build():
    cons, universe = load_universe()
    stocks, calendar = load_klines(universe)
    if not stocks:
        raise RuntimeError('沪深300成份股K线为空，请先跑 daily_run.py')
    cset = set(calendar)
    cpos = {d: i for i, d in enumerate(calendar)}
    bret = bench_daily(stocks, calendar)
    bany = base_any(stocks, calendar)

    # 预计算基准日历（entry date -> exit date）；同一信号日的基线只算一次
    base_cache = {}
    def base_same(d_sig, h):
        key = (d_sig, h)
        if key not in base_cache:
            t = cpos.get(d_sig)
            if t is None or t + h >= len(calendar):
                base_cache[key] = None
            else:
                base_cache[key] = cross_section(stocks, calendar[t + 1], calendar[t + h])
        return base_cache[key]

    signals = []
    for code, s in stocks.items():
        d, c, o, e12, e50 = s['dates'], s['c'], s['o'], s['e12'], s['e50']
        n = len(d)
        # 连续 EMA12<=EMA50 的交易日数（从序列第 1 根起维护，t>=60 才允许触发，
        # 这样首个金叉也能拿到真实空头持续天数，不用哨兵值）
        below_run = 0
        for t in range(1, n):
            bull = e12[t] > e50[t]
            if bull:
                gap = below_run
                below_run = 0
                if t < 60 or gap < 1:
                    continue
            else:
                below_run += 1
                continue
            dsig = d[t]
            if c[t] <= 0 or e50[t] <= 0:
                continue
            # ---- 只用 t 及之前数据的描述性特征 ----
            s12 = (e12[t] / e12[t - 5] - 1) * 100 if e12[t - 5] > 0 else 0.0
            s50 = (e50[t] / e50[t - 10] - 1) * 100 if e50[t - 10] > 0 else 0.0
            c20 = (c[t] / c[t - 20] - 1) * 100 if c[t - 20] > 0 else 0.0
            c60 = (c[t] / c[t - 60] - 1) * 100 if c[t - 60] > 0 else 0.0
            v5 = statistics.mean(s['v'][t - 5:t]) if t >= 5 else 0
            vr = (s['v'][t] / v5) if v5 > 0 else 0.0
            rec = {
                'code': code, 'name': universe[code]['name'], 'ind': universe[code]['ind'],
                'mv': round(universe[code]['mv'], 1), 'date': dsig, 't': t,
                'close': round(c[t], 2), 'e12': round(e12[t], 2), 'e50': round(e50[t], 2),
                'spread': round((e12[t] / e50[t] - 1) * 100, 2),
                'dist12': round((c[t] / e12[t] - 1) * 100, 2),
                'gap': gap, 's12': round(s12, 2), 's50': round(s50, 2),
                'c20': round(c20, 1), 'c60': round(c60, 1), 'vr': round(vr, 2),
                'above': bool(c[t] > e12[t] > e50[t]),
                'fwd': {}, 'base': {}, 'exc': {}, 'mature': {},
            }
            # ---- 前向：t+1 开盘买入，t+h 收盘计（无未来函数） ----
            buy = o[t + 1] if t + 1 < n and o[t + 1] > 0 else None
            rec['buy'] = round(buy, 2) if buy else None
            for h in HORIZONS:
                j = t + h
                fwd = (c[j] / buy - 1) * 100 if (buy and j < n and c[j] > 0) else None
                bs = base_same(dsig, h)
                rec['fwd'][str(h)] = round(fwd, 2) if fwd is not None else None
                rec['base'][str(h)] = round(bs, 2) if bs is not None else None
                rec['exc'][str(h)] = round(fwd - bs, 2) if (fwd is not None and bs is not None) else None
                rec['mature'][str(h)] = bool(j < n)
            # ---- 事件对齐路径：描述性（收盘对收盘，非可成交价） ----
            # 31 个点，offset = -PATH_BEFORE .. +PATH_AFTER（信号日在 index PATH_BEFORE）
            # E[k] = 从 t-PATH_BEFORE 收盘起算的「个股累计收益 - 等权全成份累计收益」
            # path[j] = E[t+j] - E[t]  → 信号日处恰为 0，两侧为相对超额
            E = {}
            p0 = t - PATH_BEFORE
            if p0 >= 0:
                stock_cum, bench_cum = 1.0, 1.0
                E[p0] = 0.0
                for k in range(p0 + 1, min(n, t + PATH_AFTER + 1)):
                    if c[k - 1] > 0:
                        stock_cum *= c[k] / c[k - 1]
                    bench_cum *= (1 + bret.get(d[k], 0.0))
                    E[k] = (stock_cum - bench_cum) * 100
                z = E.get(t)
                rec['path'] = [round(E[t + j] - z, 2) if (z is not None and (t + j) in E)
                               else None
                               for j in range(-PATH_BEFORE, PATH_AFTER + 1)]
            else:
                rec['path'] = [None] * (PATH_BEFORE + PATH_AFTER + 1)
            signals.append(rec)

    signals.sort(key=lambda x: (x['date'], x['code']))
    last = calendar[-1]
    today = [s for s in signals if s['date'] == last]
    out = {
        'date': last,
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'universe': len(universe), 'covered': len(stocks),
        'cons_date': cons.get('date'),
        'bars': {'first': calendar[0], 'last': last, 'n': len(calendar)},
        'params': {'quiet_default': QUIET_DEFAULT, 'quiet_options': QUIET_OPTIONS,
                   'horizons': HORIZONS, 'path_before': PATH_BEFORE, 'path_after': PATH_AFTER},
        'base_any': bany,
        'n_signals': len(signals), 'n_today': len(today),
        'today_today_gap20': len([s for s in today if s['gap'] >= QUIET_DEFAULT]),
        'calendar': calendar,
        'signals': signals,
        'notes': [
            '股票域为当前沪深300成份股（%s 主档），历史信号存在成份股回溯偏差' % cons.get('date'),
            '本地日K仅 %d 根（%s 起），分年份样本不足，结论仅标「描述/候选」' % (len(calendar), calendar[0]),
            '信号日收盘确认、次日开盘成交；持有 h 个交易日后收盘计；同日全成份等权为基线',
        ],
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(out, open(OUT_JSON, 'w'), ensure_ascii=False, separators=(',', ':'))
    write_csv(signals)
    return out


def write_csv(signals):
    os.makedirs(OUT_DIR, exist_ok=True)
    cols = ['date', 'code', 'name', 'ind', 'gap', 'close', 'e12', 'e50', 'spread',
            'dist12', 's12', 's50', 'c20', 'c60', 'vr', 'mv', 'above', 'buy']
    for h in HORIZONS:
        cols += ['fwd%d' % h, 'base%d' % h, 'exc%d' % h]
    with open(OUT_CSV, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in signals:
            row = [s.get(k) for k in cols[:18]]
            for h in HORIZONS:
                row += [s['fwd'].get(str(h)), s['base'].get(str(h)), s['exc'].get(str(h))]
            w.writerow(row)


# ------------------------- 统计（报告与页面共用口径） -------------------------
def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def summarize(signals, quiet=QUIET_DEFAULT, hold=5, bany=None):
    """三关纪律汇总：基准对比 / 分年份 / 前后段 / 参数敏感性 / 分箱。"""
    sel = [s for s in signals if s['gap'] >= quiet]
    hk = str(hold)
    out = {'quiet': quiet, 'hold': hold, 'n': len(sel)}
    fw = [s['fwd'][hk] for s in sel]
    ex = [s['exc'][hk] for s in sel]
    ba = [s['base'][hk] for s in sel]
    out['fwd'] = round(mean(fw), 2) if mean(fw) is not None else None
    out['base_same'] = round(mean(ba), 2) if mean(ba) is not None else None
    out['excess'] = round(mean(ex), 2) if mean(ex) is not None else None
    out['win'] = round(100 * sum(1 for x in ex if x is not None and x > 0) /
                       max(1, len([x for x in ex if x is not None])), 1)
    out['mature'] = len([x for x in ex if x is not None])
    out['base_any'] = (bany or {}).get(hk)
    out['excess_vs_any'] = (round(out['fwd'] - out['base_any'], 2)
                            if out['fwd'] is not None and out['base_any'] is not None else None)
    # ③ 分年份 / 前后段
    byyear = {}
    for s in sel:
        y = s['date'][:4]
        byyear.setdefault(y, []).append(s['exc'][hk])
    out['by_year'] = [{'year': y, 'n': len(v),
                       'excess': round(mean(v), 2) if mean(v) is not None else None,
                       'win': round(100 * sum(1 for x in v if x is not None and x > 0) /
                                    max(1, len([x for x in v if x is not None])), 1)}
                      for y, v in sorted(byyear.items())]
    pos = [b for b in out['by_year'] if (b['excess'] or 0) > 0]
    out['pos_years'] = len(pos)
    tot = sum(b['excess'] for b in pos) if pos else 0
    top2 = sorted(pos, key=lambda b: -b['excess'])[:2]
    # 全期超额为负时该比率无意义 → None（前端显示「—」），不要用绝对值和硬凑出正数
    out['top2_share'] = (round(100 * sum(b['excess'] for b in top2) / tot, 1)
                         if tot > 0 else None)
    half = len(sel) // 2
    fh, sh = sel[:half], sel[half:]
    out['split'] = {
        'first': {'n': len(fh), 'excess': round(mean([s['exc'][hk] for s in fh]), 2)
                  if mean([s['exc'][hk] for s in fh]) is not None else None},
        'last': {'n': len(sh), 'excess': round(mean([s['exc'][hk] for s in sh]), 2)
                 if mean([s['exc'][hk] for s in sh]) is not None else None},
    }
    # 参数敏感性（冷静期平台状检查）
    out['sens'] = []
    for q in QUIET_OPTIONS:
        z = [s['exc'][hk] for s in signals if s['gap'] >= q]
        m = mean(z)
        out['sens'].append({'quiet': q, 'n': len(z), 'excess': round(m, 2) if m is not None else None,
                            'win': round(100 * sum(1 for x in z if x is not None and x > 0) /
                                         max(1, len([x for x in z if x is not None])), 1)})
    # 赔率分箱：按此前空头持续天数
    bins = [(1, 5), (6, 10), (11, 20), (21, 40), (41, 10000)]
    out['bins_gap'] = []
    for lo, hi in bins:
        z = [s['exc'][hk] for s in sel if lo <= s['gap'] <= hi]
        out['bins_gap'].append({'label': ('%d-%d' % (lo, hi)) if hi < 10000 else '%d+' % lo,
                                'n': len(z), 'excess': round(mean(z), 2) if mean(z) is not None else None,
                                'win': round(100 * sum(1 for x in z if x is not None and x > 0) /
                                             max(1, len([x for x in z if x is not None])), 1)})
    # 赔率分箱：按信号日 20 日动量
    edges = [-100, -8, -3, 0, 3, 8, 100]
    out['bins_c20'] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        z = [s['exc'][hk] for s in sel if lo <= s['c20'] < hi]
        out['bins_c20'].append({'label': '%d~%d' % (lo, hi), 'n': len(z),
                                'excess': round(mean(z), 2) if mean(z) is not None else None,
                                'win': round(100 * sum(1 for x in z if x is not None and x > 0) /
                                             max(1, len([x for x in z if x is not None])), 1)})
    return out


def write_report(data):
    """专题报告：当日清单 + 三关纪律结论（只标描述/候选）。"""
    os.makedirs(DAILY, exist_ok=True)
    ds = data['date']
    q = QUIET_DEFAULT
    today = [s for s in data['signals'] if s['date'] == ds and s['gap'] >= q]
    st = summarize(data['signals'], q, 5, data['base_any'])
    L = ['# 沪深300 · EXPMA 首次上穿（转势首叉）%s' % ds, '',
         '> 客观量化筛选，不构成投资建议。数据文件 `webapp/firstcross.json`；'
         '回测表 `webapp/firstcross/signals.csv`。', '',
         '## 口径', '',
         '- 股票域：沪深300 成份股（主档 %s，%d 只；K线覆盖 %d 只）' %
         (data['cons_date'], data['universe'], data['covered']),
         '- 信号：EMA12 由 <= EMA50 转为 > EMA50 的**当日**（首次满足买入条件 = 转势起点）；'
         '此前至少 %d 个交易日持续 EMA12<=EMA50（冷静期，页面可调）' % q,
         '- 成交：信号日收盘确认 → **次日开盘买入** → 持有 h 个交易日后收盘计；无未来函数',
         '- 基准：同日全成份等权、同持有期（base_same）；另给「同期任意日买入同持有期」全样本均值', '',
         '## 当日首次转势（冷静期≥%d）' % q, '']
    if today:
        L += ['| 代码 | 名称 | 行业 | 收盘 | EMA12 | EMA50 | 双线距离% | 空头持续(交易日) | 20日% | 收盘站上双线 |',
              '|---|---|---|---|---|---|---|---|---|---|']
        for s in today:
            L.append('| %s | %s | %s | %.2f | %.2f | %.2f | %+.2f | %d | %+.1f | %s |' %
                     (s['code'], s['name'], s['ind'], s['close'], s['e12'], s['e50'],
                      s['spread'], s['gap'], s['c20'], '是' if s['above'] else '否'))
    else:
        L.append('今日无满足冷静期的首次上穿信号。')
    L += ['', '## 全历史（描述/候选）', '',
          '- 全历史金叉事件 **%d** 次；冷静期≥%d 的样本 **%d**（已成熟 %d，5日口径）' %
          (data['n_signals'], q, st['n'], st['mature']),
          '- 5日：前向均值 **%+.2f%%**、同日基线 %+.2f%% → **超额 %+.2fpp**、胜率 %.1f%%' %
          (st['fwd'] or 0, st['base_same'] or 0, st['excess'] or 0, st['win']),
          '- 对比「同期任意日买入同持有期」基线 %+.2f%% → 超额 %+.2fpp' %
          (st['base_any'] or 0, st['excess_vs_any'] or 0), '',
          '### ③ 分年份稳健性', '',
          '| 年份 | 样本 | 5日超额% | 胜率% |', '|---|---|---|---|']
    for b in st['by_year']:
        L.append('| %s | %d | %s | %.1f |' % (b['year'], b['n'],
                 ('%+.2f' % b['excess']) if b['excess'] is not None else '—', b['win']))
    L += ['', '- 正超额年数 **%d/%d**；最强两年贡献占比 **%s**' %
          (st['pos_years'], len(st['by_year']),
           ('%.1f%%' % st['top2_share']) if st['top2_share'] is not None else '—（全期超额为负，比率无意义）'),
          '- 时间前后段（样本外）：前段 n=%d 超额 %s；后段 n=%d 超额 %s' %
          (st['split']['first']['n'], st['split']['first']['excess'],
           st['split']['last']['n'], st['split']['last']['excess']), '',
          '### 参数敏感性（冷静期是否平台状）', '',
          '| 冷静期 | 样本 | 5日超额% | 胜率% |', '|---|---|---|---|']
    for b in st['sens']:
        L.append('| %d | %d | %s | %.1f |' % (b['quiet'], b['n'],
                 ('%+.2f' % b['excess']) if b['excess'] is not None else '—', b['win']))
    L += ['', '### 赔率分箱（此前空头持续天数）', '',
          '| 空头天数 | 样本 | 5日超额% | 胜率% |', '|---|---|---|---|']
    for b in st['bins_gap']:
        L.append('| %s | %d | %s | %.1f |' % (b['label'], b['n'],
                 ('%+.2f' % b['excess']) if b['excess'] is not None else '—', b['win']))
    L += ['', '## 三关结论', '',
          '① **基准对比**：已给同日等权 + 任意日两条基线，不报裸收益。',
          '② **无未来函数**：收盘确认、次日开盘成交，信号特征只用 t 及之前数据。',
          '③ **分年份稳健性**：本地日K仅 %d 根（%s 起），年份样本 %d 个，' %
          (data['bars']['n'], data['bars']['first'], len(st['by_year'])) +
          '且股票域为当前成份股（历史回溯偏差）→ **未过第三关**。'
          '全部结论标 **描述/候选**，不作为「等待 XX 以上」之类的操作建议。', '',
          '> ' + '；'.join(data['notes']), '']
    p = os.path.join(DAILY, '转势首叉_%s.md' % ds)
    open(p, 'w').write('\n'.join(L))
    return p


if __name__ == '__main__':
    d = build()
    r = write_report(d)
    print(json.dumps({
        'date': d['date'], 'universe': d['universe'], 'covered': d['covered'],
        'n_signals': d['n_signals'], 'n_today': d['n_today'],
        'n_today_quiet20': d['today_today_gap20'],
        'today': [{'code': s['code'], 'name': s['name'], 'gap': s['gap']}
                  for s in d['signals'] if s['date'] == d['date']],
        'summary': summarize(d['signals'], QUIET_DEFAULT, 5, d['base_any']),
        'json': OUT_JSON, 'csv': OUT_CSV, 'report': r,
    }, ensure_ascii=False))
