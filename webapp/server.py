#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXPMA 策略工作台 · 本地 Web 服务（纯标准库，无依赖）
数据：backtest/klines/（全市场日K，约5000只）+ snapshot.json（快照）
用法：python3 server.py [端口]  默认 8765
说明：/api/screen 默认 mvmax=100（小盘回测口径）；收盘跑批 daily_run.py 显式传
     mvmax=99999 以覆盖全市场。regime 等权指数恒为市值<=100亿小盘口径。
"""
import bisect, csv, datetime, glob, io, json, os, re, statistics, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.join(_REPO, 'backtest')
WEB = os.path.join(_REPO, 'webapp')

# 亏损股开关（PE_TTM<=0）：True=允许入选，False=只保留 PE>0
# 2026-11-04 按用户要求放开（此前四策略均硬性排除亏损股）。
# 代价：回测样本全部是「PE>0」跑的，亏损股这一段没有历史验证，只作观察。
ALLOW_LOSS = True

def pe_ok(m):
    """是否通过 PE 过滤：ALLOW_LOSS 放开时一律通过（PE 为负/缺失也可入选）"""
    return True if ALLOW_LOSS else (m.get('pe', 0) or 0) > 0

def is_st(name):
    """ST / *ST / S*ST / 退市股：全部从策略信号与观察池中剔除"""
    n = (name or '').upper().replace(' ', '').replace('　', '')
    return ('ST' in n) or ('退' in (name or ''))

def ema(vals, n):
    k = 2 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(e[-1] + k * (v - e[-1]))
    return e

def limit_ratio(code, name=''):
    """涨跌幅板宽：ST 5% / 创业板(sz30)·科创板(sh68) 20% / 北证 30% / 主板 10%"""
    if 'ST' in (name or '').upper():
        return 0.05
    if code.startswith('bj'):
        return 0.30
    if code.startswith('sz30') or code.startswith('sh68'):
        return 0.20
    return 0.10

class Data:
    def __init__(self):
        # 名单基石只读正式档。曾存在 /tmp/dk_snapshot.json 优先读取的后门，
        # 测试残留会静默替换基石，已移除；新档统一由 backtest/fetch_snapshot.py 收盘校验后写入。
        snap = json.load(open(os.path.join(_REPO, 'backtest/snapshot.json')))
        items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
        self.meta = {r['code']: {'name': r.get('name', ''),
                                 'mv': float(r.get('TotalMV') or 0),
                                 'pe': float(r.get('PE_TTM') or 0)} for r in items}
        self.k = {}
        for p in glob.glob(ROOT + '/klines/*.json'):
            code = os.path.basename(p)[:-5]
            kk = json.load(open(p))
            cl = [float(r[2]) for r in kk]
            self.k[code] = dict(
                dates=[r[0] for r in kk],
                o=[float(r[1]) for r in kk], c=cl,
                h=[float(r[3]) for r in kk], l=[float(r[4]) for r in kk],
                v=[float(r[5]) for r in kk],
                e12=ema(cl, 12),
                e50=ema(cl, 50),
                ma89=[sum(cl[max(0, i - 88):i + 1]) / len(cl[max(0, i - 88):i + 1])
                      for i in range(len(cl))])
        _now = datetime.datetime.now()
        self.today = _now.date().isoformat()
        # 15:10 之后视为已收盘，当日bar按收盘数据使用；之前视为盘中
        self.intraday = not (_now.hour > 15 or (_now.hour == 15 and _now.minute >= 10))
        # 行业分类（webapp/industry.json，东方财富 f100；fetch_industry.py 每日更新）
        self.__init_industry()
        # 指数监控（上证/科创50/创业板指/北证50）
        self.indices = {}
        idir = os.path.join(_REPO, 'backtest/indices')
        _inames = {'sh000300': '沪深300', 'sh000001': '上证指数', 'sh000688': '科创50',
                   'sz399006': '创业板指', 'bj899050': '北证50'}
        for _p in glob.glob(idir + '/*.json'):
            _code = os.path.basename(_p)[:-5]
            _k = json.load(open(_p))
            _c = [r[2] for r in _k]
            self.indices[_code] = dict(name=_inames.get(_code, _code), dates=[r[0] for r in _k],
                                       o=[r[1] for r in _k], c=_c, h=[r[3] for r in _k], l=[r[4] for r in _k],
                                       e12=ema(_c, 12), e50=ema(_c, 50))
        # 沪深300 排最前（大盘温度计，判断能否操作）
        _order = ['sh000300', 'sh000001', 'sh000688', 'sz399006', 'bj899050']
        self.indices = {k: self.indices[k] for k in _order if k in self.indices}
        # 基准交易日：以指数日历（上证）为唯一真值；缺指数时退化为个股最后 bar 的众数
        self.px_date = None
        if 'sh000001' in self.indices and self.indices['sh000001']['dates']:
            self.px_date = self.indices['sh000001']['dates'][-1]
        else:
            _c = {}
            for kk in self.k.values():
                if kk['dates']:
                    _c[kk['dates'][-1]] = _c.get(kk['dates'][-1], 0) + 1
            self.px_date = max(_c, key=_c.get) if _c else None
        # 新鲜度：最后 bar 不在基准交易日的个股 = 停牌/退市整理，禁止参与当日信号与定价
        self.fresh = {c for c, kk in self.k.items() if kk['dates'] and kk['dates'][-1] == self.px_date}
        self.stale = sorted(set(self.k) - self.fresh)
        # 等权小盘指数 + regime（保持小盘口径与回测一致：市值<=100亿）
        # 数据清洗：剔除上市前 5 个交易日（创/科无涨跌幅限制，+40% 会污染等权均值）
        # 与超出板块涨跌幅限制的脏 bar；成分数 <200 的早期日不计入 regime（避免单只股票决定指数）
        daily = {}
        for code, kk in self.k.items():
            if self.meta.get(code, {}).get('mv', 0) > 100:
                continue
            _lim = limit_ratio(code, self.meta.get(code, {}).get('name', ''))
            _ipo = len(kk['dates']) < 320   # 序列被上市日期截断 → bar0 是上市首日
            for t in range(1, len(kk['dates'])):
                if kk['v'][t] <= 0 or kk['c'][t - 1] <= 0:
                    continue
                if _ipo and t <= 5:
                    continue
                _r = kk['c'][t] / kk['c'][t - 1] - 1
                if abs(_r) > _lim + 0.03:
                    continue
                daily.setdefault(kk['dates'][t], []).append(_r)
        self.idx_days = sorted(daily)
        self.idx_set = set(self.idx_days)
        self.idx_rets = [statistics.mean(daily[d]) for d in self.idx_days]
        self.idx_cnt = [len(daily[d]) for d in self.idx_days]
        self.regime = {}
        _valid = [i for i, n in enumerate(self.idx_cnt) if n >= 200]
        _vpos = {i: j for j, i in enumerate(_valid)}
        for i in _valid:
            j = _vpos[i]
            if j >= 20:
                m = 1.0
                for x in _valid[j - 19:j + 1]:
                    m *= 1 + self.idx_rets[x]
                self.regime[self.idx_days[i]] = 'up' if m > 1 else 'down'
        # 市场环境软过滤（2026-11-05 起）：上证 > MA20 = up。
        # 回测：A级三线信号 d3 在「上证>MA20」时胜率55.6%、否则49.3%，区分度显著优于等权小盘20日动量；
        # 原 regime 仅保留作参考，不再作为三线评分的硬折扣。
        self.mkt = {}
        _sh = self.indices.get('sh000001')
        if _sh and len(_sh['c']) >= 20:
            for _i, _d in enumerate(_sh['dates']):
                if _i >= 19:
                    _ma20 = sum(_sh['c'][_i - 19:_i + 1]) / 20
                    self.mkt[_d] = 'up' if _sh['c'][_i] > _ma20 else 'down'

    def lc(self, code):
        """最后一根收盘bar的索引（盘中时剔除今日bar，收盘后纳入）"""
        kk = self.k[code]
        if kk['dates'][-1] == self.today and self.intraday:
            return len(kk['dates']) - 2
        return len(kk['dates']) - 1

    def __init_industry(self):
        _ip = os.path.join(WEB, 'industry.json')
        if os.path.exists(_ip):
            try:
                self.ind = json.load(open(_ip))
            except Exception:
                self.ind = {}
        else:
            self.ind = {}
        # 概念板块（webapp/concepts.json，fetch_concepts.py 每5天刷新）：主线识别用概念比行业更贴近短线资金
        _cp = os.path.join(WEB, 'concepts.json')
        try:
            self.con = json.load(open(_cp)).get('map', {})
        except Exception:
            self.con = {}

D = None
BOND = None

# ---------- 涨停复盘（20日内涨停池 / 主线行业 / 可操作候选） ----------
ZT_CACHE = {}

def _zt_ratio(code, name):
    """涨停板宽：ST 5% / 创业板(sz30)·科创板(sh68) 20% / 北证 30% / 主板 10%"""
    return limit_ratio(code, name)

def zt_pool(days=20):
    """扫描全市场，返回最近 days 个交易日内出现过收盘涨停的股票池 + 主线行业聚合 + 可操作候选。
    涨停判定：收盘价 >= round(昨收*(1+板宽), 2)（四舍五入到分，与交易所规则一致）。"""
    key = (D.today, D.intraday, days)
    if key in ZT_CACHE:
        return ZT_CACHE[key]
    cal = D.idx_days[-days:]
    if not cal:
        return {}
    pos = {d: i for i, d in enumerate(cal)}
    cal_set = set(cal)
    rows = []
    for code, kk in D.k.items():
        if code not in D.fresh:
            continue   # 停牌股不参与涨停复盘（避免把旧 bar 当成当日涨停）
        t = D.lc(code)
        if t < 30:
            continue
        m = D.meta.get(code, {})
        name = m.get('name', '')
        ratio = _zt_ratio(code, name)
        dates = kk['dates']
        hits = []
        for i in range(max(1, t - days - 12), t + 1):
            if dates[i] not in cal_set:
                continue
            prev = kk['c'][i - 1]
            if prev <= 0:
                continue
            if kk['c'][i] >= round(prev * (1 + ratio), 2) - 0.001:
                hits.append(i)
        if not hits:
            continue
        # 连板：以最后一次涨停收尾的连续涨停天数
        streak = 1
        j = hits[-1]
        while j >= 2:
            if kk['c'][j - 1] <= 0 or kk['c'][j - 1] < round(kk['c'][j - 2] * (1 + ratio), 2) - 0.001:
                break
            streak += 1
            j -= 1
        last_i = hits[-1]
        c20 = (kk['c'][t] / kk['c'][max(0, t - 20)] - 1) * 100
        # 26年以来收盘涨停总次数（股性活跃度代理：历史上高频拉板的票，
        # 大跌企稳后再度拉板的概率显著高于普通票）
        yi = bisect.bisect_left(dates, '2026-01-01', 1, t + 1)
        zt_ytd = 0
        for i in range(yi, t + 1):
            prev = kk['c'][i - 1]
            if prev > 0 and kk['c'][i] >= round(prev * (1 + ratio), 2) - 0.001:
                zt_ytd += 1
        # 深跌企稳标记：距60日高点回撤>=30% 且收盘站上MA89
        # （回测 2026 年：企稳后打板 5日 -0.28%（活跃票) / -1.50%（冷门票），整体负期望 → 仅作观察标记）
        hi60 = max(kk['h'][max(0, t - 59):t + 1]) if t >= 1 else 0
        dd60 = round((kk['c'][t] / hi60 - 1) * 100, 1) if hi60 > 0 else 0.0
        dip_ok = bool(dd60 <= -30 and kk['c'][t] > kk['ma89'][t])
        rows.append(dict(
            code=code, name=name, ind=D.ind.get(code, '其他'),
            mv=m.get('mv'), pe=m.get('pe'),
            close=kk['c'][t], chg=(kk['c'][t] / kk['c'][t - 1] - 1) * 100,
            zt_count=len(hits), last_zt=dates[last_i], zt_ytd=zt_ytd,
            dd60=dd60, dip_ok=dip_ok,
            since=len(cal) - 1 - pos[dates[last_i]],
            streak=streak, c20=c20,
            pb=round(kk['l'][last_i], 2),
            dist=round((kk['c'][t] / kk['l'][last_i] - 1) * 100, 1) if kk['l'][last_i] else None,
            above=bool(kk['c'][t] > kk['e50'][t] and kk['c'][t] > kk['ma89'][t]),
            vr=None))
    # 量比（5日均量）
    for r in rows:
        kk = D.k[r['code']]
        t = D.lc(r['code'])
        win = kk['v'][max(0, t - 5):t]
        if win and sum(win) > 0:
            r['vr'] = round(kk['v'][t] / (sum(win) / len(win)), 2)
    rows.sort(key=lambda r: (r['since'], -r['zt_count'], -r['streak']))
    # 主线行业聚合：近5日 / 近20日 涨停家数
    cal5 = set(cal[-5:])
    inds = {}
    for r in rows:
        d0 = r['last_zt']
        it = inds.setdefault(r['ind'], {'ind': r['ind'], 'n5': 0, 'n20': 0, 'stocks': []})
        it['n20'] += 1
        if d0 in cal5:
            it['n5'] += 1
        it['stocks'].append(r)
    industries = sorted(inds.values(),
                        key=lambda x: (-x['n5'], -x['n20']))
    for it in industries:
        it['stocks'].sort(key=lambda r: (-r['zt_count'], -r['streak'], r['since']))
        it['top'] = [{'code': r['code'], 'name': r['name'], 'zt_count': r['zt_count'],
                      'streak': r['streak']} for r in it['stocks'][:4]]
        del it['stocks']
    # 概念主线聚合（短线资金按题材流动，概念比行业更贴近盘面）
    cons = {}
    for r in rows:
        d0 = r['last_zt']
        for cn in D.con.get(r['code'], []):
            it = cons.setdefault(cn, {'con': cn, 'n5': 0, 'n20': 0, 'stocks': []})
            it['n20'] += 1
            if d0 in cal5:
                it['n5'] += 1
            it['stocks'].append(r)
    con_n5 = {k: v['n5'] for k, v in cons.items()}
    concepts = sorted(cons.values(), key=lambda x: (-x['n5'], -x['n20']))[:15]
    for it in concepts:
        seen = set()
        tops = []
        for r in sorted(it['stocks'], key=lambda r: (-r['zt_count'], -r['streak'], r['since'])):
            if r['code'] in seen:
                continue
            seen.add(r['code'])
            tops.append({'code': r['code'], 'name': r['name'],
                         'zt_count': r['zt_count'], 'streak': r['streak']})
            if len(tops) >= 4:
                break
        it['top'] = tops
        del it['stocks']
    # 可操作候选：20日内涨停>=2次（或近3日内2连板以上）+ 站上EMA50与MA89 + 未过度透支 + 非ST盈利股
    act = [r for r in rows
           if (r['zt_count'] >= 2 or (r['streak'] >= 2 and r['since'] <= 3))
           and r['above'] and r['since'] <= 8 and r['c20'] <= 45
           and 'ST' not in r['name'] and (r['pe'] or 0) > 0]
    act.sort(key=lambda r: (-r['zt_count'], -r['streak'], r['since']))
    # 精选评分（优中选优 → 每周≤5只的候选榜）：
    #   主线强度30（行业/概念 取强者：近5日涨停家数占比） + 人气20（20日涨停次数/连板高度）
    #   + 活跃度15（26年涨停总次数：高频拉板=股性活跃，跌深企稳后容易再拉板）
    #   + 买点位置25（现价距回踩参考位：0~6%满分，越远越低，破位近0） + 市值弹性10
    ind_n5 = {it['ind']: it['n5'] for it in industries}
    for r in act:
        r['_theme_n5'], r['_theme'] = ind_n5.get(r['ind'], 0), r['ind']
        for cn in D.con.get(r['code'], []):
            n = con_n5.get(cn, 0)
            if n > r['_theme_n5']:
                r['_theme_n5'], r['_theme'] = n, cn
    max_n5 = max([r['_theme_n5'] for r in act]) or 1
    for r in act:
        mv = r['mv'] or 999
        dist = r['dist'] if r['dist'] is not None else 99
        ytd = r.get('zt_ytd') or 0
        s_ind = round(r['_theme_n5'] / max_n5 * 30, 1)
        s_pop = min(12, r['zt_count'] * 4) + min(8, r['streak'] * 3)
        s_act = (15 if ytd >= 25 else 12 if ytd >= 15 else 9 if ytd >= 10
                 else 6 if ytd >= 6 else 4 if ytd >= 3 else 2 if ytd >= 2 else 0)
        s_pos = 25 if 0 <= dist <= 6 else 15 if dist <= 12 else 5 if dist <= 20 else 0
        s_mv = 10 if mv <= 60 else 7 if mv <= 120 else 4 if mv <= 300 else 0
        r['dist'] = round(dist, 1)
        r['score'] = round(s_ind + s_pop + s_act + s_pos + s_mv, 1)
        r['reason'] = '主线%s(5日%d家)·%s·26年%d次·距回踩%.1f%%·%.0f亿%s' % (
            r['_theme'], r['_theme_n5'],
            ('%d连板' % r['streak']) if r['streak'] > 1 else ('20日%d次涨停' % r['zt_count']),
            ytd, dist, mv, '·⚡深跌企稳' if r.get('dip_ok') else '')
    for r in rows:
        r.pop('_theme_n5', None)
        r.pop('_theme', None)
    elite = sorted(act, key=lambda r: -r['score'])[:8]
    ms = max(rows, key=lambda r: (r['streak'], r['zt_count']), default=None)
    res = dict(
        date=cal[-1], days=days, cal=cal,
        total=len(rows),
        today=sum(1 for r in rows if r['since'] == 0),
        yesterday=sum(1 for r in rows if r['since'] == 1),
        max_streak={'code': ms['code'], 'name': ms['name'], 'streak': ms['streak']} if ms else None,
        industries=industries[:15],
        concepts=concepts,
        actionable=act,
        elite=elite,
        pool=rows[:400])
    ZT_CACHE[key] = res
    return res

# ---------- 情绪周期（涨停梯队 / 炸板率 / 晋级率 / 情绪温度） ----------
EMOTION_CACHE = {}

def emotion_view(ndays=30):
    """全市场情绪周期：最近 ndays 个交易日，每日
    - 涨停家数 / 梯队分布（N连板家数）
    - 炸板率 = 盘中最高价触板但收盘未封 / 全部触板（含封住）
    - 晋级率 = 今日2板家数 / 昨日1板家数（1进2）
    - 最高连板高度
    - 情绪温度 = 涨停家数40 + 高度25 + 晋级率20 + (1-炸板率)15 → 阶段判定
    """
    key = (D.today, D.intraday, ndays)
    if key in EMOTION_CACHE:
        return EMOTION_CACHE[key]
    st = {}  # date -> dict
    for code, kk in D.k.items():
        t = D.lc(code)
        if t < 2:
            continue
        ratio = _zt_ratio(code, D.meta.get(code, {}).get('name', ''))
        dates = kk['dates']
        for i in range(max(1, t - ndays - 1), t + 1):
            d = dates[i]
            if d not in D.idx_set:
                continue
            prev = kk['c'][i - 1]
            if prev <= 0:
                continue
            limit = round(prev * (1 + ratio), 2)
            sealed = kk['c'][i] >= limit - 0.001
            touched = kk['h'][i] >= limit - 0.001
            if not touched:
                continue
            e = st.setdefault(d, {'zt': 0, 'zb': 0, 'ladder': {}, 'prom': 0, 'n1_y': 0})
            if sealed:
                e['zt'] += 1
                # 连板高度
                h = 1
                j = i
                while j >= 2 and kk['c'][j - 1] >= round(kk['c'][j - 2] * (1 + ratio), 2) - 0.001:
                    h += 1
                    j -= 1
                e['ladder'][h] = e['ladder'].get(h, 0) + 1
                if h == 2:
                    e['prom'] += 1
            else:
                e['zb'] += 1
    cal = [d for d in D.idx_days[-ndays:] if d in st]
    hist = []
    for k, d in enumerate(cal):
        e = st[d]
        zb_total = e['zt'] + e['zb']
        zb_rate = e['zb'] / zb_total if zb_total else 0.0
        # 晋级率(1进2)：今日2板家数 / 昨日1板家数
        n1_prev = st.get(cal[k - 1], {}).get('ladder', {}).get(1, 0) if k > 0 else None
        promo = (e['prom'] / n1_prev) if n1_prev else None
        hmax = max(e['ladder']) if e['ladder'] else 0
        s1 = min(40.0, e['zt'] * 0.4)
        s2 = min(25.0, hmax * 2.5)
        s3 = min(20.0, (promo or 0) * 100 * 0.4)
        s4 = 15.0 * (1 - min(1.0, zb_rate * 2))
        temp = round(s1 + s2 + s3 + s4, 1)
        phase = '冰点' if temp < 20 else '退潮' if temp < 40 else '回暖' if temp < 60 else '高潮'
        hist.append(dict(date=d, temp=temp, phase=phase, zt=e['zt'],
                         zhaban=e['zb'], zb_rate=round(zb_rate * 100, 1),
                         promo=round(promo * 100, 1) if promo is not None else None,
                         hmax=hmax, ladder=e['ladder']))
    EMOTION_CACHE[key] = dict(date=cal[-1] if cal else None, hist=hist)
    return EMOTION_CACHE[key]

def rt_quote(code):
    """腾讯实时行情单只（qt.gtimg.cn）：{name,price,prev_close,open,high,low,time}
    带合理性校验：high/low 偏离昨收±50%以外视为脏数据（腾讯偶发坏帧），置 None 丢弃。"""
    try:
        req = Request('https://qt.gtimg.cn/q=' + code, headers={'User-Agent': 'Mozilla/5.0'})
        text = urlopen(req, timeout=8).read().decode('gbk', 'ignore')
        val = text.split('=', 1)[1].strip().strip(';').strip('"').split('~')
        if len(val) > 34:
            price = float(val[3] or 0)
            prev = float(val[4] or 0)
            base = prev or price
            high = float(val[33] or 0)
            low = float(val[34] or 0)
            if not (base * 0.5 <= low <= price * 1.005):
                low = 0.0
            if not (price * 0.995 <= high <= base * 1.6):
                high = 0.0
            return dict(name=val[1],
                        price=price, prev_close=prev,
                        op=float(val[5] or 0), high=high, low=low,
                        time=val[30])
    except Exception:
        pass
    return None


def _phase_of_date(date):
    """查某交易日（buy_date/sell_date）的情绪阶段；超出30日窗口返回 (None, None)"""
    try:
        hist = emotion_view(30)['hist']
        m = {h['date']: h for h in hist}
        if date in m:
            return m[date]['phase'], m[date]['temp']
        # 非交易日/停牌：取该日期之前最近的一个
        before = [h for h in hist if h['date'] < date]
        if before:
            return before[-1]['phase'], before[-1]['temp']
    except Exception:
        pass
    return None, None

def emotion_now():
    """当前情绪阶段（复用 emotion_view 缓存，开销小）"""
    try:
        h = emotion_view(10)['hist']
        return h[-1] if h else None
    except Exception:
        return None

# ---------- 精选池（每周≤5只 · 自动选入/剔除纪律） ----------
PICKS_PATH = os.path.join(WEB, 'picks.json')
PICKS_HIST_PATH = os.path.join(WEB, 'picks_history.json')

def _week_key():
    iso = datetime.date.today().isocalendar()
    return '%d-W%02d' % (iso[0], iso[1])

def _eqw_ret_since(date):
    """等权全市场基线自 date 以来的累计收益%（D.idx_rets 为全市场等权日收益）"""
    try:
        i0 = next(i for i, d in enumerate(D.idx_days) if d >= date)
        v = 1.0
        for r in D.idx_rets[i0 + 1:]:
            v *= (1 + r)
        return round((v - 1) * 100, 2)
    except StopIteration:
        return None
    except Exception:
        return None

def picks_view():
    """精选纪律池：一周最多5只。选入=评分最高且不在池；剔除=破回踩止损/破MA89/20日涨幅>60%/选入10个交易日仍涨幅<3%。"""
    try:
        p = json.load(open(PICKS_PATH))
    except Exception:
        p = {'week': '', 'list': []}
    wk = _week_key()
    evicted = []
    if p.get('week') != wk:
        # 周度轮换：先归档上周精选池的最终表现（前向验证数据沉淀）
        if p.get('week') and p.get('list'):
            try:
                hist = json.load(open(PICKS_HIST_PATH))
            except Exception:
                hist = []
            entries = []
            for e in p['list']:
                bench = _eqw_ret_since(e.get('pick_date', D.idx_days[0] if D.idx_days else D.today))
                entries.append(dict(code=e['code'], name=e['name'], ind=e.get('ind', ''),
                                    score=e.get('score'), pick_date=e.get('pick_date'),
                                    pick_price=e.get('pick_price'), price=e.get('price'),
                                    pnl=e.get('pnl'), bench=bench,
                                    excess=(round(e['pnl'] - bench, 1)
                                            if e.get('pnl') is not None and bench is not None else None)))
            pn_avg = round(sum(x['pnl'] for x in entries if x['pnl'] is not None) /
                           max(1, len([x for x in entries if x['pnl'] is not None])), 1)
            b_avg = round(sum(x['bench'] for x in entries if x['bench'] is not None) /
                          max(1, len([x for x in entries if x['bench'] is not None])), 1)
            hist.append(dict(week=p['week'], entries=entries,
                             summary=dict(n=len(entries), pool=pn_avg, bench=b_avg,
                                          excess=round(pn_avg - b_avg, 1))))
            json.dump(hist[-26:], open(PICKS_HIST_PATH, 'w'), ensure_ascii=False, indent=1)
        p = {'week': wk, 'list': []}
    zt = zt_pool(20)
    act_map = {r['code']: r for r in zt.get('actionable', [])}
    # 剔除检查（纪律自动执行）
    keep = []
    for e in p['list']:
        r = act_map.get(e['code'])
        kk = D.k.get(e['code'])
        if not r or not kk:
            evicted.append(dict(e, why='已不符合可操作条件'))
            continue
        t = D.lc(e['code'])
        price = kk['c'][t]
        why = None
        if price < r['pb'] * 0.98:
            why = '跌破回踩止损(%.2f)' % r['pb']
        elif price < kk['ma89'][t]:
            why = '跌破MA89趋势线'
        elif r['c20'] > 60:
            why = '过度透支(20日%.0f%%)' % r['c20']
        else:
            held = len([d for d in D.idx_days if d >= e.get('pick_date', '')])
            if held >= 10 and (price / e.get('pick_price', price) - 1) * 100 < 3:
                why = '选入%d日涨幅<3%%，让出额度' % held
        if why:
            evicted.append(dict(e, why=why, price=price))
        else:
            e['price'] = price
            e['pnl'] = round((price / e['pick_price'] - 1) * 100, 1) if e.get('pick_price') else None
            # 回踩参考位随行情更新（新涨停会抬高参考位）；同时保留选入时点 pb_at_pick
            if e.get('pb_at_pick') is None:
                e['pb_at_pick'] = r['pb']
            e['pb'] = r['pb']
            keep.append(e)
    p['list'] = keep
    # 情绪门槛：退潮期打板最容易亏、高潮期禁追高——暂停自动选入（剔除照常），回暖/冰点才放行
    emo = emotion_now()
    gate = None
    if emo:
        gate = dict(phase=emo['phase'], temp=emo['temp'], paused=False)
        if emo['phase'] == '退潮':
            gate.update(paused=True, reason='情绪退潮(%.0f°)：暂停自动选入——退潮期接力最容易亏，等冰点埋伏或回暖试错' % emo['temp'])
        elif emo['phase'] == '高潮':
            gate.update(paused=True, reason='情绪高潮(%.0f°)：暂停自动选入——高潮禁追高，防透支后退潮急杀' % emo['temp'])
    # 自动选入：不足5只时按评分补足（情绪门槛放行才执行）
    if len(p['list']) < 5 and not (gate and gate['paused']):
        in_pool = {e['code'] for e in p['list']}
        for r in zt.get('elite', []):
            if len(p['list']) >= 5:
                break
            if r['code'] in in_pool:
                continue
            p['list'].append(dict(code=r['code'], name=r['name'], ind=r['ind'],
                                  score=r['score'], reason=r['reason'],
                                  pick_date=D.idx_days[-1] if D.idx_days else D.today,
                                  pick_price=r['close'], pb=r['pb'], pb_at_pick=r['pb'],
                                  stop_at_pick=round(r['pb'] * 0.98, 2) if r['pb'] else round(r['close'] * 0.95, 2)))
            in_pool.add(r['code'])
    p['list'].sort(key=lambda e: -e.get('score', 0))
    # 新选入条目补实时现价/浮动
    for e in p['list']:
        if e.get('price') is None:
            kk = D.k.get(e['code'])
            if kk:
                e['price'] = kk['c'][D.lc(e['code'])]
                if e.get('pick_price'):
                    e['pnl'] = round((e['price'] / e['pick_price'] - 1) * 100, 1)
    # 前向验证：每只对齐 等权全市场基线，算超额
    for e in p['list']:
        e['bench'] = _eqw_ret_since(e.get('pick_date', D.idx_days[0] if D.idx_days else D.today)) \
            if D.idx_days else None
        e['excess'] = round(e['pnl'] - e['bench'], 1) \
            if e.get('pnl') is not None and e.get('bench') is not None else None
    vals = [e['pnl'] for e in p['list'] if e.get('pnl') is not None]
    bvals = [e['bench'] for e in p['list'] if e.get('bench') is not None]
    perf = None
    if vals:
        pa = round(sum(vals) / len(vals), 1)
        ba = round(sum(bvals) / len(bvals), 1) if bvals else None
        perf = dict(pool=pa, bench=ba, excess=round(pa - ba, 1) if ba is not None else None)
    try:
        history = json.load(open(PICKS_HIST_PATH))
    except Exception:
        history = []
    json.dump(p, open(PICKS_PATH, 'w'), ensure_ascii=False, indent=1)
    return dict(week=wk, list=p['list'], evicted=evicted, gate=gate, perf=perf,
                history=history, quota_used=len(p['list']), quota_max=5)


# ---------- 沪深300 · EXPMA双线向上池（走坏剔除，每周≤5只可操作） ----------
HS300_CONS_PATH = os.path.join(WEB, 'hs300_cons.json')
HS300_POOL_PATH = os.path.join(WEB, 'hs300_pool.json')
HS300_CACHE = {}

def hs300_master():
    """沪深300成份股主档（基础数据本地化）：只读本地 webapp/hs300_cons.json，
    由 backtest/fetch_hs300.py（daily_run 跑批）负责联网刷新并留痕调入/调出。
    仅当本地完全无主档时才引导拉取一次并落盘；此后请求路径零外部依赖。"""
    try:
        return json.load(open(HS300_CONS_PATH))
    except Exception:
        pass
    try:
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
        if len(out) >= 250:
            m = {'date': D.today, 'map': out, 'hist': []}
            json.dump(m, open(HS300_CONS_PATH, 'w'), ensure_ascii=False, indent=1)
            return m
    except Exception:
        pass
    return {'date': None, 'map': {}, 'hist': []}

def hs300_metrics(code, t):
    """某股在收盘bar t 的 EXPMA 状态 → (pass, dict 明细)。判定口径：
    站上双线 c>e12>e50 且 双线向上 e12[t]>e12[t-5]、e50[t]>e50[t-10]（指数平滑，斜率用5/10日窗口）。
    剔除口径（用户规则 2026-11-04 统一）：只看 EMA50 价格结构，不看斜率、不看 EMA12——
      收盘 < EMA50 连续3个交易日不收回（含跌破当天为第1日）才剔除；
      破 EMA12×0.98 不再作为剔除/卖出条件（太严格）。"""
    kk = D.k[code]
    if t < 12:
        return False, {}
    c, e12, e50 = kk['c'][t], kk['e12'][t], kk['e50'][t]
    s12 = (e12 / kk['e12'][t - 5] - 1) * 100 if t >= 5 else 0.0
    s50 = (e50 / kk['e50'][t - 10] - 1) * 100 if t >= 10 else 0.0
    c20 = (c / kk['c'][max(0, t - 20)] - 1) * 100
    dist12 = (c / e12 - 1) * 100
    ok = c > e12 > e50 and s12 > 0 and s50 > 0
    # 连续收于EMA50下方天数（向后扫描，含今日；上限60防呆）
    below50, j = 0, t
    while j >= 0 and kk['c'][j] < kk['e50'][j] and below50 < 60:
        below50 += 1
        j -= 1
    # 滞回带：只按 EMA50 连续3日不收回判破位；破EMA12/贴线噪音一律不剔
    soft = below50 < 3
    m = dict(c=c, e12=e12, e50=e50, s12=round(s12, 2), s50=round(s50, 2),
             c20=round(c20, 1), dist12=round(dist12, 1), below50=below50,
             mv=D.meta.get(code, {}).get('mv'),
             ind=D.ind.get(code, '其他'), soft_ok=soft)
    if ok:
        return True, m
    # 剔除原因（统一口径：只有 EMA50 三日不收回才算破位）
    if below50 >= 3:
        m['why'] = '连续%d日收盘低于EMA50未收回' % below50
    else:
        m['why'] = 'EXPMA结构未达标（EMA50未破位，可保留观察）'
    return False, m

def hs300_view():
    """沪深300 EXPMA 向上池：
    - 入池：EXPMA12>EMA50 双线多头且双线向上（收盘确认）
    - 剔除（用户规则 2026-11-04 统一）：收盘<EMA50 连续3日不收回才剔（含跌破当天为第1日）；
      破EMA12×0.98 与斜率走平均不再剔除（避免"没破位就被误踢"）
    - 每周≤5只：池内评分取 Top5 —— 趋势强度30 + 买点位置25（贴近EMA12）+ 动能质量25 + 流动性10 + 相对强度10
    """
    key = (D.today, D.intraday)
    if key in HS300_CACHE:
        return HS300_CACHE[key]
    try:
        st = json.load(open(HS300_POOL_PATH))
    except Exception:
        st = {'members': {}, 'evicted': [], 'picks': {'week': '', 'list': []}, 'hist': []}
    mst = hs300_master()
    cons = mst['map']
    today = D.idx_days[-1] if D.idx_days else D.today
    passing, failing = {}, {}
    for c6, name in cons.items():
        if is_st(name) or is_st(D.meta.get((('sh' if c6[0] == '6' else 'sz') + c6), {}).get('name')):
            continue   # ST/*ST/退市成份股不进 300 池
        code = ('sh' if c6[0] == '6' else 'sz') + c6
        kk = D.k.get(code)
        if not kk:
            continue
        ok, m = hs300_metrics(code, D.lc(code))
        (passing if ok else failing)[code] = dict(m, name=name, code=code)
    # 池子维护：仍在池=严格pass 或 宽松带内（soft_ok，贴线不洗）；走坏→剔除留痕（收盘确认）
    members = st.setdefault('members', {})
    for code in list(members):
        if code not in passing:
            f = failing.get(code)
            if f and f.get('soft_ok'):
                members[code]['name'] = f['name']   # 缓冲带内：保留观察，不剔除
                continue
            if f:
                if not any(e['code'] == code and e['date'] == today for e in st['evicted']):
                    st['evicted'].append(dict(code=code, name=f['name'], date=today,
                                              why=f.get('why', 'EXPMA走坏'),
                                              price=f['c'], hold_days=len([d for d in D.idx_days if d >= members[code]['since']])))
            del members[code]
    for code, m in passing.items():
        if code not in members:
            members[code] = {'since': today, 'name': m['name']}
        members[code]['name'] = m['name']
    st['evicted'] = st['evicted'][-50:]
    # 池内评分（每周 Top5 的排序依据）
    rows = []
    bench20 = 1.0
    for r in D.idx_rets[-20:]:
        bench20 *= (1 + r)
    bench20 = (bench20 - 1) * 100
    for code in members:
        m = passing.get(code) or failing.get(code)
        if not m:
            continue
        s_trend = min(30.0, max(0.0, m['s12'] * 20))          # EMA12 五日斜率
        d12 = m['dist12']
        s_pos = 25 if 0 <= d12 <= 3 else 15 if d12 <= 6 else 5 if d12 <= 12 else 0
        c20 = m['c20']
        s_mom = 25 if 5 <= c20 <= 25 else 15 if 0 <= c20 < 5 or 25 < c20 <= 40 else 5 if c20 <= 60 else 0
        mv = m['mv'] or 0
        s_liq = 10 if mv >= 300 else 7 if mv >= 100 else 4
        s_rel = min(10.0, max(0.0, (c20 - bench20) / 3))
        m['score'] = round(s_trend + s_pos + s_mom + s_liq + s_rel, 1)
        m['reason'] = 'EMA12五日%+.1f%%·距EMA12%+.1f%%·20日%+.0f%%' % (m['s12'], d12, c20)
        m['stop'] = round(m['e50'], 2)                           # 破位参考：EMA50（连续3日不收回才离场）
        rows.append(m)
    rows.sort(key=lambda r: -r['score'])
    # 周度轮换：新周归档上周表现，再从池内选 Top5
    wk = _week_key()
    pk = st.setdefault('picks', {'week': '', 'list': []})
    if pk.get('week') != wk:
        if pk.get('week') and pk.get('list'):
            entries = []
            for e in pk['list']:
                bench = _eqw_ret_since(e.get('pick_date', today))
                entries.append(dict(code=e['code'], name=e['name'], score=e.get('score'),
                                    pick_date=e.get('pick_date'), pick_price=e.get('pick_price'),
                                    pnl=e.get('pnl'), bench=bench,
                                    excess=round(e['pnl'] - bench, 1)
                                    if e.get('pnl') is not None and bench is not None else None))
            hist = st.setdefault('hist', [])
            hist.append(dict(week=pk['week'], entries=entries,
                             summary=dict(n=len(entries))))
            st['hist'] = hist[-26:]
        # 关键：必须原地更新 st['picks']（此前 pk=新dict 只改了局部变量，week 永远存不进文件，
        # 导致每次调用都当作新周重新选股、周度归档永不触发）
        st['picks'] = {'week': wk, 'list': []}
        pk = st['picks']
        for r in rows[:5]:
            pk['list'].append(dict(code=r['code'], name=r['name'], ind=r['ind'],
                                   score=r['score'], reason=r['reason'],
                                   pick_date=today, pick_price=r['c'],
                                   e12_at_pick=r['e12'], stop_at_pick=r['stop'],
                                   dist12=r['dist12']))
    # 本周持仓的剔除检查（EXPMA 走坏 → 让出额度，收盘确认）
    picks_keep, picks_evicted = [], []
    for e in pk['list']:
        ok, m = hs300_metrics(e['code'], D.lc(e['code']))
        if ok or m.get('soft_ok'):   # 与池子剔除同口径：贴线缓冲带内不洗，只让真正走坏的让额度
            e['price'] = m['c']
            e['pnl'] = round((m['c'] / e['pick_price'] - 1) * 100, 1) if e.get('pick_price') else None
            e['stop'] = round(m['e50'], 2)
            picks_keep.append(e)
        else:
            picks_evicted.append(dict(e, why=m.get('why', 'EXPMA走坏')))
    pk['list'] = picks_keep
    # 额度未满时递补（仅池内 Top，且不在当前列表）
    if len(pk['list']) < 5:
        in_list = {e['code'] for e in pk['list']}
        for r in rows:
            if len(pk['list']) >= 5:
                break
            if r['code'] in in_list:
                continue
            pk['list'].append(dict(code=r['code'], name=r['name'], ind=r['ind'],
                                   score=r['score'], reason=r['reason'],
                                   pick_date=today, pick_price=r['c'],
                                   e12_at_pick=r['e12'], stop_at_pick=r['stop'],
                                   dist12=r['dist12']))
            in_list.add(r['code'])
    for e in pk['list']:
        e['bench'] = _eqw_ret_since(e.get('pick_date', today)) if D.idx_days else None
        e['excess'] = round(e['pnl'] - e['bench'], 1) \
            if e.get('pnl') is not None and e.get('bench') is not None else None
    json.dump(st, open(HS300_POOL_PATH, 'w'), ensure_ascii=False, indent=1)
    vals = [e['pnl'] for e in pk['list'] if e.get('pnl') is not None]
    perf = None
    if vals:
        pa = round(sum(vals) / len(vals), 1)
        bvals = [e['bench'] for e in pk['list'] if e.get('bench') is not None]
        ba = round(sum(bvals) / len(bvals), 1) if bvals else None
        perf = dict(pool=pa, bench=ba, excess=round(pa - ba, 1) if ba is not None else None)
    res = dict(date=today, total_cons=len(cons), pool_n=len(rows), pool=rows,
               cons_date=mst.get('date'),
               cons_changes=(mst.get('hist') or [None])[-1],
               picks=dict(week=wk, list=pk['list']), picks_evicted=picks_evicted,
               evicted=list(reversed(st['evicted']))[:10], hist=st.get('hist', [])[-8:],
               perf=perf, bench20=round(bench20, 1))
    HS300_CACHE[key] = res
    return res


# ---------- 交易计划（真实持仓 / 止盈止损纪律） ----------
TRADES_PATH = os.path.join(WEB, 'trades.json')
RISK_PATH = os.path.join(WEB, 'risk_rules.json')

_RISK_DEFAULTS = {'max_principal_per_stock': 200000, 'max_loss_per_stock': 10000}

def _load_risk_rules():
    """风控红线（单一事实源 webapp/risk_rules.json）：单股本金上限 / 单股亏损止损线"""
    r = dict(_RISK_DEFAULTS)
    if os.path.exists(RISK_PATH):
        try:
            r.update({k: v for k, v in json.load(open(RISK_PATH)).items()
                      if k in _RISK_DEFAULTS and v})
        except Exception:
            pass
    return r

def _stock_principal(t, code):
    """同一代码当前持仓占用本金合计（buy_price*qty）"""
    return sum(p.get('buy_price', 0) * p.get('qty', 0)
               for p in t['positions'] if p['code'] == code)

def _loss_stop_price(buy_price, qty, max_loss):
    """亏损红线下限价：买入价 - 最大亏损额/数量（到价必须触发止损）"""
    if not qty:
        return None
    return round(buy_price - max_loss / qty, 2)

def _load_trades():
    if os.path.exists(TRADES_PATH):
        try:
            return json.load(open(TRADES_PATH))
        except Exception:
            pass
    return {'cash': 100000, 'positions': [], 'closed': []}

def _save_trades(t):
    json.dump(t, open(TRADES_PATH, 'w'), ensure_ascii=False, indent=1)

def trades_view():
    """持仓实时视图：现价/浮动盈亏/持有交易日/止损止盈线/状态判定（纪律执行看板）"""
    t = _load_trades()
    _rk = _load_risk_rules()
    dd = D.idx_days[-1] if D.idx_days else None
    out_pos = []
    for p in t['positions']:
        code = p['code']
        if code not in D.k:
            out_pos.append(dict(p, status='no_data'))
            continue
        kk = D.k[code]
        ti = D.lc(code)
        price = kk['c'][ti]
        try:
            ei = kk['dates'].index(p['buy_date'])
        except ValueError:
            ei = max(0, ti - 1)
        days = len(kk['dates'][ei:ti + 1])
        # 持有期最高价（移动止盈用，持久化）
        hi = max([p.get('hi') or 0] + kk['h'][ei:ti + 1])
        p['hi'] = round(hi, 2)
        stop = p.get('stop') or round(p['buy_price'] * 0.95, 2)
        t1 = p.get('t1') or round(p['buy_price'] * 1.05, 2)
        t2 = p.get('t2') or round(p['buy_price'] * 1.10, 2)
        trail = p.get('trail', 5)
        # 移动止盈：高点回撤线高于买入价才启用（保护利润），否则交给止损负责下行
        trail_stop = round(hi * (1 - trail / 100), 2) if hi else None
        if trail_stop is not None and trail_stop <= p['buy_price']:
            trail_stop = None
        pnl = (price / p['buy_price'] - 1) * 100
        # EXPMA破位判定（用户规则·回测定稿口径，与沪深300池剔除一致）：
        # 收盘<EMA50 连续3个交易日不收回=破位离场（含跌破当天为第1日）；第1~2日=预警。
        # 不再用破EMA12作离场信号。
        b50, j = 0, ti
        while j >= 0 and kk['c'][j] < kk['e50'][j] and b50 < 60:
            b50 += 1
            j -= 1
        if price <= stop:
            status = 'stop_hit'
        elif price >= t2:
            status = 'target2'
        elif price >= t1:
            status = 'target1'
        elif trail_stop and price <= trail_stop:
            status = 'trail_hit'
        elif b50 >= 3:
            status = 'brk50'
        elif b50 >= 1:
            status = 'warn50'
        elif price <= stop * 1.02:
            status = 'near_stop'
        else:
            status = 'hold'
        out_pos.append(dict(p, price=price, days=days, pnl=round(pnl, 2),
                            pnl_amt=round((price - p['buy_price']) * p['qty'], 0),
                            stop=stop, t1=t1, t2=t2, trail_stop=trail_stop,
                            principal=round(p['buy_price'] * p['qty'], 0),
                            loss_limit_px=_loss_stop_price(p['buy_price'], p['qty'],
                                                           _rk['max_loss_per_stock']),
                            e12=round(kk['e12'][ti], 2), e50=round(kk['e50'][ti], 2),
                            below50=b50, status=status,
                            hi=hi, last_date=kk['dates'][ti]))
    _save_trades(t)
    cl = t['closed']
    # 情绪归因：每笔平仓按买入日情绪阶段打标，聚合胜率/均收益（攒数据看"哪个人性关口在亏钱"）
    changed_t = False
    for c in cl:
        if 'emo_phase' not in c:
            ph, tp = _phase_of_date(c.get('buy_date', ''))
            if ph:
                c['emo_phase'] = ph
                c['emo_temp'] = tp
                changed_t = True
    if changed_t:
        _save_trades(t)
    by_phase = {}
    for c in cl:
        ph = c.get('emo_phase') or '未知'
        b = by_phase.setdefault(ph, {'phase': ph, 'n': 0, 'win': 0, 'avg_ret': 0.0, 'sum_ret': 0.0})
        b['n'] += 1
        b['sum_ret'] += c['ret']
        if c['ret'] > 0:
            b['win'] += 1
    phase_order = {'冰点': 0, '退潮': 1, '回暖': 2, '高潮': 3, '未知': 9}
    stats_phase = []
    for b in by_phase.values():
        b['avg_ret'] = round(b['sum_ret'] / b['n'], 2)
        b['win'] = round(b['win'] / b['n'] * 100, 1)
        del b['sum_ret']
        stats_phase.append(b)
    stats_phase.sort(key=lambda x: phase_order.get(x['phase'], 8))
    emo = emotion_now()
    wins = [c for c in cl if c['ret'] > 0]
    # 风控红线：规则 + 存量持仓合规检查（本金超上限 / 止损价低于亏损红线）
    risk_violations = []
    for p in out_pos:
        if p.get('principal', 0) > _rk['max_principal_per_stock'] + 1e-6:
            risk_violations.append('%s 本金 ¥%s 超单股上限' % (
                p['code'], f"{p['principal']:,.0f}"))
        llp = p.get('loss_limit_px')
        if llp and (p.get('stop') or 0) < llp:
            risk_violations.append('%s 止损价 %s 低于亏损红线价 %s' % (
                p['code'], p.get('stop'), llp))
    return dict(date=dd, regime=D.regime.get(dd, 'down'), cash=t['cash'],
                emotion={'phase': emo['phase'], 'temp': emo['temp']} if emo else None,
                risk=dict(max_principal_per_stock=_rk['max_principal_per_stock'],
                          max_loss_per_stock=_rk['max_loss_per_stock'],
                          violations=risk_violations),
                positions=out_pos, closed=cl,
                stats=dict(n_closed=len(cl),
                           win=round(len(wins) / len(cl) * 100, 1) if cl else None,
                           avg_ret=round(sum(c['ret'] for c in cl) / len(cl), 2) if cl else None,
                           realized=round(sum((c['sell_price'] - c['buy_price']) * c['qty'] for c in cl), 0),
                           by_phase=stats_phase))

def trade_op(q):
    """op: add(建仓) / sell(平仓,qty空=全平) / stop(改止损) / del(删记录) / cash(设总资金)"""
    t = _load_trades()
    op = q.get('op', [''])[0]
    if op == 'cash':
        t['cash'] = float(q.get('v', ['0'])[0])
        _save_trades(t)
        return {'ok': True}
    if op == 'add':
        code = q.get('code', [''])[0]
        if code not in D.k:
            return {'error': '未知代码'}
        price = float(q.get('price', ['0'])[0])
        qty = int(float(q.get('qty', ['0'])[0]))
        if price <= 0 or qty <= 0:
            return {'error': '价格/数量无效'}
        # 风控红线①：单股本金上限（同票多笔合计）
        _rk = _load_risk_rules()
        _cap = _rk['max_principal_per_stock']
        _old = _stock_principal(t, code)
        _new = price * qty
        if _old + _new > _cap + 1e-6:
            return {'error': '风控红线：单股本金上限 ¥%s（已持有 ¥%s + 本笔 ¥%s 超限），不予录入'
                    % (f'{_cap:,}', f'{_old:,.0f}', f'{_new:,.0f}')}
        nid = max([0] + [p['id'] for p in t['positions']] + [c['id'] for c in t['closed']]) + 1
        g = lambda k, d=None: float(q[k][0]) if q.get(k, [None])[0] not in (None, '') else d
        # 风控红线②：止损价不得低于「亏损 ¥max_loss」对应价位（用户未填止损时直接取红线价）
        _loss_px = _loss_stop_price(price, qty, _rk['max_loss_per_stock'])
        _stop_in = g('stop')
        stop = max(_stop_in, _loss_px) if (_stop_in and _loss_px) else (_loss_px or _stop_in)
        rec = dict(id=nid, code=code, name=D.meta.get(code, {}).get('name', ''),
                   buy_date=q.get('date', [D.today])[0], buy_price=price, qty=qty,
                   stop=stop, t1=g('t1'), t2=g('t2'),
                   trail=g('trail', 5), tag=q.get('tag', ['手动'])[0],
                   note=q.get('note', [''])[0], hi=None)
        if _stop_in and _loss_px and _stop_in < _loss_px:
            rec['note'] = ((rec['note'] + '；') if rec['note'] else '') + \
                '止损价已按红线抬高至 %s（单股亏损上限 ¥%s）' % (stop, _rk['max_loss_per_stock'])
        t['positions'].append(rec)
        _save_trades(t)
        return {'ok': True, 'id': nid}
    if op == 'sell':
        pid = int(q.get('id', ['0'])[0])
        price = float(q.get('price', ['0'])[0])
        qty_s = q.get('qty', [''])[0]
        pos = next((p for p in t['positions'] if p['id'] == pid), None)
        if not pos or price <= 0:
            return {'error': '持仓不存在或价格无效'}
        sell_qty = pos['qty'] if qty_s in ('', 'all') else min(int(float(qty_s)), pos['qty'])
        if sell_qty <= 0:
            return {'error': '数量无效'}
        stop = pos.get('stop') or pos['buy_price'] * 0.95
        t1 = pos.get('t1') or pos['buy_price'] * 1.05
        t2 = pos.get('t2') or pos['buy_price'] * 1.10
        hi = pos.get('hi') or price
        if price <= stop:
            reason = '止损'
        elif price >= t2:
            reason = '止盈目标2'
        elif price >= t1:
            reason = '止盈目标1'
        elif pos.get('trail_stop') and price <= pos['trail_stop']:
            reason = '移动止盈'
        else:
            reason = '手动'
        t['closed'].append(dict(id=pos['id'], code=pos['code'], name=pos['name'],
                                buy_date=pos['buy_date'], buy_price=pos['buy_price'],
                                qty=sell_qty, sell_date=D.today, sell_price=price,
                                ret=round((price / pos['buy_price'] - 1) * 100, 2),
                                reason=reason, tag=pos.get('tag', '')))
        if sell_qty >= pos['qty']:
            t['positions'] = [p for p in t['positions'] if p['id'] != pid]
        else:
            pos['qty'] -= sell_qty
        _save_trades(t)
        return {'ok': True, 'reason': reason}
    if op == 'stop':
        pid = int(q.get('id', ['0'])[0])
        pos = next((p for p in t['positions'] if p['id'] == pid), None)
        if not pos:
            return {'error': '不存在'}
        # 风控红线②同样约束手动改止损：不得低于单股亏损上限对应价位
        _rk = _load_risk_rules()
        _loss_px = _loss_stop_price(pos['buy_price'], pos['qty'], _rk['max_loss_per_stock'])
        v = float(q.get('v', ['0'])[0])
        if _loss_px and v < _loss_px:
            return {'error': '风控红线：止损价不得低于 %s（单股亏损上限 ¥%s）'
                    % (_loss_px, _rk['max_loss_per_stock'])}
        pos['stop'] = v
        _save_trades(t)
        return {'ok': True}
    if op == 'del':
        pid = int(q.get('id', ['0'])[0])
        t['positions'] = [p for p in t['positions'] if p['id'] != pid]
        _save_trades(t)
        return {'ok': True}
    return {'error': '未知操作'}


def bond_payload():
    """股债长跑：上证指数全历史 + 上证国债指数按锚点日缩放对齐。
    锚点=两序列首个共同交易日，该日两线等值；此后国债线按其复利增速外推。
    返回与上证日期轴对齐的 bond 数组（2003年前为 None），前端画在同一坐标系。"""
    bdir = os.path.join(_REPO, 'backtest/bond')
    sse = json.load(open(os.path.join(bdir, 'sh000001.json')))
    bi = json.load(open(os.path.join(bdir, 'sh000012.json')))
    bmap = {r[0]: float(r[2]) for r in bi}
    dates = [r[0] for r in sse]
    sse_c = [float(r[2]) for r in sse]
    ai = next((i for i, d in enumerate(dates) if d in bmap), None)
    if ai is None:
        return None
    a_sse, a_bond = sse_c[ai], bmap[dates[ai]]
    bond = [None] * len(dates)
    for i in range(ai, len(dates)):
        b = bmap.get(dates[i])
        if b:
            bond[i] = round(a_sse * b / a_bond, 2)
    return {'dates': dates, 'sse': [round(v, 2) for v in sse_c], 'bond': bond,
            'anchor': dates[ai], 'anchor_sse': a_sse,
            'sse_now': sse_c[-1], 'bond_now': bond[-1],
            'ratio': round(sse_c[-1] / bond[-1], 3) if bond[-1] else None,
            'bond_date': dates[-1]}

def vr_at(kk, t):
    pv = kk['v'][t - 5:t]
    if t < 5 or min(pv) <= 0 or kk['v'][t] <= 0:
        return None
    return kk['v'][t] / (sum(pv) / 5)

def sig_pre(kk, t, dev=6, spread=8, c20lo=5, c20hi=20, c60hi=20, vrmin=1.2):
    c, e1, e2 = kk['c'][t], kk['e12'][t], kk['e50'][t]
    if not (c > e1 > e2): return False
    if (c - e1) / e1 * 100 > dev or (e1 - e2) / e2 * 100 > spread: return False
    if t < 60: return False
    c20 = (c / kk['c'][t - 20] - 1) * 100
    if not (c20lo <= c20 <= c20hi): return False
    if not (0 < (c / kk['c'][t - 60] - 1) * 100 <= c60hi): return False
    vr = vr_at(kk, t)
    return vr is not None and vr >= vrmin

def sig_dk(kk, t):
    c, e1, e2 = kk['c'][t], kk['e12'][t], kk['e50'][t]
    if not (c > e1 > e2): return False
    if (c - e1) / e1 * 100 > 12 or (e1 - e2) / e2 * 100 > 15: return False
    if t < 60: return False
    c20 = (c / kk['c'][t - 20] - 1) * 100
    if not (8 <= c20 <= 40): return False
    if not (0 < (c / kk['c'][t - 60] - 1) * 100 <= 35): return False
    vr = vr_at(kk, t)
    return vr is not None and vr >= 1.5

def sig_never(kk, t, days=11):
    if t < 60 or t - days + 1 < 0: return False
    for i in range(t - days + 1, t + 1):
        if not (kk['c'][i] > kk['e12'][i] > kk['e50'][i]): return False
    c20 = (kk['c'][t] / kk['c'][t - 20] - 1) * 100
    c60 = (kk['c'][t] / kk['c'][t - 60] - 1) * 100
    return c20 > 3 and c60 > 0

def sig_dc(kk, t):
    """双穿阳线+量比1.5+破20日高+MA89上方：回测12个月最优变体
    （MA89上方 h5超额+2.0pp；MA89下方信号超额≈0，印证'89日线之下无行情'）
    市值/PE 过滤由调用方按 mvmax 参数执行"""
    c, o, e1, e2 = kk['c'], kk['o'], kk['e12'], kk['e50']
    if t < 95: return False
    if not (c[t] > o[t]): return False                       # 阳线
    if not (c[t-1] < e1[t-1] and c[t-1] < e2[t-1]): return False  # 昨收在双线下方
    if not (c[t] > e1[t] and c[t] > e2[t]): return False     # 今收一日双穿
    vr = vr_at(kk, t)
    if vr is None or vr < 1.5: return False                  # 放量
    if c[t] <= max(c[t-20:t]): return False                  # 突破20日高
    if c[t] <= kk['ma89'][t]: return False                   # 89日均线上方
    return True

def cross_lines(kk, t):
    """今日阳柱实体内含几条均线：2=EXPMA12/50 都在实体内；3=再加 MA89 也在实体内；0=不是穿线阳。
    判据：开盘 < 线 < 收盘（严格在阳柱实体内部），比「昨收在下/今收在上」更严格。"""
    c, o = kk['c'][t], kk['o'][t]
    e1, e2, m89 = kk['e12'][t], kk['e50'][t], kk['ma89'][t]
    if not (c > o and o < min(e1, e2) and c > max(e1, e2)):
        return 0
    return 3 if o < m89 < c else 2

def sig_cross(kk, t, min_lines=2, brk20=False):
    """⑥ 穿线阳（形态全量观察，非回测定稿口径）：
      当日阳线实体同时包住 EXPMA12 与 EXPMA50；MA89 也在实体内 = 三线穿阳（更强）。
      默认不设量比 / 破20日高 / MA89上方 门槛——这三条正是④双穿阳线把「穿了线但没放量/没破20日高」
      的票挡在外面的原因（例：sh688507 索辰科技 2026-11-04 三线穿阳，vr=1.40、20日高未破）。
      min_lines=3 时只看三线穿阳；brk20=True 时叠加「收盘破20日高」（离线回测中这是唯一
      能把三线穿阳的 5 日前向期望由负转正的过滤条件）。市值/PE/ST 过滤由调用方执行。"""
    if t < 95: return False
    if cross_lines(kk, t) < min_lines: return False
    if brk20 and kk['c'][t] <= max(kk['c'][t - 20:t]): return False
    return True

def sig_m89(kk, t, flat=6.0, c20lo=3.0, c20hi=40.0, lookback=10, vol=False):
    """89金叉启动：EXPMA50 刚上穿 MA89（金叉）+ EXPMA12 在 50 上方 + MA89 走平 + 随后启动
    条件：
      1) EXPMA12 在 EXPMA50 上方
      2) EXPMA50 当前在 MA89 上方
      3) 约20日前 EXPMA50 仍在 MA89 下方（确保是"近期"金叉，而非长期多头）
      4) 金叉发生在最近 lookback 根内（e50[i]>ma89[i] 且 e50[i-1]<=ma89[i-1]）
      5) MA89 走平：最近20日斜率幅度 <= flat%
      6) 启动：价格已站上 EXPMA12 且 20日涨幅在 [c20lo, c20hi] 内
    市值/PE 过滤由调用方按 mvmax 参数执行；vol=True 时额外要求金叉日量比>=1.2"""
    c, e12, e50, ma89 = kk['c'], kk['e12'], kk['e50'], kk['ma89']
    if t < 95: return False
    if not (e12[t] > e50[t]): return False                      # 12 在 50 上方
    if not (e50[t] > ma89[t]): return False                     # 50 当前在 89 上方
    if not (e50[t - 20] <= ma89[t - 20]): return False          # 约20日前仍在89下方（近期金叉）
    crossed = any(e50[i] > ma89[i] and e50[i - 1] <= ma89[i - 1]
                  for i in range(max(1, t - lookback), t + 1))  # 金叉在最近 lookback 根内
    if not crossed: return False
    slope89 = (ma89[t] - ma89[t - 20]) / ma89[t - 20] * 100
    if abs(slope89) > flat: return False                        # 89 走平
    if not (c[t] > e12[t]): return False                        # 价格已站上 12
    c20 = (c[t] / c[t - 20] - 1) * 100
    if not (c20lo <= c20 <= c20hi): return False                # 启动初期涨幅
    if vol:
        vr = vr_at(kk, t)
        if vr is None or vr < 1.2: return False
    return True

def row_of(code, t):
    kk = D.k[code]
    m = D.meta.get(code, {})
    c, e1, e2 = kk['c'][t], kk['e12'][t], kk['e50'][t]
    chg = (c / kk['c'][t - 1] - 1) * 100 if kk['c'][t - 1] else 0
    vr = vr_at(kk, t)
    return {'code': code, 'name': m.get('name', ''), 'close': round(c, 2),
            'chg': round(chg, 2), 'dev12': round((c - e1) / e1 * 100, 1),
            'spread': round((e1 - e2) / e2 * 100, 1),
            'c5': round((c / kk['c'][t - 5] - 1) * 100, 1) if t >= 5 else None,
            'c20': round((c / kk['c'][t - 20] - 1) * 100, 1),
            'c60': round((c / kk['c'][t - 60] - 1) * 100, 1),
            'vr': round(vr, 2) if vr else None,
            'mv': m.get('mv'), 'pe': m.get('pe'),
            'date': kk['dates'][t]}

def _tl_json(path, default=None):
    try:
        return json.load(open(path))
    except Exception:
        return default if default is not None else {'error': 'file missing: ' + path}


def tlpool_view():
    return _tl_json(os.path.join(WEB, 'tlpool.json'))


def tltrack_view(q):
    """三线穿阳全量跟踪看板。默认返回统计 + 最新每日快照 + 在跟踪信号（轻量）。
    view=signals 时可以按 status/date/code 过滤，最多 800 条。"""
    base = os.path.join(WEB, 'tltrack')
    stats = _tl_json(os.path.join(base, 'stats.json'), {})
    meta = _tl_json(os.path.join(base, 'meta.json'), {})
    master = _tl_json(os.path.join(base, 'signals.json'), {})
    sigs = master.get('signals') or []
    daily_dir = os.path.join(base, 'daily')
    days = sorted(os.path.basename(p)[:-5] for p in glob.glob(daily_dir + '/*.json'))
    want = q.get('date', [None])[0] or (days[-1] if days else None)
    snap = _tl_json(os.path.join(daily_dir, (want or '') + '.json'), {}) if want else {}
    out = {'as_of': master.get('as_of'), 'live_from': meta.get('live_from'),
           'min_track_days': meta.get('min_track_days'), 'stats': stats,
           'daily_dates': days, 'daily_date': want, 'daily': snap,
           'n_signals': len(sigs),
           'files': {'master': 'webapp/tltrack/signals.json',
                     'csv': 'webapp/tltrack/signals.csv',
                     'stats': 'webapp/tltrack/stats.json',
                     'daily': 'webapp/tltrack/daily/'}}
    view = q.get('view', [''])[0]
    if view == 'signals':
        status = q.get('status', [None])[0]
        date = q.get('signal_date', [None])[0]
        code = q.get('code', [None])[0]
        lim = min(int(float(q.get('limit', ['800'])[0])), 2000)
        rows = [s for s in sigs
                if (not status or s['status'] == status)
                and (not date or s['date'] == date)
                and (not code or s['code'] == code)]
        out['signals'] = rows[-lim:]
        out['n_matched'] = len(rows)
    else:
        out['tracking'] = [s for s in sigs if s['status'] == 'tracking'][-400:]
        out['latest_matured'] = [s for s in sigs if s['status'] == 'matured'][-60:]
    return out


def dragon_view(q):
    """龙头战法看板。默认返回当日龙头池；view=stats/signals/tracking 为分视图。"""
    base = os.path.join(WEB, 'dragon')
    view = q.get('view', [''])[0]
    if view == 'stats':
        return _tl_json(os.path.join(base, 'stats.json'), {})
    if view == 'tracking':
        trk = _tl_json(os.path.join(base, 'tracking.json'), {})
        trk['signals'] = [s for s in (trk.get('signals') or [])
                          if s.get('status') == 'tracking'][-400:]
        return trk
    if view == 'signals':
        import csv as _csv
        path = os.path.join(base, 'signals.csv')
        if not os.path.exists(path):
            return {'error': 'webapp/dragon/signals.csv missing'}
        date, code = q.get('date', [None])[0], q.get('code', [None])[0]
        phase, streak = q.get('phase', [None])[0], q.get('streak', [None])[0]
        lim = min(int(float(q.get('limit', ['500'])[0])), 2000)
        n, rows = 0, []
        with open(path, newline='') as fh:
            for r in _csv.DictReader(fh):
                if date and r['date'] != date:
                    continue
                if code and r['code'] != code:
                    continue
                if phase and r['phase'] != phase:
                    continue
                if streak and r['streak'] != streak:
                    continue
                n += 1
                rows.append(r)
                if len(rows) > lim:
                    rows.pop(0)
        return {'n_matched': n, 'signals': rows}
    out = dict(_tl_json(os.path.join(WEB, 'dragon.json'), {}))
    out.pop('stats', None)       # 全量 stats 走 view=stats，避免默认响应过大
    out['files'] = {'master': 'webapp/dragon.json',
                    'csv': 'webapp/dragon/signals.csv',
                    'stats': 'webapp/dragon/stats.json',
                    'tracking': 'webapp/dragon/tracking.json',
                    'daily': 'webapp/dragon/daily/'}
    return out


def divcross_view(q):
    """背离金叉 lead 候选看板。默认返回全部候选（领先位/已确认）；view=stats 返回聚合统计。
    数据由 tlpool.screen_divcross() 生成并落盘 webapp/divcross.json。"""
    base = os.path.join(WEB, 'divcross.json')
    data = _tl_json(base, {})
    if data.get('error'):
        return data
    view = q.get('view', [''])[0]
    if view == 'stats':
        from collections import Counter
        cands = data.get('candidates') or []
        stc = Counter(c['stage'] for c in cands)
        x50 = sum(1 for c in cands if c.get('cross50'))
        three = sum(1 for c in cands if c.get('is_three'))
        fresh3 = sum(1 for c in cands if c.get('fresh3'))
        return {'as_of': data.get('as_of'), 'regime': data.get('regime'), 'n': data.get('n'),
                'fresh_days': data.get('fresh_days'), 'n_fresh3': data.get('n_fresh3', fresh3),
                'stage_counts': dict(stc), 'cross50_count': x50, 'is_three_count': three,
                'files': {'master': 'webapp/divcross.json'}}
    data['files'] = {'master': 'webapp/divcross.json'}
    return data


def divtrack_view(q):
    """背离金叉·全量持续跟踪看板（≥30 日观察 + 回测）。数据由 divtrack.update() 生成。

    默认返回 stats 概览（信号数/成熟/跟踪中/各周期前向均值与超额 + 分桶）。
    也可读 webapp/divtrack/signals.json 拿全量主档。
    """
    import divtrack as _dt
    st = _tl_json(_dt.STATS, None)
    if st is None:
        return {'error': '尚未生成 divtrack 数据，请先运行 divtrack.py 或 daily_run.py', 'files': {'stats': 'webapp/divtrack/stats.json'}}
    st['files'] = {'stats': 'webapp/divtrack/stats.json',
                   'master': 'webapp/divtrack/signals.json',
                   'csv': 'webapp/divtrack/signals.csv'}
    return st


STRAT_NAMES = {'pre': '①爆发前潜伏', 'dk': '②登康式启动', 'never': '③从未破线',
               'dc': '④双穿阳线', 'm89': '⑤89金叉启动', 'cross': '⑥穿线阳'}


TLBREAK_CACHE = {}


def tlbreak_watch(shift=0):
    """盘中「三线穿阳 + 破20日高」候选监控清单（供 tlbreak_alert.py 轮询实时行情）。

    以最近一个收盘日为基准（EMA/MA89 今日盘中不变，用最近收盘值；D.lc 盘中自动
    剔除今日bar，因此盘中运行时基准=上一收盘日，正确）。shift=1 可回溯一日做验证。
    三线 = EMA12 / EMA50 / MA89；trigger = max(三线最高值, 近20日最高收盘)
    盘中实时价站上 trigger 且阳线实体吞没三线 → 「三线穿阳+破20日高」成形（预触发）。
    服务端预过滤（压缩行情轮询量）：
      1) 非ST / 有当日定价 / 上市>=90根bar
      2) 三线带宽 <= 12%（阳柱实体吞没三线才现实）
      3) trigger 在今日一个涨停价位内可达
    盘中判定（tlbreak_alert.py 用实时行情完成）：
      开盘价 <= 三线最低值 且 现价 > trigger 且 现价 > 开盘价（阳线进行中）。
    口径说明：盘中通知是「预触发」——今日收盘的 EMA 要收盘后才知道（此处用最近收盘
    值近似），收盘确认以三线池/日报口径为准。
    """
    now = time.time()
    c = TLBREAK_CACHE.get(shift)
    if c is not None and now - c['ts'] < 30:
        return c['data']
    out = []
    out_near = []
    n_pool = 0          # 守护静态预选池口径：仅看昨收距突破价≤5%（不要求站上三线最低值）
    as_of = None
    for code, kk in D.k.items():
        if code not in D.fresh:
            continue
        m = D.meta.get(code, {})
        if is_st(m.get('name')):
            continue
        t = D.lc(code) - shift
        if t < 90:
            continue
        e12, e50, m89 = kk['e12'][t], kk['e50'][t], kk['ma89'][t]
        lo, hi = min(e12, e50, m89), max(e12, e50, m89)
        if lo <= 0 or (hi - lo) / lo > 0.12:
            continue
        prev = kk['c'][t]
        if prev <= 0:
            continue
        hi20 = max(kk['c'][max(0, t - 19):t + 1])
        trig = max(hi, hi20)
        zt = prev * (1 + _zt_ratio(code, m.get('name', '')))
        if trig > zt - 0.001:
            continue          # 今日一个涨停都够不到 → 今天不可能触发
        s89 = (m89 / kk['ma89'][t - 20] - 1) * 100 if t >= 20 and kk['ma89'][t - 20] > 0 else 0.0
        if as_of is None:
            as_of = kk['dates'][t]
        out.append({'code': code, 'name': m.get('name', ''),
                    'lo': round(lo, 3), 'hi': round(hi, 3),
                    'trigger': round(trig, 2), 'hi20': round(hi20, 2),
                    'prev_close': prev, 's89': round(s89, 2)})
        # 「即将穿三线」子池：收盘已贴近突破价（≤5%），且已站上三线最低值——
        # 次日/盘中一根阳线即可完成实体吞没三线+破20日高。dist>0 表示尚未突破。
        dist = round(trig, 2) / prev - 1      # 与 tlbreak_pool 用同一口径（trigger 已保留2位）
        if 0 < dist <= 0.05:
            n_pool += 1
        if 0 < dist <= 0.05 and prev >= lo:
            above = sum(1 for v in (e12, e50, m89) if prev >= v)
            out_near.append({'code': code, 'name': m.get('name', ''),
                             'close': round(prev, 2), 'trigger': round(trig, 2),
                             'dist': round(dist * 100, 2), 'lo': round(lo, 2),
                             'hi': round(hi, 2),
                             'band': round((hi - lo) / lo * 100, 1),
                             's89': round(s89, 2), 'above': above})
    out.sort(key=lambda r: r['code'])
    out_near.sort(key=lambda r: r['dist'])
    data = {'date': as_of, 'n': len(out), 'shift': shift, 'watch': out,
            'near': out_near[:80], 'n_near': len(out_near), 'n_pool': n_pool,
            'note': '盘中预触发口径：EMA用最近收盘值；收盘确认以三线池/日报为准'}
    TLBREAK_CACHE[shift] = {'ts': now, 'data': data}
    return data


def tlbreak_pool(top=0, dist_pct=5.0):
    """盘中守护专用「静态预选池」：全部基于日K，top=0 表示不截断取全部。

    唯一预选判据：昨收距突破价 dist = trigger/prev_close - 1 ≤ dist_pct%。
    不要求昨收已站上三线最低值（昨收在三线下方的票正是「一根阳线吞没三线」
    的典型形态），也不截断到80只、不依赖任何盘中实时行情。
    守护进程每 CYCLE 秒只轮询本池；池子每15分钟从本地 API 刷新一次即可
    （日K基准盘中不变），彻底不再扫全市场。
    dist_pct 可调：5%=834只，8%=1307只，10%=1613只（越大越接近全市场）。
    """
    d = tlbreak_watch(0)
    lim = dist_pct / 100.0
    rows = []
    for w in d['watch']:
        prev = w['prev_close']
        dist = w['trigger'] / prev - 1 if prev > 0 else 9
        if 0 < dist <= lim:
            r = dict(w)
            r['dist'] = round(dist * 100, 2)
            rows.append(r)
    rows.sort(key=lambda r: r['dist'])
    sel = rows[:top] if top else rows
    return {'date': d['date'], 'n_all': len(rows), 'dist_pct': dist_pct,
            'n': len(sel), 'pool': sel,
            'note': f'静态预选池：昨收距突破价≤{dist_pct:g}%，共 {len(sel)} 只；'
                    '盘中守护只轮询本池（不扫全市场）'}


def crossline_pool():
    """⑥穿线阳 盘中提醒专用「全市场预选池」（昨收基准线位，纯本地计算）。

    用户口径 2026-11-10：与三线穿阳·破20日高提醒并行的第二条链——
      穿线阳 = 当日阳柱实体包住 EXPMA12/EXPMA50；MA89 也在实体内 = 三线（更强，优先）；
      不设量比 / 破20日高 / 涨幅门槛（与日报 sig_cross/cross_lines 同一口径）。
    关键：日报口径用的是**含当日收盘的 EMA**。高开动量票（如 2026-11-10 海昌新材
    开 17.88 > 昨收EMA12=17.45）只有把当日涨幅算进 EMA 才判得出「实体包住」。
    因此盘中判定不能用昨收固定线位，而是「若以现价 px 收盘」的 EMA 递推估计：
      e12e = e12 + k12*(px - e12)、e50e = e50 + k50*(px - e50)、m89e = m89 + (px - c89)/89
    （e12/e50/m89 为昨收值，c89 = 89 日前收盘）。px=收盘价时与日报 cross_lines 严格相等，
    盘中即预触发。形态判定（依赖盘中开盘价与现价）由 tlbreak_alert.py 完成；
    守护用「涨停价下仍无法成形」做结构性剔除，第一轮全市场扫描后轮询量自动收敛。
    """
    now = time.time()
    c = TLBREAK_CACHE.get('crossline')
    if c is not None and now - c['ts'] < 30:
        return c['data']
    out = []
    as_of = None
    for code, kk in D.k.items():
        if code not in D.fresh:
            continue
        if is_st(D.meta.get(code, {}).get('name')):
            continue
        t = D.lc(code)
        if t < 96:
            continue
        e12, e50, m89 = kk['e12'][t], kk['e50'][t], kk['ma89'][t]   # 盘中 t=昨收（D.lc 剔除今日bar）
        prev = kk['c'][t]
        c89 = kk['c'][t - 89] if t >= 89 else 0.0
        if e12 <= 0 or e50 <= 0 or prev <= 0 or c89 <= 0:
            continue
        if as_of is None:
            as_of = kk['dates'][t]
        s89 = (m89 / kk['ma89'][t - 20] - 1) * 100 if t >= 20 and kk['ma89'][t - 20] > 0 else 0.0
        out.append({'code': code, 'name': D.meta.get(code, {}).get('name', ''),
                    'e12': round(e12, 4), 'e50': round(e50, 4), 'm89': round(m89, 4),
                    'c89': round(c89, 4), 'prev_close': prev, 's89': round(s89, 2)})
    data = {'date': as_of, 'n': len(out), 'pool': out,
            'note': '穿线阳提醒预选池：全市场昨收线位；盘中用「现价收盘」EMA估计判定实体吞没，'
                    '收盘时与日报⑥口径一致；不限涨幅/量比/破20日高'}
    TLBREAK_CACHE['crossline'] = {'ts': now, 'data': data}
    return data


def tlbreak_fired():
    """今日盘中「查询快照命中」的提醒记录（供页面标记，数据源 tlbreak_alerts.json）。

    tlbreak_alert.py 每 60 秒查一次预选池；那一帧快照里当日最高越过触发价即弹窗，
    每股每日一条。mode=live 红色（实时站上）、mode=touch 琥珀（曾触及·已回落，
    非买入点）。s89>0=MA89上行=A级特征，否则 B级（追高陷阱特征）。
    """
    today = datetime.date.today().isoformat()
    try:
        recs = json.load(open(os.path.join(WEB, 'tlbreak_alerts.json')))
    except Exception:
        recs = []
    out = []
    for r in recs:
        if r.get('date') != today:
            continue
        s89 = r.get('s89') or 0
        out.append({'code': r.get('code'), 'name': r.get('name'),
                    'time': r.get('time'), 'price': r.get('price'),
                    'high': r.get('high'), 'trigger': r.get('trigger'), 's89': s89,
                    'mode': r.get('mode') or 'live',
                    'grade': 'A' if s89 > 0 else 'B'})
    out.sort(key=lambda x: (x['grade'] != 'A', x.get('time') or ''))
    return {'date': today, 'n': len(out),
            'n_live': sum(1 for x in out if x['mode'] == 'live'),
            'n_touch': sum(1 for x in out if x['mode'] == 'touch'),
            'fired': out, 'codes': [x['code'] for x in out]}


# ---------- 提醒记录库（真实发过的提醒 + 日K历史回填，供筛选与回测） ----------
ALERT_HIST = os.path.join(WEB, 'tlbreak_alert_history.jsonl')      # 真实提醒永久档案
ALERT_BACKFILL = os.path.join(WEB, 'tlbreak_alert_backfill.jsonl')  # 日K近似回放样本
ALERT_FWD_KEYS = ('close', 'd1', 'd3', 'd5', 'd10', 'max_up', 'max_down')
_DATE_IDX = {}


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _alert_rows():
    """三处来源合并去重：永久档案(真实) > 回填样本 > 滚动窗口(兼容尚未归档的真实记录)。"""
    rows = _read_jsonl(ALERT_HIST) + _read_jsonl(ALERT_BACKFILL)
    try:
        rows += json.load(open(os.path.join(WEB, 'tlbreak_alerts.json')))
    except Exception:
        pass
    seen = {}
    for r in rows:
        r.setdefault('source', 'live')
        # live=实时弹窗 / touch=盘中曾触及但已回落(只留档) / backfill=日K回放
        r.setdefault('mode', 'backfill' if r.get('source') == 'backfill' else 'live')
        k = r.get('key') or '%s|%s|%s' % (r.get('date'), r.get('code'), r.get('source'))
        if k not in seen:
            seen[k] = r
    out = list(seen.values())
    out.sort(key=lambda r: (r.get('date', ''), r.get('time', '')), reverse=True)
    return out


def _fwd(code, date):
    """前向收益：以触发日收盘为基准，算 1/3/5/10 日收益与后10日最大涨/跌。

    口径与 tltrack/signals.csv 一致（收盘买入）；当天尚未收盘或无未来 bar 时为空。
    """
    kk = D.k.get(code)
    if not kk:
        return {}
    if code not in _DATE_IDX:
        _DATE_IDX[code] = {d: i for i, d in enumerate(kk['dates'])}
    t = _DATE_IDX[code].get(date)
    if t is None:
        return {}                      # 盘中今日记录：日K还没这根 bar
    c, h, l = kk['c'], kk['h'], kk['l']
    n = len(c)
    if c[t] <= 0:
        return {}
    base = c[t]
    out = {'close': round(base, 2)}
    for k in (1, 3, 5, 10):
        if t + k < n:
            out['d%d' % k] = round((c[t + k] / base - 1) * 100, 2)
    if t + 1 < n:
        e = min(n, t + 11)
        out['max_up'] = round((max(h[t + 1:e]) / base - 1) * 100, 2)
        out['max_down'] = round((min(l[t + 1:e]) / base - 1) * 100, 2)
    return out


def _alert_stats(rows):
    def one(rs):
        s = {'n': len(rs), 'A': sum(1 for r in rs if r['grade'] == 'A'),
             'B': sum(1 for r in rs if r['grade'] == 'B')}
        for k in ALERT_FWD_KEYS[1:]:
            vals = [r[k] for r in rs if r.get(k) is not None]
            s[k] = {'n': len(vals),
                    'avg': round(sum(vals) / len(vals), 2) if vals else None,
                    'win': round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1)
                           if vals and k.startswith('d') else None}
        return s
    return {'all': one(rows),
            'A': one([r for r in rows if r['grade'] == 'A']),
            'B': one([r for r in rows if r['grade'] == 'B']),
            'live': one([r for r in rows if r['source'] == 'live']),
            'backfill': one([r for r in rows if r['source'] == 'backfill']),
            'touch': one([r for r in rows if r.get('mode') == 'touch']),
            'date_min': min((r['date'] for r in rows), default=''),
            'date_max': max((r['date'] for r in rows), default='')}


def alert_records(q, limit=3000):
    """筛选 + 前向收益 + 分组统计。参数：from/to/grade/source/q/limit。"""
    d0 = (q.get('from', [''])[0] or '').strip()
    d1 = (q.get('to', [''])[0] or '').strip()
    grade = (q.get('grade', [''])[0] or '').strip().upper()
    source = (q.get('source', [''])[0] or '').strip()
    kw = (q.get('q', [''])[0] or '').strip().lower()
    try:
        limit = max(1, min(100000, int(q.get('limit', [limit])[0])))
    except Exception:
        pass
    sel = []
    for r in _alert_rows():
        date = r.get('date', '')
        if d0 and date < d0:
            continue
        if d1 and date > d1:
            continue
        if source and r.get('source') != source:
            continue
        s89 = r.get('s89') or 0
        g = 'A' if s89 > 0 else 'B'
        if grade and g != grade:
            continue
        if kw and kw not in (r.get('code', '') + r.get('name', '')).lower():
            continue
        rr = dict(r)
        rr['grade'] = g
        for k in ALERT_FWD_KEYS:
            rr[k] = None
        rr.update(_fwd(r.get('code'), date))
        sel.append(rr)
    return {'total': len(sel), 'shown': min(limit, len(sel)), 'rows': sel[:limit],
            'stats': _alert_stats(sel),
            'filters': {'from': d0, 'to': d1, 'grade': grade, 'source': source, 'q': kw},
            'note': '前向收益以触发日收盘为基准；真实提醒(live)为盘中预触发，收盘确认以三线池为准；'
                    'backfill 为日K近似回放，不是真实发过的提醒。'}


ALERT_CSV_COLS = [
    ('date', '日期'), ('time', '时间'), ('code', '代码'), ('name', '名称'),
    ('source', '来源'), ('mode', '触发方式'), ('grade', '级别'), ('price', '触发价/收盘近似'),
    ('trigger', '突破价'), ('s89', 'MA89斜率%'), ('close', '触发日收盘'),
    ('d1', '1日%'), ('d3', '3日%'), ('d5', '5日%'), ('d10', '10日%'),
    ('max_up', '后10日最大涨%'), ('max_down', '后10日最大跌%'), ('msg', '提醒内容'),
]


def alert_records_csv(q):
    data = alert_records(q, limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[1] for c in ALERT_CSV_COLS])
    for r in data['rows']:
        w.writerow(['' if r.get(k) is None else r.get(k) for k, _ in ALERT_CSV_COLS])
    return ('\ufeff' + buf.getvalue()).encode('utf-8')   # BOM 便于 Excel 打开中文


def anomaly_view():
    """异动前瞻（p12）：直接读收盘跑批生成的 anomaly.json，失败时给明确提示。"""
    try:
        return json.load(open(os.path.join(WEB, 'anomaly.json'), encoding='utf-8'))
    except Exception as e:
        return {'error': '异动前瞻数据未就绪，请运行 daily_run.py 或 webapp/anomaly_scan.py：%s' % e}


def locate_view(code):
    """全局定位：一只股票当前命中哪些策略/池子（供前端搜索框下方的归属标签栏）。

    汇总口径（全部为最近收盘日快照）：
      1) 当日六策略命中（sig_pre/dk/never/dc/m89 + 穿线阳含线数）
      2) 观察池（pool.json open，在跟策略与入池日/浮动盈亏）
      3) 三线池（tlpool.json：Top5 / 观察区 / 确认候选，含评级评分）
      4) 龙头战法（dragon.json：候选分级 / 龙头池Top8 / 今日陷阱）
      5) 背离金叉 lead（divcross.json：评分 / 阶段 / 交叉天数）
      6) 沪深300双线向上池（hs300_pool.json members）
      7) 涨停：近20个交易日涨停家数 + 当前连板数
    """
    if code not in D.k:
        return {'error': 'not found'}
    kk = D.k[code]
    t = D.lc(code)
    meta = D.meta.get(code, {})
    out = {
        'code': code, 'name': meta.get('name', ''),
        'close': kk['c'][t], 'date': kk['dates'][t],
        'chg': round((kk['c'][t] / kk['c'][t - 1] - 1) * 100, 2) if t >= 1 and kk['c'][t - 1] else None,
        'mv': meta.get('mv'), 'pe': meta.get('pe'),
        'stale': code not in D.fresh,
        'regime': D.regime.get(kk['dates'][t]),
    }
    # ---- 1) 当日六策略命中
    sigs = []
    if code in D.fresh and t >= 65:
        try:
            if sig_pre(kk, t):
                sigs.append({'key': 'pre', 'name': '① 爆发前潜伏'})
            if sig_dk(kk, t):
                sigs.append({'key': 'dk', 'name': '② 登康式启动'})
            if sig_never(kk, t):
                sigs.append({'key': 'never', 'name': '③ 从未破线'})
            if sig_dc(kk, t):
                sigs.append({'key': 'dc', 'name': '④ 双穿阳线'})
            if sig_m89(kk, t):
                sigs.append({'key': 'm89', 'name': '⑤ 89金叉启动'})
            ln = cross_lines(kk, t)
            if ln >= 2:
                sigs.append({'key': 'cross', 'lines': ln,
                             'name': '⑥ 三线穿阳' if ln == 3 else '⑥ 穿线阳'})
        except Exception:
            pass
    out['signals'] = sigs
    pools = []
    # ---- 2) 观察池
    try:
        p = json.load(open(os.path.join(WEB, 'pool.json')))
        rows = [e for e in p.get('open', []) if e.get('code') == code]
        if rows:
            names = ' + '.join(STRAT_NAMES.get(e.get('strategy'), str(e.get('strategy'))) for e in rows)
            e0 = rows[-1]
            pnl = ''
            if e0.get('entry_price'):
                pnl = ' · %.1f%%' % ((kk['c'][t] / e0['entry_price'] - 1) * 100)
            pools.append({'tab': 'p4', 'label': '观察池',
                          'detail': '%s · %s入池%s' % (names, e0.get('entry_date'), pnl)})
    except Exception:
        pass
    # ---- 3) 三线池
    try:
        tp = json.load(open(os.path.join(WEB, 'tlpool.json')))
        hit = next((e for e in tp.get('pool', []) if e.get('code') == code), None)
        if hit:
            pools.append({'tab': 'p8', 'label': '三线池·Top5',
                          'detail': '#%d %s级 %.1f分' % (hit.get('rank', 0), hit.get('grade', '?'), hit.get('score', 0))})
        else:
            hit = next((e for e in tp.get('watch', []) if e.get('code') == code), None)
            if hit:
                pools.append({'tab': 'p8', 'label': '三线池·观察区',
                              'detail': '#%d %.1f分 · 未破20日高' % (hit.get('rank', 0), hit.get('score', 0))})
            else:
                hit = next((e for e in tp.get('candidates', []) if e.get('code') == code), None)
                if hit:
                    pools.append({'tab': 'p8', 'label': '三线池·候选',
                                  'detail': '%s级 %.1f分' % (hit.get('grade', '?'), hit.get('score', 0))})
    except Exception:
        pass
    # ---- 4) 龙头战法
    try:
        dg = json.load(open(os.path.join(WEB, 'dragon.json')))
        hit = next((e for e in dg.get('candidates', []) if e.get('code') == code), None)
        if hit:
            det = '%s级 %.1f分 · %d板 %s' % (hit.get('grade', '?'), hit.get('score', 0),
                                             hit.get('streak', 0), hit.get('stage', ''))
            in_pool = any(e.get('code') == code for e in dg.get('pool', []))
            in_trap = any(e.get('code') == code for e in dg.get('traps_today', []))
            pools.append({'tab': 'p9', 'label': '龙头战法' + ('·Top8' if in_pool else '·候选'),
                          'detail': det + (' · ⚠️今日陷阱' if in_trap else '')})
        else:
            hit = next((e for e in dg.get('shouyin', []) if e.get('code') == code), None)
            if hit:
                pools.append({'tab': 'p9', 'label': '龙头战法·断板首阴', 'detail': hit.get('note', '')})
    except Exception:
        pass
    # ---- 5) 背离金叉 lead
    try:
        dc_ = json.load(open(os.path.join(WEB, 'divcross.json')))
        hit = next((e for e in dc_.get('candidates', []) if e.get('code') == code), None)
        if hit:
            pools.append({'tab': 'p10', 'label': '背离金叉',
                          'detail': '%.1f分 · %s · 交叉%s日%s' % (
                              hit.get('score', 0), hit.get('stage', ''),
                              hit.get('days_since_cross', '?'),
                              ' · 新信号' if hit.get('fresh3') else '')})
    except Exception:
        pass
    # ---- 6) 沪深300双线向上池
    try:
        h3 = json.load(open(os.path.join(WEB, 'hs300_pool.json')))
        mem = h3.get('members', {})
        if isinstance(mem, dict) and code in mem:
            e = mem[code] if isinstance(mem[code], dict) else {}
            pools.append({'tab': 'p7', 'label': '沪深300池',
                          'detail': '%s入选 · 双线向上' % (e.get('since', ''))})
    except Exception:
        pass
    # ---- 7) 涨停统计（近20交易日 + 当前连板）
    try:
        ratio = _zt_ratio(code, meta.get('name', ''))
        cal20 = set(D.idx_days[-20:]) if D.idx_days else set()
        n20 = streak = 0
        for i in range(1, t + 1):
            prev = kk['c'][i - 1]
            if prev <= 0:
                streak = 0
                continue
            if kk['c'][i] >= round(prev * (1 + ratio), 2) - 0.001:
                if kk['dates'][i] in cal20:
                    n20 += 1
                streak += 1
            else:
                streak = 0
        if n20 or streak >= 1:
            det = '20日涨停%d次' % n20 if n20 else ''
            if streak >= 2:
                det += (' · ' if det else '') + '现连板%d板' % streak
            elif streak == 1:
                det += (' · ' if det else '') + '今日涨停'
            if det:
                pools.append({'tab': 'p5', 'label': '涨停复盘', 'detail': det})
    except Exception:
        pass
    out['pools'] = pools
    # ---- 8) 今日盘中已真实穿阳（预触发提醒记录，全局搜索里做醒目标记）
    try:
        fr = next((e for e in tlbreak_fired()['fired'] if e.get('code') == code), None)
        if fr:
            out['fired'] = fr
    except Exception:
        pass
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass          # 浏览器/请求提前断开，不影响服务，别刷堆栈

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/':
            b = open(WEB + '/index.html', 'rb').read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            try:
                self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif u.path == '/api/stocks':
            out = []
            for code in D.k:
                m = D.meta.get(code, {})
                out.append({'code': code, 'name': m.get('name', ''), 'mv': m.get('mv'), 'pe': m.get('pe')})
            self._json(out)
        elif u.path == '/api/tlbreak_watch':
            self._json(tlbreak_watch(int(float(q.get('shift', ['0'])[0]))))
        elif u.path == '/api/tlbreak_pool':
            try:
                raw = int(q.get('top', ['0'])[0])
            except ValueError:
                raw = 0
            try:
                dp = float(q.get('dist', ['5'])[0])
            except ValueError:
                dp = 5.0
            self._json(tlbreak_pool(0 if raw <= 0 else min(1000, raw),
                                    min(20.0, max(0.5, dp))))
        elif u.path == '/api/crossline_pool':
            self._json(crossline_pool())
        elif u.path == '/api/tlbreak_fired':
            self._json(tlbreak_fired())
        elif u.path == '/api/anomaly':
            self._json(anomaly_view())
        elif u.path == '/api/alert_records':
            self._json(alert_records(q))
        elif u.path == '/api/alert_records.csv':
            b = alert_records_csv(q)
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="alert_records.csv"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == '/api/locate':
            code = q.get('code', [''])[0].strip().lower()
            if re.match(r'^\d{6}$', code):
                code = ('sh' if code[0] == '6' else 'sz') + code
            self._json(locate_view(code) if code else {'error': 'need code'})
        elif u.path == '/api/kline':
            code = q.get('code', [''])[0]
            if code not in D.k:
                return self._json({'error': 'not found'})
            kk = D.k[code]
            lc = D.lc(code)
            sigs = {'pre': [], 'dk': [], 'never': [], 'dc': [], 'm89': [], 'cross3': []}
            for t in range(65, lc + 1):
                d = kk['dates'][t]
                if sig_pre(kk, t): sigs['pre'].append(d)
                if sig_dk(kk, t): sigs['dk'].append(d)
                if sig_never(kk, t): sigs['never'].append(d)
                if sig_dc(kk, t): sigs['dc'].append(d)
                if sig_m89(kk, t): sigs['m89'].append(d)
                if cross_lines(kk, t) == 3: sigs['cross3'].append(d)   # 三线穿阳（金★）
            # 涨停标注（近60根bar）+ 回踩参考位（最近涨停日最低价，限近20个交易日内）
            _ratio = _zt_ratio(code, D.meta.get(code, {}).get('name', ''))
            _zt_dates, _pb = [], None
            _cal20 = set(D.idx_days[-20:]) if D.idx_days else set()
            # 卖出信号（统一口径 2026-11-04）：收盘跌破 EMA50 当天算第1日，
            # 连续3个交易日未收回到 EMA50 上方 = 破位，才产生卖出信号。
            # 破EMA12 / 破MA89 不再作为卖出信号（太严格）；炸板、高位长上影降级为「形态提示」。
            sells = {'brk50': [], 'warn50': []}
            patterns = {'kaiban': [], 'shadow': []}
            # 连续计数从全历史起算，避免近60根窗口把「第3日」截断漏标
            _b50 = 0
            for t in range(1, lc + 1):
                _b50 = _b50 + 1 if kk['c'][t] < kk['e50'][t] else 0
                if _b50 == 3:
                    sells['brk50'].append(kk['dates'][t])
                elif 0 < _b50 < 3:
                    sells['warn50'].append(kk['dates'][t])
            _win = set(kk['dates'][max(0, lc - 60):lc + 1])
            for _k in sells:
                sells[_k] = [d for d in sells[_k] if d in _win]
            for t in range(max(1, lc - 60), lc + 1):
                prev = kk['c'][t - 1]
                _zt_px = round(prev * (1 + _ratio), 2)
                d = kk['dates'][t]
                if prev > 0 and kk['c'][t] >= _zt_px - 0.001:
                    _zt_dates.append(d)
                    if d in _cal20:
                        _pb = kk['l'][t]  # 越近的涨停越后写入，最终留下最近一次
                # 形态提示：炸板（盘中最高触板但收盘未封住）
                if prev > 0 and kk['h'][t] >= _zt_px - 0.001 and kk['c'][t] < _zt_px - 0.001:
                    patterns['kaiban'].append(d)
                # 形态提示：高位放量长上影（20日涨幅>15% + 上影>2倍实体 + 量比>1.5）
                if t >= 20 and t >= 5:
                    body = abs(kk['c'][t] - kk['o'][t])
                    upsh = kk['h'][t] - max(kk['c'][t], kk['o'][t])
                    vavg = sum(kk['v'][t - 5:t]) / 5.0
                    if (kk['c'][t - 20] > 0 and (kk['c'][t] / kk['c'][t - 20] - 1) > 0.15
                            and body > 0 and upsh > 2 * body and vavg > 0 and kk['v'][t] > 1.5 * vavg):
                        patterns['shadow'].append(d)
            self._json({'code': code, 'name': D.meta.get(code, {}).get('name', ''),
                        'dates': kk['dates'], 'o': kk['o'], 'c': kk['c'], 'h': kk['h'], 'l': kk['l'],
                        'v': kk['v'], 'e12': [round(x, 3) for x in kk['e12']],
                        'e50': [round(x, 3) for x in kk['e50']],
                        'ma89': [round(x, 3) for x in kk['ma89']],
                        'last_closed': lc, 'today': D.today, 'signals': sigs,
                        'sells': sells, 'patterns': patterns,
                        'zt_dates': _zt_dates, 'pb': round(_pb, 2) if _pb else None,
                        'regime': D.regime.get(kk['dates'][lc])})
        elif u.path == '/api/screen':
            strategy = q.get('strategy', ['pre'])[0]
            dev = float(q.get('dev', ['6'])[0]); spread = float(q.get('spread', ['8'])[0])
            c20lo = float(q.get('c20lo', ['5'])[0]); c20hi = float(q.get('c20hi', ['20'])[0])
            c60hi = float(q.get('c60hi', ['20'])[0]); vrmin = float(q.get('vr', ['1.2'])[0])
            mvmax = float(q.get('mvmax', ['100'])[0])
            fresh = q.get('fresh', [None])[0]
            # ⑥穿线阳：lines=3 只看三线穿阳（默认2=双线+三线全量）；brk20=1 叠加破20日高
            _lines_min = int(float(q.get('lines', ['2'])[0]))
            _brk20 = q.get('brk20', ['0'])[0] in ('1', 'true', 'True')
            # pe=1 强制 PE>0；pe=0 允许亏损股；不传则跟随 ALLOW_LOSS（当前=允许）
            _peq = q.get('pe', [None])[0]
            _allow_loss = ALLOW_LOSS if _peq is None else (_peq not in ('1', 'true', 'True'))

            def _pe(m):
                return _allow_loss or (m.get('pe', 0) or 0) > 0
            fresh_n = int(float(fresh)) if fresh else None
            rows = []
            for code, kk in D.k.items():
                if code not in D.fresh:
                    continue   # 停牌/最后 bar 非基准交易日：无当日定价，不产生信号
                t = D.lc(code)
                if t < 65: continue
                m = D.meta.get(code, {})
                if is_st(m.get('name')):
                    continue   # ST/*ST/退市：不产生任何策略信号
                if strategy == 'pre':
                    ok = sig_pre(kk, t, dev, spread, c20lo, c20hi, c60hi, vrmin) and m.get('mv', 999) <= mvmax and _pe(m)
                elif strategy == 'dk':
                    ok = sig_dk(kk, t) and m.get('mv', 999) <= mvmax and _pe(m)
                elif strategy == 'never':
                    ok = sig_never(kk, t) and m.get('mv', 999) <= mvmax and _pe(m)
                elif strategy == 'dc':
                    ok = sig_dc(kk, t) and m.get('mv', 999) <= mvmax and _pe(m)
                elif strategy == 'cross':
                    ok = sig_cross(kk, t, min_lines=_lines_min, brk20=_brk20) \
                         and m.get('mv', 999) <= mvmax and _pe(m)
                elif strategy == 'w80':
                    # ⑦ 80%胜率规则：三线穿阳(阳线实体包住 EXPMA12/50+MA89)
                    #   + A级（收盘破20日高 + MA89十日斜率>0）；此处只筛「信号日」，
                    #   买点确认（收盘>信号日高 且 量比>=1.2）在后续20日扫描，见 p13 /api/win80
                    m89 = kk.get('ma89') or []
                    hi20 = max(kk['c'][t - 20:t]) if t >= 20 else 0
                    slope = (m89[t] / m89[t - 10] - 1) * 100 if t >= 10 and m89[t - 10] else 0
                    ok = (cross_lines(kk, t) == 3 and kk['c'][t] > hi20 and slope > 0
                          and m.get('mv', 999) <= mvmax and _pe(m))
                elif strategy == 'm89':
                    ok = (sig_m89(kk, t, lookback=fresh_n) if fresh_n else sig_m89(kk, t)) \
                         and m.get('mv', 999) <= mvmax and _pe(m)
                else:
                    ok = False
                if ok:
                    r = row_of(code, t)
                    if strategy == 'cross':
                        r['lines'] = cross_lines(kk, t)
                    if strategy == 'w80':
                        m89 = kk.get('ma89') or []
                        r['lines'] = 3
                        r['w80_conf'] = round(kk['h'][t], 2)                       # 突破确认价=信号日最高价
                        r['w80_need'] = round((kk['h'][t] / kk['c'][t] - 1) * 100, 1)  # 还需涨%
                        r['w80_slope'] = round((m89[t] / m89[t - 10] - 1) * 100, 2)
                        r['w80_state'] = '今日新信号·待确认'
                    rows.append(r)
            if strategy == 'cross':
                rows.sort(key=lambda r: (-(r.get('lines') or 0), -(r['vr'] or 0)))
            elif strategy == 'w80':
                rows.sort(key=lambda r: r.get('w80_need', 999))   # 距突破确认最近的在前
            else:
                rows.sort(key=lambda r: -(r['vr'] or 0))
            self._json({'strategy': strategy, 'date': D.px_date,
                        'regime': D.regime.get(D.px_date),
                        'count': len(rows),
                        'lines3': sum(1 for r in rows if r.get('lines') == 3),
                        'rows': rows[:200]})
        elif u.path == '/api/zt':
            q = parse_qs(u.query)
            days = int(float(q.get('days', ['20'])[0]))
            self._json(zt_pool(days))
        elif u.path == '/api/trades':
            self._json(trades_view())
        elif u.path == '/api/risk_rules':
            self._json(_load_risk_rules())
        elif u.path == '/api/trade':
            self._json(trade_op(q))
        elif u.path == '/api/quote':
            self._json(rt_quote(q.get('code', [''])[0]) or {'error': 'quote failed'})
        elif u.path == '/api/picks':
            self._json(picks_view())
        elif u.path == '/api/hs300':
            self._json(hs300_view())
        elif u.path == '/api/hs300_profit':
            self._json(_tl_json(os.path.join(WEB, 'hs300_profit.json'),
                                {'error': 'webapp/hs300_profit.json missing：请先跑 daily_run.py'}))
        elif u.path == '/api/firstcross':
            # 沪深300 · EXPMA12 首次上穿 EXPMA50（转势首叉）：收盘跑批产物，前端自行按冷静期/持有期重算
            self._json(_tl_json(os.path.join(WEB, 'firstcross.json'),
                                {'error': 'webapp/firstcross.json missing：请先跑 daily_run.py'}))
        elif u.path == '/api/lizhuang':
            # 立桩量：公开公式复现 + 三关前向验证，描述性研究产物，不接交易链路
            self._json(_tl_json(os.path.join(WEB, 'lizhuang.json'),
                                {'error': 'webapp/lizhuang.json missing：请先跑 daily_run.py'}))
        elif u.path == '/api/lizhuang_signals.csv':
            p = os.path.join(WEB, 'lizhuang', 'signals.csv')
            if os.path.exists(p):
                data = open(p, 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({'error': 'signals.csv missing'})
        elif u.path == '/api/emotion':
            q = parse_qs(u.query)
            ndays = int(float(q.get('days', ['30'])[0]))
            self._json(emotion_view(ndays))
        elif u.path == '/api/alerts':
            try:
                alerts = json.load(open(os.path.join(WEB, 'alerts.json')))
            except Exception:
                alerts = []
            self._json({'today': D.today, 'alerts': alerts[-50:]})
        elif u.path == '/api/pool':
            import pool as POOL
            p = POOL.load()
            dd = D.idx_days[-1]
            reg = D.regime.get(dd, 'down')
            open_rows = []
            for e in p['open']:
                code = e['code']
                if code not in D.k:
                    continue
                kk = D.k[code]
                t = D.lc(code)
                ti = kk['dates'].index(e['entry_date']) if e['entry_date'] in kk['dates'] else None
                open_rows.append(dict(
                    e, last_date=kk['dates'][t], last_price=kk['c'][t],
                    pnl=round((kk['c'][t] / e['entry_price'] - 1) * 100, 2),
                    days=(t - ti) if ti is not None else None,
                    dist_e12=round((kk['c'][t] / kk['e12'][t] - 1) * 100, 1)))
            # 基准：等权小盘指数自池子首笔入池日起的累计收益
            base = None
            all_e = p['open'] + p['closed']
            if all_e:
                start = min(e['entry_date'] for e in all_e)
                si = next((i for i, d in enumerate(D.idx_days) if d >= start), None)
                if si is not None:
                    b = 1.0
                    for r in D.idx_rets[si:]:
                        b *= 1 + r
                    base = round((b - 1) * 100, 2)
            self._json({'data_date': dd, 'regime': reg, 'open': open_rows,
                        'closed': p['closed'][-80:], 'stats': POOL.stats(p),
                        'baseline_pct': base})
        elif u.path == '/api/indices':
            out = []
            for code, s in D.indices.items():
                t = len(s['c']) - 1
                chg = (s['c'][t] / s['c'][t - 1] - 1) * 100 if s['c'][t - 1] else 0
                cross_up = [s['dates'][i] for i in range(1, t + 1)
                            if s['c'][i - 1] < s['e12'][i - 1] and s['c'][i] > s['e12'][i]][-3:]
                # 模式B（EXPMA12 在 EXPMA50 上方）的最近站上/跌破日
                cross_gold = [s['dates'][i] for i in range(1, t + 1)
                              if s['e12'][i - 1] <= s['e50'][i - 1] and s['e12'][i] > s['e50'][i]][-3:]
                dead = []
                for i in range(1, t - 3):
                    if s['e12'][i - 1] >= s['e50'][i - 1] and s['e12'][i] < s['e50'][i]:
                        ok = any(s['c'][j] > s['e12'][j] and s['c'][j] > s['e50'][j]
                                 for j in range(i + 1, min(i + 4, t + 1)))
                        dead.append({'date': s['dates'][i], 'recovered': ok})
                s12 = (s['e12'][t] / s['e12'][t - 5] - 1) * 100 if t >= 5 else 0.0
                s50 = (s['e50'][t] / s['e50'][t - 10] - 1) * 100 if t >= 10 else 0.0
                dual_up = s12 > 0 and s50 > 0
                golden = s['e12'][t] > s['e50'][t]
                oper = ('可操作' if (s['c'][t] > s['e12'][t] > s['e50'][t] and dual_up)
                        else '观察' if s['c'][t] > s['e12'][t] else '回避')
                # 模式B的可操作判定：e12>e50 且 e12 五日向上（趋势确立），否则观察/回避
                oper_b = ('可操作' if (golden and s12 > 0)
                          else '观察' if golden else '回避')
                out.append({'code': code, 'name': s['name'], 'date': s['dates'][t],
                            'close': round(s['c'][t], 2), 'chg': round(chg, 2),
                            'above12': s['c'][t] > s['e12'][t],
                            'golden': golden,
                            'e12': round(s['e12'][t], 2), 'e50': round(s['e50'][t], 2),
                            's12': round(s12, 2), 's50': round(s50, 2), 'dual_up': dual_up,
                            'oper': oper, 'oper_b': oper_b,
                            'spark': [round(v, 2) for v in s['c'][max(0, t - 59):t + 1]],
                            'cross_up': cross_up, 'cross_gold': cross_gold,
                            'dead_cross': dead[-3:]})
            self._json(out)
        elif u.path == '/api/index_kline':
            code = q.get('code', [''])[0]
            s = D.indices.get(code)
            if not s:
                self._json({'error': 'unknown index'})
            else:
                # 全量历史（2026-11-07 起本地指数已是长历史：上证 1990 年起约 8600 根）；
                # 前端按 imRange 自行截取区间，服务端只做数值圆整。超长数组体积可控（~500KB）。
                self._json({'code': code, 'name': s['name'],
                            'dates': s['dates'],
                            'o': s['o'], 'h': s['h'], 'l': s['l'], 'c': s['c'],
                            'e12': [round(x, 2) for x in s['e12']],
                            'e50': [round(x, 2) for x in s['e50']]})
        elif u.path == '/api/idx_bottom_study':
            # DEPRECATED（2026-11-07）：旧「大底研究」用未来 120 日涨幅标注大底，存在前视偏差，
            # 页面已停用，仅保留接口供历史对照。请改用 /api/idx_trend_hold。
            code = q.get('code', ['sh000300'])[0]
            try:
                import sys
                if ROOT not in sys.path:
                    sys.path.insert(0, ROOT)
                import idx_bottom_study as BS
                self._json(BS.cached(code))
            except Exception as ex:
                self._json({'error': str(ex)})
        elif u.path == '/api/idx_trend_hold':
            # EXPMA 乖离回归尺度 + 模式B 持有期/退出（三关纪律版，按指数文件 mtime 缓存）
            code = q.get('code', ['sh000300'])[0]
            try:
                import sys
                if ROOT not in sys.path:
                    sys.path.insert(0, ROOT)
                import idx_trend_hold_study as TH
                self._json(TH.cached(code))
            except Exception as ex:
                self._json({'error': str(ex)})
        elif u.path == '/api/regime':
            # 只用成分数>=200 的交易日计算动量，避免早期单只股票决定指数
            _vi = [i for i, n in enumerate(D.idx_cnt) if n >= 200]
            cur = D.regime.get(D.px_date, D.regime.get(D.idx_days[-1] if D.idx_days else None, 'down'))
            m = 1.0
            for j in _vi[-20:]:
                m *= 1 + D.idx_rets[j]
            self._json({'days': D.idx_days[-120:],
                        'rets': [round(r * 100, 3) for r in D.idx_rets[-120:]],
                        'regime': [D.regime.get(d) for d in D.idx_days[-120:]],
                        'current': cur, 'mom20': round((m - 1) * 100, 2),
                        'px_date': D.px_date, 'n_used': len(_vi),
                        'mkt': D.mkt.get(D.px_date, 'down'),
                        'mkt_series': [D.mkt.get(d) for d in D.idx_days[-120:]]})
        elif u.path == '/api/bond_stock':
            global BOND
            if BOND is None:
                try:
                    BOND = bond_payload()
                except Exception as e:
                    return self._json({'error': str(e)})
            self._json(BOND or {'error': 'bond data missing'})
        elif u.path == '/api/tlpool':
            self._json(tlpool_view())
        elif u.path == '/api/tltrack':
            self._json(tltrack_view(q))
        elif u.path == '/api/dragon':
            self._json(dragon_view(q))
        elif u.path == '/api/win80':
            self._json(_tl_json(os.path.join(WEB, 'win80.json'),
                                {'error': 'webapp/win80.json missing：请先跑 daily_run.py'}))
        elif u.path == '/api/win80_alerts':
            # 80%胜率规则买卖弹窗：滚动提醒记录 + 守护心跳（p13 面板显示「弹过没」）
            recs = _tl_json(os.path.join(WEB, 'win80_alerts.json'), [])
            st = _tl_json(os.path.join(WEB, 'win80_alert_state.json'), {})
            self._json(dict(records=(recs[-40:][::-1] if isinstance(recs, list) else []),
                            n=(len(recs) if isinstance(recs, list) else 0), state=st))
        elif u.path == '/api/divcross':
            self._json(divcross_view(q))
        elif u.path == '/api/divtrack':
            self._json(divtrack_view(q))
        elif u.path == '/api/backtest':
            out = {}
            for f, key in (('backtest_result.json', 'v1'), ('backtest_v2_result.json', 'v2'), ('backtest_m89_result.json', 'm89')):
                p = os.path.join(ROOT, f)
                if os.path.exists(p):
                    out[key] = json.load(open(p))
            self._json(out)
        elif u.path == '/qrcode.jpg':
            try:
                b = open(WEB + '/qrcode.jpg', 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'max-age=86400')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except OSError:
                self.send_response(404); self.end_headers()
        elif u.path == '/html2canvas.min.js':
            try:
                b = open(WEB + '/html2canvas.min.js', 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                self.send_header('Cache-Control', 'max-age=86400')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            except OSError:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print('loading data...', flush=True)
    D = Data()
    print('stocks: %d, days: %d, px_date: %s, fresh: %d, stale: %d, regime: %s' % (
        len(D.k), len(D.idx_days), D.px_date, len(D.fresh), len(D.stale), D.regime.get(D.px_date)), flush=True)
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    print('serving http://127.0.0.1:%d' % port, flush=True)
    srv.serve_forever()
