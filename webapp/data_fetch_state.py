#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共数据「当日一次性」抓取守卫（跨进程、跨定时任务）。

背景：目前有两个 WorkBuddy 定时任务（15:15 / 17:15）都会执行 daily_run.py。
不设防的话，指数 / 债券 / 行业 / 概念 / 沪深300 / 全市场名单 / 5009 只日K /
盘口异动会在同一天被**整批重复联网拉取两遍**。本模块用
「日期 + 成功标记 + 文件锁」保证同一天每个 key 最多成功抓一次；
失败不记成功，下一次跑批自动重试；FORCE_FETCH=1 可人工强制重抓。

只写元数据 backtest/fetch_state.json，绝不改写任何行情 / 名单主档。
用法：
    from data_fetch_state import once
    status, info = once('indices', lambda: (ok, detail))
    # status ∈ {'ran', 'skip', 'failed'}
    python3 data_fetch_state.py    # 查看今日抓取/跳过状态
"""
import datetime
import fcntl
import json
import os

BASE = os.path.expanduser('~/WorkBuddy/Claw')
STATE = os.path.join(BASE, 'backtest', 'fetch_state.json')
LOCK = STATE + '.lock'
HISTORY_MAX = 120


def force():
    return os.environ.get('FORCE_FETCH') == '1'


def _load():
    try:
        d = json.load(open(STATE, encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _brief(x):
    """状态文件只留可复查的短摘要，避免把整段 stdout / stderr 写进元数据。"""
    if isinstance(x, dict):
        return str(x.get('line') or x.get('detail') or '')[:200]
    return str(x)[:200]


def once(key, fn):
    """同一天只让 fn 成功执行一次。

    fn() -> (ok: bool, detail: str|dict)；仅 ok=True 记为「今日已完成」。
    返回 (status, detail)：
      ran    = 本次真的抓了且成功，detail 为 fn 原样返回值
      failed = 本次抓了但失败（含 fn 抛异常），detail 为错误信息
      skip   = 今日已成功抓过，未联网，detail 为上次摘要
    """
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(LOCK, 'a+') as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            state = _load()
            prev = state.get(key)
            if (not force() and isinstance(prev, dict)
                    and prev.get('date') == today and prev.get('ok')):
                return 'skip', prev.get('detail', '')
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, '%s: %s' % (type(e).__name__, e)
            state[key] = {'date': today, 'fetched_at': now,
                          'ok': bool(ok), 'detail': _brief(detail)}
            hist = state.setdefault('_history', [])
            hist.append({'date': today, 'key': key, 'at': now,
                         'ok': bool(ok), 'detail': _brief(detail)})
            state['_history'] = hist[-HISTORY_MAX:]
            _save(state)
            if ok:
                return 'ran', detail
            return 'failed', detail
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def today_status():
    d = _load()
    today = datetime.date.today().isoformat()
    return {k: v for k, v in d.items() if not k.startswith('_')}


if __name__ == '__main__':
    st = today_status()
    today = datetime.date.today().isoformat()
    print('公共数据抓取状态 · %s · FORCE_FETCH=%s' % (today, force()))
    for k in sorted(st):
        v = st[k]
        flag = '今日已抓(跳过)' if v.get('date') == today and v.get('ok') else '可重抓'
        print('  %-18s %-14s %s  %s' % (k, flag, v.get('fetched_at', '-'),
                                        _brief(v.get('detail', ''))[:100]))
