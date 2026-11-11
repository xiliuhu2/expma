#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
80% 胜率规则族 · 当日清单（数据基准日 = 本地 K 线最后一日）

规则族（2026-11-07 按 A股 T+1 修正撮合后重跑：全样本 n=471 胜率 86.8% / 均值 +0.01%，
          训练段 86.2% / -0.13%，样本外 94.6% / +1.66%，逐月最低 71.9%）：
  信号：三线穿阳（EXPMA12/EXPMA50/MA89，阳线实体包住三线）+ A级（破20日高 + MA89十日斜率>0）
  买点：再放量破信号日高 —— 收盘 > 信号日最高价 且 量比>=1.2
  执行：触发确认日的次日开盘买入；**T+1：买入当日不可卖，次日起**盘中触达 +2.5% 才止盈；
        不设个股硬止损；20日收盘时间退出
  注意：旧口径 range(bi, bi+20) 允许买入当日止盈，属不可成交的虚增收益，已改 range(bi+1, bi+20)；
        修正后该规则训练段均值转负，T+1 俱乐部最优为 +4%/20日（切换参数需用户确认）。
  扫描：信号回溯 WINDOW(20确认) + HOLD(20持有) = 40 日，否则看不到真实的 20 日时间退出

两种用法：
  CLI：python3 cross3_80win_today.py          # 打印表格
  导入：collect(D) -> dict                     # daily_run.py 收盘跑批调用（复用已加载的 Data，避免重读5000个文件）

输出：待确认清单（还需涨多少触发）+ 已确认持仓跟踪（买入日/价、止盈日/价、卖出日、预计到期日、浮盈）
"""
import os, sys, datetime, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'webapp'))
import server as S

WINDOW = 20        # 信号 → 买点确认窗口（交易日），与回测 EM.W 一致
HOLD = 20          # 买入后最长持有：第 20 根 bar 收盘时间退出，与回测 EM.HOLD 一致
SCAN_W = WINDOW + HOLD   # 40：要观察到完整的 20 日退出，信号须回溯 40 日（20确认+20持有）
TP = 2.5


def pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def idx_gate(D=None, date=None):
    """触发日指数状态（用户 2026-11-07 确认的新变量因子）。

    回测（cross3_idx_timing.py，471 事件，T+1）：触发日上证>MA20、量比>=0.9、
    mom20 0~6%、|缺口|<1% 全部满足时介入；否则当日仍要提醒（用户明确要求），
    只是标记为「指数不配合·仅观察」，弹窗换琥珀色、文案提示不介入。
    全部只用当日及之前数据，无未来函数。
    """
    if D is None:
        D = S.Data()
    if date is None:
        date = D.px_date
    ix = D.indices.get('sh000001')
    if not ix or date not in ix['dates']:
        return dict(ok=None, why='指数数据缺失', detail={})
    i = ix['dates'].index(date)
    if i < 25:
        return dict(ok=None, why='指数历史不足', detail={})
    c, o = ix['c'], ix['o']
    ma20 = sum(c[i - 19:i + 1]) / 20
    pos5 = pct(c[i], ma20)
    mom20 = pct(c[i], c[i - 20])
    gap = pct(o[i], c[i - 1])
    # 上证量能从 indices/sh000001.json 读（Data 缓存不含 v）
    raw = json.load(open(os.path.join(HERE, 'indices', 'sh000001.json')))
    vd = {r[0]: r[5] for r in raw}
    v = [vd.get(d, 0) for d in ix['dates']]
    v5 = sum(v[i - 5:i]) / 5
    vr = v[i] / v5 if v5 > 0 else 1.0
    fails = []
    if not c[i] > ma20:
        fails.append('上证<MA20(%.2f%%)' % pos5)
    if vr < 0.9:
        fails.append('指数量比%.2f<0.9' % vr)
    if not (0 <= mom20 < 6):
        fails.append('20日涨幅%.1f%%不在0~6%%' % mom20)
    if abs(gap) >= 1:
        fails.append('缺口%+.1f%%' % gap)
    return dict(ok=not fails,
                why='；'.join(fails) if fails else '指数配合（MA20上方·量比%.2f·20日%.1f%%·缺口%+.1f%%）' % (vr, mom20, gap),
                detail=dict(pos5=round(pos5, 2), mom20=round(mom20, 2),
                            vr=round(vr, 2), gap=round(gap, 2), above=c[i] > ma20))


def add_trade_days(dstr, n):
    """从 dstr 起顺延 n 个交易日（按周一至周五估算，不含节假日，故为约值）。"""
    d = datetime.date.fromisoformat(dstr)
    while n > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.isoformat()


def vr_at(v, i):
    if i < 5 or min(v[i - 5:i]) <= 0:
        return 0
    return v[i] / (sum(v[i - 5:i]) / 5)


def collect(D=None):
    """扫描近 SCAN_W(40) 个交易日的 A 级三线穿阳信号，返回当日清单。不打印。

    WINDOW=20 是「信号→买点确认」窗口（与回测 EM.W 一致），HOLD=20 是买入后最长持有；
    要观察到完整的 20 日时间退出，信号须回溯 WINDOW+HOLD 日，否则持仓永远不到 20 日。
    """
    if D is None:
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
        for t in range(max(95, last - SCAN_W), last + 1):
            if S.cross_lines(kk, t) == 3:
                t0 = t
        if t0 is None:
            continue
        hi20 = max(c[t0 - 20:t0])
        ma89_up = (m89[t0] / m89[t0 - 10] - 1) * 100 if m89[t0 - 10] else 0
        if not (c[t0] > hi20 and ma89_up > 0):       # A 级
            continue
        hi0 = h[t0]
        zlo, zhi = min(e12[last], e50[last], m89[last]), max(e12[last], e50[last], m89[last])
        glue = (max(e12[t0], e50[t0], m89[t0]) - min(e12[t0], e50[t0], m89[t0])) / c[t0] * 100
        base = dict(code=code, name=name, sig_date=kk['dates'][t0], hi0=round(hi0, 2),
                    close=round(c[last], 2), zlo=round(zlo, 2), zhi=round(zhi, 2),
                    glue=round(glue, 1), ma89_up=round(ma89_up, 2))
        hit = None
        for j in range(t0 + 1, min(t0 + WINDOW, last) + 1):   # 确认窗口严格 20 日
            if c[j] > hi0 and vr_at(v, j) >= 1.2:
                hit = j
                break
        win_left = WINDOW - (last - t0)          # 剩余可确认交易日；<0 = 窗口已关闭
        if hit is None:
            if win_left < 0:
                continue                          # 20日内未触发，信号作废，不进清单
            base['need'] = round(pct(hi0, c[last]), 1)
            base['note'] = '价已破高·待放量' if base['need'] <= 0 else '待突破'
            base['window_left'] = win_left
            pending.append(base)
            continue
        if hit == last:                           # 今日刚确认：明日开盘才买，今日不可成交
            gate = idx_gate(D, kk['dates'][last])   # 触发日=今日，取当日指数状态
            base.update(need=round(pct(hi0, c[last]), 1),
                        note='已确认·明日开盘买',
                        idx_ok=gate['ok'], idx_why=gate['why'],
                        idx_detail=gate['detail'],
                        window_left=0, trig_date=kk['dates'][last],
                        buy_hint='次一交易日 9:30 开盘价')
            pending.append(base)
            continue
        bi = hit + 1
        buy = o[bi]
        tp = buy * (1 + TP / 100)
        days = last - bi + 1
        gate = idx_gate(D, kk['dates'][hit])          # 触发日指数状态（历史持仓也带，供复盘归因）
        state = '持有中'
        tp_date = None
        tp_k = None
        # A股 T+1：买入当日不可卖出，止盈从次一交易日（bi+1）起判；
        # 时间退出仍是第 20 根 bar 收盘（bi+HOLD-1），天然满足 T+1。
        for k in range(bi + 1, min(bi + HOLD, last + 1)):
            if h[k] >= tp:
                state = '已止盈'
                tp_date = kk['dates'][k]
                tp_k = k
                break
        if days >= HOLD and state == '持有中':
            state = '到期平仓'
        # 持有日 = 实际占用资金的天数（已了结的按了结日截断，不再随基准日增长）
        held = (tp_k - bi + 1) if state == '已止盈' else (HOLD if state == '到期平仓' else days)
        if state == '已止盈':
            pnl = TP - 0.25
        elif state == '到期平仓':
            pnl = pct(c[bi + HOLD - 1], buy) - 0.25
        else:
            pnl = pct(c[last], buy) - 0.25
        left = 0 if state != '持有中' else max(0, HOLD - days)
        reached = bi + HOLD - 1 <= last
        if state == '已止盈':                       # 已了结：卖出日=盘中触达止盈价当日
            sell_date, sell_kind = tp_date, '止盈'
            exit_date, exit_estimated = None, False
        elif reached:                              # 20日已到：按第20日收盘平仓
            d20 = kk['dates'][bi + HOLD - 1]
            sell_date, sell_kind = d20, '到期'
            exit_date, exit_estimated = d20, False
        else:                                      # 持有中：卖出日未定，给预计到期日
            sell_date, sell_kind = None, None
            exit_date = add_trade_days(kk['dates'][last], left)
            exit_estimated = True
        base.update(trig_date=kk['dates'][hit], buy=round(buy, 2), tp=round(tp, 2),
                    buy_date=kk['dates'][bi],
                    tp_date=tp_date,                       # 实际盘中触达 +2.5% 的日期
                    sell_date=sell_date, sell_kind=sell_kind,
                    exit_date=exit_date,                  # 20日时间退出日；持有中为预计值
                    exit_estimated=exit_estimated,        # True=按交易日推算的约值（节假日顺延）
                    days_left=left,
                    done=state, pnl=round(pnl, 2), days=held,
                    idx_ok=gate['ok'], idx_why=gate['why'], idx_detail=gate['detail'],
                    zone='带上方' if c[last] >= zhi else ('带内' if c[last] >= zlo else '跌破带'))
        confirmed.append(base)

    pending.sort(key=lambda x: x['need'])
    confirmed.sort(key=lambda x: -x['pnl'])
    gate_today = idx_gate(D, today)
    return dict(date=today, window=WINDOW, hold=HOLD, scan_window=SCAN_W, tp=TP,
                idx_gate=gate_today,
                pending=pending, confirmed=confirmed)


def main():
    w = collect()
    pending, confirmed = w['pending'], w['confirmed']
    print('基准日 %s | A 级信号（回溯 %d 日 = %d 日确认窗口 + %d 日持有）共 %d 只：待确认/待买 %d / 已确认持仓 %d' % (
        w['date'], w['scan_window'], w['window'], w['hold'],
        len(pending) + len(confirmed), len(pending), len(confirmed)))
    print('\n=== 一、待「再放量破信号日高」确认（收盘 > 信号日高 且 量比>=1.2，确认日次日开盘买）===')
    print('%-9s %-9s %-11s %8s %10s %9s %10s %8s %s' % (
        '代码', '名称', '信号日', '现价', '突破确认价', '还需涨%', '三线带下沿', '粘合%', '状态'))
    for x in pending:
        print('%-9s %-9s %-11s %8.2f %10.2f %+8.1f%% %10.2f %7.1f%% %s' % (
            x['code'], x['name'], x['sig_date'], x['close'], x['hi0'],
            x['need'], x['zlo'], x['glue'], x['note']))
    print('\n=== 二、已确认持仓跟踪（+2.5% 止盈 / 20 日时间退出 / 无硬止损）===')
    print('%-9s %-9s %-11s %-11s %-11s %8s %10s %-9s %-12s %10s %6s %s' % (
        '代码', '名称', '信号日', '确认日', '买入日', '买入价', '+2.5%止盈', '状态', '卖出日', '了结/浮盈%', '持有日', '预计到期日'))
    for x in confirmed:
        sell = x['sell_date'] or ('~' + x['exit_date'] if x['exit_estimated'] else x['exit_date'])
        if x['sell_kind']:
            sell += '(' + x['sell_kind'] + ')'
        print('%-9s %-9s %-11s %-11s %-11s %8.2f %10.2f %-9s %-12s %+9.2f%% %6d %s%s' % (
            x['code'], x['name'], x['sig_date'], x['trig_date'], x['buy_date'], x['buy'],
            x['tp'], x['done'], sell, x['pnl'], x['days'],
            '~' if x['exit_estimated'] else '', x['exit_date'] or '—'))
    print('\n口径：确认日收盘后用当日及之前数据判定（收盘>信号日高 且 量比>=1.2）；'
          '买入日=确认日次日，按开盘价成交；+2.5% 盘中触达当日即卖；'
          '未触达则第 20 个交易日收盘平仓，未到期的「预计到期日」按周一至周五顺延估算（~，遇节假日顺延）；'
          '无个股硬止损（靠仓位分散，单票建议 <=1/4）。')


if __name__ == '__main__':
    main()
