#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取五大指数**全量历史**日K并缓存为统一格式 [date, open, close, high, low, vol]。

2026-11-07 改版（长历史）：
  腾讯源 param=code,day,START,END,N 形式支持任意区间，按年分段拉取拼接；
  上证可回到 1990-12-19（约 8700 根，36 年），沪深300 回到 2005-01-04（22 年）。
  东财源（beg=19900101 一次给全量）做首选，但本机 urllib/curl 直连常被 RST
  （限频指纹问题），故降级为 fallback；腾讯稳定，为主力。
  daily_run.py 每日收盘后自动刷新：只需补最新一段（今年），已有历史不重拉——
  用 --full 才全量重建。
用法：python3 fetch_indices.py [--full]
"""
import json, os, sys, time, urllib.request

OUT = os.path.expanduser('~/WorkBuddy/Claw/backtest/indices')
os.makedirs(OUT, exist_ok=True)
FULL = '--full' in sys.argv

# 各指数发布日（再往前没有数据）
FIRST = {
    'sh000001': '1990-12-19',
    'sh000300': '2005-01-04',
    'sh000688': '2019-12-31',
    'sz399006': '2010-06-01',
    'bj899050': '2022-04-29',
}


def tx_range(code, start, end, n=800):
    """腾讯源：拉 [start, end] 区间日K（区间超过 800 根时分段调用）。"""
    url = 'https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=%s,day,%s,%s,%d,' % (code, start, end, n)
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20).read())
    data = d.get('data') or {}
    k = (data.get(code) or {}).get('day') or []
    return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)] for r in k]


def tx_full(code, first_date, today='2026-12-31'):
    """按年分段拉全量并拼接去重（每年一次请求，约 250 根/年 < 800 限制）。"""
    y0 = int(first_date[:4])
    y1 = int(today[:4])
    all_rows = {}
    for y in range(y0, y1 + 1):
        st = first_date if y == y0 else '%d-01-01' % y
        en = today if y == y1 else '%d-12-31' % y
        for attempt in (1, 2, 3):
            try:
                k = tx_range(code, st, en)
                for r in k:
                    all_rows[r[0]] = r
                break
            except Exception as e:
                if attempt == 3:
                    print('  %d 段 FAIL: %s' % (y, e))
                else:
                    time.sleep(1.2)
        time.sleep(0.35)          # 批间限速，防触发风控
    return [all_rows[d] for d in sorted(all_rows)]


IDX = {
    'sh000300': '沪深300',
    'sh000001': '上证指数',
    'sh000688': '科创50',
    'sz399006': '创业板指',
    'bj899050': '北证50',
}

import datetime
today = datetime.date.today().isoformat()

for code, name in IDX.items():
    path = os.path.join(OUT, code + '.json')
    first = FIRST[code]
    if not FULL and os.path.exists(path):
        # 增量：只拉本地最后日期之后的今年段
        try:
            old = json.load(open(path))
            last_local = old[-1][0]
            if last_local >= today:
                print('%s %s: 已是最新（%s）' % (code, name, last_local))
                continue
            st = (datetime.date.fromisoformat(last_local) + datetime.timedelta(days=1)).isoformat()
            new = tx_range(code, st, today)
            if new:
                merged = {r[0]: r for r in old}
                for r in new:
                    merged[r[0]] = r
                rows = [merged[d] for d in sorted(merged)]
                json.dump(rows, open(path, 'w'))
                print('%s %s [增量]: %d (+%d) bars %s -> %s' % (code, name, len(rows), len(new), rows[0][0], rows[-1][0]))
            else:
                print('%s %s: 无新数据（本地 %s）' % (code, name, last_local))
            continue
        except Exception as e:
            print('%s %s: 增量失败(%s)，转全量' % (code, name, e))
    rows = tx_full(code, first, today)
    if len(rows) > 60:
        json.dump(rows, open(path, 'w'))
        print('%s %s [全量]: %d bars %s -> %s（%.1f 年）'
              % (code, name, len(rows), rows[0][0], rows[-1][0], len(rows) / 244))
    else:
        print('%s %s: 数据不足（%d 根），保留旧文件' % (code, name, len(rows)))
