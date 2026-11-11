#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXPMA12/EXPMA50/MA89 体系 · 胜率>=80% 规则挖掘（目标：80%俱乐部）

框架沿用 cross3_entry_mining（信号=穿阳状态标记 → 20日内买点 → 触发次日开盘买入），
扩展为：10 买点触发器 × 8 过滤 × 36 退出规则 = 2880 组合，
按「训练/样本外双段 胜率(收益>0)>=80% 且均值>0 且样本量达标」筛出 80% 俱乐部。

胜率口径：收益>0 即算赢（含时间退出正收益），比纯触达口径更严格诚实。
输出: backtest/cross3_80win_result.json
"""
import json, os, statistics, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, '..', 'webapp')
sys.path.insert(0, WEB)
sys.path.insert(0, HERE)
import server as S
import cross3_entry_mining as EM

OUT = os.path.join(HERE, 'cross3_80win_result.json')

W = EM.W                      # 20
CUT = EM.CUT                  # 2026-07-06

# ---- 买点触发器：旧4个最优 + 基线 + 4个新复合触发 ----
OLD_TRIGGERS = ['T+1开盘(旧基线)', '再放量破信号日高', '连3日站稳MA89', '回踩带内守MA89']
NEW_TRIGGERS = ['回踩带内后破高', '回踩带内后放量破高', '回踩EMA12后放量破高', '放量阳线过带']
TRIGGERS = OLD_TRIGGERS + NEW_TRIGGERS

# ---- 退出规则网格 ----
EXITS = []
for X in (1.5, 2, 2.5, 3, 4, 5):
    for Y in (999, 8):
        for N in (15, 20, 30):
            EXITS.append(('+%g%%/%s/%d日' % (X, '不止损' if Y == 999 else '-%d%%' % Y, N), X, Y, N))

MIN_TRAIN, MIN_OOS = 60, 30


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def find_buy(s, tm):
    """本地版：旧触发器委托 EM.find_buy_index；新复合触发自己实现。返回买入bar索引。"""
    if tm in OLD_TRIGGERS:
        return EM.find_buy_index(s, tm)
    kk = s['kk']
    t0, last = s['t0'], s['last']
    c, o, h, l, v = kk['c'], kk['o'], kk['h'], kk['l'], kk['v']
    e12, e50, m89 = kk['e12'], kk['e50'], kk['ma89']
    hi0 = h[t0]
    pulled_band = pulled_e12 = False
    for j in range(1, W + 1):
        i = t0 + j
        if i + 21 > last:            # 至少留 20 根持有
            break
        zhi = max(e12[i], e50[i], m89[i])
        v5 = sum(v[i - 5:i]) / 5 if i >= 5 and min(v[i - 5:i]) > 0 else 0
        vr = v[i] / v5 if v5 > 0 else 0
        ok = False
        if tm == '回踩带内后破高':
            ok = pulled_band and c[i] > hi0
        elif tm == '回踩带内后放量破高':
            ok = pulled_band and c[i] > hi0 and vr >= 1.2
        elif tm == '回踩EMA12后放量破高':
            ok = pulled_e12 and c[i] > hi0 and vr >= 1.2
        elif tm == '放量阳线过带':
            ok = vr >= 1.5 and c[i] > o[i] and c[i] > zhi and i > t0 + 1
        if not (c[i] > hi0):         # 未创新高的日子才可能算回踩
            if l[i] <= zhi:
                pulled_band = True
            if l[i] <= e12[i]:
                pulled_e12 = True
        if ok:
            return i + 1
    return None


def stat_full(rets):
    vals = [r for r, _ in rets]
    n = len(vals)
    sr = sorted(vals)
    return dict(n=n,
                win=round(sum(x > 0 for x in vals) / n * 100, 1),
                tp=round(sum(t == 'tp' for _, t in rets) / n * 100, 1),
                mean=round(statistics.mean(vals), 2),
                p05=round(sr[int(n * 0.05)], 2),
                worst=round(sr[0], 2))


def main():
    EM.NEED = W + 20 + 3             # 与上一轮一致：20日持有窗口（30日退出仅在bar足够时评估）
    D, sigs = EM.build_signals()
    dates = sorted(set(s['date'] for s in sigs))
    print('信号 %d 个 | %s ~ %s | 切分 %s | 组合 %d' % (
        len(sigs), dates[0], dates[-1], CUT, len(TRIGGERS) * 8 * len(EXITS)))

    # 过滤条件（按信号静态特征）
    def filters_of(s):
        A = s['brk20'] and s['ma89_up'] > 0
        f = {'全部信号': True,
             'A级': A,
             'A级+非涨停': A and not s['limit_up'],
             'A级+粘合<=3': A and s['glue'] <= 3,
             'A级+非涨停+粘合<=3': A and not s['limit_up'] and s['glue'] <= 3,
             'A级+上证>MA20': A and s['idx_above'],
             'A级+上行regime': A and s['regime'] == 'up',
             'A级+中小盘<=150亿': A and 0 < s['mv'] <= 150}
        return [k for k, v in f.items() if v]

    buckets = defaultdict(list)
    trig_count = defaultdict(int)
    waits = defaultdict(list)
    for si, s in enumerate(sigs):
        fl = filters_of(s)
        for tm in TRIGGERS:
            bi = find_buy(s, tm)
            if bi is None:
                continue
            trig_count[tm] += 1
            waits[tm].append(bi - s['t0'])
            split = 'train' if s['date'] < CUT else 'oos'
            for rn, X, Y, N in EXITS:
                r = EM.simulate(s, bi, X, Y, N)
                if r is None:
                    continue
                for f in fl:
                    buckets[(tm, f, rn, split)].append(r)

    print('\n=== 一、触发情况 ===')
    for tm in TRIGGERS:
        n, w = trig_count[tm], waits[tm]
        print('  %-16s 触发 %5d (%5.1f%%)  平均等待 %4.1f 日' % (
            tm, n, n / len(sigs) * 100, statistics.mean(w) if w else 0))

    print('\n=== 二、80%% 俱乐部（训练与样本外胜率均>=80%%，均值均>0，n_train>=%d，n_oos>=%d）===' % (
        MIN_TRAIN, MIN_OOS))
    club = []
    all_rows = []
    keys = set((tm, f, rn) for (tm, f, rn, _) in buckets.keys())
    for tm in TRIGGERS:
        for f in ['全部信号', 'A级', 'A级+非涨停', 'A级+粘合<=3', 'A级+非涨停+粘合<=3',
                  'A级+上证>MA20', 'A级+上行regime', 'A级+中小盘<=150亿']:
            for rn, X, Y, N in EXITS:
                a = buckets.get((tm, f, rn, 'train'))
                b = buckets.get((tm, f, rn, 'oos'))
                if not a or not b:
                    continue
                sa, sb = stat_full(a), stat_full(b)
                row = dict(trigger=tm, filt=f, exit=rn, train=sa, oos=sb)
                all_rows.append(row)
                if (sa['n'] >= MIN_TRAIN and sb['n'] >= MIN_OOS
                        and sa['win'] >= 80 and sb['win'] >= 80
                        and sa['mean'] > 0 and sb['mean'] > 0):
                    club.append(row)

    club.sort(key=lambda r: -r['oos']['mean'])
    print('  入选 %d 个，按样本外均值降序（前 25 个）：' % len(club))
    print('  %-16s %-14s %-16s | %-22s | %-22s' % ('买点', '过滤', '退出', '训练', '样本外'))
    for r in club[:25]:
        a, b = r['train'], r['oos']
        print('  %-16s %-14s %-16s | n=%3d %5.1f%% %+5.2f | n=%3d %5.1f%% %+5.2f P05 %+6.2f' % (
            r['trigger'], r['filt'], r['exit'],
            a['n'], a['win'], a['mean'], b['n'], b['win'], b['mean'], b['p05']))

    # 俱乐部内按 (买点,过滤) 聚合看规则族厚度
    fam = defaultdict(list)
    for r in club:
        fam[(r['trigger'], r['filt'])].append(r)
    print('\n=== 三、80%% 俱乐部按 规则族(买点×过滤) 聚合 ===')
    fam_rows = []
    for (tm, f), rs in fam.items():
        best = max(rs, key=lambda r: r['oos']['mean'])
        fam_rows.append(dict(trigger=tm, filt=f, k=len(rs), best_exit=best['exit'],
                             best_oos=best['oos'], best_train=best['train'],
                             exits=[r['exit'] for r in rs]))
        print('  %-16s %-14s  %d 个退出参数达标 | 最优 %s → 样本外 %5.1f%%/%+.2f' % (
            tm, f, len(rs), best['exit'], best['oos']['win'], best['oos']['mean']))

    res = dict(n_signals=len(sigs), date_min=dates[0], date_max=dates[-1], cut=CUT,
               cost=EM.COST, min_train=MIN_TRAIN, min_oos=MIN_OOS,
               trigger_counts={k: trig_count[k] for k in TRIGGERS},
               club=[dict(trigger=r['trigger'], filt=r['filt'], exit=r['exit'],
                          train=r['train'], oos=r['oos']) for r in club],
               families=fam_rows, n_all_rows=len(all_rows))
    json.dump(res, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print('\n结果已写入 %s' % OUT)


if __name__ == '__main__':
    main()
