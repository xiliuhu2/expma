#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线穿阳 · 胜率/期望前沿搜索（回答：90% 胜率 + 正期望 是否同时可达）

在 cross3_touch_backtest 的信号集上：
  1) 纯持有（次日开盘买、N日收盘卖、扣成本）分过滤组合统计
  2) 止盈/止损参数网格 × 过滤组合，找 触达胜率>=90% 且 扣成本后均值>0 的组合
  3) 对候选做时间样本外（70/30）验证，检验高胜率是否稳定
输出: backtest/cross3_frontier_result.json
"""
import json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cross3_touch_backtest import collect, simulate, pct, COST, MAXN

OUT = os.path.join(HERE, 'cross3_frontier_result.json')


def hold_stats(sub, N):
    rets = [pct(f['path'][N - 1][2], f['buy']) - COST for f in sub]
    return dict(n=len(rets),
                win=round(sum(r > 0 for r in rets) / len(rets) * 100, 1),
                mean=round(statistics.mean(rets), 2),
                p05=round(sorted(rets)[int(len(rets) * .05)], 2))


def rule_stats(sub, X, Y, N):
    out = [simulate(f, X, Y, N) for f in sub]
    rets = [o[0] for o in out]
    tp = sum(o[1] == 'tp' for o in out)
    return dict(X=X, Y=Y, N=N, n=len(sub),
                win=round(tp / len(sub) * 100, 1),
                mean=round(statistics.mean(rets), 2),
                p05=round(sorted(rets)[int(len(rets) * .05)], 2),
                worst=round(min(rets), 2))


def main():
    rows = collect()
    print('信号数: %d (%s ~ %s) | 单笔成本 %.2f%%' % (
        len(rows), min(f['date'] for f in rows), max(f['date'] for f in rows), COST))

    filters = {
        '全部信号': lambda f: True,
        'A级(破20高+MA89上行)': lambda f: f['brk20'] and f['ma89_up'] > 0,
        'A级+上证>MA20': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['idx_above'],
        'A级+上行regime': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['regime'] == 'up',
        'A级+粘合<=3%': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['glue'] <= 3,
        'MA89强上行(>2)+距EMA12<3%': lambda f: f['ma89_up'] > 2 and f['dev12'] < 3,
        '缩量vr<1': lambda f: 0 < f['vr'] < 1,
        '倍量vr>=1.5': lambda f: f['vr'] >= 1.5,
        '平台窄plat<=10%': lambda f: f['plat'] <= 10,
        '大盘>=200亿': lambda f: f['mv'] >= 200,
        '小盘<=60亿': lambda f: 0 < f['mv'] <= 60,
        '非涨停': lambda f: not f['limit_up'],
        '深跌c60<=-20%': lambda f: f['c60'] <= -20,
        '20日涨幅<=0': lambda f: f['c20'] <= 0,
        '粘合<=3%+倍量+上证>MA20': lambda f: f['glue'] <= 3 and f['vr'] >= 1.5 and f['idx_above'],
        'A级+非涨停+粘合<=3%': lambda f: f['brk20'] and f['ma89_up'] > 0
                                        and not f['limit_up'] and f['glue'] <= 3,
    }
    print('\n=== 纯持有（次日开盘买、N日收盘卖、扣成本）===')
    holds = {}
    for label, fn in filters.items():
        sub = [f for f in rows if fn(f)]
        if len(sub) < 50:
            continue
        h5, h10, h20 = (hold_stats(sub, n) for n in (5, 10, 20))
        holds[label] = dict(h5=h5, h10=h10, h20=h20)
        print('  %-30s n=%4d | 5日 胜率%5.1f%% 均值%+.2f%% | 10日 %5.1f%% %+.2f%% | 20日 %5.1f%% %+.2f%%' % (
            label, h5['n'], h5['win'], h5['mean'], h10['win'], h10['mean'],
            h20['win'], h20['mean']))

    # 网格前沿
    grid = []
    for label, fn in filters.items():
        sub = [f for f in rows if fn(f)]
        if len(sub) < 100:
            continue
        for X in (0.5, 1, 2, 3, 5):
            for Y in (3, 5, 6, 8, 10, 999):
                for N in (5, 10, 20):
                    s = rule_stats(sub, X, Y, N)
                    s['filter'] = label
                    grid.append(s)
    print('\n网格组合数: %d' % len(grid))
    good = [s for s in grid if s['win'] >= 90 and s['mean'] > 0]
    print('=== 触达胜率>=90%% 且 扣成本后正期望 的组合: %d 个 ===' % len(good))
    for s in sorted(good, key=lambda z: -z['mean'])[:10]:
        print('  %s | +%s%%/-%s/%d日: 胜率 %.1f%% 均值 %+.2f%% n=%d' % (
            s['filter'], s['X'], s['Y'] if s['Y'] < 999 else '不止损', s['N'],
            s['win'], s['mean'], s['n']))

    print('\n=== 胜率>=85%% 的所有组合中，均值最高的 10 个 ===')
    hi = [s for s in grid if s['win'] >= 85]
    for s in sorted(hi, key=lambda z: -z['mean'])[:10]:
        print('  %s | +%s%%/-%s/%d日: 胜率 %.1f%% 均值 %+.2f%% P05 %+.2f%% n=%d' % (
            s['filter'], s['X'], s['Y'] if s['Y'] < 999 else '不止损', s['N'],
            s['win'], s['mean'], s['p05'], s['n']))

    print('\n=== 全局正期望最优（不限胜率）Top10 ===')
    for s in sorted(grid, key=lambda z: -z['mean'])[:10]:
        print('  %s | +%s%%/-%s/%d日: 均值 %+.2f%% 胜率 %.1f%% n=%d' % (
            s['filter'], s['X'], s['Y'] if s['Y'] < 999 else '不止损', s['N'],
            s['mean'], s['win'], s['n']))

    # 样本外：最高胜率组合 + 最优期望组合
    dates = sorted(set(f['date'] for f in rows))
    cut = dates[int(len(dates) * .7)]
    print('\n=== 时间样本外验证（切分日 %s）===' % cut)
    cands = []
    cands += sorted(hi, key=lambda z: -z['mean'])[:3]
    cands += sorted(grid, key=lambda z: -z['mean'])[:3]
    cands += [s for s in grid if s['filter'] == '全部信号' and s['X'] == 1 and s['Y'] == 999 and s['N'] == 20]
    seen, oos = set(), []
    for s in cands:
        key = (s['filter'], s['X'], s['Y'], s['N'])
        if key in seen:
            continue
        seen.add(key)
        sub = [f for f in rows if filters[s['filter']](f)]
        tr = [f for f in sub if f['date'] < cut]
        te = [f for f in sub if f['date'] >= cut]
        a = rule_stats(tr, s['X'], s['Y'], s['N']) if len(tr) >= 20 else None
        b = rule_stats(te, s['X'], s['Y'], s['N']) if len(te) >= 20 else None
        rec = dict(filter=s['filter'], X=s['X'], Y=s['Y'], N=s['N'], train=a, test=b)
        oos.append(rec)
        print('  %-30s +%s%%/-%s/%d日: 训练 %s / 样本外 %s' % (
            s['filter'], s['X'], s['Y'] if s['Y'] < 999 else '不止损', s['N'],
            ('%5.1f%% %+.2f%% n=%d' % (a['win'], a['mean'], a['n'])) if a else 'n/a',
            ('%5.1f%% %+.2f%% n=%d' % (b['win'], b['mean'], b['n'])) if b else 'n/a'))

    json.dump(dict(n=len(rows), cut=cut, holds=holds,
                   good=good, hi=hi[:30], oos=oos), open(OUT, 'w'),
              ensure_ascii=False, indent=2)
    print('\n结果已写: %s' % OUT)


if __name__ == '__main__':
    main()
