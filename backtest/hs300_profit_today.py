#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 · EXPMA双线向上池「高收益候选」当日清单

读 backtest/hs300_profit_result.json（因子权重 + 回测统计），对当前池内股票计算三套排名：
  ① 现行评分（复现 server.py hs300_view）  ② walk-forward 稳健分（样本外验证过）
  ③ 高弹性分（博取大收益，仅 up 市适用）
并输出「稳健 × 高弹性」交集，供 p7 页签 /api/hs300_profit 读取。

用法：python3 hs300_profit_today.py   → 写 webapp/hs300_profit.json
      daily_run.py 收盘跑批自动调用 collect()（轻量，仅读 300 个成份 K 线）
"""
import os, sys, json, datetime, statistics
from bisect import bisect_right

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import hs300_profit_mining as M

RESULT = os.path.join(HERE, 'hs300_profit_result.json')
OUT = os.path.join(BASE, 'webapp', 'hs300_profit.json')
CONS = os.path.join(BASE, 'webapp', 'hs300_cons.json')
SNAP = os.path.join(BASE, 'backtest', 'snapshot.json')
IDX = os.path.join(BASE, 'backtest', 'indices', 'sh000300.json')


def rank_score(cross, factors):
    """按给定 (factor, sign) 组合的截面百分位等权打分，0~100。"""
    if not factors:
        return {c: 0.0 for c in cross}
    codes = list(cross)
    n = len(codes)
    sc = {}
    ranks = {f: M.rank_avg([cross[c][f] for c in codes]) for f, _ in factors}
    for i, c in enumerate(codes):
        v = sum(sgn * (ranks[f][i] - 1) / max(1, n - 1) for f, sgn in factors)
        sc[c] = round(v / len(factors) * 100, 2)
    return sc


def features_at(s, t, bench_c, bench_pos, eq20):
    if t < 65 or not M.in_pool(s, t):
        return None
    c, h, l, o, v = s['c'], s['h'], s['l'], s['o'], s['v']
    e12, e50 = s['e12'], s['e50']
    s12 = M.pct(e12[t], e12[t - 5])
    s50 = M.pct(e50[t], e50[t - 10])
    rets = [M.pct(c[t - k], c[t - k - 1]) for k in range(20)]
    hi20, hi60, lo20 = max(h[t - 20:t]), max(h[t - 60:t]), min(l[t - 20:t])
    bi = bench_pos.get(s['d'][t])
    b20 = M.pct(bench_c[bi], bench_c[bi - 20]) if bi and bi >= 20 else 0.0
    b60 = M.pct(bench_c[bi], bench_c[bi - 60]) if bi and bi >= 60 else 0.0
    c20 = M.pct(c[t], c[t - 20])
    c60 = M.pct(c[t], c[t - 60])
    f = dict(
        dist12=M.pct(c[t], e12[t]), dist50=M.pct(c[t], e50[t]), s12=s12, s50=s50,
        s12_acc=s12 - M.pct(e12[t - 5], e12[t - 10]),
        c5=M.pct(c[t], c[t - 5]), c10=M.pct(c[t], c[t - 10]), c20=c20, c60=c60,
        vr5=v[t] / (sum(v[t - 5:t]) / 5) if min(v[t - 5:t]) > 0 else 1.0,
        tr20=sum(s['tr'][t - 20:t]) / 20, amt20=sum(s['amt'][t - 20:t]) / 20,
        vol20=statistics.pstdev(rets),
        atr14=sum(h[k] - l[k] for k in range(t - 14, t)) / 14 / c[t] * 100,
        brk20=1.0 if c[t] > hi20 else 0.0, brk60=1.0 if c[t] > hi60 else 0.0,
        dist_hi60=M.pct(c[t], hi60),
        pos20=(c[t] - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50.0,
        band=(e12[t] - e50[t]) / c[t] * 100, streak=0.0,
        rs20=c20 - b20, rs60=c60 - b60,
        up20=sum(1 for x in rets if x > 0) / 20 * 100,
        eq20=eq20, bench20=b20, gap=0.0,
        c=round(c[t], 2), e50=round(e50[t], 2), e12=round(e12[t], 2),
        date=s['d'][t])
    return f


def collect():
    res = json.load(open(RESULT))
    chosen = [(x['factor'], x['sign']) for x in res['chosen']]
    wf = [(x['factor'], x['sign']) for x in res['strategy']['_extra']['wf_final_factors']]
    cons = json.load(open(CONS))['map']
    snap = json.load(open(SNAP))
    items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
    mv = {}
    for r in items:
        try:
            mv[r['code']] = float(r.get('TotalMV') or 0)
        except (ValueError, TypeError):
            pass
    eq20_all = M.eqw_baseline(BASE)
    idx = json.load(open(IDX))
    bd = [r[0] for r in idx]
    bc = [float(r[2]) for r in idx]
    bpos = {d: i for i, d in enumerate(bd)}

    cur = {}
    for c6, name in cons.items():
        code = ('sh' if c6[0] == '6' else 'sz') + c6
        p = os.path.join(BASE, 'backtest', 'klines', code + '.json')
        if not os.path.exists(p):
            continue
        s = M.load_stock(p)
        if not s:
            continue
        t = len(s['c']) - 1
        f = features_at(s, t, bc, bpos, eq20_all.get(s['d'][t], 0.0))
        if f:
            f['name'], f['mv'] = name, mv.get(code, 0.0)
            cur[code] = f

    old = {c: M.current_score(f, f['mv']) for c, f in cur.items()}
    wfs = rank_score(cur, wf)
    els = rank_score(cur, chosen)
    ro = sorted(cur, key=lambda c: -old[c])
    rw = sorted(cur, key=lambda c: -wfs[c])
    re = sorted(cur, key=lambda c: -els[c])
    inter = [c for c in rw[:30] if c in set(re[:30])][:10]

    def row(c):
        f = cur[c]
        return dict(code=c, name=f['name'], c=f['c'],
                    e50=f['e50'], stop8=round(f['c'] * 0.92, 2),
                    old_score=old[c], old_rank=ro.index(c) + 1,
                    wf_score=wfs[c], wf_rank=rw.index(c) + 1,
                    elastic_score=els[c], elastic_rank=re.index(c) + 1,
                    c20=round(f['c20'], 1), c60=round(f['c60'], 1),
                    dist12=round(f['dist12'], 1), dist_hi60=round(f['dist_hi60'], 1),
                    pos20=round(f['pos20'], 1), up20=round(f['up20'], 1),
                    vol20=round(f['vol20'], 2), vr5=round(f['vr5'], 2),
                    amt20=round(f['amt20']), brk20=int(f['brk20']), brk60=int(f['brk60']))

    bi = bpos.get(max(bd))
    above = bool(bi and bi >= 20 and bc[bi] > sum(bc[bi - 20:bi]) / 20)
    regime = dict(date=max(bd), above_ma20=above,
                  idx_c20=round(M.pct(bc[bi], bc[bi - 20]), 2) if bi and bi >= 20 else None,
                  tradeable=above)
    st = res['strategy']
    summary = dict(
        date=res['data']['last'], panels=res['data']['panels'],
        samples=res['data']['samples'], train=res['data']['train'],
        test=res['data']['test'], train_end=res['data']['train_end'],
        old_top5_all=st['all']['old_top5']['ret20']['mean'],
        pool_all=st['all']['pool_all']['ret20']['mean'],
        elastic_top5_all=st['all']['new_top5']['ret20']['mean'],
        elastic_bigwin=st['all']['new_top5']['bigwin_rate'],
        old_bigwin=st['all']['old_top5']['bigwin_rate'],
        elastic_mfe15=st['all']['new_top5']['bigmfe_rate'],
        old_mfe15=st['all']['old_top5']['bigmfe_rate'],
        wf_hold20=st['_extra']['walkforward']['all']['hold20'],
        wf_tp8sl8=st['_extra']['walkforward']['all']['tp8'],
        up_old=st['_extra']['regime']['all']['up']['old'],
        up_elastic=st['_extra']['regime']['all']['up']['new'],
        down_old=st['_extra']['regime']['all']['down']['old'],
        down_elastic=st['_extra']['regime']['all']['down']['new'])
    return dict(
        date=max(f['date'] for f in cur.values()), pool_n=len(cur),
        generated_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        regime=regime, factors=dict(walkforward=wf, elastic=chosen),
        rules=dict(
            entry='仅在沪深300收盘>MA20（up市）时执行；确认后次一交易日 9:30 开盘价买入',
            stop='买入价 -8% 硬止损（盘中触及即离场）；池子破位参考 EMA50（连续3日收盘不收回才剔除）',
            exit='不设固定止盈（+8%止盈会把大赢单砍掉）；最多持有 10 个交易日收盘了结；'
                 '保守版可用 +8%止盈 / -8%止损 / 20日',
            position='单票 ≤1/4，等权分散；down 市空仓或仅观察',
            caveat='本地K线仅 2025-07-11 起（约16个月、47个周频截面、3088样本），'
                   '未覆盖完整熊市；成份股用当前名单（幸存者偏差）；市值为当前快照静态值'),
        summary=summary,
        intersect=[row(c) for c in inter],
        by_wf=[row(c) for c in rw[:12]],
        by_elastic=[row(c) for c in re[:12]],
        by_old=[row(c) for c in ro[:8]])


def main():
    d = collect()
    json.dump(d, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print('%s 池内 %d 只 · 市场 %s（HS300近20日 %+.2f%%）· %s' % (
        d['date'], d['pool_n'], 'up' if d['regime']['above_ma20'] else 'down',
        d['regime']['idx_c20'], '可执行' if d['regime']['tradeable'] else '仅观察'))
    print('交集精选 %d 只:' % len(d['intersect']))
    for x in d['intersect']:
        print('  %-9s %-7s 稳健第%2d 弹性第%2d 现行第%3d 收%7.2f 20日%+6.1f%% 60日%+6.1f%% -8%%止损%6.2f' % (
            x['code'], x['name'], x['wf_rank'], x['elastic_rank'], x['old_rank'],
            x['c'], x['c20'], x['c60'], x['stop8']))
    print('已写入 %s' % OUT)


if __name__ == '__main__':
    main()
