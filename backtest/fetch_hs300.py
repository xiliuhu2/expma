#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沪深300成份股主档维护（基础数据本地化：请求路径只读本地文件，本脚本离线刷新）
数据源：东方财富 BK0500 板块（即沪深300成份，恰好300只；中证官网 xls 已 403）
主档：webapp/hs300_cons.json = {date, map:{code6:name}, hist:[{date, added, removed}...]}
策略：主档缺失或超过7天（或 --force）才联网刷新；失败沿用旧主档。每次刷新对比上一版，
记录成份调入/调出留痕（成份变动会改变池子口径，需可追溯）。
用法：python3 fetch_hs300.py [--force]
"""
import datetime, json, os, sys
from urllib.request import Request, urlopen
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WEB = os.path.join(_REPO, 'webapp')
PATH = os.path.join(WEB, 'hs300_cons.json')

def fetch():
    out, total = {}, 300
    for pn in (1, 2, 3):
        url = ('http://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1'
               '&fltt=2&invt=2&fid=f12&fs=b:BK0500&fields=f12,f14') % pn
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        d = json.loads(urlopen(req, timeout=10).read().decode())
        total = d['data']['total']
        for it in d['data']['diff']:
            out[str(it['f12'])] = it['f14']
        if len(out) >= total:
            break
    return out if len(out) >= 250 else None

def main():
    force = '--force' in sys.argv
    old = None
    if os.path.exists(PATH):
        try:
            old = json.load(open(PATH))
            age = (datetime.date.today() - datetime.date.fromisoformat(old['date'])).days
            if age < 7 and not force:
                print('fresh(%dd, %d只, 主档%s)' % (age, len(old['map']), old['date']))
                return
        except Exception:
            old = None
    mp = fetch()
    if mp is None:
        if old:
            print('fetch failed · 沿用主档 %s（%d只）' % (old['date'], len(old['map'])))
            return
        print('fetch failed · 且无旧主档')
        sys.exit(1)
    omap = (old or {}).get('map', {})
    added = sorted(set(mp) - set(omap))
    removed = sorted(set(omap) - set(mp))
    hist = (old or {}).get('hist', [])
    today = datetime.date.today().isoformat()
    if added or removed:
        hist.append({'date': today,
                     'added': [{'code': c, 'name': mp[c]} for c in added],
                     'removed': [{'code': c, 'name': omap[c]} for c in removed]})
    json.dump({'date': today, 'map': mp, 'hist': hist[-52:]},
              open(PATH, 'w'), ensure_ascii=False, indent=1)
    print('ok %s %d只 · 调入%d 调出%d' % (today, len(mp), len(added), len(removed)))

if __name__ == '__main__':
    main()
