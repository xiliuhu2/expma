#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线穿阳 · 触发日指数状态 × 介入时机 条件回测（T+1 口径）

用户假设：「三线穿阳是信号，介入时机要看当时指数情况」。
检验方法：把每一次「再放量破高」触发确认（原买点）落到**触发日**的指数状态网格上，
看哪类环境值得介入、哪类环境应该放弃或等待。

指数状态因子（全部只用触发日及之前数据，无未来函数）：
  idx_pos5   上证收盘距 MA20 位置：(C/MA20-1)%，分桶 <-5 / -5~-2 / -2~+2 / +2~+5 / >+5
  idx_mom5   上证 5 日累计涨跌 %：<-2 / -2~0 / 0~+2 / >+2
  idx_mom20  上证 20 日累计涨跌 %：<-6 / -6~0 / 0~+6 / >+6
  idx_ma5    上证 MA5 相对昨日 MA5 的方向（上/下）
  idx_vol    上证量比（当日量 / 前5日均量）：<0.9 / 0.9~1.1 / >1.1
  idx_gap    上证当日开盘缺口（O/C_prev-1）：<-1% / -1~+1 / >+1  ——触发日大盘跳空方向

同时检验「等待类」介入时机（变量因子调整）：
  base       触发次日开盘买（现行 T+2=9:30 开盘，基准）
  wait_pull  不追：等待回踩 EXPMA12 收回再买（最多 10 日，未触发放弃）
  wait_ma5   等指数回踩 MA5 后个股再放量突破当日高（最多 10 日）
  weak_skip  弱市（上证<MA20）时跳过该信号，强市照常 base

撮合：T+1（买入当日不可卖，止盈/止损从次日起判），成本 0.25%/笔；
退出网格：+2.5% / +4% 止盈 × 不止损 / -8% 止损 × 15 / 20 日。
输出：backtest/cross3_idx_timing_result.json
"""
import json, os, statistics, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, '..', 'webapp')
sys.path.insert(0, WEB)
sys.path.insert(0, HERE)
import server as S
import cross3_entry_mining as EM
import cross3_80win_mining as M

OUT = os.path.join(HERE, 'cross3_idx_timing_result.json')
CUT = '2026-07-06'
COST = 0.25


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def bucket(v, edges):
    for i, e in enumerate(edges):
        if v < e:
            return i
    return len(edges)


def sim_t1(s, bi, X, Y, N):
    """与 EM.simulate 相同但显式 T+1：止盈/止损从 bi+1 起（EM 已修，这里独立实现防回归）。"""
    kk = s['kk']
    h, l, c, o = kk['h'], kk['l'], kk['c'], kk['o']
    if bi is None or bi + N > s['last'] + 1:
        return None
    buy = o[bi]
    if buy <= 0:
        return None
    tp = buy * (1 + X / 100)
    sl = buy * (1 - Y / 100) if Y < 999 else -1
    for k in range(bi + 1, bi + N):
        if sl > 0 and l[k] <= sl:
            return -Y - COST, 'stop'
        if h[k] >= tp:
            return X - COST, 'tp'
    return pct(c[bi + N - 1], buy) - COST, 'time'


def build_idx_features(D=None):
    if D is None:
        D = S.Data()
    ix = D.indices.get('sh000001')
    c, o, dates = ix['c'], ix['o'], ix['dates']
    # 上证量能从 indices/sh000001.json 原始文件读（Data 缓存只保留 o/c/h/l/e12/e50）
    raw = json.load(open(os.path.join(HERE, 'indices', 'sh000001.json')))
    vd = {r[0]: r[5] for r in raw}
    v = [vd.get(d, 0) for d in dates]
    F = {}
    for i, d in enumerate(dates):
        if i < 25:
            continue
        ma20 = sum(c[i - 19:i + 1]) / 20
        ma5 = sum(c[i - 4:i + 1]) / 5
        ma5p = sum(c[i - 5:i]) / 5          # 昨日 MA5（c[i-5..i-1]）
        v5 = sum(v[i - 5:i]) / 5
        F[d] = dict(
            pos5=pct(c[i], ma20),
            mom5=pct(c[i], c[i - 5]),
            mom20=pct(c[i], c[i - 20]),
            ma5_up=1 if ma5 >= ma5p else 0,
            vr=v[i] / v5 if v5 > 0 else 1.0,
            gap=pct(o[i], c[i - 1]),
            above=c[i] > ma20,
        )
    return F


def main():
    EM.NEED = EM.W + 20 + 3
    D, sigs = EM.build_signals()
    idxF = build_idx_features(D)
    print('信号 %d | 指数特征 %d 日 | 切分 %s' % (len(sigs), len(idxF), CUT), flush=True)

    # ---- 事件集：A 级信号 + 再放量破高触发日 ----
    events = []
    for s in sigs:
        if not (s['brk20'] and s['ma89_up'] > 0):
            continue
        bi = M.find_buy(s, '再放量破信号日高')
        if bi is None:
            continue
        t_trig = bi - 1                      # 触发确认日 = 买入日的前一根
        d_trig = s['kk']['dates'][t_trig]
        f = idxF.get(d_trig)
        if f is None:
            continue
        events.append(dict(s=s, bi=bi, d_trig=d_trig, f=f))
    print('触发事件 %d 个（触发日 2025-11~2026-08，T+1 口径）' % len(events), flush=True)

    EXITS = [(2.5, 999, 20), (4, 999, 20), (2.5, 8, 20), (4, 8, 20), (2.5, 999, 15)]

    def stat(rows):
        if not rows:
            return None
        n = len(rows)
        vals = [r for r, _ in rows]
        sr = sorted(vals)
        return dict(n=n,
                    win=round(sum(x > 0 for x in vals) / n * 100, 1),
                    mean=round(statistics.mean(vals), 2),
                    p05=round(sr[int(n * .05)], 2),
                    worst=round(sr[0], 2))

    def split_tag(e):
        return 'train' if e['d_trig'] < CUT else 'oos'

    # ---------- 一、触发日指数状态分桶（基准介入：次日开盘买） ----------
    grids = {
        'pos5（距MA20%）': ('pos5', [-5, -2, 2, 5]),
        'mom5（5日涨跌%）': ('mom5', [-2, 0, 2]),
        'mom20（20日涨跌%）': ('mom20', [-6, 0, 6]),
        'ma5方向': ('ma5_up', [0.5]),
        '量比': ('vr', [0.9, 1.1]),
        '开盘缺口%': ('gap', [-1, 1]),
        '上证>MA20': ('above', [0.5]),
    }
    out_idx = {}
    for gname, (field, edges) in grids.items():
        b = defaultdict(lambda: defaultdict(list))
        for e in events:
            k = bucket(e['f'][field], edges)
            for X, Y, N in EXITS:
                r = sim_t1(e['s'], e['bi'], X, Y, N)
                if r:
                    b[k][split_tag(e)].append(r)
                    b[k]['all'].append(r)
        labels = ['<%g' % edges[0]] + \
                 ['%g~%g' % (edges[i], edges[i + 1]) for i in range(len(edges) - 1)] + \
                 ['>=%g' % edges[-1]]
        rows = {}
        for k in sorted(b):
            lab = labels[k] if k < len(labels) else str(k)
            rows[lab] = dict(train=stat(b[k]['train']), oos=stat(b[k]['oos']),
                             all=stat(b[k]['all']))
        out_idx[gname] = rows

    # ---------- 二、介入时机（变量因子）对比 ----------
    def find_wait_pull(s, bi, max_wait=10):
        """触发确认后不追，等回踩 EXPMA12 后收回再买（次日开盘成交）。"""
        kk = s['kk']
        e12, c = kk['e12'], kk['c']
        for j in range(bi, min(bi + max_wait, s['last'])):
            if c[j] < e12[j] and j + 1 <= s['last']:
                # 回踩日 j 收盘破 EMA12，j+1 收盘收回 → j+2 开盘买
                if c[j + 1] >= e12[j + 1]:
                    return j + 2
        return None

    def find_idx_ma5_pull_then_break(s, bi, idxF, max_wait=10):
        """等指数回踩 MA5 后，个股再放量突破「本触发日最高价」当日确认，次日开盘买。"""
        kk = s['kk']
        c, h, v = kk['c'], kk['h'], kk['v']
        hi_trig = h[bi - 1]
        pulled = False
        for j in range(bi, min(bi + max_wait, s['last'])):
            d = kk['dates'][j]
            f = idxF.get(d)
            if f and not f['above']:
                pass
            # 指数回踩 MA5 近似：pos5 距 MA20 由正转 <0.5% 视为回踩（数据限制的近似）
            if f and f['pos5'] < 0.5:
                pulled = True
            if pulled and c[j] > hi_trig and S.vr_at(kk, j) >= 1.2 and j + 1 <= s['last']:
                return j + 1
        return None

    variants = {
        'base（触发次日开盘买）': lambda e: e['bi'],
        'wait_pull（等回踩EMA12收回）': lambda e: find_wait_pull(e['s'], e['bi']),
        'wait_ma5（等指数回踩+再破高）': lambda e: find_idx_ma5_pull_then_break(e['s'], e['bi'], idxF),
    }
    out_var = {}
    for vname, fn in variants.items():
        b = defaultdict(list)
        n_used = 0
        for e in events:
            bi = fn(e)
            if bi is None or bi > e['s']['last']:
                continue
            n_used += 1
            for X, Y, N in EXITS:
                r = sim_t1(e['s'], bi, X, Y, N)
                if r:
                    b[('all', (X, Y, N))].append(r)
                    b[(split_tag(e), (X, Y, N))].append(r)
        rows = {}
        for X, Y, N in EXITS:
            key = '+%g%%/%s/%d日' % (X, '不止损' if Y == 999 else '-%d%%' % Y, N)
            rows[key] = dict(train=stat(b[('train', (X, Y, N))]),
                             oos=stat(b[('oos', (X, Y, N))]),
                             all=stat(b[('all', (X, Y, N))]),
                             n_used=n_used)
        out_var[vname] = rows

    # ---------- 三、弱市跳过（weak_skip）：上证<MA20 不做 ----------
    b = defaultdict(list)
    n_skip = 0
    for e in events:
        if not e['f']['above']:
            n_skip += 1
            continue
        for X, Y, N in EXITS:
            r = sim_t1(e['s'], e['bi'], X, Y, N)
            if r:
                b[('train', (X, Y, N))].append(r) if e['d_trig'] < CUT else \
                    b[('oos', (X, Y, N))].append(r)
                b[('all', (X, Y, N))].append(r)
    rows = {}
    for X, Y, N in EXITS:
        key = '+%g%%/%s/%d日' % (X, '不止损' if Y == 999 else '-%d%%' % Y, N)
        rows[key] = dict(train=stat(b[('train', (X, Y, N))]),
                         oos=stat(b[('oos', (X, Y, N))]),
                         all=stat(b[('all', (X, Y, N))]))
    out_var['weak_skip（上证<MA20 不做）'] = dict(rows, __n_skipped=n_skip)

    # ---------- 四、组合条件（指数因子 × 介入/退出网格，正式结论） ----------
    combos = {
        '全部（基准）': lambda f: True,
        '上证>MA20': lambda f: f['above'],
        '指数量比>=1.1': lambda f: f['vr'] >= 1.1,
        'MA20上方且量比>=1.1': lambda f: f['above'] and f['vr'] >= 1.1,
        'MA20上方且量比>=0.9': lambda f: f['above'] and f['vr'] >= 0.9,
        'MA20上方且mom20 0~6': lambda f: f['above'] and 0 <= f['mom20'] < 6,
        '量比>=0.9且mom5>=0': lambda f: f['vr'] >= 0.9 and f['mom5'] >= 0,
    }
    GRID = [(2.5, 999, 20), (3, 999, 20), (4, 999, 20), (5, 999, 20),
            (4, 999, 15), (4, 999, 30), (2.5, 8, 20), (4, 8, 20)]
    out_combo = {}
    for cname, fn in combos.items():
        rows = {}
        for X, Y, N in GRID:
            tr, oos, al = [], [], []
            for e in events:
                if not fn(e['f']):
                    continue
                r = sim_t1(e['s'], e['bi'], X, Y, N)
                if not r:
                    continue
                al.append(r)
                (tr if e['d_trig'] < CUT else oos).append(r)
            key = '+%g%%/%s/%d日' % (X, '不止损' if Y == 999 else '-%d%%' % Y, N)
            rows[key] = dict(train=stat(tr), oos=stat(oos), all=stat(al))
        out_combo[cname] = rows

    # ---------- 输出 ----------
    res = dict(n_events=len(events), cut=CUT, cost=COST,
               exits=['+%g%%/%s/%d日' % (X, '不止损' if Y == 999 else '-%d%%' % Y, N)
                      for X, Y, N in EXITS],
               idx_grid=out_idx, variants=out_var, combos=out_combo,
               note='T+1 口径：止盈/止损从买入次日起判；全部因子仅用触发日及之前数据')
    json.dump(res, open(OUT, 'w'), ensure_ascii=False, indent=1)
    print('结果已写入 %s' % OUT, flush=True)

    # ---- 摘要打印 ----
    print('\n=== 一、触发日指数状态 × 全样本（+2.5%/20日） ===')
    for gname, rows in out_idx.items():
        print(' [%s]' % gname)
        for lab, x in rows.items():
            a = x['all'] or {}
            tr = x['train'] or {}
            oo = x['oos'] or {}
            print('   %-10s all n=%3d %5.1f%% %+6.2f | train n=%3d %5.1f%% %+6.2f | oos n=%3d %5.1f%% %+6.2f'
                  % (lab, a.get('n', 0), a.get('win', 0), a.get('mean', 0),
                     tr.get('n', 0), tr.get('win', 0), tr.get('mean', 0),
                     oo.get('n', 0), oo.get('win', 0), oo.get('mean', 0)))
    print('\n=== 二、介入时机对比（+2.5%/不止损/20日 与 +4%/不止损/20日） ===')
    for vname, rows in out_var.items():
        print(' [%s]' % vname)
        for key in ('+2.5%/不止损/20日', '+4%/不止损/20日'):
            x = rows.get(key) or {}
            a, tr, oo = x.get('all') or {}, x.get('train') or {}, x.get('oos') or {}
            print('   %-22s all n=%3d %5.1f%% %+6.2f | train %5.1f%% %+6.2f | oos %5.1f%% %+6.2f'
                  % (key, a.get('n', 0), a.get('win', 0), a.get('mean', 0),
                     tr.get('win', 0), tr.get('mean', 0), oo.get('win', 0), oo.get('mean', 0)))

    print('\n=== 四、组合条件 × 退出网格（train | oos | all：n 胜率 均值） ===')
    for cname, rows in out_combo.items():
        print(' [%s]' % cname)
        for key, x in rows.items():
            a, tr, oo = x['all'] or {}, x['train'] or {}, x['oos'] or {}
            print('   %-20s tr n=%3d %5.1f%% %+6.2f | oos n=%3d %5.1f%% %+6.2f | all n=%3d %5.1f%% %+6.2f'
                  % (key, tr.get('n', 0), tr.get('win', 0), tr.get('mean', 0),
                     oo.get('n', 0), oo.get('win', 0), oo.get('mean', 0),
                     a.get('n', 0), a.get('win', 0), a.get('mean', 0)))


if __name__ == '__main__':
    main()
