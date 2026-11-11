#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
89日均线过滤 + 介入点优化回测
A) 策略④双穿阳线(+量比1.5+破20日高) 按 收盘价在89日均线上/下 分组，并对照基线分组（排除beta效应）
B) 介入点对比：信号日收盘 / 次日开盘 / 回踩EMA12当日收盘 / 回踩EMA12限价成交
"""
import glob, json, os, statistics

KB = os.path.expanduser('~/WorkBuddy/Claw/backtest/klines')
WIN_START = '2025-11-01'
H = [1, 3, 5, 10]

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

def sma(vals, n):
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i - n + 1):i + 1]
        out.append(sum(w) / len(w))
    return out

print('loading...', flush=True)
S = {}
for p in glob.glob(KB + '/*.json'):
    code = os.path.basename(p)[:-5]
    k = json.load(open(p))
    if len(k) < 100:
        continue
    c = [float(r[2]) for r in k]
    S[code] = dict(
        dates=[r[0] for r in k],
        o=[float(r[1]) for r in k], c=c, h=[float(r[3]) for r in k],
        l=[float(r[4]) for r in k], v=[float(r[5]) for r in k],
        e12=ema(c, 12), e50=ema(c, 50), ma89=sma(c, 89))

daily = {}
for s in S.values():
    for t in range(1, len(s['dates'])):
        if s['v'][t] > 0 and s['c'][t - 1] > 0:
            daily.setdefault(s['dates'][t], []).append(s['c'][t] / s['c'][t - 1] - 1)
days = sorted(daily)
rets = [statistics.mean(daily[d]) for d in days]
reg = {}
for i, d in enumerate(days):
    if i >= 20:
        m = 1.0
        for j in range(i - 19, i + 1):
            m *= 1 + rets[j]
        reg[d] = 'up' if m > 1 else 'down'

def vr5(s, t):
    if t < 5 or s['v'][t] <= 0:
        return None
    pv = s['v'][t - 5:t]
    if min(pv) <= 0:
        return None
    return s['v'][t] / (sum(pv) / 5)

def sig_dc(s, t):
    c, o, e1, e2 = s['c'], s['o'], s['e12'], s['e50']
    if t < 95 or t + 10 >= len(c) or s['dates'][t] < WIN_START:
        return False
    if not (c[t] > o[t]):
        return False
    if not (c[t - 1] < e1[t - 1] and c[t - 1] < e2[t - 1]):
        return False
    if not (c[t] > e1[t] and c[t] > e2[t]):
        return False
    vr = vr5(s, t)
    if vr is None or vr < 1.5:
        return False
    return c[t] > max(c[t - 20:t])

# ---------- A) 89日均线过滤 ----------
sig_above = {h: [] for h in H}
sig_below = {h: [] for h in H}
base_above = {h: [] for h in H}
base_below = {h: [] for h in H}
events = []   # (code, t) for Part B

for s in S.values():
    c = s['c']
    for t in range(95, len(c) - 10):
        d = s['dates'][t]
        if d < WIN_START:
            continue
        above = c[t] > s['ma89'][t]
        for h in H:
            r = (c[t + h] / c[t] - 1) * 100
            (base_above if above else base_below)[h].append(r)
        if sig_dc(s, t):
            events.append((id(s), t))
            for h in H:
                r = (c[t + h] / c[t] - 1) * 100
                (sig_above if above else sig_below)[h].append(r)

def fmt(dd):
    out = []
    for h in H:
        v = dd[h]
        if not v:
            continue
        out.append('h%d %+.2f%%/%.0f%%(n=%d)' % (h, statistics.mean(v), 100 * sum(1 for x in v if x > 0) / len(v), len(v)))
    return ' | '.join(out)

print('\n窗口 %s ~ %s · %d 只 · 信号事件 %d\n' % (WIN_START, days[-1], len(S), len(events)))
print('== A) 89日均线过滤 ==')
print('策略④信号 · MA89上方:', fmt(sig_above))
print('策略④信号 · MA89下方:', fmt(sig_below))
print('基线(全部日) · MA89上方:', fmt(base_above))
print('基线(全部日) · MA89下方:', fmt(base_below))

# 组合切面：MA89上方 × regime
combo = {('above', 'up'): {h: [] for h in H}, ('above', 'down'): {h: [] for h in H}}
base_combo = {('above', 'up'): {h: [] for h in H}, ('above', 'down'): {h: [] for h in H}}
for s in S.values():
    c = s['c']
    for t in range(95, len(c) - 10):
        d = s['dates'][t]
        if d < WIN_START:
            continue
        g = reg.get(d)
        if c[t] > s['ma89'][t] and g:
            for h in H:
                r = (c[t + h] / c[t] - 1) * 100
                base_combo[('above', g)][h].append(r)
            if sig_dc(s, t):
                for h in H:
                    combo[('above', g)][h].append((c[t + h] / c[t] - 1) * 100)

print('\n== A2) 组合：MA89上方 × regime ==')
for g in ('up', 'down'):
    print('信号④·MA89上方 [%s]:' % g, fmt(combo[('above', g)]))
    print('      基线·MA89上方 [%s]:' % g, fmt(base_combo[('above', g)]))

# ---------- B) 介入点 ----------
# 从 S 中找回 code: id(s) 映射
by_id = {id(s): (code, s) for code, s in S.items()}
entry = {'信号日收盘': {h: [] for h in H}, '次日开盘': {h: [] for h in H},
         '回踩EMA12收盘买': {h: [] for h in H}, '回踩EMA12限价买': {h: [] for h in H}}
missed = 0
for sid, t in events:
    code, s = by_id[sid]
    c, o, l, e12 = s['c'], s['o'], s['l'], s['e12']
    # 信号日收盘
    for h in H:
        if t + h < len(c):
            entry['信号日收盘'][h].append((c[t + h] / c[t] - 1) * 100)
    # 次日开盘
    if t + 1 < len(c) and o[t + 1] > 0:
        for h in H:
            if t + 1 + h < len(c):
                entry['次日开盘'][h].append((c[t + 1 + h] / o[t + 1] - 1) * 100)
    # 回踩（信号后3日内 触及EMA12）
    hit = None
    for j in range(t + 1, min(t + 4, len(c))):
        if l[j] <= e12[j]:
            hit = j
            break
    if hit is None:
        missed += 1
        continue
    # 收盘买入
    for h in H:
        if hit + h < len(c):
            entry['回踩EMA12收盘买'][h].append((c[hit + h] / c[hit] - 1) * 100)
    # 限价在EMA12成交（开盘低于EMA12按开盘价成交）
    fill = min(o[hit], e12[hit]) if o[hit] > 0 else e12[hit]
    for h in H:
        if hit + h < len(c):
            entry['回踩EMA12限价买'][h].append((c[hit + h] / fill - 1) * 100)

print('\n== B) 介入点（策略④信号，3日内回踩EMA12；未回踩 %.0f%%）==' % (100 * missed / len(events)))
for k in entry:
    print('%-12s:' % k, fmt(entry[k]))

# 上行/下行 regime 下回踩限价
print('\n回踩EMA12限价买 分regime:')
for g in ('up', 'down'):
    sub = {h: [] for h in H}
    for sid, t in events:
        code, s = by_id[sid]
        c, o, l, e12 = s['c'], s['o'], s['l'], s['e12']
        hit = None
        for j in range(t + 1, min(t + 4, len(c))):
            if l[j] <= e12[j]:
                hit = j
                break
        if hit is None or reg.get(s['dates'][t]) != g:
            continue
        fill = min(o[hit], e12[hit]) if o[hit] > 0 else e12[hit]
        for h in H:
            if hit + h < len(c):
                sub[h].append((c[hit + h] / fill - 1) * 100)
    print('  [%s]' % g, fmt(sub))
