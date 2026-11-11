#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取股债长跑数据：上证指数（1996~今）+ 上证国债指数（2003~今），东财全历史日K。
输出 backtest/bond/{sh000001,sh000012}.json，格式 [date, open, close, high, low, vol]"""
import json, os, urllib.request
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OUT = os.path.join(_REPO, 'backtest/bond')
os.makedirs(OUT, exist_ok=True)

def em_kline(secid, beg):
    url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s'
           '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57'
           '&klt=101&fqt=0&beg=%s&end=20500101' % (secid, beg))
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20).read())
    rows = []
    for x in (d.get('data') or {}).get('klines') or []:
        p = x.split(',')   # date, open, close, high, low, vol, amount
        rows.append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5] or 0)])
    return rows

IDX = {
    'sh000001': ('上证指数', '19960101'),
    'sh000012': ('上证国债指数', '20030101'),
}

for code, (name, beg) in IDX.items():
    try:
        rows = em_kline({'sh000001': '1.000001', 'sh000012': '1.000012'}[code], beg)
        if len(rows) > 100:
            json.dump(rows, open(os.path.join(OUT, code + '.json'), 'w'))
            print('%s %s: %d bars %s -> %s' % (code, name, len(rows), rows[0][0], rows[-1][0]))
        else:
            print('%s %s: 仅%d根，跳过' % (code, name, len(rows)))
    except Exception as e:
        print('%s %s: FAIL %s' % (code, name, e))
