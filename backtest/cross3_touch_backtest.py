#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线穿阳 · 真实交易口径回测（盘中 high/low 触达止盈止损）

回答的问题：「胜率 90%」在什么口径下可达？代价是什么？

撮合假设（偏保守）:
  - 信号日 t 收盘后选股，t+1 开盘价买入（信号日收盘买不到）
  - 持有期内逐日检查 盘中最高/最低：先判止损（同日同时触及按先止损，保守）
  - 止盈 +X% 立即卖出；止损 -Y% 立即卖出；N 日到期按收盘价平仓
  - 成本 0.25%/笔（双边佣金+印花税+一档滑点近似）
  - 只用 t 及之前数据构造信号/特征；high/low 只用于结果判定（无未来函数）
输出: backtest/cross3_touch_result.json
用法: python3 cross3_touch_backtest.py
"""
import json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, '..', 'webapp')
sys.path.insert(0, WEB)
import server as S

OUT = os.path.join(HERE, 'cross3_touch_result.json')
COST = 0.25          # 每笔往返成本 %
MAXN = 20


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def collect():
    D = S.Data()
    # 上证>MA20（通达信文章强调的大盘过滤）
    ix = D.indices.get('sh000001') or {}
    ic = ix.get('c', [])
    idates = ix.get('dates', [])
    d2i = {d: i for i, d in enumerate(idates)}
    ma20 = {}
    for i in range(20, len(ic)):
        ma20[idates[i]] = sum(ic[i - 19:i + 1]) / 20

    out = []
    for code, kk in D.k.items():
        name = D.meta.get(code, {}).get('name', '')
        if S.is_st(name):
            continue
        c, o, h, l, v = kk['c'], kk['o'], kk['h'], kk['l'], kk['v']
        last = len(c) - 1
        if last < 95 + MAXN + 2:
            continue
        mv = D.meta.get(code, {}).get('mv', 0)
        for t in range(95, last):   # 需要 t+1 开盘买入，最后一根不能作为信号
            if S.cross_lines(kk, t) != 3:
                continue
            d = kk['dates'][t]
            e12, e50, m89 = kk['e12'][t], kk['e50'][t], kk['ma89'][t]
            hi20 = max(c[t - 20:t]) if t >= 20 else c[t]
            zone_lo, zone_hi = min(e12, e50, m89), max(e12, e50, m89)
            ii = d2i.get(d)
            f = dict(
                date=d, code=code, name=name, t=t,
                buy=o[t + 1], prev_close=c[t],
                brk20=c[t] > hi20,
                ma89_up=(m89 / kk['ma89'][t - 10] - 1) * 100 if kk['ma89'][t - 10] else 0,
                vr=S.vr_at(kk, t) or 0,
                glue=(zone_hi - zone_lo) / c[t] * 100 if c[t] else 99,
                dev12=pct(c[t], e12),
                body=pct(c[t], o[t]),
                c20=pct(c[t], c[t - 20]),
                c60=pct(c[t], c[t - 60]),
                plat=(max(c[t - 20:t]) / min(c[t - 20:t]) - 1) * 100 if t >= 20 and min(c[t - 20:t]) > 0 else 99,
                mv=mv,
                regime=D.regime.get(d, 'down'),
                idx_above=(ii is not None and ii >= 20 and ic[ii] > ma20.get(d, 1e9)),
                limit_up=pct(c[t], c[t - 1]) >= S.limit_ratio(code, name) * 100 - 0.6,
                one_word=(pct(c[t], c[t - 1]) >= S.limit_ratio(code, name) * 100 - 0.6
                          and (h[t] - l[t]) / c[t - 1] * 100 < 1.0),
                gap=pct(o[t + 1], c[t]),
            )
            f['path'] = [(h[t + j], l[t + j], c[t + j]) for j in range(1, MAXN + 1)
                         if t + j <= last]
            if len(f['path']) >= MAXN:
                out.append(f)
    return out


def simulate(f, X, Y, N):
    """A股 T+1：buy=o[t+1]（买入日），path[0] 即买入当日，不可卖出；
    止盈/止损从 path[1] 起判；时间退出仍是第 N 根 bar 收盘 path[N-1][2]。"""
    buy = f['buy']
    tp, sl = buy * (1 + X / 100), buy * (1 - Y / 100)
    for hi, lo, _ in f['path'][1:N]:
        if lo <= sl:
            return -Y - COST, 'stop'
        if hi >= tp:
            return X - COST, 'tp'
    last_close = f['path'][N - 1][2]
    return pct(last_close, buy) - COST, 'time'


def stats(rows, X, Y, N):
    rets, tags = [], []
    for f in rows:
        r, tag = simulate(f, X, Y, N)
        rets.append(r)
        tags.append(tag)
    n = len(rets)
    if not n:
        return None
    sr = sorted(rets)
    return dict(
        X=X, Y=Y, N=N, n=n,
        tp_rate=round(sum(t == 'tp' for t in tags) / n * 100, 1),
        stop_rate=round(sum(t == 'stop' for t in tags) / n * 100, 1),
        net_win=round(sum(r > 0 for r in rets) / n * 100, 1),
        mean=round(statistics.mean(rets), 2),
        median=round(statistics.median(rets), 2),
        p05=round(sr[int(n * 0.05)], 2),
        worst=round(sr[0], 2),
    )


def main():
    rows = collect()
    print('可评估信号数（含完整20日盘中路径）: %d | 日期 %s ~ %s' % (
        len(rows), min(f['date'] for f in rows), max(f['date'] for f in rows)))
    print('次日开盘相对信号收盘平均跳空: %+.2f%%' % (
        statistics.mean([f['gap'] for f in rows])))

    grid = []
    for X in (1, 2, 3, 5, 7):
        for Y in (2, 3, 5, 8, 999):
            for N in (3, 5, 10, 20):
                grid.append(stats(rows, X, Y, N))
    res = dict(n=len(rows), cost=COST, grid=grid)

    print('\n=== 全部信号 · 止盈/止损网格（tp_rate=触及止盈胜率, mean=扣成本后单笔均值）===')
    print('%-28s %6s %7s %8s %8s %7s %8s' % ('X%/Y%/N日', 'tp胜率', '止损率', '净胜率', '均值%', 'P05%', '最差%'))
    for r in grid:
        if r['X'] in (1, 3, 5) and r['Y'] in (3, 8, 999) and r['N'] in (5, 20):
            print('  +%d%% / -%s / %2d日 %8.1f%% %7.1f%% %8.1f%% %+8.2f %+7.2f %+8.2f' % (
                r['X'], r['Y'] if r['Y'] < 999 else '不止损', r['N'],
                r['tp_rate'], r['stop_rate'], r['net_win'], r['mean'], r['p05'], r['worst']))

    hi = [r for r in grid if r['tp_rate'] >= 90]
    print('\n=== 触及止盈胜率 >= 90%% 的参数组合 (%d 个) ===' % len(hi))
    for r in sorted(hi, key=lambda z: -z['mean'])[:12]:
        print('  +%d%%止盈/-%s止损/%d日: 触达胜率 %.1f%% | 净胜率 %.1f%% | 均值 %+.2f%% | P05 %+.2f%% | 最差 %+.2f%%' % (
            r['X'], r['Y'] if r['Y'] < 999 else '不止损', r['N'], r['tp_rate'], r['net_win'],
            r['mean'], r['p05'], r['worst']))
    if not hi:
        print('  无任何参数组合达到 90%%')

    print('\n=== 正期望最优（按扣成本后均值排序, 全部信号）===')
    for r in sorted(grid, key=lambda z: -z['mean'])[:8]:
        print('  +%d%%/-%s/%d日: 均值 %+.2f%% | 触达胜率 %.1f%% | 净胜率 %.1f%% | P05 %+.2f%%' % (
            r['X'], r['Y'] if r['Y'] < 999 else '不止损', r['N'], r['mean'],
            r['tp_rate'], r['net_win'], r['p05']))

    # 过滤组合（把第一轮挖出的稳健因子叠加，看能否在 90% 下还有意义样本）
    filters = {
        '全部信号': lambda f: True,
        'A级基线(破20高+MA89上行)': lambda f: f['brk20'] and f['ma89_up'] > 0,
        'A级+大盘>MA20': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['idx_above'],
        'A级+上行regime': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['regime'] == 'up',
        'A级+上证>MA20+粘合<=3%': lambda f: f['brk20'] and f['ma89_up'] > 0
                                          and f['idx_above'] and f['glue'] <= 3,
        'MA89强上行+距EMA12<3%': lambda f: f['ma89_up'] > 2 and f['dev12'] < 3,
        '通达信式(粘合<=3%+倍量+大盘>MA20)': lambda f: f['glue'] <= 3 and f['vr'] >= 1.5 and f['idx_above'],
        '深跌低位平台(c60<=-30%+粘合<=3%)': lambda f: f['c60'] <= -30 and f['glue'] <= 3,
    }
    print('\n=== 过滤组合 · 收盘5日严格胜率 vs 盘中触达胜率 ===')
    filt_out = {}
    for label, fn in filters.items():
        sub = [f for f in rows if fn(f)]
        if len(sub) < 5:
            print('  %-34s n=%3d 样本过少' % (label, len(sub)))
            continue
        strict = [pct(f['path'][4][2], f['buy']) > 0 for f in sub]
        s1 = [pct(f['path'][0][2], f['buy']) > 0 for f in sub]
        tp1 = [simulate(f, 1, 999, 20)[1] == 'tp' for f in sub]
        tp3 = [simulate(f, 3, 5, 20)[1] == 'tp' for f in sub]
        r = stats(sub, 1, 999, 20)   # 最容易高胜率的口径
        rec = dict(n=len(sub), strict5=round(sum(strict) / len(sub) * 100, 1),
                   d1=round(sum(s1) / len(sub) * 100, 1),
                   touch1_20d=round(sum(tp1) / len(sub) * 100, 1),
                   tp3_sl5_20d=round(sum(tp3) / len(sub) * 100, 1),
                   mean_touch1=round(r['mean'], 2), p05_touch1=round(r['p05'], 2))
        filt_out[label] = rec
        print('  %-34s n=%4d | 严格5日 %5.1f%% | 1日 %5.1f%% | 20日触及+1%% %5.1f%% | +3%%/-5%%止盈 %5.1f%% | 触达口径均值 %+.2f%% (P05 %+.2f%%)' % (
            label, rec['n'], rec['strict5'], rec['d1'], rec['touch1_20d'],
            rec['tp3_sl5_20d'], rec['mean_touch1'], rec['p05_touch1']))
    res['filters'] = filt_out
    json.dump(res, open(OUT, 'w'), ensure_ascii=False, indent=2)
    print('\n结果已写: %s' % OUT)


if __name__ == '__main__':
    main()
