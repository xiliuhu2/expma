#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「三线穿阳 + 破20日高」提醒记录做成可回测的历史样本。

两件事：
  1) seed_live：把 tlbreak_alerts.json 里真实发过的提醒补进永久档案
     tlbreak_alert_history.jsonl（append-only，按 key 去重，可重复运行）。
  2) backfill：用本地前复权日K近似回放最近 N 个交易日的盘中触发，写入
     tlbreak_alert_backfill.jsonl，标 source=backfill。

判定口径与 tlbreak_alert.py 完全一致（基准=前一日收盘算 EMA12/EMA50/MA89、
三线带宽≤12%、trigger 在一个涨停内可达、开盘吞没三线、收盘破 trigger 且为阳线），
唯一差别：线上用盘中实时价，历史没有分时数据，只能用当日收盘价近似触发价。
因此 backfill 样本**不是真实发过的提醒**，页面与 CSV 里会单独标注来源，默认统计
也分开给出，避免混淆。前向收益不在文件里写死，由 server.py 按日K实时计算。

用法：python3 backfill_alert_history.py [天数，默认120]
"""
import datetime, json, os, sys

import server as S
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WEB = os.path.join(_REPO, 'webapp')
LIVE_JSON = os.path.join(WEB, 'tlbreak_alerts.json')
HIST = os.path.join(WEB, 'tlbreak_alert_history.jsonl')
BACKFILL = os.path.join(WEB, 'tlbreak_alert_backfill.jsonl')


def _existing_keys(path):
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                keys.add(json.loads(line).get('key'))
            except Exception:
                continue
    return keys


def _append(path, rows, seen):
    n = 0
    with open(path, 'a', encoding='utf-8') as f:
        for r in rows:
            if r.get('key') in seen:
                continue
            r.setdefault('source', 'live')
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
            seen.add(r['key'])
            n += 1
    return n


def seed_live():
    """把滚动窗口里已有的真实提醒补写进永久档案（防止 300 条上限挤掉历史）。"""
    try:
        recs = json.load(open(LIVE_JSON))
    except Exception:
        recs = []
    return _append(HIST, recs, _existing_keys(HIST))


def backfill(days=120):
    D = S.Data()
    S.D = D
    # 交易日历以等权小盘指数为准，取最近 days 个交易日作为回放窗口
    window = set(D.idx_days[-days:]) if D.idx_days else set()
    if not window:
        print('交易日日历为空，无法回填')
        return 0
    rows = []
    for code, kk in D.k.items():
        m = D.meta.get(code, {})
        name = m.get('name', '')
        if S.is_st(name):
            continue
        ds, c, o = kk['dates'], kk['c'], kk['o']
        e12, e50, m89 = kk['e12'], kk['e50'], kk['ma89']
        n = len(c)
        ratio = S._zt_ratio(code, name)
        for t in range(90, n):
            date = ds[t]
            if date not in window:
                continue
            b = t - 1
            lo3, hi3 = min(e12[b], e50[b], m89[b]), max(e12[b], e50[b], m89[b])
            if lo3 <= 0 or (hi3 - lo3) / lo3 > 0.12:
                continue
            prev = c[b]
            if prev <= 0:
                continue
            hi20 = max(c[max(0, b - 19):b + 1])
            trig = max(hi3, hi20)
            if trig > prev * (1 + ratio) - 0.001:
                continue                       # 当日一个涨停都够不到 → 线上也不会触发
            if not (o[t] <= lo3 + 1e-9 and c[t] > trig and c[t] > o[t]):
                continue
            s89 = (m89[b] / m89[b - 20] - 1) * 100 if b >= 20 and m89[b - 20] > 0 else 0.0
            rows.append({
                'key': 'bf|%s|%s' % (date, code),
                'date': date, 'time': '', 'code': code, 'name': name,
                'kind': '🔺三线穿阳·破20日高(历史回填)',
                'source': 'backfill',
                'price': round(c[t], 2),          # 当日收盘近似「触发时价」
                'trigger': round(trig, 2), 's89': round(s89, 2),
                'open': round(o[t], 2),
                'msg': '日K近似：收盘 %.2f 站上 %.2f · 开盘 %.2f 吞没三线 · %s' % (
                    c[t], trig, o[t],
                    'MA89上行·A级特征' if s89 > 0 else 'MA89仍下行·B级特征'),
            })
    rows.sort(key=lambda r: (r['date'], r['code']))
    return _append(BACKFILL, rows, _existing_keys(BACKFILL))


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print('seed live:', seed_live(), '条')
    print('backfill %d 个交易日...' % days, flush=True)
    n = backfill(days)
    print('新增回填:', n, '条 →', BACKFILL)
    print('累计回填:', len(_existing_keys(BACKFILL)), '条')
