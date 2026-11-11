#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙头战法 · 全历史盘面重建 / 龙头识别 / 生命周期 / 介入点 / 前向验证 / 持续跟踪

设计原则（这套战法最容易自欺的三个地方，全部显式处理）：
1) 无未来函数：所有特征只用 signal_date 当日及之前的数据；「谁是龙头」是事后才知道的，
   本模块只输出「当日龙头候选」及概率化评分，并在报告里明确标注事后确认偏差。
2) 可成交口径：涨停股次日买入用 **t+1 开盘价**，且次日若「一字涨停开盘」标记 buyable=False
   （买不进），从收益统计中剔除，避免用买不到的板虚增战法收益。
3) 权重来自回测：评分各维度的权重由全历史分组前向收益的 edge 自动校准，而不是拍脑袋。
   另提供「接力（次日开盘）」与「回踩低吸（5日内限价挂单）」两套并行的可执行口径。

产物：
  webapp/dragon.json              当日龙头池（总龙头/题材主线/候选/生命周期/介入点/风险）
  webapp/dragon/signals.csv        全历史龙头候选回测表（PIT特征 + 前向收益 + 超额）
  webapp/dragon/signals.json       同上主档（精简）
  webapp/dragon/stats.json         分组统计 + 因子 edge + 证据
  webapp/dragon/board_summary.json 全历史每日情绪/梯队摘要（前端周期图）
  webapp/dragon/tracking.json      龙头候选持续跟踪（≥30交易日）
  webapp/dragon/daily/*.json       每日逐日盯市快照（首次落盘不覆写）

用法：python3 dragon.py   或   import dragon; dragon.update(D)
"""
import collections
import csv
import datetime
import json
import math
import os
import statistics
import sys

WEB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WEB)
import server as _srv

DIR = os.path.join(WEB, 'dragon')
DAILY = os.path.join(DIR, 'daily')
POOL = os.path.join(WEB, 'dragon.json')
SIGJSON = os.path.join(DIR, 'signals.json')
SIGCSV = os.path.join(DIR, 'signals.csv')
STATS = os.path.join(DIR, 'stats.json')
BOARDSUM = os.path.join(DIR, 'board_summary.json')
TRACK = os.path.join(DIR, 'tracking.json')
META = os.path.join(DIR, 'meta.json')

HOR = (1, 3, 5, 10)
MIN_TRACK, MAX_TRACK = 30, 60
TOPN = 8

# 宽泛/技术性标签不是「题材主线」，会污染主线识别（融资融券、指数成分、昨日涨停等）
GENERIC = (
    '融资融券', '沪股通', '深股通', '陆股通', '标普', '富时', 'MSCI', '机构重仓', '基金重仓',
    'QFII', '社保', '养老金', '预盈预增', '业绩', '小盘', '中盘', '大盘', '低价', '高价',
    '破净', '昨日涨停', '昨日连板', '昨日触板', '昨日', '新高', '超跌', '白马', '举牌',
    '股权转让', '可转债', '转债标的', 'AH股', 'B股', 'H股', 'GDR', '破发', '微盘', '小市值',
    '成分股', '指数', '样本股', '两融', '转融通', '沪深300', '上证', '中证', '深证',
    '创业板综', '科创板', '北交所', '注册制', '次新股', '百日', '历史', '股权激励',
    '回购', '增持', '减持', '分红', '送转', '高送转', '标准普尔', '富时罗素', '茅指数',
    '宁组合', '同花顺', '东方财富', '融资', '融券', '专精特新', '低市盈率', '高市盈率',
    '消费风格', '周期风格', '成长风格', '价值风格', '大盘成长', '小盘成长', '中盘',
    '深成500', '上证180', '上证380', '中证500', '中证1000', '富时概念', '机构',
    '科技风格', '高成长股', '低成长股', '成长股', '价值股', '周期股', '消费股',
    '科技股', '题材股', '权重股', '蓝筹', '绩优股', '亏损股', '微利股', '风格',
    '含GDR', '参股新三板', '新三板', '创投', '独角兽', '昨日', '近期', '强势',
    '活跃股', '热门股', '龙头', '标杆', '示范', '百强', '民营', '国企改革',
)

STAGE_NOTE = {
    '启动': '题材第1~2天、首板卡位；只能小仓试错，等次日2板确认再加',
    '发酵': '2板确认 + 题材出现跟风，是接力胜率相对最好的窗口',
    '加速': '3板以上缩量/一字，持筹者盛宴，场外追高性价比最差',
    '分歧': '开板/放量/大振幅，多空换手；分歧转一致（弱转强）才是二次买点',
    '首阴': '断板首日，观察是否缩量、是否守住EMA12/分歧日低点，不急于接',
    '退潮': '断板后连续走弱或情绪退潮，只做反包确认，不抄底',
    '反包': '断板后重新涨停，二波/弱转强确认，需量能与题材同步配合',
}


def _j(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def _dump(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f:
        json.dump(obj, f, ensure_ascii=False)


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if b else None


def _mean(a):
    return round(statistics.mean(a), 2) if a else None


def _win(a):
    return round(sum(1 for x in a if x > 0) / len(a) * 100, 1) if a else None


def _is_generic(tag):
    if not tag or len(tag) < 2:
        return True
    return any(g in tag for g in GENERIC)


def _board_type(code):
    if code.startswith('bj'):
        return '北证30cm'
    if code.startswith('sz30'):
        return '创业20cm'
    if code.startswith('sh68'):
        return '科创20cm'
    return '主板10cm'


def _limit(prev, ratio):
    return round(prev * (1 + ratio), 2)


def _bench_cum(idx_rets, i, h):
    if i is None or i + h >= len(idx_rets):
        return None
    v = 1.0
    for r in idx_rets[i + 1:i + h + 1]:
        v *= 1 + r
    return round((v - 1) * 100, 2)


# ---------------------------------------------------------------------------
# 1) 全历史盘面重建：逐日涨停/炸板/连板梯队、题材热度、情绪周期
# ---------------------------------------------------------------------------
def rebuild(D):
    """只用日K重建盘面。返回内存 board（不落盘明细，避免几百 MB JSON）。"""
    d2i = {d: i for i, d in enumerate(D.idx_days)}
    con, ind = D.con, D.ind
    zt_by = collections.defaultdict(list)
    zb_by = collections.Counter()

    for code, kk in D.k.items():
        name = D.meta.get(code, {}).get('name', '')
        if _srv.is_st(name):
            continue
        ratio = _srv.limit_ratio(code, name)
        dates, o, c, h, l, v = kk['dates'], kk['o'], kk['c'], kk['h'], kk['l'], kk['v']
        e12, e50 = kk['e12'], kk['e50']
        cons = [x for x in con.get(code, []) if not _is_generic(x)][:12]
        indu = ind.get(code, '其他') or '其他'
        mv = D.meta.get(code, {}).get('mv') or 0
        pe = D.meta.get(code, {}).get('pe') or 0
        n = len(c)
        streak, first = 0, None
        for t in range(1, n):
            d = dates[t]
            prev = c[t - 1]
            if prev <= 0:
                continue
            lim = _limit(prev, ratio)
            sealed = c[t] >= lim - 0.001
            if not sealed:
                streak, first = 0, None
                if h[t] >= lim - 0.001 and d in d2i:
                    zb_by[d] += 1
                continue
            streak += 1
            if streak == 1:
                first = d
            if d not in d2i or t < 60:
                continue
            w = v[max(0, t - 5):t]
            vr = round(v[t] / (sum(w) / len(w)), 2) if w and sum(w) > 0 else 0
            rec = dict(
                code=code, name=name, date=d, t=t, ratio=ratio, lim=round(lim, 2),
                streak=streak, first_zt=first, ind=indu, cons=cons,
                n_cons=len(cons), mv=round(mv, 1), pe=round(pe, 1),
                close=round(c[t], 2), prev=round(prev, 2), low=round(l[t], 2),
                high=round(h[t], 2), board=_board_type(code),
                chg=round((c[t] / prev - 1) * 100, 2),
                open_gap=round((o[t] / prev - 1) * 100, 2),
                seal_gap=round(max(0.0, (lim - l[t]) / lim * 100), 2),
                one_word=int(l[t] >= lim - 0.001),
                vr=vr,
                c20=round((c[t] / c[max(0, t - 20)] - 1) * 100, 1) if c[max(0, t - 20)] else 0,
                c60=round((c[t] / c[max(0, t - 60)] - 1) * 100, 1) if c[max(0, t - 60)] else 0,
                dev12=round((c[t] / e12[t] - 1) * 100, 1) if e12[t] else 0,
                dev50=round((c[t] / e50[t] - 1) * 100, 1) if e50[t] else 0,
                e12=round(e12[t], 2), e50=round(e50[t], 2),
            )
            zt_by[d].append(rec)

    days, zt_set = {}, {}
    prev_n1 = None
    for d in D.idx_days:
        zts = zt_by.get(d, [])
        zb = zb_by.get(d, 0)
        ladder = collections.Counter(r['streak'] for r in zts)
        hmax = max(ladder) if ladder else 0
        n_zt, n_touch = len(zts), len(zts) + zb
        zb_rate = round(zb / n_touch * 100, 1) if n_touch else 0.0
        promo = round(ladder.get(2, 0) / prev_n1 * 100, 1) if prev_n1 else None
        prev_n1 = ladder.get(1, 0)
        s1 = min(40.0, n_zt * 0.4)
        s2 = min(25.0, hmax * 2.5)
        s3 = min(20.0, (promo or 0) * 0.4)
        s4 = 15.0 * (1 - min(1.0, zb_rate / 100 * 2))
        temp = round(s1 + s2 + s3 + s4, 1)
        phase = '冰点' if temp < 20 else '退潮' if temp < 40 else '回暖' if temp < 60 else '高潮'
        days[d] = dict(date=d, n_zt=n_zt, zb=zb, zb_rate=zb_rate,
                       ladder=dict(ladder), hmax=hmax, promo=promo,
                       temp=temp, phase=phase, regime=D.regime.get(d, 'down'),
                       zt=zts)
        zt_set[d] = {r['code'] for r in zts}

    # 题材热度：逐日聚合 → 5日滚动 → 题材爆发天数 → 回填到每只涨停股（PIT）
    themes = {}
    tag_daily = collections.defaultdict(dict)
    for d in D.idx_days:
        agg = {}
        for r in days[d]['zt']:
            for tag in [r['ind']] + r['cons']:
                if _is_generic(tag):
                    continue
                e = agg.setdefault(tag, dict(tag=tag, n_zt=0, n2=0, n3=0,
                                             max_streak=0, codes=[], firsts=[]))
                e['n_zt'] += 1
                if r['streak'] >= 2:
                    e['n2'] += 1
                if r['streak'] >= 3:
                    e['n3'] += 1
                e['max_streak'] = max(e['max_streak'], r['streak'])
                e['codes'].append(r['code'])
                e['firsts'].append(r['first_zt'])
        themes[d] = agg
        for tag, e in agg.items():
            tag_daily[tag][d] = [e['n_zt'], e['n2'], e['n3'], e['max_streak']]

    all_tags = set()
    for d in D.idx_days:
        all_tags.update(themes[d].keys())
    tag_run = collections.defaultdict(int)
    for pos, d in enumerate(D.idx_days):
        win = D.idx_days[max(0, pos - 4):pos + 1]
        cur = themes[d]
        for tag in all_tags:
            if tag in cur and cur[tag]['n_zt'] >= 2:
                tag_run[tag] += 1
            else:
                tag_run[tag] = 0
        for tag, e in cur.items():
            vals = [tag_daily[tag][x] for x in win if x in tag_daily[tag]]
            e['n5'] = sum(v[0] for v in vals)
            e['n2_5'] = sum(v[1] for v in vals)
            e['n3_5'] = sum(v[2] for v in vals)
            e['max5'] = max((v[3] for v in vals), default=0)
            e['day'] = tag_run[tag]   # 题材爆发第几天：连续 n_zt>=2 的天数（含当日）
            e['score'] = round(e['n5'] * 1.0 + e['n_zt'] * 1.5 +
                               e['max_streak'] * 2.0 + e['n3_5'] * 1.5 +
                               (3 if e['n2_5'] >= 2 else 0), 1)

    for d in D.idx_days:
        for r in days[d]['zt']:
            hmax = days[d]['hmax']
            r['is_top'] = int(r['streak'] >= hmax and hmax > 0)
            best = None
            for tag in [r['ind']] + r['cons']:
                e = themes[d].get(tag)
                if not e:
                    continue
                if best is None or e['score'] > best[1]['score']:
                    best = (tag, e)
            if best:
                tag, e = best
                r['theme'] = tag
                r['theme_score'] = e['score']
                r['theme_n5'] = e['n5']
                r['theme_n_zt'] = e['n_zt']
                r['theme_day'] = e['day']
                r['theme_max'] = e['max_streak']
                r['theme_n3_5'] = e['n3_5']
                r['followers'] = max(0, len(set(e['codes'])) - 1)
                r['theme_lead'] = int(r['streak'] >= e['max_streak'])
            else:
                r.update(theme=r['ind'], theme_score=0, theme_n5=0, theme_n_zt=1,
                         theme_day=1, theme_max=r['streak'], theme_n3_5=0,
                         followers=0, theme_lead=0)
            e = days[d]
            r['phase'] = e['phase']
            r['temp'] = e['temp']
            r['mkt_hmax'] = e['hmax']
            r['zb_rate'] = e['zb_rate']
            r['promo'] = e['promo']
            r['mkt_zt'] = e['n_zt']
            r['regime'] = e['regime']

    return dict(as_of=D.idx_days[-1], days=days, themes=themes,
                zt_set=zt_set, d2i=d2i)


# ---------------------------------------------------------------------------
# 2) 龙头候选 + 前向收益（接力 / 回踩低吸 两套可执行口径）
# ---------------------------------------------------------------------------
def _candidate_ok(r):
    """当日龙头候选口径：连板≥2 / 市场最高板 / 题材内有跟风且自己是最高板。"""
    return (r['streak'] >= 2 or r['is_top'] or
            (r['followers'] >= 2 and r['theme_lead']))


def _stage(r, board, d, cal):
    """个股生命周期阶段（只用 t 及之前数据）。"""
    pos = board['d2i'][d]
    zt_set = board['zt_set']
    prev_date = cal[pos - 1] if pos > 0 else None
    prev_zt = bool(prev_date and r['code'] in zt_set.get(prev_date, set()))
    # 近5日曾涨停（断板后反包判定）
    recent = any(r['code'] in zt_set.get(cal[j], set())
                 for j in range(max(0, pos - 5), pos))
    if r['streak'] >= 3 and (r['vr'] < 0.85 or r['one_word']):
        return '加速'
    if r['streak'] >= 2 and (r['vr'] >= 2.5 or r['seal_gap'] >= 6.0):
        return '分歧'
    if r['streak'] == 1 and recent:
        return '反包' if not prev_zt else '启动'
    if r['streak'] == 1:
        return '启动'
    if r['streak'] == 2:
        return '发酵'
    return '加速'


def scan(D, board):
    """全历史龙头候选 → PIT 特征 + 前向收益。"""
    cal = D.idx_days
    out = []
    for d in cal:
        b = board['days'][d]
        for r in b['zt']:
            if not _candidate_ok(r):
                continue
            kk = D.k.get(r['code'])
            if not kk:
                continue
            t = r['t']
            c, o, h, l = kk['c'], kk['o'], kk['h'], kk['l']
            last = len(c) - 1
            if t + 1 > last:
                continue   # 没有次日数据，不产生可交易信号
            buy = o[t + 1]
            lim_next = _limit(c[t], r['ratio'])
            # 次日一字涨停开盘 = 买不进；开盘价为0/异常 = 数据无效
            buyable = bool(buy > 0 and not (buy >= lim_next - 0.001 and l[t + 1] >= lim_next - 0.001))
            fwd, bench, exc = {}, {}, {}
            i = board['d2i'].get(d)
            for hh in HOR:
                if t + hh <= last and buy > 0:
                    fwd['d%d' % hh] = _pct(c[t + hh], buy)
                bp = _bench_cum(D.idx_rets, i, hh)
                if bp is not None:
                    bench['b%d' % hh] = bp
                    if fwd.get('d%d' % hh) is not None:
                        exc['e%d' % hh] = round(fwd['d%d' % hh] - bp, 2)
            # 回踩低吸：5日内限价挂在 max(EMA12, 收盘-7%)，未触发=不成交
            pull = round(max(kk['e12'][t], c[t] * 0.93), 2)
            filled = None
            for j in range(1, 6):
                if t + j <= last and l[t + j] <= pull:
                    filled = j
                    break
            low_d5 = None
            if filled and t + filled + 5 <= last:
                low_d5 = _pct(c[t + filled + 5], pull)
            # 路径风险（次日买入后10日最大上/下行）
            hi = max(h[t + 1:min(last + 1, t + 11)], default=None)
            lo = min(l[t + 1:min(last + 1, t + 11)], default=None)
            stage = _stage(r, board, d, cal)
            rec = dict(
                date=d, code=r['code'], name=r['name'], ind=r['ind'], theme=r['theme'],
                board=r['board'], streak=r['streak'], is_top=r['is_top'],
                theme_lead=r['theme_lead'], followers=r['followers'],
                theme_n5=r['theme_n5'], theme_n_zt=r['theme_n_zt'],
                theme_day=r['theme_day'], theme_max=r['theme_max'],
                theme_score=r['theme_score'], seal_gap=r['seal_gap'],
                one_word=r['one_word'], vr=r['vr'], open_gap=r['open_gap'],
                c20=r['c20'], c60=r['c60'], dev12=r['dev12'], dev50=r['dev50'],
                mv=r['mv'], pe=r['pe'], phase=r['phase'], temp=r['temp'],
                mkt_hmax=r['mkt_hmax'], zb_rate=r['zb_rate'], promo=r['promo'],
                mkt_zt=r['mkt_zt'], regime=r['regime'], stage=stage,
                close=r['close'], buy_px=round(buy, 2), buyable=int(buyable),
                maxup=_pct(hi, buy) if hi and buy else None,
                maxdn=_pct(lo, buy) if lo and buy else None,
                pull_px=pull, low_filled=int(bool(filled)), low_at=filled,
                low_d5=low_d5,
                **fwd, **bench, **exc,
            )
            out.append(rec)
    return out


CSV_COLS = [
    'date', 'code', 'name', 'ind', 'theme', 'board', 'streak', 'is_top', 'theme_lead',
    'followers', 'theme_n5', 'theme_n_zt', 'theme_day', 'theme_max', 'theme_score',
    'seal_gap', 'one_word', 'vr', 'open_gap', 'c20', 'c60', 'dev12', 'dev50',
    'mv', 'pe', 'phase', 'temp', 'mkt_hmax', 'zb_rate', 'promo', 'mkt_zt', 'regime',
    'stage', 'close', 'buy_px', 'buyable', 'd1', 'd3', 'd5', 'd10',
    'b1', 'b3', 'b5', 'b10', 'e1', 'e3', 'e5', 'e10',
    'maxup', 'maxdn', 'pull_px', 'low_filled', 'low_at', 'low_d5',
]


def write_signals(sigs):
    os.makedirs(DIR, exist_ok=True)
    with open(SIGCSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction='ignore')
        w.writeheader()
        for r in sigs:
            w.writerow({k: r.get(k) for k in CSV_COLS})
    _dump(SIGJSON, dict(as_of=sigs[-1]['date'] if sigs else None,
                        n=len(sigs), columns=CSV_COLS,
                        note='每行一个「当日龙头候选」信号；buy_px=次日开盘价，buyable=0 表示次日一字板买不进',
                        signals=sigs))


# ---------------------------------------------------------------------------
# 3) 分组统计 + 因子 edge（评分权重依据）
# ---------------------------------------------------------------------------
def _bucket_stat(rows, key='d5'):
    vals = [r[key] for r in rows if r.get(key) is not None]
    ex = [r.get('e5') for r in rows if r.get('e5') is not None]
    return dict(n=len(vals), avg=_mean(vals), win=_win(vals),
                exc=_mean(ex), worst=round(min(vals), 2) if vals else None,
                d1=_mean([r['d1'] for r in rows if r.get('d1') is not None]),
                d3=_mean([r['d3'] for r in rows if r.get('d3') is not None]),
                d10=_mean([r['d10'] for r in rows if r.get('d10') is not None]))


def make_stats(sigs, board, D):
    base = [r for r in sigs if r['buyable'] and r.get('d5') is not None]
    unbuy = [r for r in sigs if not r['buyable']]
    st = dict(
        as_of=board['as_of'], n_signals=len(sigs), n_buyable=len(base),
        n_unbuyable=len(unbuy),
        unbuyable_pct=round(len(unbuy) / len(sigs) * 100, 1) if sigs else None,
        overall=_bucket_stat(base),
        buckets={}, combos={}, factors={}, phases={}, recent_days=[],
        lowbuy=dict(
            n=sum(1 for r in base if r['low_filled']),
            fill_rate=round(sum(1 for r in base if r['low_filled']) / len(base) * 100, 1) if base else None,
            avg_d5=_mean([r['low_d5'] for r in base if r['low_filled'] and r.get('low_d5') is not None]),
            win_d5=_win([r['low_d5'] for r in base if r['low_filled'] and r.get('low_d5') is not None]),
        ),
    )

    def bucket(name, fn):
        st['buckets'][name] = _bucket_stat([r for r in base if fn(r)])

    bucket('全样本（可买）', lambda r: True)
    for lo, hi, nm in ((1, 1, '首板'), (2, 2, '2板'), (3, 3, '3板'), (4, 99, '4板及以上')):
        bucket('高度·' + nm, lambda r, a=lo, b=hi: a <= r['streak'] <= b)
    for p in ('冰点', '退潮', '回暖', '高潮'):
        bucket('情绪·' + p, lambda r, x=p: r['phase'] == x)
    for g in ('up', 'down'):
        bucket('regime·' + g, lambda r, x=g: r['regime'] == x)
    bucket('市场最高板', lambda r: r['is_top'])
    bucket('非最高板', lambda r: not r['is_top'])
    bucket('题材内最高板', lambda r: r['theme_lead'])
    bucket('有跟风(≥2家)', lambda r: r['followers'] >= 2)
    bucket('无跟风', lambda r: r['followers'] == 0)
    bucket('主线热度n5≥8', lambda r: r['theme_n5'] >= 8)
    bucket('主线热度n5<8', lambda r: r['theme_n5'] < 8)
    bucket('封板紧(未开板)', lambda r: r['seal_gap'] <= 0.2)
    bucket('封板松(开板>2%)', lambda r: r['seal_gap'] > 2)
    bucket('一字板', lambda r: r['one_word'])
    bucket('温和放量(1≤vr≤2.2)', lambda r: 1 <= r['vr'] <= 2.2)
    bucket('巨量(vr>3)', lambda r: r['vr'] > 3)
    bucket('小市值(≤60亿)', lambda r: r['mv'] <= 60)
    bucket('大市值(>200亿)', lambda r: r['mv'] > 200)
    bucket('题材启动前2天', lambda r: r['theme_day'] <= 2)
    bucket('题材第3天以后', lambda r: r['theme_day'] >= 3)
    for s in STAGE_NOTE:
        bucket('阶段·' + s, lambda r, x=s: r['stage'] == x)
    bucket('20cm/30cm板', lambda r: r['board'] != '主板10cm')

    # 关键组合
    def combo(name, fn):
        st['combos'][name] = _bucket_stat([r for r in base if fn(r)])

    combo('2板+题材有跟风+情绪回暖/高潮',
          lambda r: r['streak'] == 2 and r['followers'] >= 2 and r['phase'] in ('回暖', '高潮'))
    combo('2板+题材有跟风+封板紧',
          lambda r: r['streak'] == 2 and r['followers'] >= 2 and r['seal_gap'] <= 0.5)
    combo('最高板+情绪高潮',
          lambda r: r['is_top'] and r['phase'] == '高潮')
    combo('最高板+情绪退潮/冰点',
          lambda r: r['is_top'] and r['phase'] in ('退潮', '冰点'))
    combo('4板以上+缩量加速',
          lambda r: r['streak'] >= 4 and r['vr'] < 0.9)
    combo('4板以上+巨量分歧',
          lambda r: r['streak'] >= 4 and r['vr'] > 2.5)
    combo('题材n5≥8+题材内最高板',
          lambda r: r['theme_n5'] >= 8 and r['theme_lead'])
    combo('一字板（次日多数买不进）',
          lambda r: r['one_word'])

    # 因子 edge：高分组 - 低分组的 5日超额差（用于自动校准评分权重）
    fac_defs = {
        'height': ('streak', lambda r: r['streak']),
        'theme': ('theme_n5', lambda r: r['theme_n5']),
        'follow': ('followers', lambda r: r['followers']),
        'seal': ('seal_gap', lambda r: -r['seal_gap']),
        'vol': ('vr', lambda r: -abs(r['vr'] - 1.6)),
        'pos': ('dev12', lambda r: -abs(r['dev12'] - 8)),
        'emotion': ('temp', lambda r: r['temp']),
        'mv': ('mv', lambda r: r['mv']),
        'c20': ('c20', lambda r: -r['c20']),
    }
    for name, (col, fn) in fac_defs.items():
        arr = sorted(base, key=fn)
        if len(arr) < 200:
            continue
        k = max(1, len(arr) // 4)
        lo, hi = arr[:k], arr[-k:]
        elo = _mean([r['e5'] for r in lo if r.get('e5') is not None])
        ehi = _mean([r['e5'] for r in hi if r.get('e5') is not None])
        if elo is None or ehi is None:
            continue
        st['factors'][name] = dict(col=col, low=elo, high=ehi,
                                   edge=round(ehi - elo, 2),
                                   low_avg=_mean([r['d5'] for r in lo if r.get('d5') is not None]),
                                   high_avg=_mean([r['d5'] for r in hi if r.get('d5') is not None]),
                                   n_low=len(lo), n_high=len(hi))
    # 情绪阶段时序（前端周期图）
    cal = D.idx_days
    st['recent_days'] = [dict(date=d, temp=board['days'][d]['temp'],
                              phase=board['days'][d]['phase'],
                              n_zt=board['days'][d]['n_zt'],
                              hmax=board['days'][d]['hmax'],
                              zb_rate=board['days'][d]['zb_rate'],
                              promo=board['days'][d]['promo'],
                              n_sig=sum(1 for r in sigs if r['date'] == d))
                         for d in cal[-60:]]
    st['rules'] = discover_rules(sigs)
    return st


# ---- 自动规则发现：条件网格 + 前后半期样本外稳定性检验 ----------------------
# 传统龙头战法偏爱的「高标/一字/封板紧/小盘」在本次全历史回测中普遍是负期望，
# 因此不手工写死规则，而是让数据自己找出正期望组合，并检验时间稳定性。
RULE_CONDS = [
    ('大市值>200亿', lambda r: r['mv'] > 200),
    ('非小盘>60亿', lambda r: r['mv'] > 60),
    ('中大盘80~500亿', lambda r: 80 <= r['mv'] <= 500),
    ('首板', lambda r: r['streak'] == 1),
    ('2板', lambda r: r['streak'] == 2),
    ('高位≥3板', lambda r: r['streak'] >= 3),
    ('非一字', lambda r: not r['one_word']),
    ('有开板/振幅>2%', lambda r: r['seal_gap'] > 2),
    ('温和量能0.8~2.2', lambda r: 0.8 <= r['vr'] <= 2.2),
    ('缩量<1', lambda r: r['vr'] < 1),
    ('位置健康0~15%', lambda r: 0 <= r['dev12'] <= 15),
    ('低乖离<5%', lambda r: r['dev12'] < 5),
    ('情绪回暖/高潮', lambda r: r['phase'] in ('回暖', '高潮')),
    ('情绪温度≥55', lambda r: r['temp'] >= 55),
    ('情绪退潮/冰点', lambda r: r['phase'] in ('退潮', '冰点')),
    ('题材启动前2天', lambda r: r['theme_day'] <= 2),
    ('主线热度n5≥8', lambda r: r['theme_n5'] >= 8),
    ('有跟风≥2家', lambda r: r['followers'] >= 2),
    ('主板10cm', lambda r: r['board'] == '主板10cm'),
    ('regime上行', lambda r: r['regime'] == 'up'),
    ('未透支c20≤30', lambda r: r['c20'] <= 30),
    ('非加速阶段', lambda r: r['stage'] != '加速'),
    ('反包阶段', lambda r: r['stage'] == '反包'),
    ('市场最高板', lambda r: bool(r['is_top'])),
]


def rule_match(r, bits):
    return all(RULE_CONDS[i][1](r) for i in bits)


def discover_rules(sigs, min_n=60):
    base = [r for r in sigs if r['buyable'] and r.get('e5') is not None and r.get('d5') is not None]
    if len(base) < 300:
        return dict(tier1=[], tier2=[], traps=[], good=[], bad=[],
                    n_tested=0, mid_date=None, conds=[c[0] for c in RULE_CONDS])
    dates = sorted({r['date'] for r in base})
    mid = dates[len(dates) // 2]
    masks, e5, d5, halves = [], [], [], []
    for r in base:
        m = 0
        for i, (nm, fn) in enumerate(RULE_CONDS):
            if fn(r):
                m |= 1 << i
        masks.append(m)
        e5.append(r['e5'])
        d5.append(r['d5'])
        halves.append(0 if r['date'] < mid else 1)
    n = len(RULE_CONDS)
    combos = [(i,) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            combos.append((i, j))
    # 3 条件：从单条件正超额的前 12 个里组合，控制组合爆炸
    solo = []
    for i in range(n):
        bit = 1 << i
        sel = [k for k, m in enumerate(masks) if m & bit]
        if len(sel) >= min_n:
            solo.append((statistics.mean([e5[k] for k in sel]), i))
    topidx = [i for _, i in sorted(solo, reverse=True)[:12]]
    for a in range(len(topidx)):
        for b in range(a + 1, len(topidx)):
            for c in range(b + 1, len(topidx)):
                combos.append((topidx[a], topidx[b], topidx[c]))
    res = []
    for cb in combos:
        target = sum(1 << i for i in cb)
        sel = [k for k, m in enumerate(masks) if m & target == target]
        if len(sel) < min_n:
            continue
        h0 = [e5[k] for k in sel if halves[k] == 0]
        h1 = [e5[k] for k in sel if halves[k] == 1]
        ex = statistics.mean([e5[k] for k in sel])
        stable = bool(len(h0) >= 20 and len(h1) >= 20 and
                      statistics.mean(h0) > 0 and statistics.mean(h1) > 0)
        res.append(dict(conds=[RULE_CONDS[i][0] for i in cb], bits=list(cb),
                        n=len(sel), avg5=round(statistics.mean([d5[k] for k in sel]), 2),
                        win5=_win([d5[k] for k in sel]), exc5=round(ex, 2),
                        h0=round(statistics.mean(h0), 2) if h0 else None,
                        h1=round(statistics.mean(h1), 2) if h1 else None,
                        stable=stable))
    # tier1：双/三条件强规则（stable 优先）；tier2：单条件且前后半期都正的超额基础条件
    strong = [x for x in res if len(x['bits']) >= 2 and x['exc5'] > 0]
    tier1 = sorted(strong, key=lambda x: (not x['stable'], -x['exc5']))[:12]
    tier2 = sorted([x for x in res if len(x['bits']) == 1 and x['stable'] and x['exc5'] > 0],
                   key=lambda x: -x['exc5'])[:10]
    traps = sorted([x for x in res if x['exc5'] < 0], key=lambda x: x['exc5'])[:8]
    return dict(tier1=tier1, tier2=tier2, traps=traps,
                good=tier1, bad=traps, n_tested=len(res), mid_date=mid,
                conds=[c[0] for c in RULE_CONDS],
                note='条件网格单/双/三组合；stable=前后半期超额都为正（样本外稳健性粗检）')


def calibrate(st):
    """权重 + 方向。edge 显著为负的维度要**反向计分**（数据说连板高度/题材热度
    在次日接力口径下是反向指标，若继续给正分就是在系统性地推高陷阱票）。"""
    base_w = dict(height=18, theme=16, follow=10, seal=12, vol=10,
                  pos=10, emotion=8, mv=16, c20=10)
    out = {}
    for k, w in base_w.items():
        f = (st.get('factors') or {}).get(k) or {}
        edge = f.get('edge') or 0
        direction = 1 if edge > 0.35 else (-1 if edge < -0.35 else 0)
        ww = w * max(0.5, min(1.6, 0.8 + abs(edge) / 3.0))
        out[k] = dict(w=round(ww, 1), dir=direction, edge=edge)
    tot = sum(v['w'] for v in out.values())
    for k in out:
        out[k]['w'] = round(out[k]['w'] / tot * 100, 1)
    return out


# ---------------------------------------------------------------------------
# 4) 当日龙头池：评分 / 生命周期 / 介入点 / 风险
# ---------------------------------------------------------------------------
def _clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def score_one(r, b, cal, rules=None):
    """0~100 分。线性分由 edge 决定方向与权重，再叠加「已验证规则」命中/陷阱扣分。"""
    s = {}
    # 各维度 raw=1 代表「传统龙头战法认为最好」，方向由回测 edge 决定
    s['height'] = _clamp((min(r['streak'], 6) / 5.0) * 0.75 + 0.25 * r['is_top'])
    s['theme'] = _clamp(min(r['theme_n5'], 20) / 15.0 * 0.7 +
                        min(r['theme_n_zt'], 8) / 8.0 * 0.3)
    s['follow'] = _clamp(min(r['followers'], 6) / 5.0 * 0.7 + 0.3 * r['theme_lead'])
    s['seal'] = _clamp(1.0 - min(r['seal_gap'], 8) / 8.0) * 0.7 + \
                _clamp(1.0 - abs(r['vr'] - 1.6) / 3.0) * 0.3
    s['vol'] = _clamp(1.0 - abs(r['vr'] - 1.6) / 3.0)
    s['pos'] = _clamp(1.0 - abs(r['dev12'] - 8) / 25.0) * 0.6 + \
               _clamp(1.0 - max(0, r['c20'] - 25) / 60.0) * 0.4
    pm = {'冰点': 0.25, '退潮': 0.4, '回暖': 0.9, '高潮': 0.75}
    s['emotion'] = pm.get(r['phase'], 0.5) * _clamp(0.6 + b['promo'] / 60.0 if b['promo'] else 0.6)
    # 市值分层（全历史最强分层因子）：80~500亿最优，超大市值弱正，≤60亿显著为负
    mv = r['mv']
    s['mv'] = (1.0 if 80 <= mv <= 500 else
               0.78 if 500 < mv <= 1200 else
               0.62 if mv > 1200 else
               0.50 if 60 < mv < 80 else
               0.25 if 30 <= mv <= 60 else 0.08)
    s['c20'] = _clamp(1.0 - max(0, r['c20'] - 10) / 60.0)
    parts = {}
    lin = 0.0
    for k, raw in s.items():
        c = cal.get(k, dict(w=0, dir=0))
        eff = raw if c['dir'] > 0 else (1 - raw if c['dir'] < 0 else 0.5)
        parts[k] = round(eff * c['w'], 1)
        lin += eff * c['w']
    # 规则命中：tier1=已验证强组合，tier2=稳定正超额基础条件，traps=已验证陷阱
    hit1, hit2, trap = [], [], []
    for rule in (rules or {}).get('tier1', [])[:8]:
        if rule_match(r, rule['bits']):
            hit1.append('+'.join(rule['conds']))
    for rule in (rules or {}).get('tier2', []):
        if rule_match(r, rule['bits']):
            hit2.append(rule['conds'][0])
    for rule in (rules or {}).get('traps', [])[:6]:
        if rule_match(r, rule['bits']):
            trap.append('+'.join(rule['conds']))
    bonus = min(16, len(hit1) * 8 + len(hit2) * 2) - min(18, len(trap) * 6)
    total = round(max(0.0, min(100.0, lin + bonus)), 1)
    # 分级：A=命中已验证强组合（历史5日+3~5%）；B=仅市值合格的弱正期望（+1%左右）；
    #       D=命中稳定陷阱组合；C=其余观察
    mv_ok = bool(80 <= r['mv'] <= 500)   # 数据最强分层：中大盘 80~500亿
    if trap:
        grade = 'D'
    elif hit1:
        grade = 'A'
    elif mv_ok and hit2:
        grade = 'B'
    else:
        grade = 'C'
    qualified = grade in ('A', 'B')
    parts['规则加成'] = round(bonus, 1)
    return total, parts, hit1 + hit2, trap, qualified, grade


def _entry_plan(r, stage, b):
    """给出可执行价格：打板确认 / 弱转强 / 回踩低吸 / 硬止损 / 目标。"""
    c = r['close']
    ratio = r['ratio']
    lim_next = round(c * (1 + ratio), 2)
    pull_hi = round(min(r['e12'] * 1.01, c * 0.97), 2)
    pull_lo = round(max(r['e12'] * 0.97, c * 0.90), 2)
    if pull_lo > pull_hi:
        pull_lo, pull_hi = round(pull_hi * 0.98, 2), pull_hi
    # 两套止损不能混用：接力买在次日涨停价，止损按打板价 -7%；
    # 回踩低吸买在 EMA12 附近，止损按最低低吸价 -5%。
    # （旧版用信号日最低价当共同止损，会出现「止损落在低吸区间内」或打板止损宽到 -15% 的错误）
    chase_stop = round(max(c * 0.97, lim_next * 0.93), 2)
    pull_stop = round(pull_lo * 0.95, 2)
    primary = {
        '启动': '次日不追高开，等 2 板确认（打板价 %.2f）或回踩 %.2f~%.2f 低吸' % (lim_next, pull_lo, pull_hi),
        '发酵': '首选回踩 %.2f~%.2f 低吸；次选次日站上 %.2f 且量能不缩做接力' % (pull_lo, pull_hi, lim_next),
        '加速': '不追。持筹者持有，场外只等首次分歧后弱转强（不破 %.2f）' % r['low'],
        '分歧': '弱转强确认：次日开盘不破 %.2f 且重新封板 %.2f 才跟；破 %.2f 放弃' % (r['low'], lim_next, r['low']),
        '首阴': '缩量守住 %.2f 且次日反包 %.2f 再考虑，不接飞刀' % (r['e12'], lim_next),
        '退潮': '只做反包确认（%.2f），否则观望' % lim_next,
        '反包': '反包板 %.2f 确认，止损 %.2f；量能需放大且题材同步' % (lim_next, r['low']),
    }.get(stage, '回踩 %.2f~%.2f 低吸' % (pull_lo, pull_hi))
    pos = {'冰点': '≤1/4 试仓', '退潮': '≤1/4 试仓', '回暖': '≤1/3 仓', '高潮': '≤1/4 仓（高位兑现优先）'}
    return dict(
        board_limit=lim_next, pullback=[pull_lo, pull_hi],
        board_note='打板确认价=次日涨停价；一字开盘买不进则放弃，不排队',
        weak_turn=round(r['low'], 2),
        weak_turn_note='弱转强/分歧转一致：次日不破信号日最低价 %.2f 且重新走强' % r['low'],
        pullback_note='回踩低吸区（EMA12 %.2f 与收盘-3%% 取较近者）' % r['e12'],
        hard_stop=chase_stop, pull_stop=pull_stop,
        stop_rule=('接力/打板：跌破信号日最低价或约 -7%% 先走（%.2f）；'
                   '回踩低吸：跌破 %.2f 或 EMA50 走；'
                   '中线卖出仍统一「收盘破EMA50连续3日未收回（含跌破当日）」'
                   % (chase_stop, pull_stop)),
        ema50_ref=round(r['e50'], 2), trail_stop=round(r['e50'] * 0.99, 2),
        target1=round(c * (1 + ratio * 1.2), 2),
        target2=round(c * (1 + ratio * 2.2), 2),
        primary=primary,
        position=pos.get(r['phase'], '≤1/4 仓'),
        chase_cap=round(c * 1.03, 2),
        chase_note='次日高开 >%.2f（+3%%）不追；历史大阳次日高开追入期望为负' % round(c * 1.03, 2),
    )


def _risk_flags(r, b, st):
    f = []
    if r['one_word']:
        f.append('一字板：次日大概率买不进（历史不可买占比 %s%%），勿把纸面收益当实盘' % st.get('unbuyable_pct'))
    if r['streak'] >= 4:
        f.append('%d板高位：加速末端，断板回撤通常最快，只适合持筹不适合新开仓' % r['streak'])
    if r['phase'] in ('冰点', '退潮'):
        f.append('情绪%s：接力晋级率 %s%%，空间板容错极低' % (r['phase'], b['promo']))
    if r['phase'] == '高潮':
        f.append('情绪高潮：一致转分歧随时发生，优先兑现不优先加仓')
    if r['vr'] > 3:
        f.append('量比 %.2f 巨量分歧：换手过大，次日方向取决于弱转强确认' % r['vr'])
    if r['dev12'] > 20:
        f.append('距EMA12 +%.1f%%：乖离过大，回踩需求强' % r['dev12'])
    if r['c20'] > 60:
        f.append('20日涨幅 +%.1f%%：透支明显' % r['c20'])
    if r['board'] != '主板10cm':
        f.append('%s：单日波动20~30cm，仓位按主板减半' % r['board'])
    if r['pe'] and r['pe'] <= 0:
        f.append('亏损股（PE≤0）：题材博弈属性更强，基本面无支撑')
    if r['followers'] == 0:
        f.append('无题材跟风：独苗行情，缺乏板块合力，持续性弱')
    f.append('龙头地位是事后确认的：本表是「当日候选」，不等于最终龙头')
    return f


def build(D, board, st, cal, topn=TOPN):
    as_of = board['as_of']
    b = board['days'][as_of]
    zt_set = board['zt_set']
    cal_days = D.idx_days
    pos = board['d2i'][as_of]

    cands = []
    for r in b['zt']:
        if not _candidate_ok(r):
            continue
        stage = _stage(r, board, as_of, cal_days)
        x = dict(r)
        x.pop('t', None)
        x['stage'] = stage
        sc, parts, hit, trap, qual, grade = score_one(x, b, cal, st.get('rules'))
        mv = r['mv']
        tier = ('中大盘80~500亿' if 80 <= mv <= 500 else
                '大市值500~1200亿' if 500 < mv <= 1200 else
                '超大盘>1200亿' if mv > 1200 else
                '中小盘60~80亿' if 60 < mv < 80 else
                '小盘30~60亿' if 30 <= mv <= 60 else '微盘<30亿')
        x.update(score=sc, breakdown=parts, stage=stage, mv_tier=tier,
                 grade=grade,
                 stage_note=STAGE_NOTE.get(stage, ''),
                 rule_hits=hit, rule_traps=trap, qualified=qual,
                 entry=_entry_plan(r, stage, b),
                 risks=_risk_flags(r, b, st))
        cands.append(x)
    cands.sort(key=lambda x: (-x['score'], -x['mv'], x['code']))
    for i, x in enumerate(cands):
        x['rank'] = i + 1

    # 总龙头 / 空间龙头：当日最高板；并列按封板质量、题材强度、带动性择优
    tops = [x for x in cands if x['is_top']]
    tops.sort(key=lambda x: (-x['streak'], x['seal_gap'], -x['theme_score'], -x['followers']))
    market_leader = tops[0] if tops else None

    # 题材主线（取当日 score 前 8，且 n_zt>=2）
    th = sorted(board['themes'][as_of].values(), key=lambda e: -e['score'])
    themes = []
    for e in th[:10]:
        if e['n_zt'] < 2:
            continue
        members = [x for x in cands if x['theme'] == e['tag']]
        if not members:
            continue
        lead = members[0]
        themes.append(dict(tag=e['tag'], n_zt=e['n_zt'], n5=e['n5'],
                           max_streak=e['max_streak'], n3_5=e['n3_5'],
                           day=e['day'], score=e['score'],
                           leader=lead['code'], leader_name=lead['name'],
                           leader_streak=lead['streak'], stage=lead['stage'],
                           members=[m['code'] for m in members[:6]]))
    themes.sort(key=lambda x: -x['score'])

    # 断板 / 首阴观察（前一交易日涨停、今日未涨停，且近5日有涨停）
    prev_date = cal_days[pos - 1] if pos > 0 else None
    shouyin = []
    if prev_date:
        for code in zt_set.get(prev_date, set()):
            if code in zt_set.get(as_of, set()):
                continue
            kk = D.k.get(code)
            if not kk or len(kk['c']) < 2:
                continue
            t = len(kk['c']) - 1
            if kk['dates'][t] != as_of:
                continue
            c, l, o = kk['c'][t], kk['l'][t], kk['o'][t]
            prev_c = kk['c'][t - 1]
            e12 = kk['e12'][t]
            shouyin.append(dict(
                code=code, name=D.meta.get(code, {}).get('name', ''),
                ind=D.ind.get(code, '其他'), close=round(c, 2),
                chg=round((c / prev_c - 1) * 100, 2) if prev_c else None,
                e12=round(e12, 2), hold12=int(c >= e12),
                low=round(l, 2), vr=round(kk['v'][t] / (sum(kk['v'][max(0, t - 5):t]) / 5), 2)
                if sum(kk['v'][max(0, t - 5):t]) else None,
                note='断板首阴：缩量且守住EMA12 → 观察次日反包；放量破位 → 退潮不接'))
        shouyin.sort(key=lambda x: (-x['hold12'], x['chg'] or 0))
        shouyin = shouyin[:12]

    # 池子按 A → B → C → D 分级排序，同级按评分；不足 topn 时用观察票补足并明确标注
    gorder = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
    pool = sorted(cands, key=lambda x: (gorder.get(x['grade'], 4), -x['score']))[:topn]
    for x in pool:
        x['pool_reason'] = {
            'A': 'A级·命中已验证强正期望组合（历史5日 +3~5%），优先',
            'B': 'B级·仅市值合格（历史5日约 +1%，弱正期望），小仓试',
            'C': 'C级·未通过数据门槛，仅观察',
            'D': 'D级·命中历史负期望陷阱组合，规避',
        }.get(x['grade'], '仅观察')
    nA = sum(1 for x in cands if x['grade'] == 'A')
    nB = sum(1 for x in cands if x['grade'] == 'B')
    traps_today = [dict(code=x['code'], name=x['name'], streak=x['streak'],
                        mv=x['mv'], mv_tier=x['mv_tier'], stage=x['stage'],
                        traps=x['rule_traps'][:3]) for x in cands if x['grade'] == 'D'][:12]
    rules_found = st.get('rules') or {}
    if nA:
        verdict = ('今日 A级（强正期望组合）%d 只、B级（市值合格弱正期望）%d 只，'
                   '可按介入点小仓参与，A级优先' % (nA, nB))
    elif nB:
        verdict = ('今日无 A级强机会（缺少「中大盘 + 市场最高板」这类组合），'
                   '有 %d 只 B级中大盘票，但历史5日约 +1%%、超额 +1.2pp 属弱正期望；'
                   '按龙头战法应轻仓或空仓观察，不把 B 级当龙头重仓' % nB)
    else:
        verdict = '今日无合格龙头战法标的，按战法应空仓观察（不勉强凑票）'
    verdict += '；另有 %d 只命中历史稳定陷阱组合（D级）。全历史口径下次日开盘接力整体 5日 %+.2f%%、超额 %+.2fpp' % (
        len(traps_today), (st['overall']['avg'] or 0), (st['overall']['exc'] or 0))

    ev = dict(
        n_signals=st['n_signals'], n_buyable=st['n_buyable'],
        unbuyable_pct=st['unbuyable_pct'],
        overall=st['overall'], weights=cal,
        reversed_factors=[k for k, v in cal.items() if v['dir'] < 0],
        factors=st['factors'], best_combos=sorted(
            [(k, v) for k, v in st['combos'].items() if v['n'] >= 30],
            key=lambda kv: -(kv[1]['exc'] or -99))[:6],
        worst_combos=sorted(
            [(k, v) for k, v in st['combos'].items() if v['n'] >= 30],
            key=lambda kv: (kv[1]['exc'] if kv[1]['exc'] is not None else 99))[:4],
        rules_found=rules_found, lowbuy=st['lowbuy'],
        key_finding='市值分层是龙头战法第一分水岭：同为市场最高板，中大盘80~500亿 5日 +4.73%/超额 +5.74pp，'
                    '小盘≤60亿 -4.32%/超额 -4.02pp，前后半期均稳定；'
                    '「高位≥3板+缩量」「2板+缩量」「一字板」为稳定陷阱',
    )
    payload = dict(
        as_of=as_of, generated_at=datetime.datetime.now().isoformat(timespec='seconds'),
        emotion=dict(date=as_of, phase=b['phase'], temp=b['temp'], n_zt=b['n_zt'],
                     zb=b['zb'], zb_rate=b['zb_rate'], promo=b['promo'],
                     hmax=b['hmax'], ladder=b['ladder'], regime=b['regime'],
                     history=st['recent_days'][-30:]),
        market_leader=market_leader,
        leader_caution='最高板是客观事实，但全历史回测显示「情绪高潮时追最高板」5日 -2.37%、'
                       '超额 -1.64pp；总龙头仅作情绪高度标尺，不是买入建议',
        themes=themes,
        pool=pool, candidates=cands, shouyin=shouyin,
        traps_today=traps_today, verdict=verdict,
        n_candidates=len(cands), n_qualified=nA + nB,
        n_A=nA, n_B=nB, n_D=len(traps_today), n_zt=b['n_zt'],
        rules=dict(
            candidate='连板≥2 / 当日最高板 / 题材内最高板且有≥2家跟风（剔除ST、上市<60bar）',
            buy_px='回测统一用 t+1 开盘价；次日一字涨停开盘视为买不进并剔除',
            sell='短线：破信号日最低价 或 -7% 硬止损；中线：收盘破EMA50且连续3日未收回（含跌破当日）',
            no_lookahead='所有特征仅用 signal_date 当日及之前；题材归类为当前映射的近似（PIT 有轻微偏差）',
            weight_from='评分各维度方向/权重由全历史分组前向超额(edge)自动校准；'
                        'edge 显著为负的维度反向计分，见 evidence.weights / reversed_factors',
            rule_from='数据自动搜索单/双/三条件组合，并用前后半期超额做样本外稳健性粗检，'
                      '见 evidence.rules_found',
            stage_note=STAGE_NOTE,
        ),
        evidence=ev,
    )
    return payload


# ---------------------------------------------------------------------------
# 5) 持续跟踪：当日全部龙头候选 ≥30 交易日逐日盯市
# ---------------------------------------------------------------------------
def update_tracking(D, payload, board):
    as_of = payload['as_of']
    os.makedirs(DAILY, exist_ok=True)
    meta = _j(META, {})
    if not meta.get('live_from'):
        # 实测起点 = 首次真实运行日（不是数据日）；历史部分由 signals.csv 离线重建
        meta['live_from'] = datetime.date.today().isoformat()
        meta['min_track_days'] = MIN_TRACK
        _dump(META, meta)
    tr = _j(TRACK, {})
    recs = tr.get('signals', [])
    idx = {(r['date'], r['code']): r for r in recs}
    d2i = {d: i for i, d in enumerate(D.idx_days)}
    i_as = d2i.get(as_of, len(D.idx_days) - 1)

    # 新增：当日全部候选（不只是 Top8），保证「所有龙头候选」都有 ≥30 日档案
    for x in payload['candidates']:
        key = (as_of, x['code'])
        if key in idx:
            continue
        recs.append(dict(
            date=as_of, code=x['code'], name=x['name'], theme=x['theme'],
            streak=x['streak'], stage=x['stage'], grade=x['grade'],
            score=x['score'], mv=x['mv'], mv_tier=x['mv_tier'],
            rank=x['rank'], in_pool=int(x['rank'] <= TOPN),
            entry_px=x['close'], buy_px=None, path=[], status='tracking',
            track_days=0,
        ))
    # 逐日盯市：统一刷新所有未成熟档案（幂等；同一天重跑不会重复追加）
    for r in recs:
        kk = D.k.get(r['code'])
        if not kk:
            continue
        dates, cl = kk['dates'], kk['c']
        dmap = {d: i for i, d in enumerate(dates)}
        k0 = dmap.get(r['date'])
        i0 = d2i.get(r['date'])
        if k0 is None or i0 is None:
            continue
        # 与回测口径一致：t+1 开盘价为建仓价；当日新信号尚无次日 bar 时先用信号日收盘占位
        base = kk['o'][k0 + 1] if k0 + 1 < len(cl) else cl[k0]
        if base <= 0:
            continue
        r['buy_px'] = round(base, 2)
        have = {p['date'] for p in r['path']}
        for j in range(i0 + 1, min(i_as + 1, i0 + MAX_TRACK + 1)):
            d = D.idx_days[j]
            k = dmap.get(d)
            if d in have or k is None:
                continue
            pnl = _pct(cl[k], base)
            bp = _bench_cum(D.idx_rets, i0, j - i0)
            r['path'].append(dict(date=d, close=round(cl[k], 2), pnl=pnl,
                                  bench=bp, excess=round(pnl - bp, 2) if bp is not None else None,
                                  age=j - i0, below50=int(cl[k] < kk['e50'][k])))
        r['path'].sort(key=lambda p: p['date'])
        r['track_days'] = len(r['path'])
        r['fwd5'] = next((p['pnl'] for p in r['path'] if p['age'] == 5), None)
        r['fwd10'] = next((p['pnl'] for p in r['path'] if p['age'] == 10), None)
        r['status'] = 'matured' if r['track_days'] >= MIN_TRACK else 'tracking'
    recs = recs[-20000:]
    tracking = [r for r in recs if r['status'] == 'tracking']
    matured = [r for r in recs if r['status'] == 'matured']
    out = dict(as_of=as_of, live_from=meta['live_from'], n_signals=len(recs),
               n_tracking=len(tracking), n_matured=len(matured),
               d5=_mean([r['fwd5'] for r in recs if r.get('fwd5') is not None]),
               win5=_win([r['fwd5'] for r in recs if r.get('fwd5') is not None]),
               pool_d5=_mean([r['fwd5'] for r in recs if r.get('in_pool') and r.get('fwd5') is not None]),
               a_d5=_mean([r['fwd5'] for r in recs if r.get('grade') == 'A' and r.get('fwd5') is not None]),
               b_d5=_mean([r['fwd5'] for r in recs if r.get('grade') == 'B' and r.get('fwd5') is not None]),
               signals=recs)
    _dump(TRACK, out)
    # 每日快照：首次落盘后不覆写（留痕）
    snap_path = os.path.join(DAILY, as_of + '.json')
    if not os.path.exists(snap_path):
        _dump(snap_path, dict(date=as_of, n=len(tracking),
                              rows=[{k: r.get(k) for k in
                                     ('date', 'code', 'name', 'theme', 'streak', 'stage',
                                      'score', 'in_pool', 'track_days', 'fwd5', 'fwd10')}
                                    for r in tracking]))
    return out


# ---------------------------------------------------------------------------
# 6) 主入口
# ---------------------------------------------------------------------------
def update(D, write=True, force=False):
    """收盘跑批调用：重建盘面 → 全历史回测 → 当日龙头池 → 持续跟踪 → 落盘。"""
    os.makedirs(DIR, exist_ok=True)
    os.makedirs(DAILY, exist_ok=True)
    as_of = D.idx_days[-1]
    old = _j(STATS, {})
    if not force and old.get('as_of') == as_of and os.path.exists(SIGCSV):
        # 同一交易日重复跑批：复用回测结果，只重算当日池与跟踪
        st = old
        sigs = []
    else:
        board = rebuild(D)
        sigs = scan(D, board)
        write_signals(sigs)
        st = make_stats(sigs, board, D)
        _dump(STATS, st)
        _dump(BOARDSUM, dict(as_of=as_of, days=st['recent_days']))
    if 'board' not in locals():
        board = rebuild(D)
    cal = calibrate(st)
    payload = build(D, board, st, cal)
    trk = update_tracking(D, payload, board)
    payload['tracking'] = {k: trk[k] for k in
                           ('as_of', 'live_from', 'n_signals', 'n_tracking',
                            'n_matured', 'd5', 'win5', 'pool_d5', 'a_d5', 'b_d5')}
    payload['stats'] = st
    if write:
        _dump(POOL, payload)
    return dict(as_of=as_of, n_signals=st['n_signals'], n_buyable=st['n_buyable'],
                unbuyable_pct=st['unbuyable_pct'], overall_d5=st['overall']['avg'],
                overall_exc=st['overall']['exc'], weights=cal,
                reversed_factors=[k for k, v in cal.items() if v['dir'] < 0],
                n_rules_good=len((st.get('rules') or {}).get('good') or []),
                n_A=payload['n_A'], n_B=payload['n_B'], n_D=payload['n_D'],
                n_candidates=payload['n_candidates'], n_pool=len(payload['pool']),
                leader=(payload['market_leader'] or {}).get('name'),
                phase=payload['emotion']['phase'], tracking=trk['n_tracking'],
                matured=trk['n_matured'])


if __name__ == '__main__':
    D = _srv.Data()
    print(json.dumps(update(D), ensure_ascii=False, indent=1))
