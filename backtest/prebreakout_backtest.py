#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爆发前埋伏策略 · 本地回测
数据：本地 ~/WorkBuddy/Claw/backtest/klines/*.json（全市场日K，约5000只；无市值过滤，加载即全市场）
信号日：2026-08-01 ~ 2026-11-02（收盘数据，剔除今日盘中bar）
评估：信号日收盘买入，持有 1/3/5/10 日的收益分布，对比 universe 全体同窗基线。
（2026-11-03 已用全市场 5009 只重跑；旧小盘结果备份于 backtest_result_smallcap.json）
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
        # [date, open, close, high, low, volume, ...]
        dates = [r[0] for r in k]
        opens = [float(r[1]) for r in k]
        closes = [float(r[2]) for r in k]
        vols = [float(r[5]) for r in k]
        out[code] = (dates, opens, closes, vols)
    return out

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

def fwd(closes, t, k):
    return (closes[t + k] / closes[t] - 1) * 100 if t + k < len(closes) else None

def signals(dates, opens, closes, vols, cfg):
    """返回信号日索引列表。"""
    if len(closes) < 120:
        return []
    e12, e50 = ema(closes, 12), ema(closes, 50)
    res = []
    for t in range(60, len(dates)):
        if dates[t] < WIN_START:
            continue
        c, e1, e2 = closes[t], e12[t], e50[t]
        if not (c > e1 > e2):
            continue
        dev = (c - e1) / e1 * 100
        if dev > cfg['dev']:
            continue
        spread = (e1 - e2) / e2 * 100
        if spread > cfg['spread']:
            continue
        if t < 60:
            continue
        c20 = (c / closes[t - 20] - 1) * 100
        if not (cfg['c20lo'] <= c20 <= cfg['c20hi']):
            continue
        c60 = (c / closes[t - 60] - 1) * 100
        if not (0 < c60 <= cfg['c60hi']):
            continue
        if cfg.get('vr'):
            if t < 5:
                continue
            pv = vols[t - 5:t]
            if min(pv) <= 0:
                continue
            vr = vols[t] / (sum(pv) / 5)
            if vr < cfg['vr']:
                continue
        if cfg.get('yang') and c <= opens[t]:
            continue
        if cfg.get('c5pos'):
            if t < 5 or closes[t] <= closes[t - 5]:
                continue
        # 排除最后一根bar是今天的盘中数据：信号只允许 t <= len-2（前一收盘）
        if t > len(dates) - 2:
            continue
        res.append(t)
    return res

BASE = dict(dev=6, spread=8, c20lo=5, c20hi=20, c60hi=20, vr=1.2)
VARIANTS = {
    'base(dev6,spr8,c20_5_20,c60<20,vr1.2)': BASE,
    'dev3 贴线更紧':            dict(BASE, dev=3),
    'dev10 放宽乖离':           dict(BASE, dev=10),
    'spread4 双线更粘合':       dict(BASE, spread=4),
    'vr1.5 放量更强':           dict(BASE, vr=1.5),
    'novr 不看量':              dict(BASE, vr=0),
    'c20_0_10 更早期':          dict(BASE, c20lo=0, c20hi=10),
    'c20_8_30 动量更强':        dict(BASE, c20lo=8, c20hi=30),
    'c60_35 放宽中低位':        dict(BASE, c60hi=35),
    'base+当日阳线':            dict(BASE, yang=True),
    'base+5日为正':             dict(BASE, c5pos=True),
    'dev3+spr4+vr1.5 极致粘合': dict(dev=3, spread=4, c20lo=5, c20hi=20, c60hi=20, vr=1.5),
}

def stats_row(sig_ret):
    r = {k: v for k, v in sig_ret.items() if v}
    row = {'n': len(r.get('h1', []))}
    for h in HORIZONS:
        key = 'h%d' % h
        vals = r.get(key, [])
        if vals:
            row['avg%d' % h] = round(statistics.mean(vals), 2)
            row['med%d' % h] = round(statistics.median(vals), 2)
            row['win%d' % h] = round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)
        else:
            row['avg%d' % h] = row['med%d' % h] = row['win%d' % h] = None
    return row

def main():
    data = load()
    print('stocks loaded: %d' % len(data), file=sys.stderr)

    # 基线：universe 全体、全部信号窗内交易日的前瞻收益
    base = {'h%d' % h: [] for h in HORIZONS}
    for dates, opens, closes, vols in data.values():
        for t in range(60, len(dates) - 1):
            if dates[t] < WIN_START:
                continue
            for h in HORIZONS:
                v = fwd(closes, t, h)
                if v is not None:
                    base['h%d' % h].append(v)
    bl = stats_row(base)
    print('基线(universe全体): n=%d | ' % bl['n'] +
          ' '.join('h%d avg=%+.2f%% med=%+.2f%% win=%.1f%%' %
                   (h, bl['avg%d' % h], bl['med%d' % h], bl['win%d' % h]) for h in HORIZONS))

    print()
    results = {}
    for name, cfg in VARIANTS.items():
        rets = {'h%d' % h: [] for h in HORIZONS}
        all_sig = []
        for code, (dates, opens, closes, vols) in data.items():
            sigs = signals(dates, opens, closes, vols, cfg)
            for t in sigs:
                all_sig.append((code, dates[t]))
                for h in HORIZONS:
                    v = fwd(closes, t, h)
                    if v is not None:
                        rets['h%d' % h].append(v)
        row = stats_row(rets)
        row['excess10'] = round(row['avg10'] - bl['avg10'], 2) if row['avg10'] is not None else None
        results[name] = row
        print('%-38s n=%-5d ' % (name, row['n']) +
              ' '.join('h%d avg=%+.2f%% win=%.0f%%' % (h, row['avg%d' % h] or 0, row['win%d' % h] or 0) for h in HORIZONS) +
              ('  excess10=%+.2f%%' % row['excess10'] if row['excess10'] is not None else ''))

    # 登康口腔在 base 条件下的信号日
    dk = data.get('sz001328')
    if dk:
        dates, opens, closes, vols = dk
        sigs = signals(dates, opens, closes, vols, BASE)
        print()
        print('登康口腔 base 信号日:', [dates[t] for t in sigs],
              '对应次日收益:', ['%+.2f%%' % ((closes[t+1]/closes[t]-1)*100) for t in sigs if t+1 < len(closes)])

    json.dump({'baseline': bl, 'variants': results},
              open(os.path.expanduser('~/WorkBuddy/Claw/backtest/backtest_result.json'), 'w'),
              ensure_ascii=False, indent=1)
    print('\nsaved backtest_result.json')

if __name__ == '__main__':
    main()
