#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「三线穿阳 = 状态标记」策略的今日实盘清单。

逻辑：近 20 个交易日内最后一次三线穿阳（且满足 A 级：破20日高 + MA89 十日斜率>0）
      -> 之后逐日检查「收盘 > 信号日最高价 且 量比 >= 1.2」确认
      -> 确认日的次日开盘买入，止盈 +3%（或 +5%），20 日时间退出

输出：
  待确认：还需涨到信号日最高价多少
  已确认：触发日、买入价、+3%/+5% 止盈价、是否已止盈、当前浮盈、持有天数

用法: python3 cross3_entry_today.py
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'webapp'))
import server as S

WINDOW = 20


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def vr_at(v, i):
    if i < 5 or min(v[i - 5:i]) <= 0:
        return 0
    return v[i] / (sum(v[i - 5:i]) / 5)


def main():
    D = S.Data()
    today = D.px_date
    pending, confirmed = [], []
    for code, kk in D.k.items():
        name = D.meta.get(code, {}).get('name', '')
        if S.is_st(name) or not kk['dates'] or kk['dates'][-1] != today:
            continue
        c, o, h, l, v = kk['c'], kk['o'], kk['h'], kk['l'], kk['v']
        e12, e50, m89 = kk['e12'], kk['e50'], kk['ma89']
        last = len(c) - 1
        if last < 95:
            continue
        t0 = None
        for t in range(max(95, last - WINDOW), last + 1):
            if S.cross_lines(kk, t) == 3:
                t0 = t
        if t0 is None:
            continue
        hi20 = max(c[t0 - 20:t0])
        ma89_up = (m89[t0] / m89[t0 - 10] - 1) * 100 if m89[t0 - 10] else 0
        if not (c[t0] > hi20 and ma89_up > 0):       # A 级
            continue
        hi0, o0 = h[t0], o[t0]
        zlo, zhi = min(e12[last], e50[last], m89[last]), max(e12[last], e50[last], m89[last])
        glue = (max(e12[t0], e50[t0], m89[t0]) - min(e12[t0], e50[t0], m89[t0])) / c[t0] * 100
        base = dict(code=code, name=name, sig_date=kk['dates'][t0], hi0=round(hi0, 2),
                    close=round(c[last], 2), zlo=round(zlo, 2), zhi=round(zhi, 2),
                    glue=round(glue, 1), ma89_up=round(ma89_up, 2))
        hit = None
        for j in range(t0 + 1, last + 1):
            if c[j] > hi0 and vr_at(v, j) >= 1.2:
                hit = j
                break
        if hit is None or hit + 1 > last:
            base['need'] = round(pct(hi0, c[last]), 1)
            base['note'] = '价已破高·待放量' if base['need'] <= 0 else '待突破'
            pending.append(base)
            continue
        bi = hit + 1
        buy = o[bi]
        tp3, tp5 = buy * 1.03, buy * 1.05
        days = last - bi + 1
        state3, state5 = '持有中', '持有中'
        for k in range(bi + 1, min(bi + 20, last + 1)):   # T+1：买入当日不可卖
            if state3 == '持有中' and h[k] >= tp3:
                state3 = '已止盈'
            if state5 == '持有中' and h[k] >= tp5:
                state5 = '已止盈'
        if days >= 20:
            if state3 == '持有中':
                state3 = '到期平仓'
            if state5 == '持有中':
                state5 = '到期平仓'
        if state3 == '已止盈':
            pnl3 = 3 - 0.25
        elif state3 == '到期平仓':
            pnl3 = pct(c[min(bi + 19, last)], buy) - 0.25
        else:
            pnl3 = pct(c[last], buy) - 0.25
        base.update(trig_date=kk['dates'][hit], buy=round(buy, 2),
                    tp3=round(tp3, 2), tp5=round(tp5, 2),
                    done3=state3, done5=state5,
                    pnl3=round(pnl3, 2), days=days,
                    zone='带上方' if c[last] >= zhi else ('带内' if c[last] >= zlo else '跌破带'))
        confirmed.append(base)

    pending.sort(key=lambda x: x['need'])
    confirmed.sort(key=lambda x: -x['days'])
    print('基准日 %s | A 级信号（近 %d 交易日）共 %d 只：待确认 %d / 已确认 %d' % (
        today, WINDOW, len(pending) + len(confirmed), len(pending), len(confirmed)))
    print('\n=== 一、待「再放量破信号日高」确认（收盘 > 信号日高 且 量比>=1.2）===')
    print('%-9s %-9s %-11s %8s %8s %9s %9s %8s %s' % (
        '代码', '名称', '信号日', '现价', '突破确认价', '还需涨%', '三线带下沿', '粘合%', '状态'))
    for x in pending:
        print('%-9s %-9s %-11s %8.2f %8.2f %+8.1f%% %9.2f %7.1f%% %s' % (
            x['code'], x['name'], x['sig_date'], x['close'], x['hi0'],
            x['need'], x['zlo'], x['glue'], x['note']))
    print('\n=== 二、已确认（确认日次日开盘买入，+3% 口径）===')
    print('%-9s %-9s %-11s %-11s %8s %8s %8s %-9s %-9s %10s %6s %s' % (
        '代码', '名称', '信号日', '确认日', '买入价', '+3%止盈', '+5%止盈',
        '3%状态', '5%状态', '了结/浮盈%', '持有日', '三线带'))
    for x in confirmed:
        print('%-9s %-9s %-11s %-11s %8.2f %8.2f %8.2f %-9s %-9s %+9.2f%% %6d %s' % (
            x['code'], x['name'], x['sig_date'], x['trig_date'], x['buy'],
            x['tp3'], x['tp5'], x['done3'], x['done5'], x['pnl3'], x['days'], x['zone']))
    print('\n注：确认日次日开盘买入；同日既破高又放量才算确认；20 日收盘时间退出。')


if __name__ == '__main__':
    main()
