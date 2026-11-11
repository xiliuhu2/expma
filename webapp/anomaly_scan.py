#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⚡ 异动前瞻：日K量价异动 × EXPMA 技术状态 → 分层 + 样本外条件概率

输出 webapp/anomaly.json，供工作台 p12 页签与收盘日报使用。零外部依赖，
只读本地 klines 与**与基准日严格对齐的收盘盘口异动档案**；由 daily_run.py 收盘跑批调用。

【收盘口径 / 非实时】只使用最近一个已收盘交易日的日K；盘中不联网、不取实时
行情、不轮询、不推送。盘口异动档案日期必须等于日K基准日且为完整收盘数据，
否则一律不合并（宁可空着，也不把盘中/跨日数据挂到收盘候选上）。

严谨口径（避免把「预测」做成自欺）：
  · 因子只用 t 及之前数据；前向收益只用 t 之后数据（无未来函数）。
  · 「后面有行情」= 未来5日收益>5%（hit5）或 未来10日内最大涨幅>8%（hit10）。
  · 按交易日历 60/40 切分，train 找规律、test 做样本外验证，两段都稳定才保留。
  · 本模块**不预测收益**：实测 5 日均值接近 0，它放大的是「出现一波行情的概率」
    与波动尾部，本质是机会放大器而非收益预测器。MA89 上行是分水岭。
  · 盘口异动档案（东财当日流）只有当日、无历史，**不参与档位判定**，
    仅作为观察字段；每天收盘落档，积累够样本后才可纳入回测。
用法：python3 anomaly_scan.py
"""
import datetime
import json
import os
import sys
import time
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WEB = os.path.join(_REPO, 'webapp')
sys.path.insert(0, WEB)
import server as S  # noqa: E402

OUT = os.path.join(WEB, 'anomaly.json')
RAW_DIR = os.path.join(WEB, 'anomaly_raw')

GRADE_LABEL = {
    'S': '共振（放量长阳破20日高 + MA89上行 + 站上三线）',
    'A': '放量破20日高 + MA89上行',
    'B': '放量破20日高但 MA89 仍下行（追高陷阱）',
}
DEFS = {
    'volr': '成交量 / 过去20日均量（放量倍数）',
    'ret': '当日涨跌幅 = 收盘/昨收 - 1',
    'brk20': '收盘 > 过去20日最高收盘（破20日高）',
    's89': 'MA89 五日斜率（%），>0 为上行',
    'ab3': '收盘同时站上 EMA12 / EMA50 / MA89',
    'amp': '当日振幅 = (最高-最低)/昨收',
    'gap': '跳空 = 开盘/昨收 - 1',
    'hit5': '未来5日收益 > 5% 的历史占比',
    'hit10': '未来10日内最大涨幅 > 8% 的历史占比',
    'd5': '未来5日收益均值（%）',
    'base': '基准 = 同期全市场所有可判定样本（5009只 × 可判定交易日）',
}


def factors(kk, t):
    """只用 t 及之前数据算因子，返回 dict 或 None（数据不足/停牌）。"""
    c, h, l, o, v = kk['c'], kk['h'], kk['l'], kk['o'], kk['v']
    e12, e50, ma89 = kk['e12'], kk['e50'], kk['ma89']
    if t < 90 or t >= len(c):
        return None
    c0, c1 = c[t], c[t - 1]
    if c0 <= 0 or c1 <= 0 or v[t] <= 0:
        return None
    v20 = sum(v[t - 20:t]) / 20.0
    if v20 <= 0:
        return None
    hi20 = max(c[t - 20:t])
    return dict(
        date=kk['dates'][t], close=round(c0, 2),
        volr=round(v[t] / v20, 2),
        ret=round((c0 / c1 - 1) * 100, 2),
        amp=round((h[t] - l[t]) / c1 * 100, 2),
        gap=round((o[t] / c1 - 1) * 100, 2),
        hi20=round(hi20, 2),
        brk20=c0 > hi20,
        s89=round((ma89[t] / ma89[t - 5] - 1) * 100, 2) if ma89[t - 5] > 0 else 0.0,
        ab3=bool(c0 > e12[t] and c0 > e50[t] and c0 > ma89[t]),
        e12=round(e12[t], 2), e50=round(e50[t], 2), ma89=round(ma89[t], 2),
    )


def grade(f):
    """三档互斥。判据来自样本外验证：MA89 上行为分水岭。"""
    if not (f['volr'] >= 2 and f['brk20']):
        return None
    if f['s89'] <= 0:
        return 'B'
    if f['ret'] >= 5 and f['ab3']:
        return 'S'
    return 'A'


def blank():
    return [0, 0.0, 0.0, 0.0, 0, 0, 0]  # n, Σd5, Σd10, Σmu10, hit5, hit10, d5>0


def put(acc, seg, g, d5, d10, mu10):
    a = acc[(seg, g)]
    a[0] += 1
    a[1] += d5
    a[2] += d10
    a[3] += mu10
    a[4] += 1 if d5 > 0.05 else 0
    a[5] += 1 if mu10 > 0.08 else 0
    a[6] += 1 if d5 > 0 else 0


def summarize(a):
    n = a[0]
    if not n:
        return dict(n=0, d5=None, d10=None, mu10=None, hit5=None, hit10=None, d5_win=None)
    return dict(n=n,
                d5=round(a[1] / n * 100, 2),
                d10=round(a[2] / n * 100, 2),
                mu10=round(a[3] / n * 100, 2),
                hit5=round(a[4] / n * 100, 1),
                hit10=round(a[5] / n * 100, 1),
                d5_win=round(a[6] / n * 100, 1))


def backtest(D, cut):
    """全历史统计 + 训练/样本外两段。返回 (grades_out, verify, n_rows, stat_last_date)。"""
    keys = ('ALL', 'S', 'A', 'B')
    acc = {(s, g): blank() for s in ('all', 'train', 'test') for g in keys}
    n_rows = 0
    last_stat = ''
    for code, kk in D.k.items():
        c, h, ds = kk['c'], kk['h'], kk['dates']
        n = len(c)
        for t in range(90, n - 10):
            f = factors(kk, t)
            if not f:
                continue
            g = grade(f)
            c0 = c[t]
            d5 = c[t + 5] / c0 - 1
            d10 = c[t + 10] / c0 - 1
            mu10 = max(h[t + 1:t + 11]) / c0 - 1
            seg = 'train' if ds[t] < cut else 'test'
            n_rows += 1
            if ds[t] > last_stat:
                last_stat = ds[t]
            put(acc, 'all', 'ALL', d5, d10, mu10)
            put(acc, seg, 'ALL', d5, d10, mu10)
            if g:
                put(acc, 'all', g, d5, d10, mu10)
                put(acc, seg, g, d5, d10, mu10)
    grades = {g: summarize(acc[('all', g)]) for g in keys}
    verify = {s: {g: summarize(acc[(s, g)]) for g in keys}
              for s in ('train', 'test')}
    return grades, verify, n_rows, last_stat


def load_anomaly(date):
    """只接受与日K基准日严格对齐的完整收盘档案。

    返回 (档案|None, 档案日期|None, 状态说明)。跨日档案、盘中快照(complete=false)、
    缺失一律返回 None —— 收盘口径下不允许「拿最近一份凑数」，否则页面会把昨日/盘中
    的盘口异动次数挂到另一天的收盘候选上，形成误导。
    """
    p = os.path.join(RAW_DIR, date + '.json')
    if not os.path.exists(p):
        if date == datetime.date.today().isoformat():
            return None, None, '今日收盘档案尚未生成（收盘跑批约 16:35 后补齐）'
        return None, None, ('基准日 %s 无收盘异动档案（免费源只给当日、无法补抓历史），'
                            '将从下一交易日起逐日积累' % date)
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception as e:
        return None, None, '档案读取失败，已忽略：%s' % e
    if d.get('date') and d.get('date') != date:
        return None, None, '档案日期 %s 与基准日 %s 不一致，已忽略' % (d.get('date'), date)
    if d.get('complete') is False or d.get('mode') == 'intraday_partial':
        return None, None, '该档案为盘中快照，已忽略（只收完整收盘数据）'
    return d, d.get('date') or date, '已对齐（完整收盘）'


def scan_today(D, probs, anom, date):
    """probs 用样本外(test)统计，避免把训练段规律当成已验证结论。"""
    stocks = (anom or {}).get('stocks') or {}
    picks = []
    for code, kk in D.k.items():
        f = factors(kk, len(kk['c']) - 1)
        if not f:
            continue
        g = grade(f)
        if not g:
            continue
        a = stocks.get(code) or {}
        top = sorted(((int(t), n) for t, n in (a.get('types') or {}).items()),
                     key=lambda x: -x[1])[:3]
        names = (anom or {}).get('type_names') or {}
        rec = dict(f)
        rec.update(code=code, name=(D.meta.get(code, {}).get('name') or a.get('name') or code),
                   grade=g, grade_label=GRADE_LABEL[g],
                   prob_hit5=probs[g]['hit5'], prob_hit10=probs[g]['hit10'],
                   prob_d5=probs[g]['d5'],
                   anomaly_n=a.get('n'), anomaly_bull=a.get('bull'),
                   anomaly_first=a.get('first'), anomaly_last=a.get('last'),
                   anomaly_top='、'.join('%s×%d' % (names.get(str(t), t), n) for t, n in top))
        picks.append(rec)
    order = {'S': 0, 'A': 1, 'B': 2}
    picks.sort(key=lambda r: (order[r['grade']], -(r['anomaly_bull'] or 0), -r['volr']))
    return picks


def main():
    t0 = time.time()
    D = S.Data()
    dates = sorted({d for kk in D.k.values() for d in kk['dates']})
    cut = dates[int(len(dates) * 0.6)]
    grades, verify, n_rows, stat_last = backtest(D, cut)
    # 日K基准日以数据最多的最后交易日为准（永远是已收盘交易日，不含盘中数据）
    base_date = max(kk['dates'][-1] for kk in D.k.values() if kk['dates'])
    anom, anom_date, anom_status = load_anomaly(base_date)
    picks = scan_today(D, verify['test'], anom, base_date)
    date = base_date

    out = {
        'date': date,
        'as_of': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'mode': 'close',
        'realtime': False,
        'generated_by': 'daily_run.py 收盘跑批（本地日K，无实时行情）',
        'next_update': '下次更新：当日收盘跑批后（约 16:35）',
        'n_picks': len(picks),
        'grades': grades,
        'grade_label': GRADE_LABEL,
        'definitions': DEFS,
        'verify': {'split': cut, 'n_rows': n_rows, 'stat_last_date': stat_last,
                   'train': verify['train'], 'test': verify['test']},
        'anomaly': ({'date': anom_date, 'n_rows': anom.get('n_rows'),
                     'n_stocks': anom.get('n_stocks'), 'matched': sum(
                         1 for p in picks if p['anomaly_n']), 'status': anom_status}
                    if anom else None),
        'anomaly_status': anom_status,
        'picks': picks[:120],
        'note': ('本模块仅收盘跑批生成（非实时，盘中不抓行情/不推送），给出的是'
                 '「后续出现一波行情」的历史条件概率，不是收益预测：'
                 '5日收益均值接近0，放大的是波动与右尾机会；MA89 仍下行的 B 档为历史负期望档。'
                 '盘口异动档案无历史、不参与档位判定，仅作观察。数据为客观筛选，不构成投资建议。'),
    }
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, OUT)
    print('异动前瞻 %s: %d 只候选 (S/A/B=%d/%d/%d) · 样本 %d 条 · 样本外起点 %s · %.1fs → %s' %
          (date, len(picks),
           sum(1 for p in picks if p['grade'] == 'S'),
           sum(1 for p in picks if p['grade'] == 'A'),
           sum(1 for p in picks if p['grade'] == 'B'),
           n_rows, cut, time.time() - t0, OUT))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
