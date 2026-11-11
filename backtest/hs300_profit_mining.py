#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 · EXPMA双线向上池 —— 高收益因子挖掘与「精选Top5」对比回测

问题：现行 hs300_view() 评分（趋势30+贴近EMA12位置25+动能25+流动性10+相对强度10）
      偏向「贴近均线的大盘稳健票」，用户怀疑真正的高收益票没被选出来。

方法（遵循 quant-factor-research 口径）：
  1) 面板：每个选股截面 = 每隔 5 个交易日；样本 = 当日严格满足入池条件的沪深300成份股
     （收盘 > EMA12 > EMA50 且 EMA12五日斜率>0、EMA50十日斜率>0）
  2) 无未来函数：因子只用 T 日及之前数据；成交价用 T+1 开盘；前向收益 T+1 → T+20 收盘
  3) 目标变量：ret20（20日收益）、mfe20（20日内最大有利幅度）、
     bigwin（ret20>=10%）、bigmfe（mfe20>=15%）——「大收益」用上尾而非均值衡量
  4) 单因子：截面 Spearman 秩 IC / IR / 正率 + 五分位分层单调性
  5) 训练段定权重、样本外验证；与现行评分每周 Top5 等权对比
  6) 重要限制：本地K线仅 2025-07-11 起（约16个月）；市值为当前快照静态值（历史截面有轻微前视），
     故市值不进入新评分，仅用于复现现行评分。

输出：backtest/hs300_profit_result.json（完整数值）+ stdout 摘要
依赖：仅 Python 标准库；数据 = backtest/klines + indices + snapshot + webapp/hs300_cons.json
"""
import json, os, math, sys, statistics, datetime
from bisect import bisect_right
from collections import defaultdict
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = _REPO
KL = os.path.join(BASE, 'backtest/klines')
IDX = os.path.join(BASE, 'backtest/indices/sh000300.json')
CONS = os.path.join(BASE, 'webapp/hs300_cons.json')
SNAP = os.path.join(BASE, 'backtest/snapshot.json')
OUT = os.path.join(BASE, 'backtest/hs300_profit_result.json')

STEP = 5          # 每 5 个交易日一个截面（周频）
HOLD = 20         # 前向持有 20 个交易日
WARM = 65         # EMA50/60日窗口热身
TRAIN_END = '2026-05-31'
EQ20 = {}          # 全市场等权小盘基线的20日涨幅（复现现行评分用）


def ema(vals, n):
    a = 2.0 / (n + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * a + e * (1 - a)
        out.append(e)
    return out


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def load_stock(path):
    rows = json.load(open(path))
    if len(rows) < WARM + HOLD + 5:
        return None
    d = [r[0] for r in rows]
    o = [float(r[1]) for r in rows]
    c = [float(r[2]) for r in rows]
    h = [float(r[3]) for r in rows]
    l = [float(r[4]) for r in rows]
    v = [float(r[5]) for r in rows]
    tr, amt = [], []
    for r in rows:
        try:
            tr.append(float(r[7]))
        except (ValueError, TypeError, IndexError):
            tr.append(0.0)
        try:
            amt.append(float(r[8]))
        except (ValueError, TypeError, IndexError):
            amt.append(0.0)
    return dict(d=d, o=o, c=c, h=h, l=l, v=v, tr=tr, amt=amt,
                e12=ema(c, 12), e50=ema(c, 50), pos={x: i for i, x in enumerate(d)})


def in_pool(s, t):
    c, e12, e50 = s['c'][t], s['e12'][t], s['e50'][t]
    return (c > e12 > e50 and e12 > s['e12'][t - 5] and e50 > s['e50'][t - 10])


def features(s, t, bench_c, bench_pos):
    """T 日因子（只用 T 及之前数据）。返回 None 表示不在池或数据不足。"""
    if t < WARM or t + HOLD + 1 >= len(s['c']) or not in_pool(s, t):
        return None
    c, h, l, o, v = s['c'], s['h'], s['l'], s['o'], s['v']
    e12, e50 = s['e12'], s['e50']
    d = s['d'][t]
    s12 = pct(e12[t], e12[t - 5])
    s50 = pct(e50[t], e50[t - 10])
    c5, c10, c20, c60 = (pct(c[t], c[t - k]) for k in (5, 10, 20, 60))
    vr5 = v[t] / (sum(v[t - 5:t]) / 5) if min(v[t - 5:t]) > 0 else 1.0
    rets = [pct(c[t - k], c[t - k - 1]) for k in range(20)]
    vol20 = statistics.pstdev(rets)
    atr14 = sum(h[k] - l[k] for k in range(t - 14, t)) / 14 / c[t] * 100
    hi20, hi60 = max(h[t - 20:t]), max(h[t - 60:t])
    lo20 = min(l[t - 20:t])
    band = (e12[t] - e50[t]) / c[t] * 100
    streak = 0
    j = t
    while j >= WARM and in_pool(s, j) and streak < 60:
        streak += 1
        j -= 1
    bi = bench_pos.get(d)
    b20 = pct(bench_c[bi], bench_c[bi - 20]) if bi and bi >= 20 else 0.0
    b60 = pct(bench_c[bi], bench_c[bi - 60]) if bi and bi >= 60 else 0.0
    f = dict(
        dist12=pct(c[t], e12[t]), dist50=pct(c[t], e50[t]),
        s12=s12, s50=s50, s12_acc=s12 - pct(e12[t - 5], e12[t - 10]),
        c5=c5, c10=c10, c20=c20, c60=c60,
        vr5=vr5, tr20=sum(s['tr'][t - 20:t]) / 20,
        amt20=sum(s['amt'][t - 20:t]) / 20,
        vol20=vol20, atr14=atr14,
        brk20=1.0 if c[t] > hi20 else 0.0,
        brk60=1.0 if c[t] > hi60 else 0.0,
        dist_hi60=pct(c[t], hi60),
        pos20=(c[t] - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50.0,
        band=band, streak=float(streak),
        rs20=c20 - b20, rs60=c60 - b60,
        up20=sum(1 for x in rets if x > 0) / 20 * 100,
        eq20=EQ20.get(d, b20),
        gap=0.0,  # 占位，保持字段集稳定
    )
    # 结果变量：T+1 开盘买，T+20 收盘卖；期间最高/最低算 MFE/MAE
    buy = o[t + 1]
    if buy <= 0:
        return None
    f['ret5'] = pct(c[t + 5], buy)
    f['ret10'] = pct(c[t + 10], buy)
    f['ret20'] = pct(c[t + 20], buy)
    f['mfe20'] = pct(max(h[t + 1:t + 21]), buy)
    f['mae20'] = pct(min(l[t + 1:t + 21]), buy)
    f['bigwin'] = 1.0 if f['ret20'] >= 10 else 0.0
    f['bigmfe'] = 1.0 if f['mfe20'] >= 15 else 0.0
    # 前向逐日 OHLC，供止盈/止损/持有期模拟（不落盘，仅内存）
    f['_fwd'] = [(h[k], l[k], c[k], o[k]) for k in range(t + 1, t + HOLD + 1)]
    if bi is not None and bi + 20 < len(bench_c):
        f['bench20'] = b20                                  # 过去20日基准（复现评分用）
        f['bench_fwd20'] = pct(bench_c[bi + 20], bench_c[bi])
        f['excess20'] = f['ret20'] - f['bench_fwd20']
    else:
        f['bench20'], f['excess20'] = b20, f['ret20']
    return f


def eqw_baseline(BASE):
    """复现 server.Data().idx_rets（全市场等权、市值<=100亿）的20日累计涨幅，带本地缓存。"""
    cache = os.path.join(BASE, 'backtest', 'eqw_rets.json')
    if not os.path.exists(cache):
        sys.path.insert(0, os.path.join(BASE, 'webapp'))
        import server as S
        D = S.Data()
        json.dump(dict(idx_days=D.idx_days, idx_rets=D.idx_rets),
                  open(cache, 'w'), ensure_ascii=False)
    z = json.load(open(cache))
    days, rets = z['idx_days'], z['idx_rets']
    cum = [1.0]
    for r in rets:
        cum.append(cum[-1] * (1 + r))
    return {d: pct(cum[i], cum[i - 20]) for i, d in enumerate(days) if i >= 20}


def current_score(f, mv):
    """原样复现 server.py hs300_view() 的评分（相对强度用全市场等权小盘基线）"""
    s_trend = min(30.0, max(0.0, f['s12'] * 20))
    d12 = f['dist12']
    s_pos = 25 if 0 <= d12 <= 3 else 15 if d12 <= 6 else 5 if d12 <= 12 else 0
    c20 = f['c20']
    s_mom = 25 if 5 <= c20 <= 25 else 15 if 0 <= c20 < 5 or 25 < c20 <= 40 else 5 if c20 <= 60 else 0
    s_liq = 10 if mv >= 300 else 7 if mv >= 100 else 4
    s_rel = min(10.0, max(0.0, (c20 - f.get('eq20', f['bench20'])) / 3))
    return round(s_trend + s_pos + s_mom + s_liq + s_rel, 1)


def rank_avg(vals):
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def pearson(a, b):
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / math.sqrt(va * vb)


def main():
    global EQ20
    EQ20 = eqw_baseline(BASE)
    cons = json.load(open(CONS))['map']
    snap = json.load(open(SNAP))
    items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
    mv_map = {}
    for r in items:
        try:
            mv_map[r['code']] = float(r.get('TotalMV') or 0)
        except (ValueError, TypeError):
            pass
    idx = json.load(open(IDX))
    bench_d = [r[0] for r in idx]
    bench_c = [float(r[2]) for r in idx]
    bench_pos = {d: i for i, d in enumerate(bench_d)}

    stocks = {}
    for c6, name in cons.items():
        code = ('sh' if c6[0] == '6' else 'sz') + c6
        p = os.path.join(KL, code + '.json')
        if not os.path.exists(p):
            continue
        s = load_stock(p)
        if s:
            s['name'], s['mv'] = name, mv_map.get(code, 0.0)
            stocks[code] = s
    print('成份股可用: %d/300 · 基准 %s ~ %s' % (len(stocks), bench_d[0], bench_d[-1]))

    # 截面日期：取指数日历中与个股数据重叠的部分，每 STEP 根取一次
    first = min(s['d'][0] for s in stocks.values())
    last = min(s['d'][-1] for s in stocks.values())
    cal = [d for d in bench_d if first <= d <= last]
    dates = cal[WARM::STEP]
    # 最后一个截面必须能留出 HOLD+1 根前向 bar（用指数日历近似）
    last_i = bench_pos[last]
    dates = [d for d in dates if bench_pos[d] + HOLD + 1 <= last_i]
    print('截面数 %d · %s ~ %s' % (len(dates), dates[0], dates[-1]))

    panels = []   # [(date, {code: f})]
    for d in dates:
        cross = {}
        for code, s in stocks.items():
            t = s['pos'].get(d)
            if t is None:
                continue
            f = features(s, t, bench_c, bench_pos)
            if f:
                cross[code] = f
        if len(cross) >= 8:
            panels.append((d, cross))
    print('有效截面 %d · 平均池内 %.1f 只 · 样本 %d 条' % (
        len(panels), sum(len(x) for _, x in panels) / max(1, len(panels)),
        sum(len(x) for _, x in panels)))

    FACTORS = ['dist12', 'dist50', 's12', 's50', 's12_acc', 'c5', 'c10', 'c20', 'c60',
               'vr5', 'tr20', 'amt20', 'vol20', 'atr14', 'brk20', 'brk60',
               'dist_hi60', 'pos20', 'band', 'streak', 'rs20', 'rs60', 'up20']
    TARGETS = ['ret20', 'mfe20', 'bigwin', 'bigmfe']

    def split(panels_, end=TRAIN_END):
        return [p for p in panels_ if p[0] <= end], [p for p in panels_ if p[0] > end]
    train, test = split(panels)
    print('训练截面 %d (%s~%s) / 样本外 %d (%s~%s)' % (
        len(train), train[0][0], train[-1][0], len(test), test[0][0], test[-1][0]))

    # ---- 单因子截面 IC / IR + 五分位分层 ----
    def factor_stats(panels_):
        ics = {f: {tg: [] for tg in TARGETS} for f in FACTORS}
        quint = {f: [[] for _ in range(5)] for f in FACTORS}
        bin_grp = {f: defaultdict(list) for f in FACTORS}
        for d, cross in panels_:
            codes = list(cross)
            tg_rank = {tg: rank_avg([cross[c][tg] for c in codes]) for tg in TARGETS}
            for f in FACTORS:
                vals = [cross[c][f] for c in codes]
                fr = rank_avg(vals)
                for tg in TARGETS:
                    ics[f][tg].append(pearson(fr, tg_rank[tg]))
                order = sorted(range(len(codes)), key=lambda i: vals[i])
                for q in range(5):
                    grp = order[q * len(order) // 5:(q + 1) * len(order) // 5]
                    quint[f][q].extend(cross[codes[i]]['ret20'] for i in grp)
                for i, cd in enumerate(codes):
                    bin_grp[f][round(vals[i], 1)].append(cross[cd]['ret20'])
        out = {}
        for f in FACTORS:
            row = {}
            for tg in TARGETS:
                a = ics[f][tg]
                mu = sum(a) / len(a) if a else 0
                sd = statistics.pstdev(a) if len(a) > 1 else 0
                row[tg] = dict(ic=round(mu, 4), ir=round(mu / sd, 3) if sd else 0,
                               pos=round(sum(1 for x in a if x > 0) / len(a), 3) if a else 0)
            row['quint_ret20'] = [round(sum(q) / len(q), 2) if q else None for q in quint[f]]
            row['quint_bigwin'] = [round(sum(1 for x in q if x >= 10) / len(q) * 100, 1) if q else None
                                   for q in quint[f]]
            row['binary'] = f in ('brk20', 'brk60')
            if row['binary']:
                row['by_value'] = {str(k): dict(n=len(v), ret20=round(sum(v) / len(v), 2),
                                                bigwin=round(sum(1 for x in v if x >= 10) / len(v) * 100, 1))
                                   for k, v in sorted(bin_grp[f].items())}
            out[f] = row
        return out

    st_train, st_test, st_all = factor_stats(train), factor_stats(test), factor_stats(panels)

    # ---- 构造「高收益评分」：训练段按 mfe20/bigwin 的 IC 选因子，方向由训练段 IC 决定 ----
    cand = []
    for f in FACTORS:
        r = st_train[f]
        ic_mfe, ic_ret = r['mfe20']['ic'], r['ret20']['ic']
        strength = 0.6 * ic_mfe + 0.4 * ic_ret
        if abs(strength) >= 0.03 and r['mfe20']['pos'] >= 0.55 and not r['binary']:
            cand.append((abs(strength), f, 1 if strength > 0 else -1))
    cand.sort(reverse=True)
    chosen = cand[:6]
    print('\n入选因子（训练段，按 |0.6*IC_mfe+0.4*IC_ret| 排序，取前6）:')
    for a, f, sgn in chosen:
        print('  %-12s %s  IC_mfe=%+.4f IC_ret=%+.4f IR=%.2f 分位ret20=%s' % (
            f, '+' if sgn > 0 else '-', st_train[f]['mfe20']['ic'],
            st_train[f]['ret20']['ic'], st_train[f]['mfe20']['ir'],
            st_train[f]['quint_ret20']))

    def new_score(cross, chosen_factors=None):
        cf = chosen if chosen_factors is None else chosen_factors
        if not cf:
            return {c: 0.0 for c in cross}
        codes = list(cross)
        ranks = {f: rank_avg([cross[c][f] for c in codes]) for _, f, _ in cf}
        n = len(codes)
        sc = {}
        for i, cd in enumerate(codes):
            v = 0.0
            for _, f, sgn in cf:
                p = (ranks[f][i] - 1) / max(1, n - 1)   # 0~1 百分位
                v += sgn * p
            sc[cd] = round(v / max(1, len(cf)) * 100, 2)
        return sc

    def old_score(cross):
        return {c: current_score(f, stocks[c]['mv']) for c, f in cross.items()}

    # ---- 策略对比：每截面 Top5 等权，T+1 开盘买 / T+20 收盘卖 ----
    def evaluate(panels_, score_fn, topn=5):
        rec = []
        for d, cross in panels_:
            sc = score_fn(cross)
            pick = sorted(cross, key=lambda c: -sc[c])[:topn]
            if not pick:
                continue
            rec.append(dict(date=d, n=len(pick),
                            ret20=sum(cross[c]['ret20'] for c in pick) / len(pick),
                            ret10=sum(cross[c]['ret10'] for c in pick) / len(pick),
                            mfe20=sum(cross[c]['mfe20'] for c in pick) / len(pick),
                            mae20=sum(cross[c]['mae20'] for c in pick) / len(pick),
                            excess=sum(cross[c]['excess20'] for c in pick) / len(pick),
                            bigwin=sum(cross[c]['bigwin'] for c in pick) / len(pick) * 100,
                            bigmfe=sum(cross[c]['bigmfe'] for c in pick) / len(pick) * 100,
                            win=sum(1 for c in pick if cross[c]['ret20'] > 0) / len(pick) * 100,
                            codes=pick))
        if not rec:
            return {}
        def agg(key):
            a = [r[key] for r in rec]
            return dict(mean=round(sum(a) / len(a), 2),
                        median=round(statistics.median(a), 2),
                        p10=round(sorted(a)[int(0.1 * (len(a) - 1))], 2),
                        p90=round(sorted(a)[int(0.9 * (len(a) - 1))], 2),
                        worst=round(min(a), 2))
        return dict(n_weeks=len(rec), ret20=agg('ret20'), ret10=agg('ret10'),
                    mfe20=agg('mfe20'), mae20=agg('mae20'), excess=agg('excess'),
                    bigwin_rate=round(sum(r['bigwin'] for r in rec) / len(rec), 1),
                    bigmfe_rate=round(sum(r['bigmfe'] for r in rec) / len(rec), 1),
                    win_rate=round(sum(r['win'] for r in rec) / len(rec), 1))

    result = {}
    for name, ps in (('train', train), ('test', test), ('all', panels)):
        result[name] = dict(
            pool_all=evaluate(ps, lambda c: {k: 0 for k in c}, topn=999),
            old_top5=evaluate(ps, old_score, 5),
            new_top5=evaluate(ps, new_score, 5),
            new_top10=evaluate(ps, new_score, 10),
            old_bottom5=evaluate(ps, lambda c: {k: -v for k, v in old_score(c).items()}, 5),
        )

    # ---- 市场状态（T 日只用过去数据）----
    def regime_at(d):
        i = bench_pos[d]
        if i < 25:
            return dict(above_ma20=False, r20=0.0)
        ma20 = sum(bench_c[i - 20:i]) / 20
        return dict(above_ma20=bench_c[i] > ma20,
                    r20=round(pct(bench_c[i], bench_c[i - 20]), 2))
    regime = {d: regime_at(d) for d, _ in panels}

    # ---- 出场规则模拟：把 MFE 变成落袋收益 ----
    def exit_ret(f, tp=None, sl=None, hold=20, cost=0.25):
        """T+1开盘买；逐日先判止损、再判止盈（保守）；未触发第 hold 日收盘平仓。扣往返成本。"""
        seq = f['_fwd'][:hold]
        if len(seq) < hold:
            return None
        buy = seq[0][3]
        if buy <= 0:
            return None
        tp_px = buy * (1 + tp / 100) if tp else None
        sl_px = buy * (1 - sl / 100) if sl else None
        for hi, lo, cl, op in seq:
            if sl_px and lo <= sl_px:
                return -sl - cost
            if tp_px and hi >= tp_px:
                return tp - cost
        return pct(seq[-1][2], buy) - cost

    def trail_ret(f, trail=5, sl=8, hold=20, cost=0.25):
        """移动止盈：买入后记录最高价，回撤超过 trail% 即离场；硬止损 sl% 优先；最长 hold 日。"""
        seq = f['_fwd'][:hold]
        if len(seq) < hold:
            return None
        buy = seq[0][3]
        if buy <= 0:
            return None
        peak = buy
        sl_px = buy * (1 - sl / 100) if sl else None
        for hi, lo, cl, op in seq:
            if sl_px and lo <= sl_px:
                return -sl - cost
            peak = max(peak, hi)
            if peak > buy * (1 + trail / 100) and lo <= peak * (1 - trail / 100):
                return pct(peak * (1 - trail / 100), buy) - cost
        return pct(seq[-1][2], buy) - cost

    def eval_trail(panels_, score_fn, topn=5, trail=5, sl=8, hold=20, reg=None):
        recs = []
        for d, cross in panels_:
            if reg is not None and bool(regime[d]['above_ma20']) != reg:
                continue
            sc = score_fn(cross)
            pick = sorted(cross, key=lambda c: -sc[c])[:topn]
            rs = [trail_ret(cross[c], trail, sl, hold) for c in pick]
            rs = [x for x in rs if x is not None]
            if rs:
                recs.append(sum(rs) / len(rs))
        if not recs:
            return None
        return dict(n=len(recs), mean=round(sum(recs) / len(recs), 2),
                    median=round(statistics.median(recs), 2),
                    win=round(sum(1 for x in recs if x > 0) / len(recs) * 100, 1),
                    worst=round(min(recs), 2),
                    best=round(max(recs), 2))

    def eval_exit(panels_, score_fn, topn=5, tp=None, sl=None, hold=20, reg=None):
        recs = []
        for d, cross in panels_:
            if reg is not None and bool(regime[d]['above_ma20']) != reg:
                continue
            sc = score_fn(cross)
            pick = sorted(cross, key=lambda c: -sc[c])[:topn]
            rs = [exit_ret(cross[c], tp, sl, hold) for c in pick]
            rs = [x for x in rs if x is not None]
            if rs:
                recs.append(sum(rs) / len(rs))
        if not recs:
            return None
        return dict(n=len(recs), mean=round(sum(recs) / len(recs), 2),
                    median=round(statistics.median(recs), 2),
                    win=round(sum(1 for x in recs if x > 0) / len(recs) * 100, 1),
                    worst=round(min(recs), 2))

    # ---- walk-forward：每个截面只用「之前」的 IC 选因子，严格无前视 ----
    def wf_factors(hist, k=5):
        ics = {}
        for f in FACTORS:
            a = [pearson(rank_avg([x[f] for x in cs.values()]),
                         rank_avg([x['ret20'] for x in cs.values()])) for cs in hist]
            mu = sum(a) / len(a)
            pos = sum(1 for x in a if x > 0) / len(a)
            ics[f] = (mu, pos)
        use = [(abs(mu), f, 1 if mu > 0 else -1)
               for f, (mu, pos) in ics.items()
               if abs(mu) >= 0.03 and (pos >= 0.6 if mu > 0 else pos <= 0.4)]
        use.sort(reverse=True)
        return use[:k]

    wf = {}       # date -> chosen factors（仅由历史决定）
    hist = []
    for d, cross in panels:
        if len(hist) >= 10:
            wf[d] = wf_factors(hist)
        hist.append(cross)
    wf_panels = [(d, c) for d, c in panels if d in wf]
    wf_train = [(d, c) for d, c in wf_panels if d <= TRAIN_END]
    wf_test = [(d, c) for d, c in wf_panels if d > TRAIN_END]
    wf_score = lambda cross: new_score(cross, None)   # 占位，逐截面覆盖
    def eval_wf(panels_, tp=None, sl=None, hold=20, reg=None, topn=5):
        recs = []
        for d, cross in panels_:
            if reg is not None and bool(regime[d]['above_ma20']) != reg:
                continue
            sc = new_score(cross, wf[d])
            pick = sorted(cross, key=lambda c: -sc[c])[:topn]
            rs = [exit_ret(cross[c], tp, sl, hold) for c in pick]
            rs = [x for x in rs if x is not None]
            if rs:
                recs.append(sum(rs) / len(rs))
        if not recs:
            return None
        return dict(n=len(recs), mean=round(sum(recs) / len(recs), 2),
                    median=round(statistics.median(recs), 2),
                    win=round(sum(1 for x in recs if x > 0) / len(recs) * 100, 1),
                    worst=round(min(recs), 2))

    # 出场规则网格：训练段挑，样本外验证
    EXIT_GRID = [(tp, sl, hold) for tp in (None, 6, 8, 10, 12, 15)
                 for sl in (None, 5, 8) for hold in (5, 10, 20)]
    grid_train, grid_test = {}, {}
    for tp, sl, hold in EXIT_GRID:
        key = 'tp%s_sl%s_h%d' % (tp or 0, sl or 0, hold)
        a = eval_exit(train, lambda c: new_score(c, chosen), 5, tp, sl, hold)
        b = eval_exit(test, lambda c: new_score(c, chosen), 5, tp, sl, hold)
        if a:
            grid_train[key] = a['mean']
        if b:
            grid_test[key] = b['mean']
    best_grid = sorted(grid_train, key=lambda k: -grid_train[k])[:8]
    # 移动止盈网格（不设固定止盈，保留上尾）
    trail_cmp = {}
    for seg, ps in (('train', train), ('test', test), ('all', panels)):
        trail_cmp[seg] = {}
        for trail in (5, 8, 12):
            for sl in (5, 8, 12):
                for hold in (10, 20):
                    key = 'tr%d_sl%d_h%d' % (trail, sl, hold)
                    trail_cmp[seg][key] = eval_trail(
                        ps, lambda c: new_score(c, chosen), 5, trail, sl, hold)
    trail_rank = sorted(
        [(k, trail_cmp['train'][k]['mean'], trail_cmp['test'][k]['mean'])
         for k in trail_cmp['train'] if trail_cmp['test'][k]],
        key=lambda x: -min(x[1], x[2]))[:8]
    # 逐截面用各自 walk-forward 因子的移动止盈
    wf_trail = {}
    def eval_wf_trail(panels_, trail=8, sl=8, hold=20, reg=None):
        recs = []
        for d, cross in panels_:
            if reg is not None and bool(regime[d]['above_ma20']) != reg:
                continue
            sc = new_score(cross, wf[d])
            pick = sorted(cross, key=lambda c: -sc[c])[:5]
            rs = [trail_ret(cross[c], trail, sl, hold) for c in pick]
            rs = [x for x in rs if x is not None]
            if rs:
                recs.append(sum(rs) / len(rs))
        if not recs:
            return None
        return dict(n=len(recs), mean=round(sum(recs) / len(recs), 2),
                    median=round(statistics.median(recs), 2),
                    win=round(sum(1 for x in recs if x > 0) / len(recs) * 100, 1),
                    worst=round(min(recs), 2), best=round(max(recs), 2))
    for seg, ps in (('train', train), ('test', test), ('all', panels)):
        wf_trail[seg] = eval_wf_trail([(d, c) for d, c in ps if d in wf])

    regime_cmp, wf_cmp = {}, {}
    for seg, ps in (('train', train), ('test', test), ('all', panels)):
        for reg in (True, False, None):
            k = 'up' if reg is True else ('down' if reg is False else 'all')
            regime_cmp.setdefault(seg, {})[k] = dict(
                n=sum(1 for d, _ in ps if reg is None or bool(regime[d]['above_ma20']) == reg),
                old=eval_exit(ps, old_score, 5, None, None, 20, reg),
                new=eval_exit(ps, lambda c: new_score(c, chosen), 5, None, None, 20, reg),
                new_tp8=eval_exit(ps, lambda c: new_score(c, chosen), 5, 8, 8, 20, reg),
                pool=eval_exit(ps, lambda c: {x: 0 for x in c}, 999, None, None, 20, reg))
        wf_score = None
        wf_cmp[seg] = dict(
            hold20=eval_wf([(d, c) for d, c in ps if d in wf], None, None, 20),
            tp8=eval_wf([(d, c) for d, c in ps if d in wf], 8, 8, 20),
            up_only=eval_wf([(d, c) for d, c in ps if d in wf], 8, 8, 20, True))

    result['_extra'] = dict(
        exit_grid_train_top=best_grid,
        exit_grid_train=grid_train, exit_grid_test=grid_test,
        trail_rank=trail_rank, trail_cmp=trail_cmp, wf_trail=wf_trail,
        regime=regime_cmp, walkforward=wf_cmp,
        wf_panels=len(wf_panels),
        wf_final_factors=[dict(factor=f, sign=s, w=round(w, 4))
                          for w, f, s in wf[max(wf)] if wf])

    # ---- 当前池内（最新 bar）两套评分排名 ----
    latest = max(s['d'][-1] for s in stocks.values())
    cur = {}
    for code, s in stocks.items():
        t = s['pos'].get(latest)
        if t is None:
            # 停牌：取最近一根已收盘（仅用于展示，不参与回测）
            t = bisect_right(s['d'], latest) - 1
        f = features(s, t, bench_c, bench_pos)
        if f:
            f['mv'] = s['mv']
            cur[code] = f
    # 最新截面没有 20 根前向数据，features 会返回 None；用「仅算因子不算结果」的宽松模式
    def features_now(s, t):
        if t < WARM or not in_pool(s, t):
            return None
        c, h, l, v = s['c'], s['h'], s['l'], s['v']
        e12, e50 = s['e12'], s['e50']
        s12, s50 = pct(e12[t], e12[t - 5]), pct(e50[t], e50[t - 10])
        rets = [pct(c[t - k], c[t - k - 1]) for k in range(20)]
        hi20, hi60, lo20 = max(h[t - 20:t]), max(h[t - 60:t]), min(l[t - 20:t])
        bi = bench_pos.get(s['d'][t])
        b20 = pct(bench_c[bi], bench_c[bi - 20]) if bi and bi >= 20 else 0.0
        b60 = pct(bench_c[bi], bench_c[bi - 60]) if bi and bi >= 60 else 0.0
        f = dict(dist12=pct(c[t], e12[t]), dist50=pct(c[t], e50[t]), s12=s12, s50=s50,
                 s12_acc=s12 - pct(e12[t - 5], e12[t - 10]),
                 c5=pct(c[t], c[t - 5]), c10=pct(c[t], c[t - 10]),
                 c20=pct(c[t], c[t - 20]), c60=pct(c[t], c[t - 60]),
                 vr5=v[t] / (sum(v[t - 5:t]) / 5) if min(v[t - 5:t]) > 0 else 1.0,
                 tr20=sum(s['tr'][t - 20:t]) / 20, amt20=sum(s['amt'][t - 20:t]) / 20,
                 vol20=statistics.pstdev(rets),
                 atr14=sum(h[k] - l[k] for k in range(t - 14, t)) / 14 / c[t] * 100,
                 brk20=1.0 if c[t] > hi20 else 0.0, brk60=1.0 if c[t] > hi60 else 0.0,
                 dist_hi60=pct(c[t], hi60),
                 pos20=(c[t] - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50.0,
                 band=(e12[t] - e50[t]) / c[t] * 100, streak=0.0,
                 rs20=pct(c[t], c[t - 20]) - b20, rs60=pct(c[t], c[t - 60]) - b60,
                 up20=sum(1 for x in rets if x > 0) / 20 * 100, gap=0.0,
                 bench20=b20, eq20=EQ20.get(s['d'][t], b20),
                 c=round(c[t], 2), e50=round(e50[t], 2), mv=s['mv'],
                 name=s['name'])
        return f
    cur = {}
    for code, s in stocks.items():
        t = s['pos'].get(latest)
        if t is None:
            t = bisect_right(s['d'], latest) - 1
        if t >= WARM and s['d'][t] == latest:   # 只要当日有 bar 的（避免用停牌旧价）
            f = features_now(s, t)
            if f:
                cur[code] = f
    ns = new_score(cur)
    os_ = old_score(cur)
    ws = new_score(cur, wf[max(wf)] if wf else [])
    rank_new = sorted(cur, key=lambda c: -ns[c])
    rank_old = sorted(cur, key=lambda c: -os_[c])
    rank_wf = sorted(cur, key=lambda c: -ws[c])
    # 两套都靠前 = 既有验证过的稳健结构、又有高弹性上尾
    inter = [c for c in rank_wf[:30] if c in set(rank_new[:30])][:10]
    bi = bench_pos.get(latest)
    now_reg = dict(date=latest,
                   above_ma20=bool(bi and bi >= 20 and
                                   bench_c[bi] > sum(bench_c[bi - 20:bi]) / 20),
                   idx_c20=round(pct(bench_c[bi], bench_c[bi - 20]), 2) if bi and bi >= 20 else None)
    current = dict(date=latest, pool_n=len(cur),
                    by_new=[dict(code=c, name=cur[c]['name'], score=ns[c], old_score=os_[c],
                                 old_rank=rank_old.index(c) + 1,
                                 c=cur[c]['c'], c20=round(cur[c]['c20'], 1),
                                 c60=round(cur[c]['c60'], 1), dist12=round(cur[c]['dist12'], 1),
                                 s12=round(cur[c]['s12'], 2), vr5=round(cur[c]['vr5'], 2),
                                 vol20=round(cur[c]['vol20'], 2), brk20=cur[c]['brk20'],
                                 brk60=cur[c]['brk60'], rs20=round(cur[c]['rs20'], 1),
                                 tr20=round(cur[c]['tr20'], 2), e50=cur[c]['e50'])
                            for c in rank_new[:15]],
                    by_old=[dict(code=c, name=cur[c]['name'], score=os_[c],
                                 new_rank=rank_new.index(c) + 1) for c in rank_old[:8]],
                    by_wf=[dict(code=c, name=cur[c]['name'], wf_score=ws[c],
                                old_rank=rank_old.index(c) + 1, elastic_rank=rank_new.index(c) + 1,
                                c=cur[c]['c'], c20=round(cur[c]['c20'], 1),
                                c60=round(cur[c]['c60'], 1),
                                dist_hi60=round(cur[c]['dist_hi60'], 1),
                                pos20=round(cur[c]['pos20'], 1),
                                up20=round(cur[c]['up20'], 1),
                                amt20=round(cur[c]['amt20'], 0),
                                vol20=round(cur[c]['vol20'], 2),
                                dist12=round(cur[c]['dist12'], 1),
                                e50=cur[c]['e50']) for c in rank_wf[:12]],
                    intersect=[dict(code=c, name=cur[c]['name'],
                                    wf_rank=rank_wf.index(c) + 1,
                                    elastic_rank=rank_new.index(c) + 1,
                                    old_rank=rank_old.index(c) + 1,
                                    wf_score=ws[c], elastic_score=ns[c],
                                    c=cur[c]['c'], c20=round(cur[c]['c20'], 1),
                                    c60=round(cur[c]['c60'], 1),
                                    dist_hi60=round(cur[c]['dist_hi60'], 1),
                                    vol20=round(cur[c]['vol20'], 2),
                                    e50=cur[c]['e50']) for c in inter],
                    regime_now=now_reg)

    payload = dict(generated_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                   data=dict(stocks=len(stocks), first=first, last=last,
                             panels=len(panels), samples=sum(len(x) for _, x in panels),
                             train=len(train), test=len(test), train_end=TRAIN_END,
                             step=STEP, hold=HOLD),
                   chosen=[dict(factor=f, sign=sgn, w=round(w, 4),
                                ic_mfe_train=st_train[f]['mfe20']['ic'],
                                ic_ret_train=st_train[f]['ret20']['ic']) for w, f, sgn in chosen],
                   factors=dict(train=st_train, test=st_test, all=st_all),
                   strategy=result, current=current)
    json.dump(payload, open(OUT, 'w'), ensure_ascii=False, indent=1)

    # ---- stdout 摘要 ----
    print('\n=== 单因子（全样本，IC 对 ret20 / mfe20；按 |IC_mfe| 排序）===')
    print('%-12s %9s %9s %7s %7s  %s' % ('因子', 'IC_ret20', 'IC_mfe20', 'IR', '正率', '五分位ret20(低→高)'))
    for f in sorted(FACTORS, key=lambda x: -abs(st_all[x]['mfe20']['ic']))[:14]:
        r = st_all[f]
        print('%-12s %+9.4f %+9.4f %7.2f %7.2f  %s' % (
            f, r['ret20']['ic'], r['mfe20']['ic'], r['mfe20']['ir'],
            r['mfe20']['pos'], r['quint_ret20']))
    for seg in ('train', 'test', 'all'):
        print('\n=== %s：每周 Top5 等权（T+1开盘买 / T+20收盘卖）===' % seg)
        for k in ('pool_all', 'old_top5', 'new_top5', 'new_top10', 'old_bottom5'):
            r = result[seg].get(k) or {}
            if not r:
                continue
            print(' %-12s n=%2d ret20 均%+6.2f%% 中%+6.2f%% P90%+6.2f%% | MFE%+6.2f%%'
                  ' | 大赢率%5.1f%% MFE>=15%% %5.1f%% 胜率%5.1f%% 超额%+6.2fpp' % (
                      k, r['n_weeks'], r['ret20']['mean'], r['ret20']['median'],
                      r['ret20']['p90'], r['mfe20']['mean'], r['bigwin_rate'],
                      r['bigmfe_rate'], r['win_rate'], r['excess']['mean']))
    print('\n=== 当前池内 %s 共 %d 只 · 新评分 Top10 ===' % (latest, len(cur)))
    for x in current['by_new'][:10]:
        print(' %-9s %-7s 新%+6.2f 旧%5.1f(第%3d名) 收%7.2f 20日%+6.1f%% 60日%+6.1f%%'
              ' 距EMA12%+5.1f%% 量比%4.2f 波动%4.2f 破20高%d' % (
                  x['code'], x['name'], x['score'], x['old_score'], x['old_rank'],
                  x['c'], x['c20'], x['c60'], x['dist12'], x['vr5'], x['vol20'], x['brk20']))
    print('\n=== 现行 Top5 在新评分里的名次 ===')
    for x in current['by_old'][:5]:
        print(' %-9s %-7s 旧%5.1f → 新评分第 %3d 名' % (x['code'], x['name'], x['score'], x['new_rank']))
    ex = result['_extra']
    print('\n=== 市场状态分层（沪深300 收盘 > MA20 = up；ret20 = 20日持有，扣0.25%%成本）===')
    for seg in ('train', 'test', 'all'):
        for k in ('up', 'down'):
            r = ex['regime'][seg][k]
            if not r['n']:
                continue
            def fm(x):
                return '—' if not x else '%+.2f%%(胜%4.1f%%)' % (x['mean'], x['win'])
            print(' %-5s %-4s n=%2d 池均%-18s 旧Top5%-18s 新Top5%-18s 新Top5+8%%止盈/8%%止损%-18s' % (
                seg, k, r['n'], fm(r['pool']), fm(r['old']), fm(r['new']), fm(r['new_tp8'])))
    print('\n=== walk-forward（每截面只用历史IC选因子，n=%d 个有效截面）===' % ex['wf_panels'])
    for seg in ('train', 'test', 'all'):
        r = ex['walkforward'][seg]
        print(' %-5s 持有20日 %-22s +8%%止盈/8%%止损 %-22s 仅up市 %s' % (
            seg, str(r['hold20']), str(r['tp8']), str(r['up_only'])))
    print('\n=== 出场网格：训练段 Top8 → 同参数样本外表现（新评分Top5）===')
    for k in ex['exit_grid_train_top']:
        print(' %-18s 训练 %+6.2f%%  样本外 %+6.2f%%' % (
            k, ex['exit_grid_train'][k], ex['exit_grid_test'].get(k, float('nan'))))
    print('\n=== 移动止盈（不设固定止盈，保留上尾；按 train/test 较差者排序）===')
    for k, a, b in ex['trail_rank']:
        print(' %-16s 训练 %+6.2f%%  样本外 %+6.2f%%' % (k, a, b))
    print('\n=== walk-forward 评分 + 8%%移动止盈/8%%止损/20日 ===')
    for seg in ('train', 'test', 'all'):
        print(' %-5s %s' % (seg, ex['wf_trail'][seg]))
    print('\n=== walk-forward 最新一次入选因子 ===')
    for x in ex['wf_final_factors']:
        print('  %-12s sign=%+d w=%.4f' % (x['factor'], x['sign'], x['w']))
    print('\n=== 当前市场状态 ===')
    print(' %s · 沪深300 收盘%sMA20 · 近20日 %+.2f%%' % (
        latest, '>' if current['regime_now']['above_ma20'] else '<=',
        current['regime_now']['idx_c20']))
    print('\n=== 当前池内 · walk-forward 稳健分 Top12 ===')
    for x in current['by_wf']:
        print(' %-9s %-7s WF%+6.2f 旧第%3d 弹性第%3d 收%7.2f 20日%+6.1f%% 60日%+6.1f%%'
              ' 距60高%+6.1f%% 区间位%5.1f 连涨%4.0f%% 额%6.0f' % (
                  x['code'], x['name'], x['wf_score'], x['old_rank'], x['elastic_rank'],
                  x['c'], x['c20'], x['c60'], x['dist_hi60'], x['pos20'], x['up20'], x['amt20']))
    print('\n=== 两套都靠前（稳健×高弹性交集）===')
    for x in current['intersect']:
        print(' %-9s %-7s WF第%2d 弹性第%2d 旧第%3d 收%7.2f 20日%+6.1f%% 60日%+6.1f%% '
              '距60高%+5.1f%% 波动%4.2f EMA50=%6.2f' % (
                  x['code'], x['name'], x['wf_rank'], x['elastic_rank'], x['old_rank'],
                  x['c'], x['c20'], x['c60'], x['dist_hi60'], x['vol20'], x['e50']))
    print('\n结果已写入 %s' % OUT)


if __name__ == '__main__':
    main()
