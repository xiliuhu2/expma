#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取东方财富行业分类（f100 字段），存 webapp/industry.json {code: 行业名}
覆盖沪深A股 + 北交所。daily_run 每日调用一次（行业归属极少变动，也可手动重跑）。"""
import json, os, time, urllib.request

BASE = os.path.expanduser('~/WorkBuddy/Claw')

def fetch():
    out = {}
    pn = 1
    while pn <= 80:
        url = ('https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1'
               '&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048'
               '&fields=f12,f14,f100') % pn
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        data = d.get('data') or {}
        diff = data.get('diff') or []
        if not diff:
            break
        for r in diff:
            raw = r.get('f12')
            ind = r.get('f100')
            if not raw or not ind or ind == '-':
                continue
            code = str(raw)
            if code.startswith(('6',)) or (code.startswith('9') and not code.startswith('92')):
                pre = 'sh'
            elif code.startswith(('4', '8', '92')):
                pre = 'bj'
            else:
                pre = 'sz'
            out[pre + code] = ind
        total = data.get('total', 0)
        if pn * 100 >= total:
            break
        pn += 1
        time.sleep(0.15)
    json.dump(out, open(os.path.join(BASE, 'webapp', 'industry.json'), 'w'), ensure_ascii=False)
    return len(out)

if __name__ == '__main__':
    n = fetch()
    print('industry saved:', n)
