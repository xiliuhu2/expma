#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「信号后买点」候选规则的稳健性验证（不另做搜索，只复核已选出的 6 个组合）

验证维度：
  1) 样本量置信度：样本外胜率的 Wilson 95% 下界
  2) 成本敏感性：0.25% / 0.35% / 0.50% 往返成本
  3) 切分点敏感性：2026-05-01 / 06-01 / 07-06 / 08-01 四个样本外起点
  4) 参数邻域：止盈 1 / 1.5 / 2 / 3 / 5%，持有 10 / 15 / 20 日
  5) 覆盖率调整：未触发买点的信号按「不交易 = 0 收益」计入，
     比较「只在该买点入场」与「全部信号 T+1 追入」的每信号平均收益

输出: backtest/cross3_entry_verify.json
"""
import json, os, statistics, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cross3_entry_mining as EM

OUT = os.path.join(HERE, 'cross3_entry_verify.json')

F = {
    'A级': lambda s: s['brk20'] and s['ma89_up'] > 0,
    'A级+非涨停': lambda s: s['brk20'] and s['ma89_up'] > 0 and not s['limit_up'],
}

CAND = [
    ('A  再放量破信号日高 / A级 / +3%/20日', '再放量破信号日高', 'A级', 3, 999, 20),
    ('B  再放量破信号日高 / A级 / +5%/20日', '再放量破信号日高', 'A级', 5, 999, 20),
    ('C  连3日站稳MA89 / A级 / +3%/20日', '连3日站稳MA89', 'A级', 3, 999, 20),
    ('D  再放量破信号日高 / A级 / +1%/20日', '再放量破信号日高', 'A级', 1, 999, 20),
    ('E  回踩带内守MA89 / A级+非涨停 / +1%/20日', '回踩带内守MA89', 'A级+非涨停', 1, 999, 20),
    ('F  T+1追入 / A级 / +3%/20日（上一轮交付，对照）', 'T+1开盘(旧基线)', 'A级', 3, 999, 20),
]


def wilson(k, n, z=1.96):
    if not n:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round((c - h) * 100, 1)


def brief(rets):
    if not rets:
        return None
    vals = [r[0] for r in rets]
    n = len(vals)
    win = sum(r[1] == 'tp' for r in rets)
    sr = sorted(vals)
    return dict(n=n, win=round(win / n * 100, 1), wilson_lo=wilson(win, n),
                mean=round(statistics.mean(vals), 2),
                p05=round(sr[int(n * .05)], 2), worst=round(sr[0], 2))


def run(sigs, trig, filt, X, Y, N, cost=0.25, cut=EM.CUT):
    EM.COST = cost
    ffn = F[filt]
    tr, oos, cov = [], [], []
    traded = 0
    for s in sigs:
        if not ffn(s):
            continue
        bi = EM.find_buy_index(s, trig)
        r = EM.simulate(s, bi, X, Y, N) if bi else None
        cov.append(r[0] if r else 0.0)
        if r:
            traded += 1
            (tr if s['date'] < cut else oos).append(r)
    return brief(tr), brief(oos), dict(n=len(cov),
                                        mean=round(statistics.mean(cov), 2),
                                        traded=round(traded / len(cov) * 100, 1) if cov else 0)


def main():
    D, sigs = EM.build_signals()
    print('复核样本: %d 个三线穿阳信号 | %s ~ %s\n' % (
        len(sigs), min(s['date'] for s in sigs), max(s['date'] for s in sigs)))

    print('=== 一、基准复核（成本 0.25%%，切分 %s）===' % EM.CUT)
    print('%-52s %-28s %-28s' % ('组合', '训练段', '样本外'))
    base = {}
    for label, trig, filt, X, Y, N in CAND:
        tr, oos, cov = run(sigs, trig, filt, X, Y, N)
        base[label] = (tr, oos, cov)
        print('  %-50s n=%3d %5.1f%% (CI下界%4.1f) %+5.2f | n=%3d %5.1f%% (CI下界%4.1f) %+5.2f | P05 %+6.2f 最差 %+7.2f' % (
            label, tr['n'], tr['win'], tr['wilson_lo'], tr['mean'],
            oos['n'], oos['win'], oos['wilson_lo'], oos['mean'],
            oos['p05'], oos['worst']))

    print('\n=== 二、成本敏感性（样本外单笔均值 %）===')
    print('%-52s %10s %10s %10s' % ('组合', '0.25%', '0.35%', '0.50%'))
    cost_rows = {}
    for label, trig, filt, X, Y, N in CAND:
        vals = []
        for c in (0.25, 0.35, 0.50):
            tr, oos, cov = run(sigs, trig, filt, X, Y, N, cost=c)
            vals.append(oos['mean'])
        cost_rows[label] = vals
        print('  %-50s %+10.2f %+10.2f %+10.2f' % (label, *vals))

    print('\n=== 三、切分点敏感性（样本外胜率 / 均值）===')
    print('%-52s %16s %16s %16s %16s' % ('组合', '05-01起', '06-01起', '07-06起', '08-01起'))
    cut_rows = {}
    for label, trig, filt, X, Y, N in CAND:
        cells = []
        for cut in ('2026-05-01', '2026-06-01', '2026-07-06', '2026-08-01'):
            tr, oos, cov = run(sigs, trig, filt, X, Y, N, cut=cut)
            cells.append(oos)
        cut_rows[label] = cells
        print('  %-50s %s' % (label, ' '.join(
            '%5.1f%%/%+.2f(n=%d)' % (c['win'], c['mean'], c['n']) if c else 'n/a' for c in cells)))

    print('\n=== 四、参数邻域：止盈幅度（A / D 两个代表，持有20日，样本外）===')
    neigh = {}
    for label, trig, filt, X, Y, N in (CAND[0], CAND[3], CAND[5]):
        cells = []
        for xx in (1, 1.5, 2, 3, 5):
            tr, oos, cov = run(sigs, trig, filt, xx, 999, 20)
            cells.append((xx, oos))
        neigh[label] = [dict(x=x, **(o or {})) for x, o in cells]
        print('  %-50s %s' % (label, '  '.join(
            '+%.1f%%: %5.1f%%/%+.2f' % (x, o['win'], o['mean']) if o else 'n/a' for x, o in cells)))

    print('\n=== 五、持有期邻域（止盈 +3%%，样本外）===')
    hold_rows = {}
    for label, trig, filt, X, Y, N in (CAND[0], CAND[5]):
        cells = []
        for nn in (10, 15, 20):
            tr, oos, cov = run(sigs, trig, filt, 3, 999, nn)
            cells.append((nn, oos))
        hold_rows[label] = [dict(days=n, **(o or {})) for n, o in cells]
        print('  %-50s %s' % (label, '  '.join(
            '%d日: %5.1f%%/%+.2f' % (n, o['win'], o['mean']) if o else 'n/a' for n, o in cells)))

    print('\n=== 六、覆盖率调整：符合过滤的全部信号，未触发买点按「不交易=0」计入 ===')
    print('%-52s %8s %10s %14s' % ('组合', '信号数', '实际入场%', '每信号均值%'))
    cov_rows = {}
    for label, trig, filt, X, Y, N in CAND:
        tr, oos, cov = base[label]
        cov_rows[label] = cov
        print('  %-50s %8d %9.1f%% %+13.2f' % (label, cov['n'], cov['traded'], cov['mean']))

    EM.COST = 0.25
    res = dict(n_signals=len(sigs),
              base={k: dict(train=v[0], oos=v[1], coverage=v[2]) for k, v in base.items()},
              cost_sensitivity=cost_rows, cut_sensitivity={
                  k: [dict(cut=c, **(o or {})) for c, o in zip(
                      ('2026-05-01', '2026-06-01', '2026-07-06', '2026-08-01'), v)]
                  for k, v in cut_rows.items()},
              tp_neighborhood=neigh, hold_neighborhood=hold_rows,
              coverage=cov_rows)
    json.dump(res, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print('\n结果已写入 %s' % OUT)


if __name__ == '__main__':
    main()
