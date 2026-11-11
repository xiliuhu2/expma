#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐北证50缺失段（2022-04-29 ~ 2023-01-02，东财源）并合并落盘。

东财对本机 urllib/curl 均间歇性限频（Remote end closed / exit 52），
本脚本带长间隔重试（20 次 × 30 秒），成功即合并退出；被 daily_run 调用时
限制重试次数（--quick 模式，3 次）避免拖慢跑批。缺失段只有约 170 根，
失败也不影响其它四指数（它们走腾讯稳定源）。
用法：python3 fetch_bj50_patch.py [--quick]
"""
import datetime, json, os, subprocess, sys, time

QUICK = '--quick' in sys.argv
TRIES = 3 if QUICK else 20
SLEEP = 15 if QUICK else 30


def em_kline(secid, beg='19900101'):
    """东财全量历史（curl 子进程，见 fetch_indices.py 说明）。"""
    url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s'
           '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57'
           '&klt=101&fqt=0&beg=%s&end=20500101' % (secid, beg))
    r = subprocess.run(['curl', '-s', '--max-time', '30',
                        '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
                        '-e', 'https://quote.eastmoney.com/',
                        url], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError('curl exit %d' % r.returncode)
    d = json.loads(r.stdout)
    rows = []
    for x in (d.get('data') or {}).get('klines') or []:
        p = x.split(',')
        rows.append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5] or 0)])
    return rows

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'indices', 'bj899050.json')
QUICK = '--quick' in sys.argv
TRIES = 3 if QUICK else 20
SLEEP = 15 if QUICK else 30


def main():
    old = json.load(open(PATH))
    have = {r[0] for r in old}
    want_start = '2022-04-29'
    have_from = old[0][0]
    if have_from <= want_start:
        print('北证50 已含 %s 起数据，无需补' % have_from)
        return
    rows = []
    for i in range(TRIES):
        try:
            rows = em_kline('0.899050')
            if rows:
                break
        except Exception as e:
            print('try%d/%d: %s' % (i + 1, TRIES, e))
            if i < TRIES - 1:
                time.sleep(SLEEP)
    if not rows:
        print('东财仍限频：北证50 维持 %d 根（%s 起，缺 %s ~ %s 段）'
              % (len(old), have_from, want_start, have_from))
        return
    merged = {r[0]: r for r in old}
    for r in rows:
        merged[r[0]] = r
    out = [merged[d] for d in sorted(merged)]
    json.dump(out, open(PATH, 'w'))
    print('北证50 补齐：%d bars %s -> %s' % (len(out), out[0][0], out[-1][0]))


if __name__ == '__main__':
    main()
