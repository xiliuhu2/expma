#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""到价提醒守护进程：盘中每60秒轮询腾讯实时行情（qt.gtimg.cn），
检查 trades.json 持仓的 止损/止盈目标1/2/移动止盈 线，到价即 macOS 通知 + 声音，
记录到 webapp/alerts.json（每条每类型每天只提醒一次）。
用法：python3 alertd.py   （daily_run 每日收盘后自动重启，保持常驻）"""
import datetime, json, os, subprocess, time, urllib.request

from flashwin import flash, RED, GREEN

WEB = os.path.expanduser('~/WorkBuddy/Claw/webapp')
TRADES = os.path.join(WEB, 'trades.json')
ALERTS = os.path.join(WEB, 'alerts.json')
SRV = 'http://127.0.0.1:8765'

def quotes(codes):
    """腾讯实时行情：返回 {bare_code: {name, price, high, low}}"""
    out = {}
    if not codes:
        return out
    url = 'https://qt.gtimg.cn/q=' + ','.join(codes)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    text = urllib.request.urlopen(req, timeout=10).read().decode('gbk', 'ignore')
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        val = line.split('=', 1)[1].strip('"').split('~')
        if len(val) > 34:
            try:
                out[val[2]] = dict(name=val[1],
                                   price=float(val[3]) if val[3] else 0,
                                   high=float(val[33]) if val[33] else 0,
                                   low=float(val[34]) if val[34] else 0)
            except ValueError:
                pass
    return out

def notify(title, msg, url='', color=RED):
    # 1) 闪屏窗（按信号类型主色闪烁 5 秒：止损绿/止盈红，点击 → Chrome 交易计划页）
    flash(title, msg, url, color)
    # 2) macOS 系统通知（横幅 + Glass 提示音，进通知中心可回看）
    msg = msg.replace('"', "'")
    subprocess.run(['osascript', '-e',
                    'display notification "%s" with title "%s" sound name "Glass"' % (msg, title)],
                   capture_output=True)

def check():
    if not os.path.exists(TRADES):
        return []
    t = json.load(open(TRADES))
    pos = [p for p in t.get('positions', [])
           if isinstance(p.get('code'), str) and len(p['code']) >= 8]
    if not pos:
        return []
    codes = sorted({p['code'] for p in pos})
    qs = quotes(codes)
    if not qs:
        return []
    # 盘中判别：所有价格与上次轮询一致 → 停牌/休市，跳过（防静态价误报）
    prev_path = os.path.join(WEB, '.alertd_prev.json')
    try:
        prev = json.load(open(prev_path))
    except Exception:
        prev = {}
    cur = {c: qs[c]['price'] for c in qs}
    moving = any(prev.get(c) != v for c, v in cur.items())
    json.dump(cur, open(prev_path, 'w'))
    if prev and not moving:
        return []
    try:
        alerts = json.load(open(ALERTS))
    except Exception:
        alerts = []
    today = datetime.date.today().isoformat()
    fired = []
    changed = False
    for p in pos:
        q = qs.get(p['code'][2:])
        if not q:
            continue
        price, high = q['price'], q['high']
        # 更新持有期最高价（移动止盈更精准，写回 trades.json）
        if high and high > (p.get('hi') or 0):
            p['hi'] = high
            changed = True
        stop = p.get('stop')
        t1, t2 = p.get('t1'), p.get('t2')
        trail = p.get('trail', 5)
        hi = max(p.get('hi') or 0, high)
        tstop = round(hi * (1 - trail / 100), 2) if hi else None
        if tstop is not None and p.get('buy_price') and tstop <= p['buy_price']:
            tstop = None  # 移动止盈只保护利润：回撤线高于买入价才启用，下行交给止损
        rules = []
        if stop and price <= stop:
            rules.append(('🟢止损到价', '现价 %.2f ≤ 止损 %.2f —— 无条件离场，不侥幸！' % (price, stop), GREEN))
        if t2 and price >= t2:
            rules.append(('🔴止盈目标2', '现价 %.2f ≥ 目标2 %.2f —— 清仓落袋。' % (price, t2), RED))
        elif t1 and price >= t1:
            rules.append(('🔴止盈目标1', '现价 %.2f ≥ 目标1 %.2f —— 减半仓。' % (price, t1), RED))
        if tstop and price <= tstop and (not stop or price > stop):
            rules.append(('🔴移动止盈', '现价 %.2f 回撤至 %.2f（高点 %.2f 回撤 %d%%）—— 利润保护线，到线减仓/离场。' % (price, tstop, hi, trail), RED))
        for kind, msg, color in rules:
            key = '%s|%s|%s' % (today, p['id'], kind)
            if any(a.get('key') == key for a in alerts):
                continue
            rec = dict(key=key, date=today,
                       time=datetime.datetime.now().strftime('%H:%M:%S'),
                       code=p['code'], name=p['name'], kind=kind, msg=msg, price=price)
            alerts.append(rec)
            fired.append(rec)
            notify('%s · %s' % (kind, p['name']), msg,
                   SRV + '/?jump=%s/p6' % p['code'], color)   # 点击闪窗 → Chrome 交易计划页；止损绿/止盈红
    if fired or changed:
        json.dump(alerts[-300:], open(ALERTS, 'w'), ensure_ascii=False, indent=1)
        if changed:
            json.dump(t, open(TRADES, 'w'), ensure_ascii=False, indent=1)
    return fired

def main():
    print('alertd started: watching trades.json @ qt.gtimg.cn every 60s (9:25-15:05 工作日)')
    while True:
        now = datetime.datetime.now()
        if now.weekday() < 5:
            mins = now.hour * 60 + now.minute
            if 9 * 60 + 25 <= mins <= 15 * 60 + 5:
                try:
                    check()
                except Exception as e:
                    print('check error:', e)
        time.sleep(60)

if __name__ == '__main__':
    main()
