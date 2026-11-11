#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""89金叉启动 策略回测（全市场）
复用 server.Data 的 e12/e50/ma89 计算，保证「筛选」与「回测」信号定义完全一致。
评估：信号日收盘买入，持有 1/3/5/10 日的均值、胜率，对比全市场基线（所有股票同窗平均）；
并按信号日全市场20日动量拆分 regime 上行/下行。
"""
import json, os, sys, time
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, 'webapp'))
import server as S

H = (1, 3, 5, 10)


def stats_by_h(rh):
    n = len(rh[1]) if rh else 0
    if n == 0:
        return {'n': 0, 'avg1': None, 'avg3': None, 'avg5': None, 'avg10': None,
                'win1': None, 'win3': None, 'win5': None, 'win10': None}
    out = {'n': n}
    for h in H:
        rs = rh[h]
        out['avg%d' % h] = round(100 * sum(rs) / n, 3)
        out['win%d' % h] = round(100 * sum(1 for x in rs if x > 0) / n, 1)
    return out


t0 = time.time()
D = S.Data()
print('data loaded in %.1fs, stocks=%d' % (time.time() - t0, len(D.k)), flush=True)

# 全市场基线 + regime 代理：逐日横截面日均收益（用于 regime 拆分）
mkt_sum, mkt_cnt = {}, {}
all_base = {h: [] for h in H}
for code, kk in D.k.items():
    c = kk['c']
    for t in range(1, len(c) - max(H)):
        if kk['v'][t] > 0 and c[t - 1] > 0:
            d = kk['dates'][t]
            mkt_sum[d] = mkt_sum.get(d, 0) + (c[t] / c[t - 1] - 1)
            mkt_cnt[d] = mkt_cnt.get(d, 0) + 1
dates = sorted(mkt_sum)
mret = {d: mkt_sum[d] / mkt_cnt[d] for d in dates}
reg = {}
prod = 1.0
for i, d in enumerate(dates):
    prod *= (1 + mret[d])
    if i >= 20:
        prod /= (1 + mret[dates[i - 20]])
    if i >= 19:
        reg[d] = 'up' if prod > 1 else 'down'

# 变体定义（base + 稳健性对照）
variants = {
    'base(flat6,c20_3_40,look20)': {},
    'fresh8(金叉≤8日)': {'lookback': 8},
    'fresh5(金叉≤5日)': {'lookback': 5},
    'fresh8_c20_3_15': {'lookback': 8, 'c20hi': 15},
    'flat4': {'flat': 4},
    'flat8': {'flat': 8},
    'c20_0_40': {'c20lo': 0},
    'vol1.2(金叉日放量)': {'vol': True},
}
sig = {name: {h: [] for h in H} for name in variants}
sig_reg = {'up': {h: [] for h in H}, 'down': {h: [] for h in H}}

for code, kk in D.k.items():
    c = kk['c']
    n = len(c)
    for t in range(95, n - max(H)):
        fwd = {h: c[t + h] / c[t] - 1 for h in H}
        for h in H:
            all_base[h].append(fwd[h])
        for name, kw in variants.items():
            if S.sig_m89(kk, t, **kw):
                for h in H:
                    sig[name][h].append(fwd[h])
                rg = reg.get(kk['dates'][t], 'down')
                for h in H:
                    sig_reg[rg][h].append(fwd[h])

baseline = stats_by_h(all_base)
result = {
    'baseline': baseline,
    'variants': {name: stats_by_h(sig[name]) for name in variants},
    'regime': {rg: stats_by_h(sig_reg[rg]) for rg in ('up', 'down')},
}
out = os.path.join(_REPO, 'backtest/backtest_m89_result.json')
json.dump(result, open(out, 'w'), ensure_ascii=False, indent=1)
print('done. 基线 10日均值=%.3f%% 胜率=%s%%' % (baseline['avg10'], baseline['win10']), flush=True)
for name in variants:
    v = result['variants'][name]
    ex = (v['avg10'] or 0) - (baseline['avg10'] or 0)
    print('  %-26s n=%-6d 10日均值=%+.3f%% 胜率=%s%% 超额=%+.2fpp'
          % (name, v['n'], v['avg10'], v['win10'], ex), flush=True)
