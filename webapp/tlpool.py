#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三线池（穿三线 · Top5 + 介入点）+ 共享的「穿三线信号特征」计算。

穿三线定义（与 server.py cross_lines 完全一致）：
  当日阳线实体（open, close）同时包住 EXPMA12 / EXPMA50 / MA89 三条线。

三线池目标：在「当日全部穿三线」名单里，从技术形态出发、叠加市场状态与事件催化，
挑出最可能继续拉升的前 5 只，给出可执行的介入点/止损/目标，供参考。
评分满分 ≈106 = 技术形态 62(含背离金叉lead附加≤6) + 市场适配 14 + 事件/主线 24。
背离金叉lead 是「MA89已上行(确认位)」之前的早期买点：EXPMA12/50上行 + MA89浅降/刚勾 + EMA12新上穿。
所有特征只用截至信号日 t 的数据，事件库带日期，早于事件日的信号不会命中（防未来函数）。

产物：webapp/tlpool.json（当日候选全量 + Top5 + 历史归档与前向验证）
用法：
  import tlpool
  tlpool.build(D, ctx={'regime':..., 'indices':..., 'zt':..., 'emotion':...})
"""
import datetime, json, os, statistics, sys

WEB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WEB)
import server as _srv   # vr_at / cross_lines / is_st / limit_ratio

POOL_PATH = os.path.join(WEB, 'tlpool.json')
EVENTS_PATH = os.path.join(WEB, 'events.json')
CONCEPTS_PATH = os.path.join(WEB, 'concepts.json')
INDUSTRY_PATH = os.path.join(WEB, 'industry.json')

TOPN = 5
HORIZONS = (1, 3, 5, 10, 20, 30)

# 评分上限（用于报告透明化；技术形态含背离金叉lead附加≤6）
MAXTECH, MAXMKT, MAXEVT = 68, 14, 24


# ---------------------------------------------------------------- 基础工具
def _j(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _safe(a, b, default=0.0):
    try:
        return a / b if b else default
    except Exception:
        return default


def _iso(s):
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


_CONCEPT_CACHE = None


def _concepts(code):
    global _CONCEPT_CACHE
    if _CONCEPT_CACHE is None:
        _CONCEPT_CACHE = _j(CONCEPTS_PATH, {}).get('map', {})
    return _CONCEPT_CACHE.get(code, [])[:30]


# --------------------------------------------------- 背离金叉 lead signal
def divergence_state(kk, t):
    """背离金叉（领先买点）原始状态判定（只用 <=t 的数据，无未来函数）。

    用户观察到的形态：MA89 仍在下行，但 EXPMA(12/50) 已经开始上行，
    在上行过程中 EMA12 与 MA89 开始交叉（上穿）。这是「确认位(MA89已上行)」
    之前的早期买点，提前约 1~2 周。

    判别（区分成功交叉 vs 假交叉陷阱，参照龙溪股份 2025-11 成 / 2026-09-30 败）：
      1) EMA12 当前在 MA89 上方（已交叉）
      2) 交叉是「近期」发生的：最近 20 根内出现 e12 上穿 ma89 事件，且 20 根前 e12 仍在 ma89 下方
         —— 排除早已多头排列的成熟上涨（那是确认位，不是 lead）
      3) MA89 未俯冲（20日斜率 > -6%，陷阱多发生在 -6% 以下深跌区）
      4) MA89 尚未明确翻多（20日斜率 < +0.6%）——若已明确上行则归为确认位(A)，不作 lead
      5) EMA12 / EMA50 均上行（10日斜率 > 0）
      6) 价格已在 EMA12 上方且 20日涨幅 <= 35%（未过度透支）
    满足全部 → signal=True。
    """
    if t < 95:
        return {'signal': False, 'reason': 't<95'}
    e12a, m89a = kk['e12'], kk['ma89']
    e12, e50, ma89, c = kk['e12'][t], kk['e50'][t], kk['ma89'][t], kk['c'][t]
    s12 = (e12 - kk['e12'][t - 10]) / kk['e12'][t - 10] * 100 if t >= 10 else 0
    s50 = (e50 - kk['e50'][t - 10]) / kk['e50'][t - 10] * 100 if t >= 10 else 0
    s89 = (ma89 - kk['ma89'][t - 20]) / kk['ma89'][t - 20] * 100 if t >= 20 else 0
    cross12 = e12 > ma89
    cross50 = e50 > ma89
    look = 20
    lo = max(1, t - look)
    last_cross = None
    for i in range(t, lo - 1, -1):
        if e12a[i] > m89a[i] and e12a[i - 1] <= m89a[i - 1]:
            last_cross = t - i
            break
    was_below = (t - look < 0) or (e12a[t - look] <= m89a[t - look])
    fresh = (last_cross is not None) and was_below
    c20 = (c - kk['c'][t - 20]) / kk['c'][t - 20] * 100 if t >= 20 else None
    not_crashing = s89 > -6.0
    lead_zone = s89 < 0.6
    e12_up = s12 > 0
    e50_up = s50 > 0
    price_up = c > e12
    health = (c20 is not None) and (c20 <= 35)
    signal = bool(cross12 and fresh and not_crashing and lead_zone
                  and e12_up and e50_up and price_up and health)
    return {
        'signal': signal, 'cross12': bool(cross12), 'cross50': bool(cross50),
        'fresh': bool(fresh), 'days_since_cross': last_cross,
        's12': round(s12, 2), 's50': round(s50, 2), 's89': round(s89, 2),
        'c20': round(c20, 2) if c20 is not None else None,
        'price_above_e12': bool(price_up),
        'ma89_not_crashing': bool(not_crashing), 'ma89_lead_zone': bool(lead_zone),
        'reason': ('OK' if signal else
                   'no_cross' if not cross12 else
                   'stale_cross' if not fresh else
                   'ma89_crash' if not not_crashing else
                   'ma89_up_confirmed' if not lead_zone else
                   'expma_not_up' if not (e12_up and e50_up) else
                   'price_low/over_ext' if not health else 'price_below_e12'),
    }


def divergence_points(d, regime='down'):
    """把背离金叉状态折算成 0~19 的 lead 分（2026-11-05 起取消 regime 折扣）。

    权重依据 divtrack 全历史回填（5122 信号，2025-11~2026-11）分桶回测，
    旧权重与本证据**完全相反**，已于 2026-11-05 重定权：

      · MA89刚勾(0~0.6%)：d5 +0.92% / 胜率47.5% / 超额+0.67% → 唯一正期望区，给最高权重(6)
      · MA89浅降(-6%~0)：d5 -0.54% / 超额-0.49% → 负期望，仅给过渡权重(3~1)
      · EMA50已上穿：d5 -1.22% / 胜率34.7% → 越成熟越差，纯lead不奖励；
        改为「仅快线上穿(EMA50未上穿)=最早期位」给 +2（匹配龙溪成功样本 cross50=False）。
    """
    if not d.get('signal'):
        return 0.0
    s89 = d['s89']
    # MA89 坡度：刚勾区最高，浅降区递减（与回测 edge 对齐）
    if 0 <= s89 < 0.6:
        s = 6.0                       # 刚勾 —— 正期望核心区
    elif -2 <= s89 < 0:
        s = 3.0                       # 即将勾头 —— 过渡
    elif -6 <= s89 < -2:
        s = 1.0                       # 仍在下行 —— 弱
    else:
        s = 0.0
    dc = d['days_since_cross'] or 20
    s += 4 if dc <= 5 else (3 if dc <= 12 else 2)     # 交叉新鲜度
    if d['s12'] > 0:
        s += 2
    if d['s50'] > 0:
        s += 2
    c20 = d['c20'] or 99
    s += 3 if c20 <= 15 else (2 if c20 <= 25 else 1)  # 位置健康
    # 纯lead（仅 EMA12 上穿 MA89、EMA50 尚未上穿）= 最早期位，匹配龙溪成功样本；
    # EMA50 已上穿代表形态更成熟，不再加分（回测显示其正期望为负）。
    s += 2 if not d['cross50'] else 0
    # 2026-11-05：移除 regime 折扣。回测显示 down regime 的背离/三线信号并不更差
    # （A级 d3 down +1.28% ≥ up +1.01%），旧的 ×0.6 无依据且会压低好信号。
    return round(s, 1)


def divergence_points_old(d, regime='down'):
    """回测对照用：2026-11-05 之前的旧权重（与回测证据相反的版本）。"""
    if not d.get('signal'):
        return 0.0
    s89 = d['s89']
    s = (min(6.0, -s89) if s89 < 0 else 1.0)
    dc = d['days_since_cross'] or 20
    s += 4 if dc <= 5 else (3 if dc <= 12 else 2)
    if d['s12'] > 0:
        s += 2
    if d['s50'] > 0:
        s += 2
    c20 = d['c20'] or 99
    s += 3 if c20 <= 15 else (2 if c20 <= 25 else 1)
    if d['cross50']:
        s += 2
    rf = {'up': 1.0, 'down': 0.6}.get(regime, 0.8)
    return round(s * rf, 1)


def signal_features(D, code, t):
    """某只股票在 t 日穿三线时的形态特征；只读取 <=t 的数据。"""
    kk = D.k[code]
    m = D.meta.get(code, {})
    c, o, h, l, v = kk['c'][t], kk['o'][t], kk['h'][t], kk['l'][t], kk['v'][t]
    prev = kk['c'][t - 1] if t > 0 else c
    e12, e50, m89 = kk['e12'][t], kk['e50'][t], kk['ma89'][t]
    chg = (c / prev - 1) * 100 if prev else 0
    rng = h - l
    close_pos = _safe(c - l, rng, 1.0)
    upsh = (h - c) / c * 100 if c else 0
    body = (c / o - 1) * 100 if o else 0
    vr = _srv.vr_at(kk, t)
    v20 = sum(kk['v'][max(0, t - 20):t]) / max(1, len(kk['v'][max(0, t - 20):t]))
    c20 = (c / kk['c'][t - 20] - 1) * 100 if t >= 20 else None
    c60 = (c / kk['c'][t - 60] - 1) * 100 if t >= 60 else None
    c5 = (c / kk['c'][t - 5] - 1) * 100 if t >= 5 else None
    win20 = kk['c'][max(0, t - 20):t]
    win60h = kk['h'][max(0, t - 60):t]
    hi20 = max(win20) if win20 else c
    hi60 = max(win60h) if win60h else h
    brk20 = bool(win20) and c > hi20
    plat = ((max(win20) / min(win20) - 1) * 100) if win20 and min(win20) > 0 else 99
    ma89_slope = ((m89 / kk['ma89'][t - 10] - 1) * 100) if t >= 10 and kk['ma89'][t - 10] else 0
    e12_slope = ((e12 / kk['e12'][t - 5] - 1) * 100) if t >= 5 and kk['e12'][t - 5] else 0
    e50_slope = ((e50 / kk['e50'][t - 10] - 1) * 100) if t >= 10 and kk['e50'][t - 10] else 0
    lim = _srv.limit_ratio(code, m.get('name', '')) * 100
    limit_up = chg >= lim - 0.6
    one_word = limit_up and ((h - l) / prev * 100 if prev else 0) < 1.0
    zone_lo, zone_hi = min(e12, e50, m89), max(e12, e50, m89)
    return {
        'code': code, 'name': m.get('name', ''), 'date': kk['dates'][t],
        'ind': D.ind.get(code, ''), 'mv': m.get('mv'), 'pe': m.get('pe'),
        'loss': (m.get('pe') or 0) <= 0,
        'close': round(c, 2), 'open': round(o, 2), 'high': round(h, 2), 'low': round(l, 2),
        'chg': round(chg, 2), 'vr': round(vr, 2) if vr else None,
        'v_vs_v20': round(_safe(v, v20), 2), 'body': round(body, 2),
        'upsh': round(upsh, 2), 'close_pos': round(close_pos, 3),
        'e12': round(e12, 2), 'e50': round(e50, 2), 'ma89': round(m89, 2),
        'zone_lo': round(zone_lo, 2), 'zone_hi': round(zone_hi, 2),
        'dev12': round((c / e12 - 1) * 100, 2) if e12 else None,
        'stack': bool(e12 > e50 > m89), 'ma89_slope': round(ma89_slope, 2),
        'e12_slope': round(e12_slope, 2), 'e50_slope': round(e50_slope, 2),
        'c5': round(c5, 2) if c5 is not None else None,
        'c20': round(c20, 2) if c20 is not None else None,
        'c60': round(c60, 2) if c60 is not None else None,
        'brk20': bool(brk20), 'hi20': round(hi20, 2), 'hi60': round(hi60, 2),
        'dist_hi60': round((c / hi60 - 1) * 100, 2) if hi60 else None,
        'plat20': round(plat, 2),         'limit_up': bool(limit_up), 'one_word': bool(one_word),
        'concepts': _concepts(code),
        'div': divergence_state(kk, t),
    }


# ------------------------------------------------------------ 事件/主线因子
def _load_events():
    return _j(EVENTS_PATH, {'events': [], 'market': {}})


def event_match(f, signal_date, ev_lib=None):
    """命中事件（防未来函数：signal_date 必须 >= 事件日，且未过 ttl_days）。"""
    ev_lib = ev_lib or _load_events()
    d = _iso(signal_date)
    txt = ' '.join([f.get('ind', '')] + list(f.get('concepts') or []))
    hits, score = [], 0.0
    for ev in ev_lib.get('events', []):
        ed = _iso(ev.get('date'))
        if not d or not ed or d < ed:
            continue
        age = (d - ed).days
        ttl = int(ev.get('ttl_days', 7))
        if age > ttl:
            continue
        if any(k in txt for k in ev.get('keywords', [])):
            decay = max(0.4, 1 - age / max(1, ttl))
            score += float(ev.get('strength', 1)) * decay
            hits.append({'id': ev['id'], 'tag': ev.get('tag', ''),
                         'title': ev.get('title', '')[:60], 'date': ev['date']})
    return min(14.0, score), hits


def sector_heat(f, ctx):
    """本地涨停复盘的主线行业/概念热度（近5日涨停家数，20日口径，偏中期主线）。"""
    zt = (ctx or {}).get('zt') or {}
    inds = zt.get('industries') or []
    cons = zt.get('concepts') or []
    imax = max([x.get('n5', 0) for x in inds], default=0) or 1
    cmax = max([x.get('n5', 0) for x in cons], default=0) or 1
    imap = {x['ind']: x.get('n5', 0) for x in inds}
    cmap = {x['con']: x.get('n5', 0) for x in cons}
    ni = imap.get(f.get('ind'), 0)
    nc = max([cmap.get(c, 0) for c in (f.get('concepts') or [])], default=0)
    hot_con = next((c for c in (f.get('concepts') or []) if cmap.get(c, 0) == nc and nc > 0), '')
    return (min(5.0, 5.0 * ni / imax), min(5.0, 5.0 * nc / cmax),
            f.get('ind', '') if ni else '', hot_con)


def market_score(f, ctx, mkt):
    """市场适配：板块指数强度 6 + 拥挤度 4 + 上证>MA20 适配 4（软过滤，不排除候选）。"""
    idx = (ctx or {}).get('indices') or []
    code = f['code']
    key = ('科创50' if code.startswith('sh68') else
           '创业板指' if code.startswith('sz30') else
           '北证50' if code.startswith('bj') else '上证指数')
    ix = next((x for x in idx if x.get('name') == key), None)
    b = 0.0
    if ix:
        b += 4 if ix.get('above12') else 0
        b += 2 if (ix.get('chg') or 0) > 0 else 0
    emo = ((ctx or {}).get('emotion') or {}).get('hist') or []
    temp = emo[-1].get('temp', 50) if emo else 50
    vr, c20, onew = f.get('vr') or 0, f.get('c20') or 0, f.get('one_word')
    if temp >= 60:
        crowd = 4.0 if (vr <= 2.2 and not onew and c20 <= 20) else 1.0
    elif temp < 20:
        crowd = 4.0 if f.get('brk20') else 2.0
    else:
        crowd = 3.0 if vr <= 3 else 1.5
    if mkt == 'down':
        fit = 4.0 if ((f.get('dev12') or 99) <= 6 and c20 <= 15 and (f.get('c60') or 99) <= 30) \
            else (2.0 if c20 <= 25 else 0.0)
    else:
        fit = 4.0 if (f.get('brk20') and vr >= 1.5) else 2.0
    return round(b + crowd + fit, 1), {'board': key, 'board_score': b,
                                       'emotion_temp': temp, 'crowd': crowd,
                                       'regime_fit': fit, 'mkt_fit': fit, 'mkt': mkt}


def tech_score(f, regime='down'):
    """技术形态 62 分。

    权重来自 tltrack 全历史回填（8992 个三线信号，2025-11~2026-11）：
      · 破20日高 5日 +0.34% / 未破 -0.30%
      · MA89上行 5日 +0.49%、胜率48.7%（单因子最强）；破20高+MA89上行 +1.63%、胜率53.4%
      · 下行 regime 里「破20高但MA89仍下行」5日 -0.90%、胜率40.5%（追高陷阱）
      · 实体长度 / 上影 / 收盘位置 单独看几乎没有 edge（甚至略负），故只给象征性权重
    量能一项按 regime 调权：弱市回测「放量越大亏越多」，缩量突破不再重罚。
    """
    b = {}
    b['破20日高'] = 12.0 if f['brk20'] else 0.0
    b['MA89上行'] = 12.0 if (f.get('ma89_slope') or -99) > 0 else 0.0
    dh = f.get('dist_hi60') or -99
    b['距60日高'] = 6.0 if dh >= -0.5 else (4.0 if dh >= -5 else (2.0 if dh >= -12 else 0.0))
    vr = f.get('vr') or 0
    if regime == 'down':
        b['量能质量'] = (11.0 if 1.0 <= vr <= 2.2 else
                       9.0 if 0.8 <= vr < 1.0 else
                       7.0 if 2.2 < vr <= 3.2 else
                       2.0 if vr > 3.2 else 5.0)
    else:
        b['量能质量'] = (11.0 if 1.2 <= vr <= 2.2 else
                       9.0 if 2.2 < vr <= 3.2 else
                       4.0 if vr > 3.2 else
                       6.0 if 0.9 <= vr < 1.2 else 3.0)
    b['均线结构'] = (4.0 if f['stack'] else 0.0) + \
                (2.0 if (f.get('dev12') or 99) <= 8 else 0.0) + \
                (2.0 if (f.get('e50_slope') or -99) > 0 else 0.0)
    b['收盘强度'] = round(3.0 * (f.get('close_pos') or 0) +
                     (2.0 if (f.get('upsh') or 99) <= 1.5 else
                      1.0 if (f.get('upsh') or 99) <= 3 else 0.0), 1)
    bd = f.get('body') or 0
    b['实体力度'] = 2.0 if bd >= 3 else (1.0 if bd >= 1.5 else 0.0)
    c20, c60 = f.get('c20') or 0, f.get('c60') or 0
    b['位置健康'] = (1.5 if -5 <= c20 <= 15 else 0.0) + \
                (1.0 if c60 <= 35 else 0.0) + \
                (0.5 if (f.get('plat20') or 99) <= 20 else 0.0)
    b['可介入性'] = 0.0 if f['one_word'] else 3.0
    # 背离金叉 lead（提前1~2周早期位）：EXPMA上行+MA89浅降/刚勾+EMA12新上穿。
    # 与「MA89已上行」(MA89上行12分)互斥——此处 MA89 仍下行/刚勾，填补「确认前」的 early 空白。
    dv = f.get('div') or {}
    dvp = divergence_points(dv, regime) if dv.get('signal') else 0.0
    b['背离金叉lead'] = round(min(6.0, dvp * 0.4), 1)
    return round(sum(b.values()), 1), b


# ------------------------------------------------------------------ 介入点
def entry_plan(f, ctx, mkt):
    c, h = f['close'], f['high']
    zlo, zhi = f['zone_lo'], f['zone_hi']
    hard_stop = round(min(c * 0.93, zlo * 0.98), 2)
    risk_per_share = max(c - zlo, c * 0.04)
    t1 = round(max((f.get('hi60') or c) * 1.01, c + 1.5 * risk_per_share), 2)
    t2 = round(c + 3.0 * risk_per_share, 2)
    rr = round((t1 - c) / max(c - hard_stop, 0.01), 2)
    emo = ((ctx or {}).get('emotion') or {}).get('hist') or []
    temp = emo[-1].get('temp', 50) if emo else 50
    if mkt == 'down' and temp >= 60:
        size = '试仓 ≤1/3（上证<MA20+情绪高潮，禁追高）'
    elif mkt == 'down':
        size = '试仓 ≤1/3（上证<MA20，环境偏弱）'
    else:
        size = '首仓 1/3~1/2，回踩确认后再加'
    breakout_level = round(max(h, f.get('hi20') or h) * 1.002, 2)
    return {
        'pullback': [round(zlo * 0.99, 2), round(zhi * 1.01, 2)],
        'pullback_note': '首选：回踩三线密集带（EXPMA12/50/MA89）不破且缩量企稳时分批低吸',
        'breakout_level': breakout_level,
        'trigger': breakout_level,
        'trigger_note': ('次选：次日站上信号日最高价且量能不缩，突破确认后跟进；不追高开>3%'
                         if f['brk20'] else
                         '⚠️该票尚未破20日高：须先放量站上 %.2f（20日高）才算突破确认，否则只观察' % breakout_level),
        'chase_cap': round(c * 1.03, 2),
        'stop_ref': f['e50'],
        'e12': f['e12'], 'e50': f['e50'], 'ma89': f['ma89'],
        'stop_rule': 'A级：信号日/次日买入 → 持 2~3 日了结（历史胜率 55~56%），最多 5 日；'
                     '破 EMA50 三日不收回仅作趋势票兜底（回测最差：25% 胜率 / -1%），不推荐长持',
        'hard_stop': hard_stop,
        'target1': t1, 'target2': t2, 'rr': rr, 'size': size,
        'hold_note': '回测：信号有效期 1~3 天；A级+上证>MA20 持 2~3 日胜率 55.6%、持 5 日均值 +1.98%；'
                     '次日开盘卖仅 35%，不要开盘就跑',
    }


def _reason(f, tb, ms, ev_hits):
    p = []
    if f['brk20']:
        p.append('收盘破20日高')
    else:
        p.append('未破20日高')
    if f['stack']:
        p.append('12>50>89多头排列')
    if (f.get('ma89_slope') or -99) > 0:
        p.append('MA89上行')
    vr = f.get('vr') or 0
    p.append(('温和放量' if 1.2 <= vr <= 2.2 else '巨量(拥挤)' if vr > 3.2 else '量能一般') + ' vr%.2f' % vr)
    if (f.get('upsh') or 99) <= 1.5 and (f.get('close_pos') or 0) >= 0.9:
        p.append('光头阳/收盘近最高')
    if f['one_word']:
        p.append('⚠️一字板难介入')
    if f['limit_up']:
        p.append('涨停')
    if (f.get('c20') or 0) > 20 or (f.get('c60') or 0) > 40:
        p.append('⚠️短期已透支')
    if f['loss']:
        p.append('⚠️亏损股(PE≤0)')
    if ev_hits:
        p.append('事件:' + '/'.join(dict.fromkeys(h['tag'] for h in ev_hits)))
    if ms.get('board_score', 0) >= 4:
        p.append(ms['board'] + '站上EMA12')
    return '；'.join(p)


def score_one(D, code, t, ctx, regime, ev_lib=None, mkt=None):
    # regime：仅用于 tech_score 的历史量能权重（保留原口径）；
    # mkt（上证>MA20）：用于市场适配与仓位，作为软过滤（不排除候选）。
    mkt = mkt or regime
    f = signal_features(D, code, t)
    f['div_points'] = divergence_points(f['div'], regime)
    tech, tb = tech_score(f, regime)
    mkscore, ms = market_score(f, ctx, mkt)
    evs, ev_hits = event_match(f, f['date'], ev_lib)
    hi, hc, hot_ind, hot_con = sector_heat(f, ctx)
    evt = round(evs + hi + hc, 1)
    total = round(tech + mkscore + evt, 1)
    up89 = (f.get('ma89_slope') or -99) > 0
    grade = 'A' if (f['brk20'] and up89) else ('B' if f['brk20'] else 'C')
    grade_note = {
        'A': 'A级·破20日高+MA89上行：历史同组合5日 +1.63%、胜率53.4%（三线形态里唯一稳定正期望）',
        'B': 'B级·破20日高但MA89仍下行：下行regime同组合5日 -1.76%、胜率35.7%，需等MA89转正或回踩确认',
        'C': 'C级·未破20日高：历史5日 -0.30%，只作观察，不占三线池额度',
    }[grade]
    if f.get('div', {}).get('signal'):
        grade_note += ('；含背离金叉lead：EXPMA上行+MA89浅降/刚勾+EMA12新上穿，'
                       '属提前1~2周早期位（优于普通%s，回踩三线密集带低吸优先）' % grade)
    return {
        **f,
        'tech': tech, 'market': mkscore, 'event': evt,
        'score': total, 'grade': grade, 'grade_note': grade_note,
        'breakdown': {'tech': tb, 'event_lib': round(evs, 1),
                      'ind_heat': round(hi, 1), 'con_heat': round(hc, 1),
                      'market': ms},
        'event_hits': ev_hits, 'hot_ind': hot_ind, 'hot_con': hot_con,
        'reason': _reason(f, tb, ms, ev_hits) + '；评级' + grade,
        'entry': entry_plan(f, ctx, mkt),
    }


# --------------------------------------------------------------- 池子构建
def _fwd(D, code, date, horizon):
    """用已实现K线计算某信号自 date 起 horizon 个交易日的收益（供历史池归档）。"""
    if code not in D.k:
        return None
    kk = D.k[code]
    if date not in kk['dates']:
        return None
    i = kk['dates'].index(date)
    if i + horizon >= len(kk['c']):
        return None
    return round((kk['c'][i + horizon] / kk['c'][i] - 1) * 100, 2)


def _archive(prev, D, new_date):
    """把上一期 Top5 归档进 history，并补记可实现的前向收益。"""
    if not prev or not prev.get('pool'):
        return None
    ent = {'date': prev['as_of'], 'pool': [], 'perf': {}}
    for e in prev['pool']:
        ent['pool'].append({k: e.get(k) for k in
                            ('code', 'name', 'score', 'close', 'entry', 'reason')})
    vals = {}
    for h in HORIZONS:
        arr = [_fwd(D, e['code'], prev['as_of'], h) for e in prev['pool']]
        arr = [x for x in arr if x is not None]
        if arr:
            vals['d%d' % h] = {'n': len(arr), 'avg': round(statistics.mean(arr), 2),
                               'win': round(sum(1 for x in arr if x > 0) / len(arr) * 100, 1)}
    ent['perf'] = vals
    return ent


def _load_evidence():
    """从 tltrack 全历史跟踪结果取评分依据（每日跑批自动刷新，不写死数字）。"""
    st = _j(os.path.join(WEB, 'tltrack', 'stats.json'), {})
    combos, bks, hz = st.get('combos', {}), st.get('buckets', {}), st.get('horizons', {})
    keep_c = ['破20日高+MA89上行', '破20日高+MA89仍下行',
              'regime下行+破20日高', 'regime下行+破20高+MA89上行',
              'regime下行+破20高+MA89下行', 'regime上行+破20高+MA89上行']
    keep_b = ['破20日高', '未破20日高', 'MA89上行', 'regime上行', 'regime下行']
    return {
        'as_of': st.get('as_of'), 'n_signals': st.get('n_signals'),
        'min_track_days': st.get('min_track_days'),
        'horizons': {k: hz.get(k) for k in ('d1', 'd5', 'd10', 'd20', 'd30') if hz.get(k)},
        'combos': {k: combos[k] for k in keep_c if combos.get(k)},
        'buckets': {k: {'n': bks[k]['n'], 'd5': bks[k]['d5']} for k in keep_b if bks.get(k)},
        'note': '来源 tltrack/signals.csv 全历史回填；历史≠未来，样本以2025-11~2026-11为主',
    }


def build(D, ctx=None, date=None, topn=TOPN, write=True):
    """扫描 as_of 交易日的全部穿三线信号 → 评分 → Top5 三线池。"""
    ctx = ctx or {}
    as_of = date or D.px_date
    regime = (ctx.get('regime') or {}).get('current') or D.regime.get(as_of, 'down')
    mkt = getattr(D, 'mkt', {}).get(as_of) or regime   # 上证>MA20 软过滤（regime 降级为参考）
    ev_lib = _load_events()
    old = _j(POOL_PATH, {})
    hist = list(old.get('history') or [])
    if old.get('as_of') != as_of:
        ent = _archive(old, D, as_of)
        if ent:
            hist = [h for h in hist if h.get('date') != ent['date']]
            hist.append(ent)
    cands = []
    for code, kk in D.k.items():
        if as_of not in kk['dates']:
            continue
        if _srv.is_st(D.meta.get(code, {}).get('name')):
            continue
        t = kk['dates'].index(as_of)
        if t < 95 or _srv.cross_lines(kk, t) != 3:
            continue
        cands.append(score_one(D, code, t, ctx, regime, ev_lib, mkt))
    cands.sort(key=lambda r: (-r['score'], -(r.get('vr') or 0), r['code']))
    for i, r in enumerate(cands):
        r['rank'] = i + 1

    # 三线池硬门槛：收盘破20日高。离线前向检验（全A 15个月）显示三线形态本身无 alpha，
    # 叠加「收盘破20日高」后 5 日期望才由负转正；未破高的票事件再强也只进观察区，不占池子额度。
    confirmed = [r for r in cands if r['brk20']]
    watch = [r for r in cands if not r['brk20']]
    grade_order = {'A': 0, 'B': 1, 'C': 2}
    confirmed.sort(key=lambda r: (grade_order[r['grade']], -r['score'], r['code']))
    watch.sort(key=lambda r: (-r['score'], r['code']))
    for i, r in enumerate(confirmed):
        r['pool_rank'] = i + 1
    for i, r in enumerate(watch):
        r['watch_rank'] = i + 1

    def _rec(r, tier):
        return {**{k: r[k] for k in
                   ('rank', 'code', 'name', 'ind', 'concepts', 'score', 'tech',
                    'market', 'event', 'close', 'chg', 'vr', 'brk20', 'c20',
                    'c60', 'dev12', 'stack', 'ma89_slope', 'dist_hi60',
                    'mv', 'pe', 'loss', 'limit_up', 'reason', 'breakdown',
                    'event_hits', 'hot_ind', 'hot_con', 'entry',
                    'grade', 'grade_note', 'div', 'div_points')},
                'pick_date': as_of, 'pick_price': r['close'], 'tier': tier}

    pool = [_rec(r, '首选' if i < 2 else '备选') for i, r in enumerate(confirmed[:topn])]
    watch_top = [_rec(r, '观察·未破20日高') for r in watch[:topn]]
    mk = ev_lib.get('market', {})
    payload = {
        'as_of': as_of, 'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'regime': regime, 'mkt': mkt, 'market': mk,
        'rules': {
            'definition': '当日阳柱实体同时包住 EXPMA12 / EXPMA50 / MA89（三线穿阳）',
            'universe': '全市场·剔除ST/退市/停牌·允许亏损股（沿用现行筛选口径）',
            'score': '技术形态62(含背离金叉lead附加≤6：EXPMA上行+MA89浅降/刚勾+EMA12新上穿，提前1~2周早期位)'
                     '（破20日高12/MA89上行12/距60日高6/量能11/均线8/收盘强度5/实体2/位置3/可介入3）'
                     ' + 市场适配14（板块指数6/拥挤度4/上证>MA20适配4，软过滤不排除）'
                     ' + 事件主线24（事件库14/行业热度5/概念热度5）；权重依据 tltrack 全历史前向统计',
            'gate': '三线池硬门槛 = 收盘破20日高（离线前向检验中唯一让三线形态转正的过滤）；'
                    '未破高者列入 watch 观察区，不占前5额度',
            'sell_rule': 'A级：信号日/次日买入 → 持 2~3 日了结（胜率 55~56%），最多 5 日；'
                         '破 EMA50 三日不收回仅作趋势票兜底（回测最差 25% 胜率 / -1%），不推荐长持',
            'risk': mk.get('risk', ''),
            'event_lib_updated': ev_lib.get('updated'),
            'note': '事件库为人工/联网录入，带生效日与有效期，过期自动降权；本地行业/概念热度每日随跑批自动更新。'
        },
        'n_candidates': len(cands), 'n_confirmed': len(confirmed),
        'pool': pool, 'watch': watch_top,
        'candidates': [{k: r[k] for k in
                        ('rank', 'code', 'name', 'ind', 'score', 'tech', 'market',
                         'event', 'close', 'chg', 'vr', 'brk20', 'dev12', 'c20',
                         'c60', 'stack', 'ma89_slope', 'dist_hi60', 'mv', 'pe',
                         'loss', 'limit_up', 'one_word', 'grade', 'reason', 'entry',
                         'div', 'div_points')}
                       for r in cands],
        'evidence': _load_evidence(),
        'history': hist[-120:],
    }
    if write:
        with open(POOL_PATH, 'w') as fh:
            json.dump(payload, fh, ensure_ascii=False)
    return payload


# --------------------------------------------------- 背离金叉 全市场扫描
def screen_divcross(D, as_of=None, regime=None, topn=300, fresh_days=3):
    """全市场扫描「背离金叉 lead」候选（当前快照 as_of）。
    与三线池 gate 不同——这里不要求当日穿三线/破20日高，专门找「确认位之前」的早期位。

    ⚠️ 口径（用户 2026-11-05 修正）：**不做「距交叉>3日就剔除」的排除**！
    所有满足背离金叉形态的候选都进入池子（页面全量展示），由 divtrack 持续跟踪 ≥30 日。
    fresh3 仅作为「新信号」高亮标记（距交叉 ≤ fresh_days 日），**不参与筛选/排除**。
    stage 区分「领先位(尚未三线/破20高)」与「已确认(三线/破20高)」。"""
    as_of = as_of or D.px_date
    regime = regime or D.regime.get(as_of, 'down')
    out = []
    for code, kk in D.k.items():
        if as_of not in kk['dates']:
            continue
        if code not in D.fresh:
            continue
        if _srv.is_st(D.meta.get(code, {}).get('name')):
            continue
        t = kk['dates'].index(as_of)
        if t < 95:
            continue
        d = divergence_state(kk, t)
        if not d['signal']:
            continue
        pts = divergence_points(d, regime)
        c = kk['c'][t]
        brk20 = c > max(kk['c'][t - 20:t])
        is_three = _srv.cross_lines(kk, t) == 3
        stage = '已确认(三线/破20高)' if (is_three or brk20) else '领先位(尚未三线/破20高)'
        dsc = d['days_since_cross']
        out.append({
            'code': code, 'name': D.meta.get(code, {}).get('name', ''),
            'close': round(c, 2), 'mv': D.meta.get(code, {}).get('mv'),
            'pe': D.meta.get(code, {}).get('pe'),
            'score': pts, 's89': d['s89'], 's12': d['s12'], 's50': d['s50'],
            'days_since_cross': dsc, 'cross50': d['cross50'],
            'c20': d['c20'], 'stage': stage, 'is_three': is_three, 'brk20': brk20,
            'fresh3': (dsc is not None and dsc <= fresh_days), 'div': d,
        })
    out.sort(key=lambda r: (-r['score'], -(r['mv'] or 0), r['code']))
    return {'as_of': as_of, 'regime': regime, 'n': len(out), 'fresh_days': fresh_days,
            'n_fresh3': sum(1 for r in out if r['fresh3']),
            'candidates': out[:topn]}


if __name__ == '__main__':
    import urllib.request
    def api(p):
        return json.load(urllib.request.urlopen('http://127.0.0.1:8765' + p, timeout=180))
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else 'pool'
    D = _srv.Data()
    if mode == 'divcross':
        as_of = sys.argv[2] if len(sys.argv) > 2 else D.px_date
        reg = sys.argv[3] if len(sys.argv) > 3 else D.regime.get(as_of, 'down')
        res = screen_divcross(D, as_of, reg, topn=400)
        with open('divcross.json', 'w') as fh:
            json.dump(res, fh, ensure_ascii=False)
        print('as_of=%s regime=%s  全量候选(不剔除)=%d 只 ｜ 其中 近%d日新交叉(fresh)=%d' % (
            res['as_of'], res['regime'], res['n'], res['fresh_days'], res['n_fresh3']))
        print('已写入 divcross.json')
        hdr = '%-8s %-9s %7s %9s %6s %7s %7s %7s %6s %5s %6s %6s  %s' % (
            'code', 'name', 'close', 'mv(亿)', 'score', 's89%', 's12%', 's50%', 'c20%',
            'dCrs', 'x50', 'f3', 'stage')
        print(hdr)
        for r in res['candidates']:
            print('%-8s %-9s %7.2f %9.1f %6.1f %7.2f %7.2f %7.2f %6.1f %5s %6s %6s  %s' % (
                r['code'], r['name'][:9], r['close'], (r['mv'] or 0), r['score'],
                r['s89'], r['s12'], r['s50'], r['c20'] or 0, r['days_since_cross'],
                r['cross50'], ('Y' if r['fresh3'] else ''), r['stage']))
    else:
        ctx = {'regime': api('/api/regime'), 'indices': api('/api/indices'),
               'zt': api('/api/zt?days=20'), 'emotion': api('/api/emotion?days=30')}
        out = build(D, ctx)
        print('as_of=%s candidates=%d regime=%s' % (out['as_of'], out['n_candidates'], out['regime']))
        for e in out['pool']:
            print('#%d %s %s score=%.1f (技%.1f/市%.1f/事%.1f) close=%.2f vr=%s brk20=%s 回踩=%s 触发=%s 止损=%s' % (
                e['rank'], e['code'], e['name'], e['score'], e['tech'], e['market'], e['event'],
                e['close'], e['vr'], e['brk20'], e['entry']['pullback'],
                e['entry']['trigger'], e['entry']['hard_stop']))
            print('    ', e['reason'])
