#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取东方财富概念板块（fs=m:90+t:3）及成分股，构建 股票→概念列表 映射。
存 webapp/concepts.json：{"fetched": 日期, "n_boards": N, "map": {code: [概念,...]}}
概念归属变动慢，daily_run 每 5 天刷新一次即可；主线识别用概念比行业(f100)更贴近短线资金。
噪音概念（融资融券/沪深股通/重仓类等泛化板块）进黑名单剔除。"""
import datetime, json, os, time, urllib.request
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = _REPO
OUT = os.path.join(BASE, 'webapp', 'concepts.json')

# 泛化/风格类概念：成员过杂，会稀释主线信号，直接剔除
NOISE = {
    '融资融券', '转融券标的', '沪股通', '深股通', '机构重仓', '基金重仓', '社保重仓',
    'QFII重仓', '预盈预增', '预亏预减', '养老金', '证金持股', '汇金概念', '创业板综',
    '标准普尔', '富时罗素', 'MSCI中国', '昨日涨停', '昨日连板', '昨日触板', '昨日涨停_含一字',
    '百元股', '高送转', '低价股', '微盘股', '破净股', 'ST股', '次新股', '新股与次新股',
    '转债标的', '同花顺漂亮100', '标普道琼斯A股', 'AH股', 'B股', 'GDR', '股权转让',
    '增持回购', '股权激励', '员工持股', '送转填权', '送转预期', '摘帽', '壳资源',
    '国企改革', '央企改革', '混改', '并购重组', '再融资', '举牌', '独角兽',
    # 状态/行情类伪概念（描述近期走势而非题材属性）
    '昨日高换手', '昨日首板', '昨日连板_含一字', '昨日触板', '题材股', '趋势股',
    '百日新高', '近期新高', '最近多板', '东方财富热股', '连续亏损', '高负债率',
    '低价股', '微盘股', '破净股', '百元股', '活跃股', '强势股', '填权股',
}

def _is_noise(name):
    if name in NOISE:
        return True
    # 规则过滤：状态前缀 / 财报统计类
    if name.startswith(('昨日', '近期', '连续', '今日')):
        return True
    if any(k in name for k in ('预增', '预减', '首亏', '扭亏', '预亏', '中报', '年报', '一季报', '三季报')):
        return True
    return False

def _get(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
    return json.load(urllib.request.urlopen(req, timeout=15))

def _pre(code):
    if code.startswith('6') or (code.startswith('9') and not code.startswith('92')):
        return 'sh'
    if code.startswith(('4', '8', '92')):
        return 'bj'
    return 'sz'

def fetch():
    # 1) 概念板块清单
    boards = []
    pn = 1
    while pn <= 20:
        d = _get('https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1'
                 '&fltt=2&invt=2&fid=f12&fs=m:90+t:3&fields=f12,f14' % pn)
        diff = (d.get('data') or {}).get('diff') or []
        if not diff:
            break
        boards += [(r['f12'], r['f14']) for r in diff if r.get('f12')]
        total = (d.get('data') or {}).get('total', 0)
        if pn * 100 >= total:
            break
        pn += 1
        time.sleep(0.1)
    # 2) 逐板块成分股 → 反向映射
    mapping = {}
    n_ok = 0
    for bk, name in boards:
        if _is_noise(name):
            continue
        try:
            d = _get('https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1000&po=1&np=1'
                     '&fltt=2&invt=2&fid=f12&fs=b:%s&fields=f12' % bk)
        except Exception:
            continue
        diff = (d.get('data') or {}).get('diff') or []
        if len(diff) < 5:  # 太小的板块无代表性
            continue
        for r in diff:
            code = _pre(str(r['f12'])) + str(r['f12'])
            mapping.setdefault(code, []).append(name)
        n_ok += 1
        time.sleep(0.06)
    json.dump({'fetched': datetime.date.today().isoformat(),
               'n_boards': n_ok, 'map': mapping},
              open(OUT, 'w'), ensure_ascii=False)
    return n_ok, len(mapping)

if __name__ == '__main__':
    nb, ns = fetch()
    print('concepts saved: %d boards, %d stocks mapped' % (nb, ns))
