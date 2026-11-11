#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线穿阳 · 稳健高胜率规则筛选（训练+样本外双达标）

目标不再是单纯最大化胜率，而是找「训练/样本外都保持高胜率且正期望」的规则。
输出: backtest/cross3_robust_result.json
"""
import json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cross3_touch_backtest import collect, simulate, pct, COST
from cross3_frontier import hold_stats, rule_stats

OUT = os.path.join(HERE, 'cross3_robust_result.json')


def main():
    rows = collect()
    dates = sorted(set(f['date'] for f in rows))
    cut = dates[int(len(dates) * .7)]
    tr_all = [f for f in rows if f['date'] < cut]
    te_all = [f for f in rows if f['date'] >= cut]
    print('切分日 %s | 训练 %d / 样本外 %d' % (cut, len(tr_all), len(te_all)))

    filters = {
        '全部信号': lambda f: True,
        'A级(破20高+MA89上行)': lambda f: f['brk20'] and f['ma89_up'] > 0,
        'A级+上证>MA20': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['idx_above'],
        'A级+上行regime': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['regime'] == 'up',
        'A级+粘合<=3%': lambda f: f['brk20'] and f['ma89_up'] > 0 and f['glue'] <= 3,
        'A级+非涨停+粘合<=3%': lambda f: f['brk20'] and f['ma89_up'] > 0
                                        and not f['limit_up'] and f['glue'] <= 3,
        'MA89强上行>2': lambda f: f['ma89_up'] > 2,
        '大盘>=200亿': lambda f: f['mv'] >= 200,
        '非涨停': lambda f: not f['limit_up'],
        '缩量vr<1': lambda f: 0 < f['vr'] < 1,
    }
    rules = []
    # 纯持有
    for N in (5, 10, 20):
        rules.append(('纯持有%d日' % N, None, None, N))
    # 止盈止损
    for X in (1, 2, 3, 5, 7):
        for Y in (3, 5, 8, 999):
            for N in (5, 10, 20):
                rules.append(('+%g%%止盈/-%s/%d日' % (X, Y if Y < 999 else '不止损', N), X, Y, N))

    cand = []
    for label, fn in filters.items():
        tr = [f for f in tr_all if fn(f)]
        te = [f for f in te_all if fn(f)]
        if len(tr) < 100 or len(te) < 50:
            continue
        for rname, X, Y, N in rules:
            if X is None:
                a, b = hold_stats(tr, N), hold_stats(te, N)
            else:
                a, b = rule_stats(tr, X, Y, N), rule_stats(te, X, Y, N)
            cand.append(dict(filter=label, rule=rname, train=a, test=b,
                             min_win=min(a['win'], b['win']),
                             min_mean=min(a['mean'], b['mean'])))
    print('候选数: %d' % len(cand))

    strong = [c for c in cand if c['min_win'] >= 55 and c['min_mean'] > 0]
    print('\n=== 严格稳健（训练&样本外 胜率均>=55%% 且 均值均>0）: %d 个 ===' % len(strong))
    for c in sorted(strong, key=lambda z: -z['min_win'])[:15]:
        print('  %-24s %-22s | 训练 %5.1f%% %+.2f%% | 样本外 %5.1f%% %+.2f%%' % (
            c['filter'], c['rule'], c['train']['win'], c['train']['mean'],
            c['test']['win'], c['test']['mean']))

    relax = [c for c in cand if c['min_win'] >= 50 and c['min_mean'] > 0]
    print('\n=== 宽松稳健（>=50%% 且 均值>0）: %d 个，按样本外均值 Top10 ===' % len(relax))
    for c in sorted(relax, key=lambda z: -z['test']['mean'])[:10]:
        print('  %-24s %-22s | 训练 %5.1f%% %+.2f%% | 样本外 %5.1f%% %+.2f%%' % (
            c['filter'], c['rule'], c['train']['win'], c['train']['mean'],
            c['test']['win'], c['test']['mean']))

    print('\n=== 90%% 俱乐部的样本外表现（全部候选里训练胜率>=90%% 的）===')
    club = [c for c in cand if c['train']['win'] >= 90]
    for c in sorted(club, key=lambda z: -z['test']['win'])[:10]:
        print('  %-24s %-22s | 训练 %5.1f%% %+.2f%% | 样本外 %5.1f%% %+.2f%%' % (
            c['filter'], c['rule'], c['train']['win'], c['train']['mean'],
            c['test']['win'], c['test']['mean']))

    json.dump(dict(cut=cut, strong=strong[:30], relax=relax[:30], club=club[:30]),
              open(OUT, 'w'), ensure_ascii=False, indent=2)
    print('\n结果已写: %s' % OUT)


if __name__ == '__main__':
    main()
