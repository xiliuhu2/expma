#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爆发策略 v3 · regime 分层 + 次日开盘入场
1. 用 universe 构建等权小盘指数，按指数 20 日动量分 上行/下行 regime
2. 核心信号在两种 regime 下分别统计
3. 大阳线信号的"次日开盘入场"变体（打板者真实买点）
"""
import glob, json, os, statistics
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KB = os.path.join(_REPO, 'backtest/klines')
WIN_START = '2026-08-01'
HORIZONS = [1, 3, 5, 10]

def load():
    out = {}
    for p in glob.glob(os.path.join(KB, '*.json')):
        code = os.path.basename(p)[:-5]
        k = json.load(open(p))
        out[code] = ([r[0] for r in k], [float(r[1]) for r in k],
                     [float(r[2]) for r in k], [float(r[5]) for r in k])
    return out

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

def fwd(closes, t, k):
    return (closes[t + k] / closes[t] - 1) * 100 if t + k < len(closes) else None

# ---- 等权小盘指数日收益 ----
def build_index(data):
    daily = {}
    for code, (dates, opens, closes, vols) in data.items():
        for t in range(1, len(dates)):
            if closes[t - 1] <= 0 or vols[t] <= 0:  # 停牌日跳过
                continue
            daily.setdefault(dates[t], []).append(closes[t] / closes[t - 1] - 1)
    days = sorted(daily)
    rets = {d: statistics.mean(daily[d]) for d in days}
    return rets  # date -> 平均日收益

def regime_states(idx_rets, mom=20):
    """返回 date -> 'up'/'down'（指数近20日累计动量>0为上行）"""
    days = sorted(idx_rets)
    cum = {}
    for i, d in enumerate(days):
        if i < mom:
            cum[d] = None
            continue
        m = 1.0
        for j in range(i - mom + 1, i + 1):
            m *= 1 + idx_rets[days[j]]
        cum[d] = 'up' if m > 1 else 'down'
    return cum

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

def show(name, row, bl):
    print('%-30s n=%-6d ' % (name, row['n']) +
          ' '.join('h%d avg=%+.2f%% win=%.0f%%' % (h, row['avg%d' % h] or 0, row['win%d' % h] or 0) for h in HORIZONS))

def main():
    data = load()
    idx_rets = build_index(data)
    reg = regime_states(idx_rets)

    # 指数概览
    days = sorted(idx_rets)
    bt_days = [d for d in days if d >= WIN_START]
    print('等权小盘指数 %s ~ %s: 累计 %+.2f%%' % (bt_days[0], bt_days[-1],
          (100 * (lambda m: m)(__import__('math').prod(1 + idx_rets[d] for d in bt_days) - 1))))
    up_days = sum(1 for d in bt_days if reg[d] == 'up')
    print('regime: 上行 %d 天 / 下行 %d 天' % (up_days, len(bt_days) - up_days))
    print()

    # 信号定义（v2 中相对最好的三个 + 基准）
    def sig_brk60(dates, opens, closes, vols, e12, e50, t):
        if t < 65: return False
        c = closes[t]
        if not (c > e12[t] > e50[t]): return False
        if c <= max(closes[t-60:t]): return False
        if (c / closes[t-1] - 1) * 100 <= 0: return False
        return vols[t] / (sum(vols[t-5:t]) / 5) >= 2.0

    def sig_bigyang(dates, opens, closes, vols, e12, e50, t):
        if t < 65: return False
        if not (closes[t] > e12[t] > e50[t]): return False
        if (closes[t] / closes[t-1] - 1) * 100 < 7: return False
        return vols[t] / (sum(vols[t-5:t]) / 5) >= 2.0

    def sig_reclaim(dates, opens, closes, vols, e12, e50, t):
        if t < 65: return False
        if not (e12[t] > e50[t] and e12[t-1] > e50[t-1]): return False
        if not (closes[t-1] <= e12[t-1] and closes[t] > e12[t]): return False
        return vols[t] / (sum(vols[t-5:t]) / 5) >= 1.5

    SIGS = {'创60日新高+vr2': sig_brk60, '大阳7%+vr2': sig_bigyang, '回踩收回+vr1.5': sig_reclaim}

    # 按 regime 分层统计
    for name, fn in SIGS.items():
        rets = {'up': {'h%d' % h: [] for h in HORIZONS},
                'down': {'h%d' % h: [] for h in HORIZONS}}
        for dates, opens, closes, vols in data.values():
            e12, e50 = ema(closes, 12), ema(closes, 50)
            for t in range(65, len(dates) - 1):
                if dates[t] < WIN_START: continue
                r = reg.get(dates[t])
                if r is None or not fn(dates, opens, closes, vols, e12, e50, t): continue
                for h in HORIZONS:
                    v = fwd(closes, t, h)
                    if v is not None: rets[r]['h%d' % h].append(v)
        print('== %s' % name)
        for r in ('up', 'down'):
            show('  [%s]' % r, stats_row(rets[r]), None)
        print()

    # 次日开盘入场：大阳线 T 日收盘确认，T+1 开盘买，持有到 T+1+k 收盘
    print('== 次日开盘入场（大阳7%+vr2）')
    rets_nx = {'up': {'h0': [], 'h2': [], 'h4': [], 'h9': []},
               'down': {'h0': [], 'h2': [], 'h4': [], 'h9': []}}
    for dates, opens, closes, vols in data.values():
        if len(closes) < 130: continue
        e12, e50 = ema(closes, 12), ema(closes, 50)
        for t in range(65, len(dates) - 2):
            if dates[t] < WIN_START: continue
            if not sig_bigyang(dates, opens, closes, vols, e12, e50, t): continue
            r = reg.get(dates[t])
            if r is None: continue
            entry = opens[t + 1]
            if entry <= 0: continue
            for k, key in ((0, 'h0'), (2, 'h2'), (4, 'h4'), (9, 'h9')):
                if t + 1 + k < len(closes):
                    rets_nx[r][key].append((closes[t + 1 + k] / entry - 1) * 100)
    for r in ('up', 'down'):
        for key in ('h0', 'h2', 'h4', 'h9'):
            vals = rets_nx[r][key]
            if vals:
                print('  [%s] %s: n=%d avg=%+.2f%% med=%+.2f%% win=%.1f%%' % (
                    r, key, len(vals), statistics.mean(vals), statistics.median(vals),
                    100 * sum(1 for v in vals if v > 0) / len(vals)))

    json.dump({'idx_daily': {d: round(idx_rets[d] * 100, 3) for d in days}},
              open(os.path.join(_REPO, 'backtest/idx_daily.json'), 'w'))
    print('\nsaved idx_daily.json')

if __name__ == '__main__':
    main()
