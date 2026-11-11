#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 EXPMA池 · 剔除规则对比回测
入场逻辑完全一致（严格口径入池 + 每周Top5评分轮换），只变剔除规则：
  A 现行线上：收盘跌破EMA12×0.98（缓冲带） 或 EMA12下穿EMA50 或 双线斜率走平向下 → 当日剔除
  B 用户规则：收盘跌破EMA50，连续2日收不回来 → 第2日剔除（中间收回则重新计数）
  C 混合：B + 结构破坏EMA12≤EMA50（2日确认）
入场=入选周收盘价；离场=剔除日收盘价。等权每笔。
"""
import json, os, datetime
from bisect import bisect_right
from collections import defaultdict
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = _REPO
KL = os.path.join(BASE, 'backtest/klines')
IDX = os.path.join(BASE, 'backtest/indices')
CONS = os.path.join(BASE, 'webapp/hs300_cons.json')

def ema(vals, n):
    a = 2.0 / (n + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * a + e * (1 - a)
        out.append(e)
    return out

def load(code):
    try:
        rows = json.load(open(os.path.join(KL, code + '.json')))
    except Exception:
        return None
    if not rows or len(rows) < 60:
        return None
    d = [r[0] for r in rows]
    c = [float(r[2]) for r in rows]   # [date, open, close, high, low, ...]
    return {'d': d, 'c': c, 'e12': ema(c, 12), 'e50': ema(c, 50)}

def main():
    cons = json.load(open(CONS))['map']
    stocks = {}
    for c6, name in cons.items():
        code = ('sh' if c6[0] == '6' else 'sz') + c6
        k = load(code)
        if k:
            stocks[code] = dict(k, name=name)
    idx = json.load(open(os.path.join(IDX, 'sh000300.json')))
    idx_d = [r[0] for r in idx]
    idx_c = [float(r[2]) for r in idx]
    print('成份股有数据: %d/300 · 区间 %s ~ %s (%d根)' % (
        len(stocks), idx_d[0], idx_d[-1], len(idx_d)))

    # 所有交易日全集（取并集，个股停牌日用前收盘）
    all_days = sorted({d for s in stocks.values() for d in s['d']})
    di = {d: i for i, d in enumerate(all_days)}
    for s in stocks.values():
        s['pos'] = {d: i for i, d in enumerate(s['d'])}

    def bar(s, day):
        """最近已收盘bar索引（<=day，无状态二分，可安全多次回放）"""
        return bisect_right(s['d'], day) - 1

    # 周分组（按 all_days 的 ISO 周）
    weeks = defaultdict(list)
    for d in all_days:
        y, w, _ = datetime.date.fromisoformat(d).isocalendar()
        weeks['%dW%02d' % (y, w)].append(d)
    week_keys = sorted(weeks)

    def bench_ret(d0, d1):
        try:
            i0 = idx_d.index(d0); i1 = idx_d.index(d1)
        except ValueError:
            return 0.0
        return (idx_c[i1] / idx_c[i0] - 1) * 100

    def run(variant):
        """variant: 'A' | 'B' | 'C'"""
        members = {}      # code -> dict(since, below50_cnt)
        trades = []       # {code, name, in_day, in_px, out_day, out_px, hold}
        open_pos = {}     # code -> trade dict
        picks_prev = None
        below = defaultdict(int)   # code -> 连续收在EMA50下方天数
        below12 = defaultdict(int) # C: e12<=e50 连续天数
        for wi, wk in enumerate(week_keys):
            days = weeks[wk]
            # --- 每日剔除检查 ---
            for day in days:
                for code in list(members):
                    s = stocks[code]
                    t = bar(s, day)
                    if t < 12:
                        continue
                    c, e12, e50 = s['c'][t], s['e12'][t], s['e50'][t]
                    evict = False
                    if variant in ('A', 'D'):
                        s12 = s['e12'][t] / s['e12'][t - 5] - 1
                        s50 = s['e50'][t] / s['e50'][t - 10] - 1
                        hard = c <= e12 * 0.98
                        struct = e12 <= e50 or s12 <= 0 or s50 <= 0
                        evict = hard or (struct if variant == 'A' else False)
                        if evict:
                            below[code] = 0
                    elif variant == 'E':
                        evict = c < e50
                        if not evict:
                            below[code] = 0
                    else:
                        below[code] = below[code] + 1 if c < e50 else 0
                        if variant == 'B':
                            evict = below[code] >= 2
                        elif variant == 'B3':
                            evict = below[code] >= 3
                        elif variant in ('D2', 'D3'):
                            # 线上口径：①硬止损 EMA12×0.98 当日 ②EMA50 连续N日不收回（含跌破当日为第1日）
                            evict = c <= e12 * 0.98 or below[code] >= (2 if variant == 'D2' else 3)
                        elif variant == 'N3':
                            # 新口径（用户 2026-11-04）：只按 EMA50 三日不收回，彻底拿掉 EMA12 硬止损
                            evict = below[code] >= 3
                        else:  # C
                            below12[code] = below12[code] + 1 if e12 <= e50 else 0
                            evict = below[code] >= 2 or below12[code] >= 2
                    if evict:
                        if code in open_pos:
                            tr = open_pos.pop(code)
                            tr['out_day'], tr['out_px'] = day, c
                            tr['hold'] = di[day] - di[tr['in_day']]
                            tr['ret'] = (c / tr['in_px'] - 1) * 100
                            trades.append(tr)
                        del members[code]
            # --- 周末选股（每周最后交易日收盘） ---
            last = days[-1]
            cand = []
            for code, s in stocks.items():
                t = bar(s, last)
                if t < 60:
                    continue
                c, e12, e50 = s['c'][t], s['e12'][t], s['e50'][t]
                s12 = (e12 / s['e12'][t - 5] - 1) * 100
                s50 = (e50 / s['e50'][t - 10] - 1) * 100
                ok = c > e12 > e50 and s12 > 0 and s50 > 0
                if ok:
                    members.setdefault(code, {'since': last})
                    d12 = (c / e12 - 1) * 100
                    c20 = (c / s['c'][t - 20] - 1) * 100
                    b20 = bench_ret(all_days[max(0, di[last] - 20)], last)
                    score = (min(30.0, max(0.0, s12 * 20))
                             + (25 if 0 <= d12 <= 3 else 15 if d12 <= 6 else 5 if d12 <= 12 else 0)
                             + (25 if 5 <= c20 <= 25 else 15 if 0 <= c20 < 5 or 25 < c20 <= 40 else 5 if c20 <= 60 else 0)
                             + 7  # 流动性桶（统一取中档，口径一致即可）
                             + min(10.0, max(0.0, (c20 - b20) / 3)))
                    cand.append((score, code, c, last))
            cand.sort(reverse=True)
            picks = {code for _, code, _, _ in cand[:5]}
            for sc, code, px, day in cand[:5]:
                if code not in open_pos:
                    open_pos[code] = {'code': code, 'name': stocks[code]['name'],
                                      'in_day': day, 'in_px': px, 'score': sc}
            # 换周被挤出Top5但未触发剔除 → 按线上口径：池内保留、继续持有到剔除信号
            # （open_pos 不动，只有剔除才离场）
            _ = picks_prev
        # 期末未平仓按最后收盘估值
        last_day = all_days[-1]
        for code, tr in open_pos.items():
            s = stocks[code]
            t = bar(s, last_day)
            c = s['c'][t]
            tr['out_day'], tr['out_px'] = last_day, c
            tr['hold'] = di[last_day] - di[tr['in_day']]
            tr['ret'] = (c / tr['in_px'] - 1) * 100
            tr['open'] = True
            trades.append(tr)
        return trades

    def stats(name, trades):
        if not trades:
            print('%s: 无交易' % name); return
        rets = [t['ret'] for t in trades]
        win = sum(1 for r in rets if r > 0)
        avg = sum(rets) / len(rets)
        # 等权复利
        eq = 1.0
        peak, mdd = 1.0, 0.0
        for r in rets:
            eq *= (1 + r / 100)
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        holds = [t['hold'] for t in trades]
        # 鞭打：离场后10个交易日内又重新入选
        whipsaw = 0
        for i, t in enumerate(trades):
            if t.get('open'):
                continue
            for u in trades:
                if u['code'] == t['code'] and u is not t and 0 <= di.get(u['in_day'], -1) - di.get(t['out_day'], 0) <= 10:
                    whipsaw += 1
                    break
        print('%s | 笔数%3d 胜率%2d%% 笔均%+.2f%% 中位%+.2f%% 复利%+.1f%% 最大回撤%.1f%% '
              '均持%2d日 最差%+.1f%% 鞭打%d' % (
                  name, len(rets), round(win / len(rets) * 100), avg,
                  sorted(rets)[len(rets) // 2], (eq - 1) * 100, mdd * 100,
                  sum(holds) / len(holds), min(rets), whipsaw))

    print('\n区间: %s ~ %s · 基准沪深300: %+.1f%%' % (
        all_days[0], all_days[-1], bench_ret(all_days[0], all_days[-1])))
    print('-' * 110)
    res = {}
    for v, label in (('A', 'A 现行:破EMA12×0.98/斜率坏'), ('B', 'B 用户:破EMA50两日不收回'),
                     ('C', 'C 混合:B+EMA12≤EMA50两日'), ('D', 'D 新:硬止损EMA12×0.98+EMA50两日'),
                     ('E', 'E 敏感:破EMA50当日即剔'),
                     ('D2', 'D2 线上旧:硬止损+EMA50两日(含当日)'),
                     ('D3', 'D3 线上新:硬止损+EMA50三日(含当日)'),
                     ('N3', 'N3 本次改:仅EMA50三日·无EMA12硬止损')):
        tr = run(v)
        res[v] = tr
        stats(label, tr)
    print('-' * 110)
    # 明细：各规则下最伤的5笔
    for v in ('A', 'B', 'C', 'D', 'E', 'D2', 'D3', 'N3'):
        worst = sorted(res[v], key=lambda t: t['ret'])[:3]
        print('\n[%s] 最差3笔:' % v)
        for t in worst:
            print('  %s %s  %s→%s(%d日)  %+.1f%%' % (
                t['code'], t['name'], t['in_day'], t['out_day'], t['hold'], t['ret']))
    json.dump({v: [{k: t.get(k) for k in ('code', 'name', 'in_day', 'in_px', 'out_day', 'out_px', 'hold', 'ret', 'open')} for t in res[v]] for v in res},
              open(os.path.join(BASE, 'backtest/hs300_evict_bt.json'), 'w'), ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main()
