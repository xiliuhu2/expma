#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基线按 regime 分层（最后一块拼图）"""
import glob, json, os, statistics

KB = os.path.expanduser('~/WorkBuddy/Claw/backtest/klines')
WIN_START = '2026-08-01'
idx = json.load(open(os.path.expanduser('~/WorkBuddy/Claw/backtest/idx_daily.json')))['idx_daily']
days = sorted(idx)
reg = {}
for i, d in enumerate(days):
    if i >= 20:
        m = 1.0
        for j in range(i - 19, i + 1):
            m *= 1 + idx[days[j]] / 100
        reg[d] = 'up' if m > 1 else 'down'

H = [1, 3, 5, 10]
base = {'up': {h: [] for h in H}, 'down': {h: [] for h in H}}
for p in glob.glob(os.path.join(KB, '*.json')):
    k = json.load(open(p))
    dates = [r[0] for r in k]
    closes = [float(r[2]) for r in k]
    for t in range(65, len(dates) - 1):
        r = reg.get(dates[t])
        if dates[t] < WIN_START or r is None:
            continue
        for h in H:
            if t + h < len(closes):
                base[r][h].append((closes[t + h] / closes[t] - 1) * 100)

for r in ('up', 'down'):
    print('基线 [%s]:' % r, ' '.join('h%d avg=%+.2f%% win=%.1f%%' % (
        h, statistics.mean(base[r][h]),
        100 * sum(1 for v in base[r][h] if v > 0) / len(base[r][h])) for h in H))
