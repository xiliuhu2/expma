#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
80% 俱乐部候选规则的稳健性验证（不重新搜索，只复核挖掘入选的 7 个代表组合）

维度：
  1) Wilson 95% 下界（训练/样本外真实胜率的保守估计）
  2) 成本敏感性 0.25 / 0.35 / 0.50
  3) 切分点敏感性 05-01 / 06-01 / 07-06 / 08-01
  4) 参数邻域：止盈 1.5~5% × 持有 10/15/20/30
  5) 逐月稳定性（训练+样本外全部交易的按月均值/胜率）
  6) 覆盖率调整（未触发按 0 计入，每信号平均收益）
输出: backtest/cross3_80win_verify.json
"""
import json, os, statistics, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'webapp'))
import cross3_entry_mining as EM
import cross3_80win_mining as M

OUT = os.path.join(HERE, 'cross3_80win_verify.json')

F = {
    'A级': lambda s: s['brk20'] and s['ma89_up'] > 0,
    'A级+非涨停': lambda s: s['brk20'] and s['ma89_up'] > 0 and not s['limit_up'],
    'A级+中小盘<=150亿': lambda s: s['brk20'] and s['ma89_up'] > 0 and 0 < s['mv'] <= 150,
}

CAND = [
    ('A 再放量破高/A级/+3%/20日（核心推荐）', '再放量破信号日高', 'A级', 3, 999, 20),
    ('B 再放量破高/A级/+4%/20日（期望最优）', '再放量破信号日高', 'A级', 4, 999, 20),
    ('C 再放量破高/A级/+2.5%/20日（胜率最优）', '再放量破信号日高', 'A级', 2.5, 999, 20),
    ('D 放量阳线过带/A级/+2.5%/15日', '放量阳线过带', 'A级', 2.5, 999, 15),
    ('E 连3日站稳MA89/A级/+3%/15日', '连3日站稳MA89', 'A级', 3, 999, 15),
    ('F 回踩带内守MA89/A级+非涨停/+2.5%/15日', '回踩带内守MA89', 'A级+非涨停', 2.5, 999, 15),
    ('G T+1追入/A级+中小盘/+3%/20日（对照）', 'T+1开盘(旧基线)', 'A级+中小盘<=150亿', 3, 999, 20),
]


def wilson(k, n, z=1.96):
    if not n:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    hh = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round((c - hh) * 100, 1)


def brief(rets):
    if not rets:
        return None
    vals = [r for r, _ in rets]
    n = len(vals)
    win = sum(v > 0 for v in vals)
    sr = sorted(vals)
    return dict(n=n, win=round(win / n * 100, 1), wilson_lo=wilson(win, n),
                mean=round(statistics.mean(vals), 2),
                p05=round(sr[int(n * .05)], 2), worst=round(sr[0], 2))


def run(sigs, trig, filt, X, Y, N, cost=0.25, cut=EM.CUT, monthly=False):
    EM.COST = cost
    ffn = F[filt]
    tr, oos, cov, trades_all = [], [], [], []
    traded = 0
    for s in sigs:
        if not ffn(s):
            continue
        bi = M.find_buy(s, trig)
        r = EM.simulate(s, bi, X, Y, N) if bi else None
        cov.append(r[0] if r else 0.0)
        if r:
            traded += 1
            (tr if s['date'] < cut else oos).append(r)
            trades_all.append((s['date'], r[0]))
    EM.COST = 0.25
    out = dict(brief(tr), ) if tr else None
    res = dict(train=out, oos=brief(oos),
               coverage=dict(n=len(cov), mean=round(statistics.mean(cov), 2),
                             traded=round(traded / len(cov) * 100, 1) if cov else 0))
    if monthly:
        mm = defaultdict(list)
        for d, r in trades_all:
            mm[d[:7]].append(r)
        res['monthly'] = {k: dict(n=len(v), mean=round(statistics.mean(v), 2),
                                   win=round(sum(x > 0 for x in v) / len(v) * 100, 1))
                          for k, v in sorted(mm.items())}
    return res


def main():
    EM.NEED = EM.W + 20 + 3
    D, sigs = EM.build_signals()
    print('复核样本 %d 个信号 | %s ~ %s\n' % (len(sigs), min(s['date'] for s in sigs),
                                            max(s['date'] for s in sigs)))

    print('=== 一、基准（0.25%%成本，切分 %s）===' % EM.CUT)
    base = {}
    for label, trig, filt, X, Y, N in CAND:
        b = run(sigs, trig, filt, X, Y, N, monthly=True)
        base[label] = b
        t, o = b['train'], b['oos']
        print('  %-38s 训练 n=%3d %5.1f%%(CI %4.1f)%+6.2f | 样本外 n=%3d %5.1f%%(CI %4.1f)%+6.2f P05 %+6.2f' % (
            label, t['n'], t['win'], t['wilson_lo'], t['mean'],
            o['n'], o['win'], o['wilson_lo'], o['mean'], o['p05']))

    print('\n=== 二、成本敏感性（样本外均值）===')
    cost_rows = {}
    for label, trig, filt, X, Y, N in CAND:
        vals = [run(sigs, trig, filt, X, Y, N, cost=c)['oos']['mean'] for c in (0.25, 0.35, 0.50)]
        cost_rows[label] = vals
        print('  %-38s %+7.2f %+7.2f %+7.2f' % (label, *vals))

    print('\n=== 三、切分点敏感性（样本外胜率/均值）===')
    cut_rows = {}
    for label, trig, filt, X, Y, N in CAND:
        cells = []
        for cut in ('2026-05-01', '2026-06-01', '2026-07-06', '2026-08-01'):
            o = run(sigs, trig, filt, X, Y, N, cut=cut)['oos']
            cells.append(o)
        cut_rows[label] = cells
        print('  %-38s %s' % (label, ' '.join(
            '%5.1f%%/%+.2f(n=%d)' % (c['win'], c['mean'], c['n']) if c else 'n/a' for c in cells)))

    print('\n=== 四、参数邻域（A/B/C 三个代表，样本外胜率/均值）===')
    neigh = {}
    for label, trig, filt, X, Y, N in CAND[:3]:
        rows = []
        for xx in (1.5, 2, 2.5, 3, 4, 5):
            cells = []
            for nn in (10, 15, 20, 30):
                o = run(sigs, trig, filt, xx, 999, nn)['oos']
                cells.append(dict(days=nn, **o) if o else None)
            rows.append(dict(x=xx, cells=cells))
        neigh[label] = rows
        print('  %s' % label)
        for row in rows:
            print('    +%.1f%%: %s' % (row['x'], '  '.join(
                '%d日 %5.1f%%/%+.2f' % (c['days'], c['win'], c['mean']) if c else '%d日 n/a' % 0
                for c in row['cells'])))

    print('\n=== 五、逐月稳定性（全部交易按信号月，均值/胜率）===')
    for label in list(base)[:6]:
        mm = base[label]['monthly']
        print('  %s' % label)
        print('    ' + ' | '.join('%s %+.2f%%/%.0f%%' % (k[2:], v['mean'], v['win'])
                                  for k, v in mm.items()))

    print('\n=== 六、覆盖率调整（符合过滤的全部信号，未触发按0计入）===')
    for label, trig, filt, X, Y, N in CAND:
        c = base[label]['coverage']
        print('  %-38s 信号 %4d 实际入场 %5.1f%% 每信号 %+6.2f%%' % (
            label, c['n'], c['traded'], c['mean']))

    json.dump(dict(n_signals=len(sigs), base=base, cost_sensitivity=cost_rows,
                   cut_sensitivity={k: v for k, v in zip(base, [cut_rows[k] for k in base])},
                   neighborhood=neigh),
              open(OUT, 'w'), ensure_ascii=False, indent=1)
    print('\n结果已写入 %s' % OUT)


if __name__ == '__main__':
    main()
