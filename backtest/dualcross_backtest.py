#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双穿阳线策略回测：价格一日内同时上穿 EXPMA12 与 EXPMA50 + 当日阳线。
变体消融 + regime 分层 + 基线对照。数据：本地K线缓存（前复权）。
入场假设：信号日收盘买入（另有次日开盘口径在v3验证过更差，此处用收盘口径）。
"""
import glob, json, os, statistics
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KB = os.path.join(_REPO, 'backtest/klines')
WIN_START = '2025-11-01'   # 近12个月
H = [1, 3, 5, 10]

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

print('loading...', flush=True)
S = {}
for p in glob.glob(KB + '/*.json'):
    code = os.path.basename(p)[:-5]
    k = json.load(open(p))
    if len(k) < 80:
        continue
    S[code] = dict(
        dates=[r[0] for r in k],
        o=[float(r[1]) for r in k], c=[float(r[2]) for r in k], v=[float(r[5]) for r in k],
        e12=ema([float(r[2]) for r in k], 12), e50=ema([float(r[2]) for r in k], 50))

# 等权小盘指数 + regime（20日动量）
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

def variants(s, t):
    """返回信号日命中的变体列表"""
    c, o, e12, e50 = s['c'], s['o'], s['e12'], s['e50']
    if t < 21 or t + 10 >= len(c) or s['dates'][t] < WIN_START:
        return []
    if not (c[t] > o[t]):                      # 阳线
        return []
    if not (c[t - 1] < e12[t - 1] and c[t - 1] < e50[t - 1]):   # 昨收在双线下方
        return []
    if not (c[t] > e12[t] and c[t] > e50[t]):  # 今收在双线上方（一日双穿）
        return []
    out = ['A 双穿+阳线（基础）']
    vr = vr5(s, t)
    if vr and vr >= 1.5: out.append('B A+量比>=1.5')
    if vr and vr >= 2.5: out.append('C A+量比>=2.5')
    if (c[t] / c[t - 1] - 1) >= 0.03: out.append('D A+涨幅>=3%')
    if e12[t] > e50[t]: out.append('E A+多头排列(e12>e50)')
    if c[t] > max(c[t - 20:t]): out.append('F A+破20日高')
    if vr and vr >= 1.5 and c[t] > max(c[t - 20:t]): out.append('G A+量比1.5+破20日高')
    return out

sig = {}   # variant -> h -> [(ret, regime)]

def add(var, h, r, g):
    sig.setdefault(var, {}).setdefault(h, []).append((r, g))

for s in S.values():
    c = s['c']
    for t in range(65, len(c) - 10):
        vs = variants(s, t)
        if not vs:
            continue
        for var in vs:
            for h in H:
                add(var, h, (c[t + h] / c[t] - 1) * 100, reg.get(s['dates'][t]))

# 基线
base = {h: [] for h in H}
base_reg = {'up': {h: [] for h in H}, 'down': {h: [] for h in H}}
for s in S.values():
    c = s['c']
    for t in range(65, len(c) - 10):
        d = s['dates'][t]
        if d < WIN_START:
            continue
        for h in H:
            r = (c[t + h] / c[t] - 1) * 100
            base[h].append((r, None))
            g = reg.get(d)
            if g:
                base_reg[g][h].append((r, g))

def fmt(rs):
    out = []
    for h in H:
        vals = [r for r, _ in rs[h]]
        if not vals:
            continue
        avg = statistics.mean(vals)
        win = 100 * sum(1 for v in vals if v > 0) / len(vals)
        out.append('h%d %+.2f%%/%.0f%%(n=%d)' % (h, avg, win, len(vals)))
    return ' | '.join(out)

print('\n窗口 %s ~ %s · universe %d 只\n' % (WIN_START, days[-1], len(S)))
print('基线(全部):', fmt(base))
for g in ('up', 'down'):
    print('基线[%s]:' % g, fmt(base_reg[g]))
print()
for var in sorted(sig):
    rs = sig[var]
    vals10 = [r for r, _ in rs[10]]
    b10 = statistics.mean([r for r, _ in base[10]])
    avg10 = statistics.mean(vals10)
    print('%-28s 超额10日 %+.2f%%' % (var, avg10 - b10))
    print('   ', fmt(rs))
    for g in ('up', 'down'):
        sub = {h: [(r, gg) for r, gg in rs[h] if gg == g] for h in H}
        if sub[1]:
            print('    [%s]' % g, fmt(sub))
