#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指数级信号回测（上证/科创50/创业板指/北证50，2023-07 ~ 今）：
信号A「站上EXPMA12」：收盘从EMA12下方上穿到上方 → 指数未来1/3/5/10日收益
信号B「死叉3日不收复」：EMA12跌破EMA50，且随后3日内收盘未站回双线上方 → 未来收益
状态「EMA12上/下方」：指数全样本分状态前瞻收益（可操作窗口检验）
门控检验：以上证「EMA12上/下」状态门控小盘股策略④信号，对比收益差
"""
import glob, json, os, statistics

IDX_DIR = os.path.expanduser('~/WorkBuddy/Claw/backtest/indices')
KB = os.path.expanduser('~/WorkBuddy/Claw/backtest/klines')
H = [1, 3, 5, 10]
NAMES = {'sh000001': '上证指数', 'sh000688': '科创50', 'sz399006': '创业板指', 'bj899050': '北证50'}

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

IDX = {}
for p in glob.glob(IDX_DIR + '/*.json'):
    code = os.path.basename(p)[:-5]
    k = json.load(open(p))
    c = [r[2] for r in k]
    IDX[code] = dict(name=NAMES.get(code, code), dates=[r[0] for r in k],
                     o=[r[1] for r in k], c=c, e12=ema(c, 12), e50=ema(c, 50))

def fmt(dd):
    out = []
    for h in H:
        v = dd.get(h, [])
        if not v:
            continue
        out.append('h%d %+.2f%%/%.0f%%(n=%d)' % (h, statistics.mean(v), 100 * sum(1 for x in v if x > 0) / len(v), len(v)))
    return ' | '.join(out)

print('== 信号A：收盘站上EXPMA12（从下方穿越），指数前瞻收益 ==')
for code, s in IDX.items():
    ev = {h: [] for h in H}
    for t in range(1, len(s['c']) - 10):
        if s['c'][t - 1] < s['e12'][t - 1] and s['c'][t] > s['e12'][t]:
            for h in H:
                ev[h].append((s['c'][t + h] / s['c'][t] - 1) * 100)
    print('%s(%d次):' % (s['name'], len(ev[1])), fmt(ev))
    # 最近一次
    last = None
    for t in range(1, len(s['c']) - 1):
        if s['c'][t - 1] < s['e12'][t - 1] and s['c'][t] > s['e12'][t]:
            last = s['dates'][t]
    if last:
        print('    最近一次站上: %s' % last)

print('\n== 信号B：EMA12跌破EMA50（死叉）且3日内未收复双线，死叉日起前瞻收益 ==')
for code, s in IDX.items():
    ev = {h: [] for h in H}
    recovered = 0
    for t in range(1, len(s['c']) - 13):
        if s['e12'][t - 1] >= s['e50'][t - 1] and s['e12'][t] < s['e50'][t]:
            ok = any(s['c'][j] > s['e12'][j] and s['c'][j] > s['e50'][j] for j in range(t + 1, t + 4))
            if ok:
                recovered += 1
                continue
            base = s['c'][t + 3]   # 确认日的收盘
            for h in H:
                if t + 3 + h < len(s['c']):
                    ev[h].append((s['c'][t + 3 + h] / base - 1) * 100)
    print('%s(死叉%d次,3日内收复%d次):' % (s['name'], len(ev[1]) + recovered, recovered), fmt(ev))

print('\n== 状态：指数收盘在EMA12 上方/下方 的前瞻收益（全样本） ==')
for code, s in IDX.items():
    up = {h: [] for h in H}
    dn = {h: [] for h in H}
    for t in range(15, len(s['c']) - 10):
        tgt = up if s['c'][t] > s['e12'][t] else dn
        for h in H:
            tgt[h].append((s['c'][t + h] / s['c'][t] - 1) * 100)
    print('%s 上方:' % s['name'], fmt(up))
    print('%s 下方:' % s['name'], fmt(dn))

print('\n== 门控检验：上证「EMA12上/下」 × 小盘股策略④信号（963事件） ==')
sh = IDX['sh000001']
state = dict(zip(sh['dates'], ['above' if s['c'][t] > s['e12'][t] else 'below' for t in range(len(s['c']))]))
gate = {'above': {h: [] for h in H}, 'below': {h: [] for h in H}}
for p in glob.glob(KB + '/*.json'):
    k = json.load(open(p))
    if len(k) < 100:
        continue
    c = [float(r[2]) for r in k]
    o = [float(r[1]) for r in k]
    v = [float(r[5]) for r in k]
    e12, e50 = ema(c, 12), ema(c, 50)
    for t in range(95, len(c) - 10):
        if not (c[t] > o[t]):
            continue
        if not (c[t - 1] < e12[t - 1] and c[t - 1] < e50[t - 1] and c[t] > e12[t] and c[t] > e50[t]):
            continue
        if t < 5 or v[t] <= 0 or min(v[t - 5:t]) <= 0 or v[t] / (sum(v[t - 5:t]) / 5) < 1.5:
            continue
        if c[t] <= max(c[t - 20:t]):
            continue
        g = state.get(k[t][0])
        if not g:
            continue
        for h in H:
            gate[g][h].append((c[t + h] / c[t] - 1) * 100)
print('上证EMA12上方(可操作):', fmt(gate['above']))
print('上证EMA12下方(应回避):', fmt(gate['below']))
