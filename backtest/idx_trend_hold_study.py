# -*- coding: utf-8 -*-
"""
指数 EXPMA 乖离的「回归/反弹」尺度检验 + 模式B(EMA12>EMA50) 持有期与退出。

三关纪律（用户 2026-11-07 定）：
  1) 基准对比：任何收益必须对比「同期任意日买入、持有同样天数」的基准；
  2) 无未来函数：信号当日收盘确认、次日开盘成交，退出信号次日开盘成交；
  3) 分年份稳健性：按年拆，报告正收益年数、最强两年贡献占比。
  未过三关的一律标注「描述」，不写成操作建议。

口径：
  d12 = close/EMA12-1, d50 = close/EMA50-1, gap = EMA12/EMA50-1
  描述性统计用 close→close 前瞻收益；交易检验用 open 成交（次日开盘）。
  有效样本 V = [60, n-120)，剔除 EMA 未收敛段与无完整未来收益段。

用法：
  python3 idx_trend_hold_study.py            # 重算沪深300并写 idx_trend_hold_result.json
  python3 idx_trend_hold_study.py sh000001  # 写 idx_trend_hold_sh000001.json
  from idx_trend_hold_study import cached; data = cached('sh000300')
"""
import json, statistics as st, math, os

HERE = os.path.dirname(os.path.abspath(__file__))
INDICES = os.path.join(HERE, 'indices')
NAMES = {'sh000001': '上证指数', 'sh000300': '沪深300', 'sz399006': '创业板指',
         'sh000688': '科创50', 'bj899050': '北证50'}

BINS = [(-99, -8), (-8, -6), (-6, -5), (-5, -4), (-4, -3), (-3, -2),
        (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 4), (4, 99)]
HOR = [1, 3, 5, 10, 20, 40, 60, 120]
HOLDS = [5, 10, 20, 40, 60, 120]


def ema(v, p):
    k = 2 / (p + 1)
    e = [v[0]]
    for x in v[1:]:
        e.append(e[-1] + k * (x - e[-1]))
    return e


def mean(a):
    return st.mean(a) if a else 0.0


def r(x, d=2):
    return round(x, d)


def bin_label(b):
    a, z = b
    if a <= -99:
        return '≤-8'
    if z >= 99:
        return '≥+4'
    return '%g~%g' % (a, z)


def which_bin(v):
    for j, (a, z) in enumerate(BINS):
        if a <= v < z:
            return j
    return len(BINS) - 1


def stdev(a):
    return st.pstdev(a) if len(a) > 1 else 0.0


def build(code='sh000300'):
    path = os.path.join(INDICES, code + '.json')
    D = json.load(open(path))
    dates = [x[0] for x in D]
    o = [x[1] for x in D]
    c = [x[2] for x in D]
    n = len(c)
    if n < 300:
        raise ValueError('样本不足：%s 仅 %d 根' % (code, n))
    e12, e50 = ema(c, 12), ema(c, 50)
    d12 = [(c[i] / e12[i] - 1) * 100 for i in range(n)]
    d50 = [(c[i] / e50[i] - 1) * 100 for i in range(n)]
    gap = [(e12[i] / e50[i] - 1) * 100 for i in range(n)]
    golden = [e12[i] > e50[i] for i in range(n)]
    V = list(range(60, n - 120))
    fwd = {k: [(c[i + k] / c[i] - 1) * 100 if i + k < n else None for i in range(n)] for k in HOR}
    base = {k: mean([fwd[k][i] for i in V]) for k in HOR}
    basewin = {k: sum(1 for i in V if fwd[k][i] > 0) / len(V) * 100 for k in HOR}
    base_std5 = stdev([fwd[5][i] for i in V])
    base_down3 = sum(1 for i in V if (min(c[i + 1:i + 6]) / c[i] - 1) * 100 <= -3) / len(V) * 100

    out = {'code': code, 'name': NAMES.get(code, code), 'start': dates[0], 'end': dates[-1],
           'n': n, 'valid_n': len(V), 'bins': [{'label': bin_label(b), 'lo': b[0], 'hi': b[1]} for b in BINS],
           'base': {str(k): r(base[k]) for k in HOR},
           'basewin': {str(k): r(basewin[k], 1) for k in HOR},
           'base_std5': r(base_std5), 'base_down3': r(base_down3, 1),
           'discipline': ['基准对比：同期任意日买入、同持有期',
                          '无未来函数：收盘确认信号 / 次日开盘成交',
                          '分年份稳健性：正收益年数 + 最强两年贡献']}

    # ---------- 0. 当前定位（纯描述，不写操作建议） ----------
    t = n - 1
    pct = lambda arr, v: r(sum(1 for x in arr if x <= v) / len(arr) * 100, 1)
    d12v = [d12[i] for i in V]
    d50v = [d50[i] for i in V]
    gapv = [gap[i] for i in V]
    out['current'] = {'date': dates[t], 'close': r(c[t]), 'd12': r(d12[t]), 'd50': r(d50[t]),
                      'gap': r(gap[t]), 'bin': which_bin(d12[t]),
                      'pct_d12': pct(d12v, d12[t]), 'pct_d50': pct(d50v, d50[t]),
                      'pct_gap': pct(gapv, gap[t]), 'golden': bool(golden[t])}

    # ---------- 1. 多尺度：d12 分箱 → 各持有期超额 + 分年份稳健性 ----------
    scale = []
    for a, b in BINS:
        s = [i for i in V if a <= d12[i] < b]
        if len(s) < 8:
            continue
        row = {'bin': [a, b], 'label': bin_label((a, b)), 'idx': which_bin((a + b) / 2 if b < 99 else 5),
               'n': len(s)}
        for k in HOR:
            m = mean([fwd[k][i] for i in s])
            w = sum(1 for i in s if fwd[k][i] > 0) / len(s) * 100
            row[str(k)] = {'ret': r(m), 'ex': r(m - base[k]), 'win': r(w, 1)}
        byyear = {}
        for i in s:
            byyear.setdefault(dates[i][:4], []).append(i)
        gates = {}
        for k in HOR:
            vals = [mean([fwd[k][i] for i in idxs]) - base[k] for y, idxs in sorted(byyear.items())]
            gates[str(k)] = {'years': len(vals), 'pos_years': sum(1 for x in vals if x > 0)}
        row['gates'] = gates
        scale.append(row)
    out['scale'] = scale

    # ---------- 2. 「均线会回归」的机制拆解：收敛来自价涨还是线下移 ----------
    mech = []
    for a, b in [(-99, -8), (-8, -6), (-6, -5), (-5, -4), (-4, -3)]:
        s = [i for i in V if a <= d12[i] < b]
        if len(s) < 8:
            continue
        rows = []
        for i in s:
            if i + 10 >= n:
                continue
            dc = (c[i + 10] / c[i] - 1) * 100
            de = (e12[i + 10] / e12[i] - 1) * 100
            rows.append((dc, de, d12[i + 10] - d12[i], c[i + 10] > c[i]))
        price_up = sum(1 for x in rows if x[0] > 0) / len(rows) * 100
        ema_down = sum(1 for x in rows if x[1] < 0) / len(rows) * 100
        fake = sum(1 for x in rows if x[3] and x[1] < 0 and x[1] < x[0]) / len(rows) * 100
        pchg = mean([x[0] for x in rows])
        echg = mean([x[1] for x in rows])
        denom = abs(pchg) + abs(echg)
        mech.append({'bin': [a, b], 'label': bin_label((a, b)), 'n': len(rows),
                     'd12_conv': r(mean([x[2] for x in rows])), 'price_chg': r(pchg),
                     'ema12_chg': r(echg), 'ema_share': r(abs(echg) / denom * 100, 1) if denom else 0,
                     'price_up_pct': r(price_up, 1), 'ema_down_pct': r(ema_down, 1),
                     'fake_rebound_pct': r(fake, 1)})
    out['mechanism'] = mech

    # ---------- 3. 「乖离多少会反弹」：5 日内最大涨/跌幅命中率 + 波动 ----------
    reb = []
    for a, b in BINS:
        s = [i for i in V if a <= d12[i] < b]
        if len(s) < 8:
            continue
        row = {'bin': [a, b], 'label': bin_label((a, b)), 'n': len(s),
               'std5': r(stdev([fwd[5][i] for i in s])),
               'std5_ex': r(stdev([fwd[5][i] for i in s]) - base_std5)}
        for X in (2, 3, 5):
            hit = [i for i in s if (max(c[i + 1:i + 6]) / c[i] - 1) * 100 >= X]
            bh = [i for i in V if (max(c[i + 1:i + 6]) / c[i] - 1) * 100 >= X]
            row['x' + str(X)] = {'hit': r(len(hit) / len(s) * 100, 1),
                                 'base': r(len(bh) / len(V) * 100, 1),
                                 'ex': r(len(hit) / len(s) * 100 - len(bh) / len(V) * 100, 1)}
        dn = sum(1 for i in s if (min(c[i + 1:i + 6]) / c[i] - 1) * 100 <= -3) / len(s) * 100
        row['down3'] = {'hit': r(dn, 1), 'base': r(base_down3, 1), 'ex': r(dn - base_down3, 1)}
        reb.append(row)
    out['rebound'] = reb

    # ---------- 4. 趋势中乖离的作用：仅在 golden 为真时按 d12 分箱 ----------
    G = [i for i in V if golden[i]]
    trend = []
    for a, b in [(-99, -3), (-3, -1), (-1, 1), (1, 2), (2, 4), (4, 99)]:
        s = [i for i in G if a <= d12[i] < b]
        if len(s) < 8:
            continue
        row = {'bin': [a, b], 'label': bin_label((a, b)), 'n': len(s)}
        for k in (5, 10, 20, 60, 120):
            m = mean([fwd[k][i] for i in s])
            g = mean([fwd[k][i] for i in G])
            row[str(k)] = {'ret': r(m), 'vs_trend': r(m - g), 'vs_base': r(m - base[k])}
        trend.append(row)
    state = {}
    byyear_g = {}
    for i in G:
        byyear_g.setdefault(dates[i][:4], []).append(i)
    for k in HOR:
        m = mean([fwd[k][i] for i in G])
        vals = [mean([fwd[k][i] for i in idxs]) - base[k] for y, idxs in sorted(byyear_g.items())]
        state[str(k)] = {'n': len(G), 'ret': r(m), 'ex': r(m - base[k]),
                         'win': r(sum(1 for i in G if fwd[k][i] > 0) / len(G) * 100, 1),
                         'years': len(vals), 'pos_years': sum(1 for x in vals if x > 0)}
    out['in_trend'] = {'golden_n': len(G), 'rows': trend, 'state': state}

    # ---------- 5. 模式B 金叉事件：持有期 × 退出规则（无未来函数） ----------
    ev = [i for i in range(60, n - 121) if e12[i - 1] <= e50[i - 1] and e12[i] > e50[i]]
    ev2 = []
    for i in ev:
        if not ev2 or i - ev2[-1] >= 15:
            ev2.append(i)
    ev = ev2

    def trade(i, rule, N):
        """i=金叉日；次日开盘买；退出信号当日收盘判定、次日开盘卖；最多持有 N 日。"""
        b = i + 1
        if b >= n - 1:
            return None
        buy = o[b]
        tp, sl, exline, trail = rule.get('tp'), rule.get('sl'), rule.get('ex'), rule.get('trail')
        peak = buy
        for j in range(b + 1, min(b + N, n - 2) + 1):
            peak = max(peak, c[j])
            if tp and (c[j] / buy - 1) * 100 >= tp:
                return (o[j + 1] / buy - 1) * 100, j - b
            if sl and (c[j] / buy - 1) * 100 <= -sl:
                return (o[j + 1] / buy - 1) * 100, j - b
            if trail and peak > buy and (c[j] / peak - 1) * 100 <= -trail:
                return (o[j + 1] / buy - 1) * 100, j - b
            if exline == 'e12' and c[j] < e12[j]:
                return (o[j + 1] / buy - 1) * 100, j - b
            if exline == 'e50' and c[j] < e50[j]:
                return (o[j + 1] / buy - 1) * 100, j - b
            if exline == 'e12_2' and j > b + 1 and c[j] < e12[j] and c[j - 1] < e12[j - 1]:
                return (o[j + 1] / buy - 1) * 100, j - b
        j = min(b + N, n - 2)
        return (o[j + 1] / buy - 1) * 100, j - b

    rules = [
        ('固定持有', {}),
        ('跌破EMA12退出', {'ex': 'e12'}),
        ('连续2日破EMA12', {'ex': 'e12_2'}),
        ('跌破EMA50退出', {'ex': 'e50'}),
        ('止盈+5%', {'tp': 5}),
        ('止盈+8%', {'tp': 8}),
        ('止损-5%', {'sl': 5}),
        ('止盈8/止损5', {'tp': 8, 'sl': 5}),
        ('止盈5/止损3', {'tp': 5, 'sl': 3}),
        ('移动止损5%', {'trail': 5}),
        ('破EMA12+止盈8', {'ex': 'e12', 'tp': 8}),
    ]
    grid = []
    for N in HOLDS:
        bN = mean([(o[min(i + 1 + N, n - 2) + 1] / o[i + 1] - 1) * 100 for i in V if i + 1 + N < n - 2])
        for nm, rule in rules:
            if rule and N <= 5:
                continue
            res = [trade(i, rule, N) for i in ev]
            pairs = [(i, x) for i, x in zip(ev, res) if x]
            if len(pairs) < 8:
                continue
            rets = [x[0] for i, x in pairs]
            byyear = {}
            for i, x in pairs:
                byyear.setdefault(dates[i][:4], []).append(x[0])
            ys = sorted(byyear)
            contrib = sorted(((mean(byyear[y]) * len(byyear[y]), y) for y in ys), reverse=True)
            tot = sum(rets)
            grid.append({'hold': N, 'rule': nm, 'n': len(rets), 'ret': r(mean(rets)),
                         'ex': r(mean(rets) - bN),
                         'win': r(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
                         'avg_days': r(mean([x[1] for i, x in pairs]), 1),
                         'years': len(ys), 'pos_years': sum(1 for y in ys if mean(byyear[y]) > 0),
                         'top2_pct': r(sum(x for x, _ in contrib[:2]) / tot * 100, 1) if tot > 0 else None,
                         'worst': r(min(rets), 1), 'best': r(max(rets), 1)})
        out.setdefault('hold_base', {})[str(N)] = r(bN)
    out['events'] = {'n': len(ev), 'dates': [dates[i] for i in ev]}
    out['rules'] = [x[0] for x in rules]
    out['grid'] = grid
    out['event_fixed'] = {str(g['hold']): g['ex'] for g in grid if g['rule'] == '固定持有'}
    # 三关筛选（ex>0、正收益年数≥60%、n≥15、最强两年贡献<100%）
    passed = [g for g in grid if g['ex'] > 0 and g['pos_years'] >= max(3, int(g['years'] * 0.6))
              and g['n'] >= 15 and g['top2_pct'] is not None and g['top2_pct'] < 100]
    passed.sort(key=lambda x: -x['ex'])
    out['passed'] = passed[:12]

    # ---------- 5b. golden 状态内任意日入场（重叠样本，仅描述） ----------
    # 与 5 的区别：5 只在「金叉事件日」入场；这里在「状态持续的每一天」入场。
    # 样本互相重叠，不能当独立样本做显著性判断，只能比较退出规则之间的相对差异。
    sexits = []
    for N in (20, 40, 60, 120):
        bN = out['hold_base'][str(N)]
        for nm, rule in rules:
            res = [trade(i, rule, N) for i in G]
            res = [x for x in res if x is not None]
            if len(res) < 30:
                continue
            rets = [x[0] for x in res]
            sexits.append({'hold': N, 'rule': nm, 'n': len(rets), 'ret': r(mean(rets)),
                           'ex': r(mean(rets) - bN),
                           'win': r(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
                           'avg_days': r(mean([x[1] for x in res]), 1),
                           'worst': r(min(rets), 1), 'best': r(max(rets), 1)})
    out['state_exits'] = sexits
    out['state_exits_meta'] = {'n': len(G), 'note': '状态内每日入场，样本重叠；只比较规则相对差异，不做显著性判断'}

    # ---------- 6. 稳健性：深乖离的年份结构 + 前后半段样本外式拆分 ----------
    deep = [i for i in V if d12[i] < -8]
    byyear = {}
    for i in deep:
        byyear[dates[i][:4]] = byyear.get(dates[i][:4], 0) + 1
    end_year = int(dates[-1][:4])
    out['deep_years'] = {'rows': [{'year': y, 'n': byyear[y]} for y in sorted(byyear)],
                         'total': len(deep),
                         'recent10': sum(v for y, v in byyear.items() if int(y) >= end_year - 10),
                         'since': str(end_year - 10)}
    mid = V[len(V) // 2]
    oos = {}
    for tag, s in [('first', [i for i in deep if i < mid]), ('second', [i for i in deep if i >= mid])]:
        oos[tag] = {'n': len(s), 'from': dates[s[0]] if s else None, 'to': dates[s[-1]] if s else None,
                    'ex': {str(k): r(mean([fwd[k][i] for i in s]) - base[k]) if s else None for k in HOR}}
    out['oos'] = oos
    return out


_CACHE = {}


def cached(code='sh000300'):
    """按 (指数代码, 数据文件 mtime) 缓存，避免每次请求重算。"""
    path = os.path.join(INDICES, code + '.json')
    key = (code, os.path.getmtime(path))
    if _CACHE.get(code, (None,))[0] != key:
        _CACHE[code] = (key, build(code))
    return _CACHE[code][1]


if __name__ == '__main__':
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else 'sh000300'
    d = build(code)
    fn = 'idx_trend_hold_result.json' if code == 'sh000300' else 'idx_trend_hold_%s.json' % code
    json.dump(d, open(os.path.join(HERE, fn), 'w'), ensure_ascii=False)
    print('已输出 backtest/%s · %s %s~%s · 有效样本 %d 日' % (fn, d['name'], d['start'], d['end'], d['valid_n']))
