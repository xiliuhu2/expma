#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线穿阳 · 高胜率组合挖掘（纯标准库，位集穷举 + 时间样本外验证）

数据源: webapp/tltrack/signals.csv （9046 个全历史三线穿阳信号，特征均为信号日可得）
口径  : 胜率 = 信号日收盘 -> 5 个交易日后收盘 收益 > 0 的比例（与 daily 日报一致）
      超额 = fwd5 - bench5（等权小盘基准）
防过拟合: 按信号日期 70/30 拆分训练/样本外；搜索只在训练集做，样本外只用于评估。
      同时报告不同最小样本量下的最高胜率，展示"样本越小胜率越高"的过拟合陷阱。
用法: python3 cross3_highwin_mining.py [--csv PATH] [--min-train 100] [--maxk 3]
"""
import argparse, csv, itertools, json, os, statistics, sys

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'webapp')
DEFAULT_CSV = os.path.join(WEB, 'tltrack', 'signals.csv')
OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cross3_highwin_result.json')


def num(r, k):
    try:
        v = r.get(k, '')
        return float(v) if v not in ('', 'None', None) else None
    except (ValueError, TypeError):
        return None


def build_conditions():
    """每个条件: (名称, 判断函数) —— 只使用信号日 t 及之前的数据。"""
    C = []
    add = lambda name, fn: C.append((name, fn))
    # 趋势/位置
    add('破20日高', lambda r: num(r, 'brk20') == 1)
    add('未破20日高', lambda r: num(r, 'brk20') == 0)
    add('MA89上行(>0)', lambda r: (num(r, 'ma89_slope') or -99) > 0)
    add('MA89强上行(>1)', lambda r: (num(r, 'ma89_slope') or -99) > 1)
    add('MA89强上行(>2)', lambda r: (num(r, 'ma89_slope') or -99) > 2)
    add('MA89下行', lambda r: (num(r, 'ma89_slope') or 99) <= 0)
    add('多头排列stack', lambda r: num(r, 'stack') == 1)
    add('regime上行', lambda r: r.get('regime') == 'up')
    add('regime下行', lambda r: r.get('regime') == 'down')
    # 量能（通达信倍量口径）
    add('倍量vr>=1.5', lambda r: (num(r, 'vr') or 0) >= 1.5)
    add('放量vr>=2', lambda r: (num(r, 'vr') or 0) >= 2)
    add('缩量vr<1', lambda r: 0 < (num(r, 'vr') or 0) < 1)
    add('温和量1<=vr<1.5', lambda r: 1 <= (num(r, 'vr') or 0) < 1.5)
    # K线形态
    add('实体>3%', lambda r: (num(r, 'body') or -99) > 3)
    add('实体>5%', lambda r: (num(r, 'body') or -99) > 5)
    add('上影<1%', lambda r: (num(r, 'upsh') or 99) < 1)
    add('上影<2%', lambda r: (num(r, 'upsh') or 99) < 2)
    add('收盘位>0.8', lambda r: (num(r, 'close_pos') or 0) > 0.8)
    add('收盘位>0.9', lambda r: (num(r, 'close_pos') or 0) > 0.9)
    add('距EMA12<3%', lambda r: (num(r, 'dev12') or 99) < 3)
    add('距EMA12<5%', lambda r: (num(r, 'dev12') or 99) < 5)
    add('距EMA12<8%', lambda r: (num(r, 'dev12') or 99) < 8)
    # 位置/涨幅
    add('20日涨幅<=0', lambda r: (num(r, 'c20') or 99) <= 0)
    add('0<20日涨幅<=20', lambda r: 0 < (num(r, 'c20') or -99) <= 20)
    add('20日涨幅>20', lambda r: (num(r, 'c20') or -99) > 20)
    add('60日深跌<=-30%', lambda r: (num(r, 'c60') or 99) <= -30)
    add('60日上涨>0', lambda r: (num(r, 'c60') or -99) > 0)
    add('距60日高点<=-30%', lambda r: (num(r, 'dist_hi60') or 99) <= -30)
    add('距60日高点>=-15%(高位)', lambda r: (num(r, 'dist_hi60') or -99) >= -15)
    add('20日平台窄<=15%', lambda r: (num(r, 'plat20') or 99) <= 15)
    add('20日平台窄<=10%', lambda r: (num(r, 'plat20') or 99) <= 10)
    # 均线粘合度：(zone_hi-zone_lo)/close （通达信 <=3%）
    def glue(r, th):
        lo, hi, c = num(r, 'zone_lo'), num(r, 'zone_hi'), num(r, 'close')
        return (lo is not None and hi is not None and c and (hi - lo) / c * 100 <= th)
    add('三线粘合<=3%', lambda r: glue(r, 3))
    add('三线粘合<=5%', lambda r: glue(r, 5))
    # 市值/基本面/涨停
    add('小盘<=60亿', lambda r: 0 < (num(r, 'mv') or 0) <= 60)
    add('中盘<=120亿', lambda r: 0 < (num(r, 'mv') or 0) <= 120)
    add('大盘>=200亿', lambda r: (num(r, 'mv') or 0) >= 200)
    add('亏损股PE<=0', lambda r: num(r, 'loss') == 1)
    add('当日涨停', lambda r: num(r, 'limit_up') == 1)
    add('非涨停', lambda r: num(r, 'limit_up') == 0)
    add('一字板', lambda r: num(r, 'one_word') == 1)
    # 评分/事件/池
    add('技术分>=50', lambda r: (num(r, 'tech') or 0) >= 50)
    add('技术分>=60', lambda r: (num(r, 'tech') or 0) >= 60)
    add('事件催化>0', lambda r: (num(r, 'event_lib') or 0) > 0)
    add('曾入三线池Top5', lambda r: num(r, 'in_tlpool') == 1)
    add('近5日不涨c5<=0', lambda r: (num(r, 'c5') or 99) <= 0)
    add('近5日大涨c5>=10', lambda r: (num(r, 'c5') or -99) >= 10)
    return C


def bitset_indices(mask):
    out = []
    while mask:
        lsb = mask & -mask
        out.append(lsb.bit_length() - 1)
        mask ^= lsb
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=DEFAULT_CSV)
    ap.add_argument('--min-train', type=int, default=100)
    ap.add_argument('--maxk', type=int, default=3)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding='utf-8')))
    rows = [r for r in rows if num(r, 'fwd5') is not None]
    n = len(rows)
    vals = [num(r, 'fwd5') for r in rows]
    excs = [num(r, 'excess5') if num(r, 'excess5') is not None else 0.0 for r in rows]
    dates = [r['date'] for r in rows]

    # 时间 70/30 拆分（排序后按行数切，不用随机，模拟真实"用过去预测未来"）
    order = sorted(range(n), key=lambda i: (dates[i], rows[i]['code']))
    cut = int(n * 0.7)
    train_ids, test_ids = set(order[:cut]), set(order[cut:])
    split_date = dates[order[cut]]
    train_m = sum(1 << i for i in train_ids)
    test_m = sum(1 << i for i in test_ids)

    C = build_conditions()
    masks = []
    for name, fn in C:
        m = 0
        for i, r in enumerate(rows):
            try:
                if fn(r):
                    m |= 1 << i
            except Exception:
                pass
        masks.append(m)

    def stats(mask):
        idx = bitset_indices(mask)
        if not idx:
            return dict(n=0, win=None, mean=None, exc=None)
        v = [vals[i] for i in idx]
        return dict(n=len(idx),
                    win=round(sum(x > 0 for x in v) / len(v) * 100, 1),
                    mean=round(statistics.mean(v), 2),
                    exc=round(statistics.mean([excs[i] for i in idx]), 2))

    print('样本 %d 条 | 日期 %s ~ %s | 训练/样本外切分日 %s' % (
        n, min(dates), max(dates), split_date))
    print('全样本基线: %s' % stats(train_m | test_m))
    print('训练集基线: %s' % stats(train_m))
    print('样本外基线: %s' % stats(test_m))
    print('\n=== 单条件 5日胜率（全样本, n>=100，按胜率降序）===')
    singles = []
    for (name, _), m in zip(C, masks):
        s = stats(m)
        if s['n'] >= 100:
            singles.append((s['win'], name, s))
    for w, name, s in sorted(singles, reverse=True)[:15]:
        print('  %-24s n=%4d win5=%5.1f%% mean5=%+6.2f%% exc5=%+6.2f%%' % (
            name, s['n'], s['win'], s['mean'], s['exc']))

    # 组合穷举（训练集）
    best_by_min = {mn: [] for mn in (100, 50, 30, 10)}
    top = []
    cnt = 0
    names = [c[0] for c in C]
    for k in range(2, args.maxk + 1):
        for combo in itertools.combinations(range(len(C)), k):
            m = train_m
            for j in combo:
                m &= masks[j]
                if not m:
                    break
            if not m:
                continue
            pc = m.bit_count() if hasattr(int, 'bit_count') else bin(m).count('1')
            cnt += 1 if pc else 0
            if pc < 10:
                continue
            s = stats(m)
            rec = dict(combo=[names[j] for j in combo], k=k, train=s)
            for mn in best_by_min:
                if s['n'] >= mn:
                    best_by_min[mn].append((s['win'], rec))
                    if len(best_by_min[mn]) > 4000:
                        best_by_min[mn] = sorted(best_by_min[mn], reverse=True,
                                                 key=lambda z: z[0])[:200]
            if s['n'] >= args.min_train:
                top.append((s['win'], rec))
    print('\n穷举组合数（n>=10）: %d' % cnt)
    print('\n=== 不同最小样本量下「训练集最高胜率」（展示过拟合陷阱）===')
    for mn in (100, 50, 30, 10):
        if best_by_min[mn]:
            w, rec = max(best_by_min[mn], key=lambda z: z[0])
            print('  n>=%-3d 最高训练胜率 %5.1f%% (n=%d) : %s' % (
                mn, w, rec['train']['n'], ' + '.join(rec['combo'])))
    print('\n=== 训练集 Top10 组合 -> 样本外验证 ===')
    top.sort(reverse=True, key=lambda z: (z[0], z[1]['train']['n']))
    out = []
    for w, rec in top[:10]:
        m_full = (train_m | test_m)
        m_test = test_m
        for nm in rec['combo']:
            j = names.index(nm)
            m_full &= masks[j]
            m_test &= masks[j]
        rec['full'] = stats(m_full)
        rec['test'] = stats(m_test)
        out.append(rec)
        t, s = rec['train'], rec['test']
        print('  %5.1f%% (n=%4d) | 样本外 %5.1f%% (n=%4d) | 全样本 %5.1f%% (n=%4d) | %s' % (
            t['win'], t['n'], s['win'] or 0, s['n'], rec['full']['win'], rec['full']['n'],
            ' + '.join(rec['combo'])))
    # 稳健组合：训练+样本外双高，且样本外 n>=30
    print('\n=== 稳健组合（训练>=%d, 样本外 n>=30，按 min(训练胜率,样本外胜率) 排序）===' % args.min_train)
    robust = [r for r in out if r['test']['n'] >= 30 and r['test']['win'] is not None]
    for r in sorted(robust, key=lambda z: min(z['train']['win'], z['test']['win']), reverse=True)[:10]:
        print('  训练 %5.1f%% / 样本外 %5.1f%% / 全样本 %5.1f%% (n=%d) | %s' % (
            r['train']['win'], r['test']['win'], r['full']['win'], r['full']['n'],
            ' + '.join(r['combo'])))

    # 网上宣称组合的本地复现
    print('\n=== 网上「三线穿阳」宣称 82%/81.3% 条件的本地复现 ===')
    claimed = {
        '通达信核心: 粘合<=3%+倍量>=1.5': ['三线粘合<=3%', '倍量vr>=1.5'],
        '加低位平台(60日深跌<=-30%)': ['三线粘合<=3%', '倍量vr>=1.5', '60日深跌<=-30%'],
        '加大盘上行regime': ['三线粘合<=3%', '倍量vr>=1.5', 'regime上行'],
        '全部条件(粘合+倍量+低位+上行)': ['三线粘合<=3%', '倍量vr>=1.5', '60日深跌<=-30%', 'regime上行'],
        '实体>3%+倍量+粘合<=3%': ['实体>3%', '倍量vr>=1.5', '三线粘合<=3%'],
    }
    claimed_out = {}
    for label, conds in claimed.items():
        try:
            m = (train_m | test_m)
            for nm in conds:
                m &= masks[names.index(nm)]
            s = stats(m)
            claimed_out[label] = s
            print('  %-36s n=%4d win5=%5s%% mean5=%+6s%% exc5=%+6s%%' % (
                label, s['n'], s['win'], s['mean'], s['exc']))
        except ValueError:
            pass

    res = dict(n=n, split_date=split_date,
               baseline=stats(train_m | test_m), train_baseline=stats(train_m),
               test_baseline=stats(test_m), top=out, claimed=claimed_out,
               max_by_min={str(k): (max(v, key=lambda z: z[0])[0],
                                   max(v, key=lambda z: z[0])[1]['train']['n'],
                                   max(v, key=lambda z: z[0])[1]['combo'])
                           for k, v in best_by_min.items() if v})
    json.dump(res, open(OUT_JSON, 'w'), ensure_ascii=False, indent=2)
    print('\n结果已写: %s' % OUT_JSON)


if __name__ == '__main__':
    main()
