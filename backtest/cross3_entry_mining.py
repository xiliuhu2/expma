#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线穿阳 = 状态标记（signal），不是买点（entry）。

与前面几轮的根本区别：
  旧口径：t0 三线穿阳 -> t0+1 开盘买入（把信号日当买点）
  本口径：t0 三线穿阳 -> 在 t0+1..t0+W 窗口内等待买点触发器 -> 触发确认后次日开盘买入

三线定义（与 server.py / 通达信一致）：
  e12 = ema(close,12)、e50 = ema(close,50)、ma89 = SMA(close,89)
  三线穿阳判据：阳线且 open < min(三线) 且 close > max(三线)（实体包住全部三线）

撮合（保守、可实现）：
  - 触发日 j 用当日及之前数据确认；买入价 = 触发日次日开盘价 o[t0+j+1]
  - 基线「T+1开盘」= 信号日次日开盘 o[t0+1]（与旧口径完全一致，便于配对比较）
  - 持有期逐日检查 high/low：先判止损（同日同时触及按先止损），再判止盈
  - 未触发则第 N 日收盘平仓；往返成本 0.25%/笔
  - 无未来函数：high/low 只用于结果判定，触发器只用 j 及之前数据

输出: backtest/cross3_entry_result.json
用法: python3 cross3_entry_mining.py
"""
import json, os, statistics, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, '..', 'webapp')
sys.path.insert(0, WEB)
import server as S

OUT = os.path.join(HERE, 'cross3_entry_result.json')
COST = 0.25
W = 20           # 信号后买点搜索窗口（交易日）
HOLD = 20         # 最长持有期
NEED = W + HOLD + 3
CUT = '2026-07-06'

TRIGGERS = [
    'T+1开盘(旧基线)', 'T+3后买', 'T+5后买',
    '回踩EMA12收回', '回踩三线带收回', '回踩带内守MA89',
    '缩量企稳不破MA89', '跌破MA89后收回', '再放量破信号日高',
    '回踩穿阳日开盘', '回撤3%后企稳', '连3日站稳MA89',
]

RULES = [
    ('持有5日', None, None, 5),
    ('+1%/不止损/20日', 1, 999, 20),
    ('+3%/不止损/20日', 3, 999, 20),
    ('+3%/-8%/20日', 3, 8, 20),
    ('+5%/不止损/20日', 5, 999, 20),
]


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def build_signals():
    D = S.Data()
    ix = D.indices.get('sh000001') or {}
    ic, idates = ix.get('c', []), ix.get('dates', [])
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
        e12, e50, m89 = kk['e12'], kk['e50'], kk['ma89']
        last = len(c) - 1
        if last < 95 + NEED:
            continue
        mv = D.meta.get(code, {}).get('mv', 0)
        for t in range(95, last - NEED + 1):
            if S.cross_lines(kk, t) != 3:
                continue
            d = kk['dates'][t]
            zlo, zhi = min(e12[t], e50[t], m89[t]), max(e12[t], e50[t], m89[t])
            hi20 = max(c[t - 20:t])
            ii = d2i.get(d)
            out.append(dict(
                code=code, name=name, t0=t, last=last, date=d, kk=kk,
                brk20=c[t] > hi20,
                ma89_up=(m89[t] / m89[t - 10] - 1) * 100 if m89[t - 10] else 0,
                glue=(zhi - zlo) / c[t] * 100 if c[t] else 99,
                vr=S.vr_at(kk, t) or 0,
                mv=mv,
                regime=D.regime.get(d, 'down'),
                idx_above=(ii is not None and ii >= 20 and ic[ii] > ma20.get(d, 1e9)),
                limit_up=pct(c[t], c[t - 1]) >= S.limit_ratio(code, name) * 100 - 0.6,
            ))
    return D, out


def find_buy_index(s, tm):
    """返回买入 bar 索引；None = 窗口内未触发。买入价取该 bar 开盘价。"""
    kk = s['kk']
    t0, last = s['t0'], s['last']
    c, o, h, l, v = kk['c'], kk['o'], kk['h'], kk['l'], kk['v']
    e12, e50, m89 = kk['e12'], kk['e50'], kk['ma89']
    hi0, o0 = h[t0], o[t0]
    peak = c[t0]
    broke = False
    if tm == 'T+1开盘(旧基线)':
        return t0 + 1
    for j in range(1, W + 1):
        i = t0 + j
        if i + HOLD + 1 > last:      # 买入后需完整 20 根
            break
        zhi = max(e12[i], e50[i], m89[i])
        zlo = min(e12[i], e50[i], m89[i])
        v5 = sum(v[i - 5:i]) / 5 if i >= 5 and min(v[i - 5:i]) > 0 else 0
        vr = v[i] / v5 if v5 > 0 else 0
        peak = max(peak, c[i])
        ok = False
        if tm == 'T+3后买':
            ok = (j == 3)
        elif tm == 'T+5后买':
            ok = (j == 5)
        elif tm == '回踩EMA12收回':
            ok = l[i] <= e12[i] and c[i] >= e12[i]
        elif tm == '回踩三线带收回':
            ok = l[i] <= zhi and c[i] >= zhi
        elif tm == '回踩带内守MA89':
            ok = l[i] <= zhi and c[i] >= m89[i] and c[i] >= zlo
        elif tm == '缩量企稳不破MA89':
            ok = 0 < vr < 0.8 and c[i] >= e12[i] and l[i] >= m89[i] * 0.99
        elif tm == '跌破MA89后收回':
            ok = broke and c[i] >= m89[i]
        elif tm == '再放量破信号日高':
            ok = c[i] > hi0 and vr >= 1.2
        elif tm == '回踩穿阳日开盘':
            ok = l[i] <= o0 * 1.01 and c[i] > o0
        elif tm == '回撤3%后企稳':
            ok = (peak > 0 and pct(c[i], peak) <= -3 and c[i] >= e12[i])
        elif tm == '连3日站稳MA89':
            ok = (j >= 3 and c[i] >= m89[i] and c[i - 1] >= m89[i - 1]
                  and c[i - 2] >= m89[i - 2])
        if c[i] < m89[i]:
            broke = True
        if ok:
            return i + 1            # 触发确认后次日开盘买入
    return None


def simulate(s, bi, X, Y, N):
    kk = s['kk']
    h, l, c, o = kk['h'], kk['l'], kk['c'], kk['o']
    if bi + N > s['last'] + 1:
        return None
    buy = o[bi]
    if buy <= 0:
        return None
    if X is None:                       # 纯持有 N 日收盘
        return pct(c[bi + N - 1], buy) - COST, 'hold'
    tp = buy * (1 + X / 100)
    sl = buy * (1 - Y / 100) if Y < 999 else -1
    # A 股 T+1：买入当日（bi）不可卖出，止盈/止损只能从次一交易日（bi+1）起判。
    # 时间退出仍是第 N 根 bar 收盘 c[bi+N-1]（N>=2 时天然满足 T+1）。
    # 旧口径 range(bi, bi+N) 会把「买入当日 high 触达止盈」计为已卖出，属不可成交的虚增收益。
    for k in range(bi + 1, bi + N):
        if sl > 0 and l[k] <= sl:
            return -Y - COST, 'stop'
        if h[k] >= tp:
            return X - COST, 'tp'
    return pct(c[bi + N - 1], buy) - COST, 'time'


def stat(rets):
    if not rets:
        return None
    n = len(rets)
    sr = sorted(r for r, _ in rets)
    return dict(n=n,
                win=round(sum(r > 0 for r, _ in rets) / n * 100, 1),
                tp_rate=round(sum(t == 'tp' for _, t in rets) / n * 100, 1),
                mean=round(statistics.mean(sr), 2),
                p05=round(sr[int(n * 0.05)], 2),
                worst=round(sr[0], 2))


def filters_of(s):
    f = {
        '全部信号': True,
        'A级(破20高+MA89上行)': s['brk20'] and s['ma89_up'] > 0,
        'A级+粘合<=3%': s['brk20'] and s['ma89_up'] > 0 and s['glue'] <= 3,
        'A级+非涨停': s['brk20'] and s['ma89_up'] > 0 and not s['limit_up'],
        '三线粘合<=3%': s['glue'] <= 3,
        '上证>MA20': s['idx_above'],
        '上行regime': s['regime'] == 'up',
    }
    return [k for k, v in f.items() if v]


def main():
    D, sigs = build_signals()
    dates = sorted(set(s['date'] for s in sigs))
    print('可评估三线穿阳信号: %d  |  %s ~ %s  |  切分日 %s' % (
        len(sigs), dates[0], dates[-1], CUT))
    print('成本 %.2f%%/笔 | 买点窗口 %d 日 | 持有上限 %d 日\n' % (COST, W, HOLD))

    buckets = defaultdict(list)
    waits = defaultdict(list)
    pair = defaultdict(list)          # (tm, split, si) -> +3% 规则收益，用于配对
    trig_count = defaultdict(int)
    all_filts = ['全部信号', 'A级(破20高+MA89上行)', 'A级+粘合<=3%', 'A级+非涨停',
                 '三线粘合<=3%', '上证>MA20', '上行regime']

    for si, s in enumerate(sigs):
        fl = filters_of(s)
        for tm in TRIGGERS:
            bi = find_buy_index(s, tm)
            if bi is None:
                continue
            trig_count[tm] += 1
            waits[tm].append(bi - s['t0'])
            split = 'train' if s['date'] < CUT else 'oos'
            for rn, X, Y, N in RULES:
                r = simulate(s, bi, X, Y, N)
                if r is None:
                    continue
                key = (tm, rn, split)
                for f in fl:
                    buckets[(tm, f, rn, split)].append(r)
                if rn == '+3%/不止损/20日':
                    pair[(tm, split, si)] = r[0]

    # 触发率 / 平均等待天数
    print('=== 一、各买点在信号后 20 日内的触发情况 ===')
    print('%-22s %8s %8s %10s' % ('买点', '触发数', '触发率', '平均等待'))
    for tm in TRIGGERS:
        n = trig_count[tm]
        w = waits[tm]
        print('  %-20s %8d %7.1f%% %8.1f日' % (
            tm, n, n / len(sigs) * 100, statistics.mean(w) if w else 0))

    # 主结果表：+3%/不止损/20日
    rn = '+3%/不止损/20日'
    print('\n=== 二、各买点 × 过滤  [%s]  全样本 / 训练 / 样本外 ===' % rn)
    print('%-22s %-24s %6s %7s %8s | %6s %7s %8s | %6s %7s %8s' % (
        '买点', '过滤', 'n', '胜率', '均值', 'n', '胜率', '均值', 'n', '胜率', '均值'))
    rows_out = []
    for tm in TRIGGERS:
        for f in all_filts:
            a = buckets.get((tm, f, rn, 'train'))
            b = buckets.get((tm, f, rn, 'oos'))
            if not a or not b or len(a) < 20 or len(b) < 15:
                continue
            sa, sb = stat(a), stat(b)
            allr = stat(a + b)
            flag = ' *' if (sa['tp_rate'] >= 55 and sb['tp_rate'] >= 55
                            and sa['mean'] > 0 and sb['mean'] > 0) else ''
            rows_out.append(dict(trigger=tm, filt=f, all=allr, train=sa, oos=sb, robust=bool(flag)))
            print('  %-20s %-24s %6d %6.1f%% %+7.2f | %6d %6.1f%% %+7.2f | %6d %6.1f%% %+7.2f%s' % (
                tm, f, allr['n'], allr['tp_rate'], allr['mean'],
                sa['n'], sa['tp_rate'], sa['mean'], sb['n'], sb['tp_rate'], sb['mean'], flag))

    # 全部规则里找 90% + 正期望 + 样本外稳定
    print('\n=== 三、90% 检查：训练段胜率>=90% 且正期望的组合，及样本外表现 ===')
    hi = []
    for (tm, f, rnk, split), v in buckets.items():
        if split != 'train':
            continue
        b = buckets.get((tm, f, rnk, 'oos'))
        if not b:
            continue
        sa, sb = stat(v), stat(b)
        if sa['tp_rate'] >= 90 and sa['mean'] > 0:
            hi.append((tm, f, rnk, sa, sb))
    if hi:
        for tm, f, rnk, sa, sb in sorted(hi, key=lambda z: -z[3]['mean'])[:15]:
            print('  %-20s %-22s %-18s 训练 %.1f%%/%+.2f (n=%d) | 样本外 %.1f%%/%+.2f (n=%d)' % (
                tm, f, rnk, sa['tp_rate'], sa['mean'], sa['n'], sb['tp_rate'], sb['mean'], sb['n']))
    else:
        print('  无：训练段没有任何「胜率>=90% 且正期望」的买点×过滤×卖出组合')

    # 稳健规则排行（两段胜率>=55%、均值>0），按样本外均值排序
    print('\n=== 四、稳健组合排行（训练/样本外同时 胜率>=55%% 且均值>0）===')
    robust = []
    for (tm, f, rnk, split), v in buckets.items():
        if split != 'train':
            continue
        b = buckets.get((tm, f, rnk, 'oos'))
        if not b or len(v) < 30 or len(b) < 15:
            continue
        sa, sb = stat(v), stat(b)
        if sa['tp_rate'] >= 55 and sb['tp_rate'] >= 55 and sa['mean'] > 0 and sb['mean'] > 0:
            robust.append((tm, f, rnk, sa, sb))
    robust.sort(key=lambda z: -z[4]['mean'])
    for tm, f, rnk, sa, sb in robust[:20]:
        print('  %-20s %-22s %-18s 训练 %.1f%%/%+.2f | 样本外 %.1f%%/%+.2f (n=%d)' % (
            tm, f, rnk, sa['tp_rate'], sa['mean'], sb['tp_rate'], sb['mean'], sb['n']))
    print('  合计 %d 个稳健组合' % len(robust))

    # 配对比较：同一信号下「买点收益 - T+1旧基线收益」（全样本，+3%/不止损/20日）
    print('\n=== 五、配对比较：等买点 vs 信号次日追入 [%s]（同一信号相减）===' % rn)
    base = {(split, si): r for (tm, split, si), r in pair.items()
            if tm == 'T+1开盘(旧基线)'}
    diff = defaultdict(list)
    for (tm, split, si), r in pair.items():
        if tm == 'T+1开盘(旧基线)':
            continue
        if (split, si) in base:
            diff[tm].append(r - base[(split, si)])
    for tm in TRIGGERS:
        v = diff.get(tm)
        if not v:
            continue
        print('  %-20s n=%5d 平均差 %+6.2fpp | 跑赢基线占比 %5.1f%% | 中位差 %+6.2fpp' % (
            tm, len(v), statistics.mean(v), sum(x > 0 for x in v) / len(v) * 100,
            statistics.median(v)))

    # 今日观察名单：最近 20 日内出现过三线穿阳、当前仍站在三线带上方
    today = D.px_date
    watch = []
    for s in sigs:
        kk, t0 = s['kk'], s['t0']
        c, e12, e50, m89 = kk['c'], kk['e12'], kk['e50'], kk['ma89']
        last = s['last']
        if kk['dates'][last] != today:
            continue
        # 用完整数据（含最近信号，不受 NEED 限制）另扫一次由调用方处理
    watch = recent_watch(D, today)
    print('\n=== 六、今日可等待回踩的观察名单（近20日曾三线穿阳，现仍在三线带上方）===')
    for w in watch[:15]:
        print('  %s %-8s 信号日%s 收%7.2f | 三线带 %.2f~%.2f | 现价距带上沿 %+.1f%% | A级=%s' % (
            w['code'], w['name'], w['sig_date'], w['close'], w['zlo'], w['zhi'],
            w['dist'], 'Y' if w['a'] else 'N'))

    res = dict(
        n_signals=len(sigs), date_min=dates[0], date_max=dates[-1], cut=CUT,
        cost=COST, window=W, hold=HOLD,
        trigger_counts={k: v for k, v in trig_count.items()},
        trigger_wait={k: (round(statistics.mean(v), 1) if v else None)
                      for k, v in waits.items()},
        main_rows=rows_out,
        high_win_positive=[dict(trigger=t, filt=f, rule=r, train=a, oos=b)
                           for t, f, r, a, b in hi],
        robust=[dict(trigger=t, filt=f, rule=r, train=a, oos=b)
                for t, f, r, a, b in robust],
        paired={k: dict(n=len(v), mean_diff=round(statistics.mean(v), 2),
                        win_share=round(sum(x > 0 for x in v) / len(v) * 100, 1),
                        median_diff=round(statistics.median(v), 2))
                for k, v in diff.items()},
        watchlist=watch,
    )
    json.dump(res, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print('\n结果已写入 %s' % OUT)


def recent_watch(D, today):
    """近 20 交易日内出现过三线穿阳，且今日仍站在三线带上方（完整数据，不受 NEED 限制）"""
    out = []
    for code, kk in D.k.items():
        name = D.meta.get(code, {}).get('name', '')
        if S.is_st(name) or not kk['dates'] or kk['dates'][-1] != today:
            continue
        c, o, h, l = kk['c'], kk['o'], kk['h'], kk['l']
        e12, e50, m89 = kk['e12'], kk['e50'], kk['ma89']
        last = len(c) - 1
        if last < 95:
            continue
        sig_t = None
        for t in range(max(95, last - 20), last + 1):
            if S.cross_lines(kk, t) == 3:
                sig_t = t
        if sig_t is None:
            continue
        zlo, zhi = min(e12[last], e50[last], m89[last]), max(e12[last], e50[last], m89[last])
        if c[last] < zhi:
            continue
        hi20 = max(c[sig_t - 20:sig_t])
        ma89_up = (m89[sig_t] / m89[sig_t - 10] - 1) * 100 if m89[sig_t - 10] else 0
        out.append(dict(
            code=code, name=name, sig_date=kk['dates'][sig_t], close=round(c[last], 2),
            zlo=round(zlo, 2), zhi=round(zhi, 2),
            dist=round(pct(c[last], zhi), 1),
            a=bool(c[sig_t] > hi20 and ma89_up > 0),
            glue=round((max(e12[sig_t], e50[sig_t], m89[sig_t])
                        - min(e12[sig_t], e50[sig_t], m89[sig_t])) / c[sig_t] * 100, 1),
        ))
    out.sort(key=lambda w: w['dist'])
    return out


if __name__ == '__main__':
    main()
