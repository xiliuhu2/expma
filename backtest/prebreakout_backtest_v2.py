#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爆发策略 v2 · 突破类信号族回测
对比 v1 潜伏类（全部跑输基线），测试突破类：
  A. 平台突破：创60日新高 + 放量(5日均量比>=vr) + 阳线 + 站上双线
  B. 涨停/大阳突破：单日涨幅>=x% + 放量 + 站上双线
  C. 回踩收回：昨日跌破EMA12、今日收回（双线多头保持中）+ 放量
  D. 平台紧凑+突破：前20日振幅小 + 创20日新高 + 放量
评估同 v1：信号收盘买，持有1/3/5/10日，对比 universe 基线。
数据：本地 klines/*.json（全市场约5000只，无市值过滤）。2026-11-03 已用全市场 5009 只重跑。
"""
import glob, json, os, statistics, sys

KB = os.path.expanduser('~/WorkBuddy/Claw/backtest/klines')
WIN_START = '2026-08-01'
HORIZONS = [1, 3, 5, 10]

def load():
    out = {}
    for p in glob.glob(os.path.join(KB, '*.json')):
        code = os.path.basename(p)[:-5]
        k = json.load(open(p))
        out[code] = ([r[0] for r in k],
                     [float(r[1]) for r in k],
                     [float(r[2]) for r in k],
                     [float(r[5]) for r in k])
    return out

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

def fwd(closes, t, k):
    return (closes[t + k] / closes[t] - 1) * 100 if t + k < len(closes) else None

def make_signal(kind, **p):
    def fn(dates, opens, closes, vols, e12, e50, t):
        n = len(closes)
        if t < 65 or t > n - 2: return False
        c, o, v = closes[t], opens[t], vols[t]
        e1, e2 = e12[t], e50[t]
        pv5 = sum(vols[t-5:t]) / 5
        pv20 = sum(vols[t-20:t]) / 20
        if pv5 <= 0 or pv20 <= 0: return False
        vr = v / pv5
        chg = (c / closes[t-1] - 1) * 100

        if kind == 'brk60':          # A: 创60日新高(不含当日) + 站上双线 + 放量
            if not (c > e1 > e2): return False
            if not (c > max(closes[t-60:t])): return False
            if chg <= 0: return False
            return vr >= p.get('vr', 2.0)
        if kind == 'bigyang':        # B: 大阳线 + 站上双线 + 放量
            if not (c > e1 > e2): return False
            if chg < p.get('chg', 7): return False
            return vr >= p.get('vr', 2.0)
        if kind == 'reclaim':        # C: 回踩收回：昨日<EMA12, 今日>EMA12, 双线多头
            if not (e1 > e2 and e12[t-1] > e50[t-1]): return False
            if not (closes[t-1] <= e12[t-1] and c > e12[t]): return False
            return vr >= p.get('vr', 1.5)
        if kind == 'tight_brk20':    # D: 前20日紧凑平台 + 创20日新高 + 放量
            w = closes[t-20:t]
            if (max(w) - min(w)) / min(w) * 100 > p.get('amp', 8): return False
            if not (c > max(w) and chg > 0): return False
            if not (c > e1 > e2): return False
            return vr >= p.get('vr', 2.0)
        return False
    return fn

VARIANTS = {
    'A 创60日新高+vr2.0':        make_signal('brk60', vr=2.0),
    'A 创60日新高+vr3.0':        make_signal('brk60', vr=3.0),
    'A 创60日新高+vr1.5':        make_signal('brk60', vr=1.5),
    'B 大阳7%+vr2.0':            make_signal('bigyang', chg=7, vr=2.0),
    'B 大阳5%+vr2.0':            make_signal('bigyang', chg=5, vr=2.0),
    'B 大阳7%+vr3.0':            make_signal('bigyang', chg=7, vr=3.0),
    'C 回踩收回EMA12+vr1.5':     make_signal('reclaim', vr=1.5),
    'C 回踩收回EMA12+vr1.2':     make_signal('reclaim', vr=1.2),
    'D 平台<8%+破20日高+vr2.0':  make_signal('tight_brk20', amp=8, vr=2.0),
    'D 平台<12%+破20日高+vr2.0': make_signal('tight_brk20', amp=12, vr=2.0),
}

def stats_row(rets):
    row = {'n': len(rets.get('h1', []))}
    for h in HORIZONS:
        vals = rets.get('h%d' % h, [])
        if vals:
            row['avg%d' % h] = round(statistics.mean(vals), 2)
            row['med%d' % h] = round(statistics.median(vals), 2)
            row['win%d' % h] = round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)
        else:
            row['avg%d' % h] = row['med%d' % h] = row['win%d' % h] = None
    return row

def main():
    data = load()
    print('stocks: %d' % len(data), file=sys.stderr)

    base = {'h%d' % h: [] for h in HORIZONS}
    for dates, opens, closes, vols in data.values():
        e12, e50 = ema(closes, 12), ema(closes, 50)
        for t in range(65, len(dates) - 1):
            if dates[t] < WIN_START: continue
            for h in HORIZONS:
                v = fwd(closes, t, h)
                if v is not None: base['h%d' % h].append(v)
    bl = stats_row(base)
    print('基线: n=%d | ' % bl['n'] +
          ' '.join('h%d avg=%+.2f%% win=%.1f%%' % (h, bl['avg%d' % h], bl['win%d' % h]) for h in HORIZONS))
    print()

    results = {'baseline': bl}
    for name, fn in VARIANTS.items():
        rets = {'h%d' % h: [] for h in HORIZONS}
        for dates, opens, closes, vols in data.values():
            e12, e50 = ema(closes, 12), ema(closes, 50)
            for t in range(65, len(dates) - 1):
                if dates[t] < WIN_START: continue
                if not fn(dates, opens, closes, vols, e12, e50, t): continue
                for h in HORIZONS:
                    v = fwd(closes, t, h)
                    if v is not None: rets['h%d' % h].append(v)
        row = stats_row(rets)
        row['excess10'] = round(row['avg10'] - bl['avg10'], 2) if row['avg10'] is not None else None
        results[name] = row
        print('%-26s n=%-6d ' % (name, row['n']) +
              ' '.join('h%d avg=%+.2f%% med=%+.2f%% win=%.0f%%' %
                       (h, row['avg%d' % h], row['med%d' % h], row['win%d' % h]) for h in HORIZONS) +
              ('  excess10=%+.2f%%' % row['excess10'] if row['excess10'] is not None else ''))

    # 登康在核心突破信号上的命中
    print()
    dates, opens, closes, vols = data['sz001328']
    e12, e50 = ema(closes, 12), ema(closes, 50)
    for name in ['A 创60日新高+vr2.0', 'B 大阳7%+vr2.0']:
        fn = VARIANTS[name]
        sigs = [t for t in range(65, len(dates) - 1)
                if dates[t] >= WIN_START and fn(dates, opens, closes, vols, e12, e50, t)]
        print('%s → 登康信号日: %s' % (name, [(dates[t], '%+.1f%%' % fwd(closes, t, 1)) for t in sigs]))

    json.dump(results, open(os.path.expanduser('~/WorkBuddy/Claw/backtest/backtest_v2_result.json'), 'w'),
              ensure_ascii=False, indent=1)
    print('\nsaved backtest_v2_result.json')

if __name__ == '__main__':
    main()
