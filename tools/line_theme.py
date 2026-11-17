#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线路主题: 为每个车站解析「主线路 + 线路色」。

数据来源
--------
* mini-japanrail-3d/data/segments3d.geojson
  每个 segment = (line, from, to, color, op)。天然带官方线路色,
  比从线路图标 SVG 反推颜色可靠得多。
* 站点 -> 线路: 统计哪些线路的 segment 端点出现过该站, 取「经过该站的主要线路」。

归因局限
--------
segments3d 的 from/to 只是 segment 端点, 长区间的中间站不会出现(北海道部分站点尤明显);
共站枢纽(东京/新宿)的"主线路"本质上是主观的。因此:
  1. 用 STATION_LINE_OVERRIDE 钉住标志性车站;
  2. 用 LINE_COLOR_OVERRIDE 修正明显取色错误(如中央線被数据集并入中央·総武线取到蓝色);
  3. 都没有则回退默认藏青(indigo), 不硬凑。
"""
from collections import Counter, defaultdict

DEFAULT_INK = '#1F3A66'      # 回退藏青(与旧版一致)

# 数据集里取色明显偏离官方印象的线路 —— 人工校正
LINE_COLOR_OVERRIDE = {
    '中央線': '#EB5C01',          # 数据集把中央·総武各駅停車并入 -> 取到蓝; 官方橙
    '中央本線': '#EB5C01',
    '中央・総武線': '#FFD400',
    '山手線': '#85C023',          # 黄緑
    '京浜東北線': '#00B2E5',
    '根岸線': '#00B2E5',
    '埼京線': '#00AC9A',
    '大阪環状線': '#F8405C',
    '御堂筋線': '#E5171F',
    '東海道線': '#FF9845',
    '東海道本線': '#FF9845',
    '東北線': '#00A7E3',
    '東北本線': '#00A7E3',
    '総武線': '#FFD400',
    '総武本線': '#FFD400',
    '常磐線': '#0071C5',
    '横浜線': '#85C023',
    '武蔵野線': '#EB5C01',
    '南武線': '#FFE400',
    '京葉線': '#CF1225',
    '南海本線': '#0077CE',
    '鹿児島本線': '#EE3D49',
    '函館本線': '#036FC2',
}

# 标志性车站 -> 主线路(优先于自动归因)
STATION_LINE_OVERRIDE = {
    '東京': '山手線', '新宿': '山手線', '池袋': '山手線', '渋谷': '山手線',
    '品川': '山手線', '田町': '山手線', '新橋': '山手線', '有楽町': '山手線',
    '秋葉原': '山手線', '上野': '山手線', '目黒': '山手線', '恵比寿': '山手線',
    '高田馬場': '山手線', '大崎': '山手線', '五反田': '山手線', '巣鴨': '山手線',
    '御茶ノ水': '中央線', '立川': '中央線', '中野': '中央線', '吉祥寺': '中央線',
    '三鷹': '中央線', '八王子': '中央線', '神田': '中央線',
    '横浜': '東海道線', '川崎': '東海道線', '名古屋': '東海道線',
    '大阪': '大阪環状線', '天王寺': '大阪環状線', '京都': '東海道線',
    '難波': '南海本線', '博多': '鹿児島本線', '札幌': '函館線',
    '仙台': '東北線', '大宮': '東北線', '福島': '東北線',
    '神戸': '東海道線', '三ノ宮': '東海道線', '姫路': '東海道線',
    '岡山': '山陽線', '広島': '山陽線', '小倉': '鹿児島本線', '熊本': '鹿児島本線',
    '北千住': '常磐線', '西船橋': '総武線', '町田': '横浜線',
}

# (都道府県, 站名) -> 线路; '' 表示强制回退藏青(数据集无该线路色)
# 用于同名站消歧: 千葉站被数据集同时挂到千葉都市モノレール, 群馬的中野实为上信電鉄
PREF_OVERRIDE = {
    ('千葉県', '千葉'): '総武線',
    ('群馬県', '中野'): '上信線',
    ('群馬県', '前橋'): '両毛線',
    ('栃木県', '宇都宮'): '東北線',
}

_NEUTRAL_TOL = 18   # 灰度容差: 过灰的线路色不优先


def _grey(hex_color):
    if not hex_color:
        return True
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return True
    return max(r, g, b) - min(r, g, b) < _NEUTRAL_TOL


def build_index(geojson):
    """segments3d.geojson -> (station_lines, line_color, line_segs)"""
    feats = geojson.get('features') if isinstance(geojson, dict) else geojson
    line_color, line_segs = {}, Counter()
    station_lines = defaultdict(Counter)
    cand = defaultdict(Counter)
    for f in feats:
        p = f.get('properties') or {}
        ln = p.get('line')
        if not ln:
            continue
        line_segs[ln] += 1
        c = p.get('color')
        if c:
            cand[ln][c] += 1
        for s in (p.get('from'), p.get('to')):
            if s:
                station_lines[s][ln] += 1
    for ln, c in cand.items():
        line_color[ln] = c.most_common(1)[0][0]
    return station_lines, line_color, line_segs


def resolve(station_name, station_lines, line_color, line_segs,
            station_override=None, line_override=None, pref=None):
    """返回 (line 或 None, color)。color 一定可用(无匹配时回退藏青)。"""
    so = STATION_LINE_OVERRIDE if station_override is None else station_override
    lo = LINE_COLOR_OVERRIDE if line_override is None else line_override

    base = station_name.split('(')[0]
    station_name = station_name.strip()

    forced_key = next((k for k in ((pref, station_name), (pref, base))
                       if pref and k in PREF_OVERRIDE), None)
    if forced_key:
        forced = PREF_OVERRIDE[forced_key]
        if not forced:
            return None, DEFAULT_INK
        return forced, (lo.get(forced) or line_color.get(forced) or DEFAULT_INK)

    line = so.get(station_name) or so.get(base)
    if line and line not in line_color:
        line = None            # 覆盖项在这个数据集里没色 -> 走自动

    if not line:
        # 完整站名优先(带"群馬県"之类后缀的站名不会串到同名站)
        full = station_lines.get(station_name) or station_lines.get(base) or {}
        c = {l: v for l, v in full.items() if '新幹線' not in l}
        if not c:
            c = dict(full)          # 新干线专属站(新函館北斗/岐阜羽島等): 保留新干线色
        if c:
            skip_grey = {l: v for l, v in c.items() if not _grey(line_color.get(l))}
            c = skip_grey or c
            line = max(c, key=lambda x: (c[x], -line_segs.get(x, 0)))

    color = lo.get(line) or line_color.get(line) or DEFAULT_INK
    return line, color


def apply(rows, geojson, key_name='name', pref_key='pref'):
    """原地给 worklist 行补 line / line_color 字段"""
    station_lines, line_color, line_segs = build_index(geojson)
    n_line = n_color = 0
    for r in rows:
        line, color = resolve(r.get(key_name) or '', station_lines, line_color, line_segs,
                              pref=r.get(pref_key))
        r['line'] = line or ''
        r['line_color'] = color
        if line:
            n_line += 1
        if color != DEFAULT_INK:
            n_color += 1
    return rows, dict(total=len(rows), with_line=n_line, with_color=n_color)
