#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙头战法 · 报告段落生成（日报 ⑧ 段 + 独立专题报告）。

独立用法：python3 dragon_report.py
  → 生成 daily/龙头战法_<数据日>.md
  → 幂等替换当日日报里的「## 🐉 龙头战法」段（保留 ⑦ 三线池段与末尾免责声明）

注意：不采用 tl_report.py 那种「截断到 MARK 之前再重写」的方式，
      因为日报里 ⑧ 段在 ⑦ 段之后，整体截断会把另一段删掉。
"""
import json
import os
import sys

WEB = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(WEB)
MARK = '## 🐉 龙头战法'
DISC = '> 结果为客观数据筛选'


def _j(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d if d is not None else {}


def load_all():
    return (_j(os.path.join(WEB, 'dragon.json')),
            _j(os.path.join(WEB, 'dragon', 'stats.json')))


def sections(pool, out=None, inline=False):
    """返回龙头战法 markdown 行列表。inline=True 时用于日报（标题层级一致）。"""
    out = out or {}
    if not pool.get('pool') and not pool.get('candidates'):
        return []
    em = pool.get('emotion') or {}
    ev = pool.get('evidence') or {}
    trk = pool.get('tracking') or {}
    rf = ev.get('rules_found') or {}
    ov = ev.get('overall') or {}
    lb = ev.get('lowbuy') or {}
    pool8 = pool.get('pool') or []
    cands = pool.get('candidates') or []
    traps = pool.get('traps_today') or []
    themes = pool.get('themes') or []
    ml = pool.get('market_leader')
    d = pool.get('as_of')
    L = []
    L.append(MARK + ' · 龙头候选分级与介入点（数据日 %s）' % d)
    L.append('')
    L.append('- **今日结论**：%s' % pool.get('verdict', ''))
    L.append('- 分级：**A 级 %s 只**（命中已验证强正期望组合）/ **B 级 %s 只**（中大盘市值合格，弱正期望）/ '
             '**C 级 %s 只**（未过门槛）/ **D 级 %s 只**（命中历史稳定陷阱）· 当日龙头候选 %s 只 · 全市场涨停 %s 家' % (
                 pool.get('n_A', 0), pool.get('n_B', 0),
                 sum(1 for x in cands if x.get('grade') == 'C'), pool.get('n_D', 0),
                 pool.get('n_candidates', 0), pool.get('n_zt', 0)))
    ladder = ' / '.join('%s板×%s' % (k, em['ladder'][k])
                        for k in sorted((em.get('ladder') or {}), key=lambda x: int(x))) or '无'
    L.append('- 情绪周期：**%s %.1f°** · 涨停 %s 家 · 炸板率 %s%% · 1进2晋级率 %s%% · 最高 **%s板** · 梯队 %s · regime %s' % (
        em.get('phase', '-'), em.get('temp') or 0, em.get('n_zt', 0), em.get('zb_rate', 0),
        em.get('promo') if em.get('promo') is not None else '-', em.get('hmax', 0),
        ladder, em.get('regime', '-')))
    if ml:
        L.append('- 空间龙头（客观最高板）：**%s %s %s板** · %s · %s %s亿 · %s级 %.1f分 · %s' % (
            ml['code'], ml['name'], ml['streak'], ml['stage'], ml['mv_tier'], ml['mv'],
            ml['grade'], ml['score'], ml['theme']))
        L.append('  - ⚠️ %s' % pool.get('leader_caution', ''))
    L.append('')

    if pool8:
        L.append('### 龙头池 Top%d · 分级 / 介入点 / 止损' % len(pool8))
        L.append('| # | 代码 | 名称 | 评级 | 评分 | 连板 | 阶段 | 市值层 | 收盘 | 回踩低吸区 | 低吸止损 | 打板确认 | 打板止损 | 仓位 |')
        L.append('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')
        for i, x in enumerate(pool8):
            e = x['entry']
            L.append('| %d | %s | %s | **%s** | %.1f | %d板 | %s | %s | %.2f | **%.2f~%.2f** | %.2f | %.2f | %.2f | %s |' % (
                i + 1, x['code'], x['name'], x['grade'], x['score'], x['streak'], x['stage'],
                x['mv_tier'], x['close'], e['pullback'][0], e['pullback'][1], e['pull_stop'],
                e['board_limit'], e['hard_stop'], e['position']))
        L.append('')
        L.append('**介入点明细**（首选回踩低吸，次选打板确认；次日高开 >收盘×1.03 不追）：')
        for i, x in enumerate(pool8[:5]):
            e = x['entry']
            L.append('%d. **%s %s**（%s级 %.1f分 · %d板 %s · %s）' % (
                i + 1, x['code'], x['name'], x['grade'], x['score'], x['streak'],
                x['stage'], x['mv_tier']))
            L.append('   - %s' % e['primary'])
            L.append('   - 回踩低吸 **%.2f~%.2f**（止损 %.2f，约 -5%%）｜打板确认 **%.2f**（止损 %.2f，约 -7%%）｜'
                     '弱转强参考不破 %.2f｜目标 %.2f / %.2f｜%s' % (
                         e['pullback'][0], e['pullback'][1], e['pull_stop'],
                         e['board_limit'], e['hard_stop'], e['weak_turn'],
                         e['target1'], e['target2'], e['position']))
            L.append('   - 命中规则：%s｜风险：%s' % (
                '、'.join(x.get('rule_hits') or []) or '无',
                '；'.join((x.get('risks') or [])[:2])))
        L.append('')

    if traps:
        L.append('### 今日陷阱名单（D级 · 命中历史稳定负期望组合）')
        L.append('| 代码 | 名称 | 连板 | 市值(亿) | 市值层 | 阶段 | 命中陷阱 |')
        L.append('|---|---|---|---|---|---|---|')
        for x in traps:
            L.append('| %s | %s | %d板 | %.0f | %s | %s | %s |' % (
                x['code'], x['name'], x['streak'], x['mv'], x['mv_tier'], x['stage'],
                '；'.join(x.get('traps') or [])[:70]))
        L.append('')

    if themes:
        L.append('### 题材主线（近5日涨停家数 + 梯队完整度）')
        L.append('| 题材 | 近5日家数 | 当日家数 | 最高板 | 爆发天数 | 题材龙头 | 阶段 |')
        L.append('|---|---|---|---|---|---|---|')
        for x in themes[:6]:
            L.append('| %s | %d | %d | %d板 | 第%d天 | %s(%d板) | %s |' % (
                x['tag'], x['n5'], x['n_zt'], x['max_streak'], x['day'],
                x['leader_name'], x['leader_streak'], x['stage']))
        L.append('')

    sy = pool.get('shouyin') or []
    if sy:
        L.append('### 断板 / 首阴观察（%d 只，列前6）' % len(sy))
        L.append('| 代码 | 名称 | 涨跌% | EMA12 | 守E12 | 观察要点 |')
        L.append('|---|---|---|---|---|---|')
        for x in sy[:6]:
            L.append('| %s | %s | %+.2f | %.2f | %s | %s |' % (
                x['code'], x['name'], x['chg'] or 0, x['e12'],
                '守住' if x['hold12'] else '跌破', x['note']))
        L.append('')

    # ---- 全历史证据 ----
    L.append('### 全历史前向验证（评分权重与规则的来源）')
    L.append('- 全历史 **%s** 个龙头候选 · 可买 **%s** · 次日一字涨停开盘买不进 **%s%%**（已从收益统计剔除）' % (
        ev.get('n_signals'), ev.get('n_buyable'), ev.get('unbuyable_pct')))
    L.append('- **次日开盘接力整体 5日 %+.2f%%、胜率 %s%%、超额 %+.2fpp**（3日 %+.2f%% / 10日 %+.2f%%）'
             ' → 无脑打板接力是负期望，这是本模块所有过滤的出发点' % (
                 ov.get('avg') or 0, ov.get('win'), ov.get('exc') or 0,
                 ov.get('d3') or 0, ov.get('d10') or 0))
    L.append('- **关键发现**：%s' % ev.get('key_finding', ''))
    L.append('- 评分方向反转因子：**%s**（全历史 edge 显著为负 → 反向计分）；其余维度权重由分组超额自动校准' % (
        '、'.join(ev.get('reversed_factors') or []) or '无'))
    L.append('- 回踩低吸模式检验：5日内回踩成交率 %s%%、成交后 5日 %+.2f%%、胜率 %s%% → 涨停后接回踩同样负期望，'
             '不能把「低吸」当默认解' % (
                 lb.get('fill_rate'), lb.get('avg_d5') or 0, lb.get('win_d5')))
    L.append('')
    if rf.get('tier1'):
        L.append('**数据自动发现的正期望规则（单/双/三条件网格，共测试 %s 组，分割日 %s）**' % (
            rf.get('n_tested'), rf.get('mid_date')))
        L.append('| 组合条件 | 样本 | 5日均值% | 胜率% | 超额pp | 前半期 | 后半期 | 时间稳定 |')
        L.append('|---|---|---|---|---|---|---|---|')
        for x in rf['tier1'][:6]:
            L.append('| %s | %d | %+.2f | %s | %+.2f | %+.2f | %+.2f | %s |' % (
                ' + '.join(x['conds']), x['n'], x['avg5'], x['win5'], x['exc5'],
                x['h0'] if x['h0'] is not None else 0,
                x['h1'] if x['h1'] is not None else 0,
                '**稳定**' if x['stable'] else '否'))
        L.append('')
    if rf.get('traps'):
        L.append('**稳定负期望陷阱规则**')
        L.append('| 组合条件 | 样本 | 5日均值% | 胜率% | 超额pp |')
        L.append('|---|---|---|---|---|')
        for x in rf['traps'][:5]:
            L.append('| %s | %d | %+.2f | %s | %+.2f |' % (
                ' + '.join(x['conds']), x['n'], x['avg5'], x['win5'], x['exc5']))
        L.append('')

    L.append('### 持续跟踪（供回测，≥30交易日）')
    L.append('- 当日全部龙头候选独立建档、逐日盯市；跟踪库 **%s** 个 · 跟踪中 **%s** · 已成熟 **%s** · 实测起点 %s。'
             '注意：跟踪库与全历史回测样本是两套数据 —— %s 个历史候选由本地日K离线重建（用于规则发现与权重校准），'
             '跟踪库从实测起点起每个交易日追加、快照首次落盘后不覆写（用于实盘前向留痕）。' % (
                 trk.get('n_signals'), trk.get('n_tracking'), trk.get('n_matured'),
                 trk.get('live_from'), ev.get('n_signals')))
    L.append('- 回测表 `webapp/dragon/signals.csv`（每行一个候选，含 PIT 特征 + 1/3/5/10日前向收益与等权小盘超额）；'
             '主档 `webapp/dragon/signals.json`；统计 `webapp/dragon/stats.json`；'
             '每日快照 `webapp/dragon/daily/`。每日收盘跑批自动追加。')
    L.append('')
    L.append('> 口径与风险：特征仅用信号日及之前数据（无未来函数）；题材归类用当前概念映射，历史有轻微 PIT 偏差；'
             '封板强度用日K「最低价距涨停价」代理，无法还原真实封板时间/封单；买入假设次日开盘可成交且不计滑点。'
             '「龙头」事后才能确认，本表是概率化候选。样本仅约15个月日K，规则发现存在过拟合可能，已做前后半期稳健性粗检，仍需实盘验证。')
    L.append('')
    return L


def write_standalone(pool, stats=None):
    d = pool.get('as_of')
    if not d:
        return None
    body = ['# 龙头战法 · %s' % d, ''] + sections(pool)
    p = os.path.join(BASE, 'daily', '龙头战法_%s.md' % d)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, 'w').write('\n'.join(body))
    return p


def standalone():
    pool, stats = load_all()
    if not pool.get('candidates'):
        print('dragon.json 无数据，请先运行 dragon.py')
        return 1
    p = write_standalone(pool, stats)
    print(p)
    # 幂等替换日报 ⑧ 段：只截掉旧 ⑧ 段（若有），保留 ⑦ 段与免责声明
    d = pool['as_of']
    dp = os.path.join(BASE, 'daily', '日报_%s.md' % d)
    if os.path.exists(dp):
        old = open(dp).read()
        head = (old.split(MARK)[0] if MARK in old else old).rstrip()
        head = '\n'.join(ln for ln in head.splitlines()
                         if not ln.startswith(DISC)).rstrip()
        disc = [ln for ln in old.splitlines() if ln.startswith(DISC)]
        merged = head + '\n\n' + '\n'.join(sections(pool, inline=True))
        if disc:
            merged += '\n' + disc[-1] + '\n'
        open(dp, 'w').write(merged)
    print(dp if os.path.exists(dp) else '(当日日报不存在，跳过合并)')
    return 0


if __name__ == '__main__':
    sys.exit(standalone())
