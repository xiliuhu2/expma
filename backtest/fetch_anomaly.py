#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收盘抓取东方财富「盘口异动」当日全量 → webapp/anomaly_raw/YYYY-MM-DD.json

数据源：https://push2ex.eastmoney.com/getAllStockChanges
重要限制：**免费接口只提供当日**，加 date 参数无效（实测传历史日期仍返回当天数据）。
因此必须每天收盘后抓一次、按日落盘，长期积累后才是可回测的「真实异动样本」。
本脚本只做抓取与聚合，不参与任何判定；失败不影响收盘跑批主流程。

【收盘口径 / 非实时】
  · 盘中（15:05 前）默认直接跳过、不联网、不写档案，绝不落半日快照。
  · 抓回的数据若最大异动时间 < 15:00，判定为盘中快照，只存 anomaly_raw/partial/
    供留痕，不写正式档案（正式档案只收完整交易日）。
  · 每日由 daily_run.py 收盘跑批触发一次，无后台轮询、无盘中推送。

输出按个股聚合：异动总次数、偏多类型次数、各类型次数、首/末异动时间。
用法：python3 fetch_anomaly.py [YYYY-MM-DD] [--force]
      （缺省=今天；--force 跳过收盘守卫，仅用于人工补抓/排障）
"""
import collections
import datetime
import json
import os
import sys
import time
import urllib.request

BASE = os.path.expanduser('~/WorkBuddy/Claw')
OUT_DIR = os.path.join(BASE, 'webapp', 'anomaly_raw')
PARTIAL_DIR = os.path.join(OUT_DIR, 'partial')
CLOSE_HHMM = (15, 5)      # 15:05 之后才允许落正式档案（15:00 收盘 + 5 分钟接口落定）
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

# 东财 23 类盘口异动（akshare stock_changes_em 同款编码）
TYPE_NAME = {
    8201: '火箭发射', 8202: '快速反弹', 8193: '大笔买入', 4: '封涨停板',
    32: '打开跌停板', 64: '有大买盘', 8207: '竞价上涨', 8209: '高开5日线',
    8211: '向上缺口', 8213: '60日新高', 8215: '60日大幅上涨',
    8204: '加速下跌', 8203: '高台跳水', 8194: '大笔卖出', 8: '封跌停板',
    16: '打开涨停板', 128: '有大卖盘', 8208: '竞价下跌', 8210: '低开5日线',
    8212: '向下缺口', 8214: '60日新低', 8216: '60日大幅下跌',
}
BULL = {8201, 8202, 8193, 4, 32, 64, 8207, 8209, 8211, 8213, 8215}
TYPES = ','.join(str(t) for t in TYPE_NAME)


def full_code(c):
    """东财裸代码 → 工作台带前缀代码。北交所本地无K线，仍留档但不参与扫描。"""
    c = str(c).strip()
    if c.startswith(('sh', 'sz', 'bj')):
        return c
    if c[0] == '6':
        return 'sh' + c
    if c[0] in ('0', '3'):
        return 'sz' + c
    return 'bj' + c


def fetch_all():
    rows, tc = [], 0
    for page in range(8):
        url = ('https://push2ex.eastmoney.com/getAllStockChanges?'
               'type=%s&pageindex=%d&pagesize=5000&ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges'
               % (TYPES, page))
        try:
            req = urllib.request.Request(url, headers=UA)
            d = json.load(urllib.request.urlopen(req, timeout=15))
        except Exception as e:
            print('page %d 抓取失败: %s' % (page, e))
            break
        a = (d.get('data') or {}).get('allstock') or []
        tc = (d.get('data') or {}).get('tc', tc)
        rows.extend(a)
        if len(a) < 5000:
            break
        time.sleep(0.25)
    return rows, tc


def after_close(now=None):
    """是否已过收盘落档时点。单独成函数便于验收时按不同时间做验证。"""
    now = now or datetime.datetime.now()
    return (now.hour, now.minute) >= CLOSE_HHMM


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    force = '--force' in sys.argv
    date = args[0] if args else datetime.date.today().isoformat()
    now = datetime.datetime.now()
    if not force and not after_close(now):
        # 盘中不联网、不写文件：避免半日快照污染「每日一条完整样本」的档案库
        print('异动抓取跳过：当前 %s 未到收盘 %02d:%02d，按收盘口径不抓盘中快照；'
              '由 daily_run.py 收盘跑批触发（人工补抓用 --force）' %
              (now.strftime('%Y-%m-%d %H:%M'), CLOSE_HHMM[0], CLOSE_HHMM[1]))
        return 0
    rows, tc = fetch_all()
    if not rows:
        print('异动抓取失败：无数据（非交易日或接口异常）')
        return 1
    max_tm = max((str(r.get('tm') or '').zfill(6) for r in rows), default='')
    complete = max_tm >= '150000'
    per = {}
    type_count = collections.Counter()
    for r in rows:
        t = int(r.get('t') or 0)
        type_count[str(t)] += 1
        code = full_code(r.get('c') or '')
        p = per.setdefault(code, {'name': r.get('n') or '', 'n': 0, 'bull': 0,
                                  'types': {}, 'first': '', 'last': ''})
        p['name'] = r.get('n') or p['name']
        p['n'] += 1
        if t in BULL:
            p['bull'] += 1
        p['types'][str(t)] = p['types'].get(str(t), 0) + 1
        tm = str(r.get('tm') or '').zfill(6)
        if tm:
            if not p['first'] or tm < p['first']:
                p['first'] = tm
            if tm > p['last']:
                p['last'] = tm
    out = {
        'date': date,
        'fetched_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'eastmoney push2ex getAllStockChanges (当日全量，无历史)',
        'mode': 'close' if complete else 'intraday_partial',
        'complete': complete,
        'max_tm': max_tm,
        'tc': tc, 'n_rows': len(rows), 'n_stocks': len(per),
        'type_count': dict(type_count),
        'type_names': {str(k): v for k, v in TYPE_NAME.items()},
        'bull_types': sorted(BULL),
        'stocks': per,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    if not complete and not force:
        # 接口数据只到盘中 → 留痕但不进正式档案，防止与收盘日K错配
        os.makedirs(PARTIAL_DIR, exist_ok=True)
        path = os.path.join(PARTIAL_DIR, date + '_' + now.strftime('%H%M') + '.json')
    else:
        path = os.path.join(OUT_DIR, date + '.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)
    print('异动档案 %s: %d 条 / %d 只票 (接口 tc=%d, 最晚 %s, %s) → %s' %
          (date, len(rows), len(per), tc, max_tm,
           '完整收盘' if complete else '盘中快照仅留痕', path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
