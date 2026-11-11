#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300 EXPMA 乖离与大底研究（离线计算模块，供 webapp/server.py 调用）。

口径（全部可复现）：
  d12 = close/EMA12 - 1      股价相对 EXPMA12 乖离
  d50 = close/EMA50 - 1      股价相对 EXPMA50 乖离
  gap = EMA12/EMA50 - 1      双线间距（负=空头排列）
  s12 / s50                  EMA12 五日 / EMA50 十日斜率
  dd20 / dd60                近 20 / 60 日相对区间最高收盘的回撤
  panic                      过去 60 日内 d12 的最深值（是否经历过急跌）
  f60 / f120                 未来 60 / 120 个交易日收盘收益
  mx120                      未来 120 日内最大浮盈

「大底」定义：前后各 20 日窗口内的最低收盘，且未来 120 日涨幅 >= 15%，
事件间隔 >= 40 个交易日去重。有效统计区间剔除前 60 根（EMA 未收敛）
与末尾 120 根（无完整未来收益）。

用法：python3 idx_bottom_study.py  → 打印摘要
      from idx_bottom_study import build; data = build('sh000300')
"""
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
INDICES = os.path.join(HERE, 'indices')

NAMES = {'sh000001': '上证指数', 'sh000300': '沪深300', 'sz399006': '创业板指',
         'sh000688': '科创50', 'bj899050': '北证50'}


def ema(vals, p):
    k = 2 / (p + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def build(code='sh000300'):
    path = os.path.join(INDICES, code + '.json')
    D = json.load(open(path))
    dates = [r[0] for r in D]
    c = [r[2] for r in D]
    n = len(c)
    e12, e50 = ema(c, 12), ema(c, 50)

    d12 = [(c[i] / e12[i] - 1) * 100 for i in range(n)]
    d50 = [(c[i] / e50[i] - 1) * 100 for i in range(n)]
    gap = [(e12[i] / e50[i] - 1) * 100 for i in range(n)]
    s12 = [(e12[i] / e12[i - 5] - 1) * 100 if i >= 5 else 0.0 for i in range(n)]
    s50 = [(e50[i] / e50[i - 10] - 1) * 100 if i >= 10 else 0.0 for i in range(n)]
    dd20 = [(c[i] / max(c[max(0, i - 19):i + 1]) - 1) * 100 for i in range(n)]
    dd60 = [(c[i] / max(c[max(0, i - 59):i + 1]) - 1) * 100 for i in range(n)]
    panic = [min(d12[max(0, i - 59):i + 1]) for i in range(n)]
    f60 = [(c[i + 60] / c[i] - 1) * 100 if i + 60 < n else None for i in range(n)]
    f120 = [(c[i + 120] / c[i] - 1) * 100 if i + 120 < n else None for i in range(n)]
    mx120 = [(max(c[i + 1:min(i + 121, n)]) / c[i] - 1) * 100 if i + 1 < n else None
             for i in range(n)]

    V = list(range(60, n - 120))          # 有效统计样本
    t = n - 1                              # 当前

    def pct(arr, v):
        s = [arr[i] for i in V]
        return round(sum(1 for x in s if x <= v) / len(s) * 100, 1)

    def quant(arr, p):
        s = sorted(arr)
        return round(s[min(len(s) - 1, int(p * len(s)))], 2)

    # ---------- 大底事件 ----------
    def is_low(i, w=20):
        return c[i] == min(c[max(0, i - w):min(n, i + w + 1)])

    cand = [i for i in V if is_low(i) and f120[i] >= 15]
    ev = []
    for i in sorted(cand, key=lambda x: -f120[x]):
        if all(abs(i - j) >= 40 for j in ev):
            ev.append(i)
    ev.sort()
    events = [{'i': i, 'date': dates[i], 'close': _r(c[i]), 'd12': _r(d12[i]),
               'd50': _r(d50[i]), 'gap': _r(gap[i]), 's12': _r(s12[i]),
               'dd20': _r(dd20[i]), 'f60': _r(f60[i], 1), 'f120': _r(f120[i], 1),
               'mx120': _r(mx120[i], 1)} for i in ev]

    ev_d12 = [d12[i] for i in ev]
    ev_d50 = [d50[i] for i in ev]
    ev_gap = [gap[i] for i in ev]
    ev_dd20 = [dd20[i] for i in ev]
    bands = {
        'd12': {'p25': quant(ev_d12, .25), 'p50': quant(ev_d12, .5),
                'p75': quant(ev_d12, .75), 'min': _r(min(ev_d12)), 'max': _r(max(ev_d12)),
                'mean': _r(st.mean(ev_d12))},
        'd50': {'p25': quant(ev_d50, .25), 'p50': quant(ev_d50, .5),
                'p75': quant(ev_d50, .75), 'min': _r(min(ev_d50)), 'max': _r(max(ev_d50)),
                'mean': _r(st.mean(ev_d50))},
        'gap': {'p25': quant(ev_gap, .25), 'p50': quant(ev_gap, .5),
                'p75': quant(ev_gap, .75), 'min': _r(min(ev_gap)), 'max': _r(max(ev_gap)),
                'mean': _r(st.mean(ev_gap))},
        'dd20': {'p25': quant(ev_dd20, .25), 'p50': quant(ev_dd20, .5),
                 'p75': quant(ev_dd20, .75), 'min': _r(min(ev_dd20)), 'max': _r(max(ev_dd20)),
                 'mean': _r(st.mean(ev_dd20))},
    }

    # ---------- 事件对齐平均形态 T=-30..+30 ----------
    shape = []
    for T in range(-30, 31):
        rows = [(d12[i + T], d50[i + T], gap[i + T], s12[i + T],
                 (c[i + T] / c[i] - 1) * 100) for i in ev if 0 <= i + T < n]
        shape.append({'t': T,
                      'd12': _r(st.mean([r[0] for r in rows])),
                      'd50': _r(st.mean([r[1] for r in rows])),
                      'gap': _r(st.mean([r[2] for r in rows])),
                      's12': _r(st.mean([r[3] for r in rows])),
                      'px': _r(st.mean([r[4] for r in rows]))})

    # ---------- 当前最近 30 日轨迹（对齐形态图用） ----------
    cur_path = []
    for T in range(-30, 1):
        i = t + T
        if i < 0:
            continue
        cur_path.append({'t': T, 'date': dates[i], 'd12': _r(d12[i]), 'd50': _r(d50[i]),
                         'gap': _r(gap[i]), 's12': _r(s12[i]),
                         'px': _r((c[i] / c[t] - 1) * 100)})

    # ---------- d12 分箱 → 未来赔率 ----------
    bins12 = []
    for lo, hi in [(-99, -8), (-8, -6), (-6, -5), (-5, -4), (-4, -3), (-3, -2),
                   (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 4), (4, 99)]:
        s = [i for i in V if lo <= d12[i] < hi]
        if not s:
            continue
        bins12.append({'lo': lo if lo > -90 else None, 'hi': hi if hi < 90 else None,
                       'label': ('≤-8' if lo <= -90 else '+4↑' if hi >= 90 else '%+.0f~%+.0f' % (lo, hi)),
                       'n': len(s),
                       'win60': _r(sum(1 for i in s if f60[i] > 0) / len(s) * 100, 1),
                       'mean60': _r(st.mean([f60[i] for i in s])),
                       'med60': _r(st.median([f60[i] for i in s])),
                       'mean120': _r(st.mean([f120[i] for i in s])),
                       'big': _r(sum(1 for i in s if f120[i] >= 15) / len(s) * 100, 1),
                       'mx120': _r(st.mean([mx120[i] for i in s]))})
    base_big = _r(sum(1 for i in V if f120[i] >= 15) / len(V) * 100, 1)

    # ---------- gap 分箱 ----------
    bins_gap = []
    for lo, hi in [(-99, -8), (-8, -6), (-6, -5), (-5, -4), (-4, -3), (-3, -2),
                   (-2, -1), (-1, 0), (0, 2), (2, 99)]:
        s = [i for i in V if lo <= gap[i] < hi]
        if not s:
            continue
        bins_gap.append({'lo': lo if lo > -90 else None, 'hi': hi if hi < 90 else None,
                         'label': ('≤-8' if lo <= -90 else '+2↑' if hi >= 90 else '%+.0f~%+.0f' % (lo, hi)),
                         'n': len(s),
                         'win60': _r(sum(1 for i in s if f60[i] > 0) / len(s) * 100, 1),
                         'mean60': _r(st.mean([f60[i] for i in s])),
                         'mean120': _r(st.mean([f120[i] for i in s])),
                         'big': _r(sum(1 for i in s if f120[i] >= 15) / len(s) * 100, 1)})

    # ---------- 二维地形 d50 × d12 → f120 ----------
    d50_bins = [(-99, -10), (-10, -7), (-7, -5), (-5, -3), (-3, -1), (-1, 99)]
    d12_bins = [(-99, -5), (-5, -3), (-3, -1), (-1, 1), (1, 99)]
    terrain = []
    for a, b in d50_bins:
        cells = []
        for x, y in d12_bins:
            s = [i for i in V if a <= d50[i] < b and x <= d12[i] < y]
            cells.append({'lo': x if x > -90 else None, 'hi': y if y < 90 else None,
                          'n': len(s),
                          'mean120': _r(st.mean([f120[i] for i in s])) if s else None,
                          'big': _r(sum(1 for i in s if f120[i] >= 15) / len(s) * 100, 1) if s else None})
        terrain.append({'lo': a if a > -90 else None, 'hi': b if b < 90 else None, 'cells': cells})

    # ---------- 当前状态相似时段（标准化欧氏距离，去重 60 日） ----------
    FEATS = [d12, d50, gap, s12, s50, dd20]
    mu = [st.mean([f[i] for i in V]) for f in FEATS]
    sd = [st.pstdev([f[i] for i in V]) or 1.0 for f in FEATS]
    tv = [(FEATS[j][t] - mu[j]) / sd[j] for j in range(6)]
    dist = []
    for i in V:
        v = [(FEATS[j][i] - mu[j]) / sd[j] for j in range(6)]
        dist.append((sum((v[j] - tv[j]) ** 2 for j in range(6)) ** .5, i))
    dist.sort()
    sim = []
    dmap = {}
    for dd, i in dist:
        if all(abs(i - j) >= 60 for j in sim):
            sim.append(i)
            dmap[i] = dd
        if len(sim) >= 16:
            break
    similar = []
    for i in sim:
        dd = dmap[i]
        lab = ('大涨' if f120[i] >= 15 else '小涨' if f120[i] >= 3 else
               '震荡' if f120[i] >= -5 else '续跌')
        similar.append({'i': i, 'date': dates[i], 'dist': _r(dd, 3),
                        'close': _r(c[i]), 'd12': _r(d12[i]), 'd50': _r(d50[i]),
                        'gap': _r(gap[i]), 's12': _r(s12[i]), 's50': _r(s50[i]),
                        'dd20': _r(dd20[i]), 'f60': _r(f60[i], 1), 'f120': _r(f120[i], 1),
                        'label': lab})
    sim_summary = {
        'n': len(similar),
        'mean120': _r(st.mean([f120[i] for i in sim]), 1),
        'win': _r(sum(1 for i in sim if f120[i] > 0) / len(sim) * 100, 0),
        'best': _r(max(f120[i] for i in sim), 1),
        'worst': _r(min(f120[i] for i in sim), 1),
        'up': sum(1 for i in sim if f120[i] >= 3),
        'down': sum(1 for i in sim if f120[i] < -5),
        'years': sorted(set(dates[i][:4] for i in sim)),
    }

    # 相似时段合并成连续区间（相邻 <= 20 日归为一段，用于时间轴色块）
    spans = []
    for x in sorted(similar, key=lambda z: z['i']):
        if spans and x['i'] - spans[-1]['end'] <= 20:
            spans[-1]['end'] = x['i']
            spans[-1]['dates'].append(x['date'])
            spans[-1]['n'] += 1
            spans[-1]['f120'].append(x['f120'])
        else:
            spans.append({'start': x['i'], 'end': x['i'], 'dates': [x['date']],
                          'n': 1, 'f120': [x['f120']]})
    for s in spans:
        s['start_date'] = dates[s['start']]
        s['end_date'] = dates[s['end']]
        s['mean120'] = _r(st.mean(s['f120']), 1)
        s['label'] = ('后续偏强' if s['mean120'] >= 3 else '后续偏弱' if s['mean120'] <= -5 else '后续震荡')
        del s['f120']

    # ---------- 组合条件（底部确认的赔率） ----------
    def evaluate(name, fn):
        s = [i for i in V if fn(i)]
        if not s:
            return None
        return {'name': name, 'n': len(s),
                'win60': _r(sum(1 for i in s if f60[i] > 0) / len(s) * 100, 1),
                'mean60': _r(st.mean([f60[i] for i in s]), 1),
                'mean120': _r(st.mean([f120[i] for i in s]), 1),
                'big': _r(sum(1 for i in s if f120[i] >= 15) / len(s) * 100, 1)}

    combos = [x for x in [
        evaluate('深跌形态 d50≤-5 且 gap≤-4', lambda i: d50[i] <= -5 and gap[i] <= -4),
        evaluate('＋双线收敛 gap∈[-4,-2]', lambda i: d50[i] <= -5 and -4 <= gap[i] < -2),
        evaluate('＋d12 已收复 ≥-3%', lambda i: d50[i] <= -5 and -4 <= gap[i] < -2 and d12[i] >= -3),
        evaluate('＋20日回撤 ≥-8%', lambda i: d50[i] <= -5 and gap[i] <= -4 and dd20[i] >= -8),
        evaluate('＋EMA12 斜率转正', lambda i: d50[i] <= -5 and gap[i] <= -4 and s12[i] > 0),
    ] if x]

    # ---------- 当前状态 ----------
    cur = {
        'date': dates[t], 'close': _r(c[t]), 'e12': _r(e12[t]), 'e50': _r(e50[t]),
        'd12': _r(d12[t]), 'd50': _r(d50[t]), 'gap': _r(gap[t]),
        's12': _r(s12[t]), 's50': _r(s50[t]), 'dd20': _r(dd20[t]), 'dd60': _r(dd60[t]),
        'panic': _r(panic[t]),
        'pct': {'d12': pct(d12, d12[t]), 'd50': pct(d50, d50[t]), 'gap': pct(gap, gap[t]),
                's12': pct(s12, s12[t]), 's50': pct(s50, s50[t]), 'dd20': pct(dd20, dd20[t])},
        'hist': {k: {'min': _r(min(a[i] for i in V)), 'p10': quant([a[i] for i in V], .1),
                     'p50': quant([a[i] for i in V], .5), 'p90': quant([a[i] for i in V], .9),
                     'max': _r(max(a[i] for i in V))}
                 for k, a in [('d12', d12), ('d50', d50), ('gap', gap), ('s12', s12),
                              ('s50', s50), ('dd20', dd20)]},
        'in_band': {'d12': bands['d12']['p25'] <= d12[t] <= bands['d12']['p75'],
                    'd50': bands['d50']['p25'] <= d50[t] <= bands['d50']['p75'],
                    'gap': bands['gap']['p25'] <= gap[t] <= bands['gap']['p75']},
    }
    # 定性结论：按「中期是否跌透 / 短期是否贴线 / 双线是否收敛」三段动态判定
    inb = cur['in_band']
    gg = gap[t] - bands['gap']['p50']
    d12_prev = d12[t - 5] if t >= 5 else d12[t]
    d50_in = inb['d50']
    short_ok = inb['d12'] or d12[t] > bands['d12']['p75']   # 已贴线或已收复
    gap_ok = gap[t] >= bands['gap']['p25']
    cur['short_recover'] = _r(d12[t] - d12_prev)
    cur['d12_prev5'] = _r(d12_prev)
    if d50_in and short_ok and gap_ok:
        title = '底部形态已确认'
        detail = ('中期乖离 %+.2f%% 与短期乖离 %+.2f%% 均处历史大底区间，双线间距 %+.2f%% 也已收敛到'
                  '大底四分位内，形态上完成筑底；仍以站稳 EXPMA12 为最终确认。'
                  % (d50[t], d12[t], gap[t]))
    elif d50_in and short_ok:
        title = '形似大底，双线未收敛'
        detail = ('股价对 EXPMA50 的乖离 %+.2f%% 已落入历史大底带，d12 由 5 日前的 %+.2f%% '
                  '收回至 %+.2f%%（短期贴线企稳）；但双线间距 %+.2f%% 比大底中位 %+.2f%% '
                  '仍宽 %.2f 个百分点，EMA12 未与 EMA50 收敛，属下跌末段而非确认底部。'
                  % (d50[t], d12_prev, d12[t], gap[t], bands['gap']['p50'], abs(gg)))
    elif d50_in:
        title = '中期跌透，短线仍在急跌'
        detail = ('股价对 EXPMA50 的乖离已进入历史大底带，但 d12 %+.2f%% 比大底四分位下沿'
                  ' %+.2f%% 更深，短期尚无贴线企稳动作，通常对应下跌中继。'
                  % (d12[t], bands['d12']['p25']))
    else:
        title = '未落入历史大底带'
        detail = ('d12 %+.2f%% / d50 %+.2f%% 均未进入历史大底的四分位区间，'
                  '当前形态与历史大底差异较大。' % (d12[t], d50[t]))
    cur['verdict'] = {
        'title': title, 'detail': detail, 'gap_gap': _r(gg),
        'wait': ['双线间距收敛到 %+.1f%% 以上' % max(-4.0, bands['gap']['p25']),
                 'd12 站稳 0 轴', 'EMA12 五日斜率转正'],
    }

    return {
        'code': code, 'name': NAMES.get(code, code),
        'start': dates[0], 'end': dates[-1], 'n': n, 'valid_n': len(V),
        'base_big': base_big,
        'current': cur, 'bands': bands, 'combos': combos,
        'events': events, 'shape': shape, 'cur_path': cur_path,
        'bins12': bins12, 'bins_gap': bins_gap, 'terrain': terrain,
        'similar': similar, 'sim_summary': sim_summary, 'spans': spans,
        # 全历史序列（地形图 / 时间轴 / hover 用）
        'series': {
            'dates': dates,
            'close': [_r(x) for x in c],
            'd12': [_r(x) for x in d12],
            'd50': [_r(x) for x in d50],
            'gap': [_r(x) for x in gap],
            's12': [_r(x) for x in s12],
            'f120': [_r(x, 1) if x is not None else None for x in f120],
        },
    }


_CACHE = {}


def cached(code='sh000300'):
    """按数据文件 mtime 缓存，避免每次请求重算（约 1~2 秒）。"""
    path = os.path.join(INDICES, code + '.json')
    key = (code, os.path.getmtime(path))
    if _CACHE.get('key') != key:
        _CACHE['key'] = key
        _CACHE['data'] = build(code)
    return _CACHE['data']


if __name__ == '__main__':
    d = build()
    c = d['current']
    print('%s  %s ~ %s  %d 根 / 有效 %d 日' % (d['name'], d['start'], d['end'], d['n'], d['valid_n']))
    print('当前 %s  收=%.2f  d12=%+.2f%%  d50=%+.2f%%  gap=%+.2f%%' %
          (c['date'], c['close'], c['d12'], c['d50'], c['gap']))
    print('大底带: d12 %+.2f ~ %+.2f (中位 %+.2f) | d50 %+.2f ~ %+.2f (中位 %+.2f) | gap 中位 %+.2f' %
          (d['bands']['d12']['p25'], d['bands']['d12']['p75'], d['bands']['d12']['p50'],
           d['bands']['d50']['p25'], d['bands']['d50']['p75'], d['bands']['d50']['p50'],
           d['bands']['gap']['p50']))
    print('大底事件 %d 个；相似时段 %d 个，f120 均值 %+.1f%%，胜率 %.0f%%' %
          (len(d['events']), d['sim_summary']['n'], d['sim_summary']['mean120'], d['sim_summary']['win']))
    print('结论:', c['verdict']['title'], '|', c['verdict']['detail'])
