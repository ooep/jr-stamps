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
import math
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
    ('群馬県', '中野'): 'わたらせ渓谷線',
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


GRID = 0.01          # ~1.1km 网格, 用于「线路是否经过该站」的邻域检索
NEAR_ENDPOINT = 2.0  # km: 端点同名站 + 位置吻合才算同一站
NEAR_LINE = 0.9      # km: 线路几何经过站点的最大距离


def _flat(coords, out):
    for c in coords:
        if isinstance(c[0], (list, tuple)):
            _flat(c, out)
        else:
            out.append((float(c[0]), float(c[1])))


def _km(lon1, lat1, lon2, lat2):
    k = math.cos(math.radians((lat1 + lat2) * 0.5))
    return math.hypot((lon1 - lon2) * k * 111.0, (lat1 - lat2) * 111.0)


def _cell(lon, lat):
    return (int(math.floor(lon / GRID)), int(math.floor(lat / GRID)))


def build_index(geojson):
    """segments3d.geojson -> (station_lines, line_color, line_segs, geo)

    geo = (endpoint, grid, line_op):
      endpoint[name]      -> [(lon, lat, line)]   段端点坐标(用于同名站消歧)
      grid[(cx,cy)]       -> [(lon, lat, line)]   线路采样点(用于「线路是否经过」)
      line_op[line]       -> Counter(op)          线路运营方分布
    """
    feats = geojson.get('features') if isinstance(geojson, dict) else geojson
    line_color, line_segs = {}, Counter()
    station_lines = defaultdict(Counter)
    cand = defaultdict(Counter)
    endpoint = defaultdict(list)
    grid = defaultdict(list)
    line_pts = defaultdict(list)
    line_op = defaultdict(Counter)
    for f in feats:
        p = f.get('properties') or {}
        ln = p.get('line')
        if not ln:
            continue
        line_segs[ln] += 1
        c = p.get('color')
        if c:
            cand[ln][c] += 1
        op = p.get('op')
        if op:
            line_op[ln][op] += 1
        for s in (p.get('from'), p.get('to')):
            if s:
                station_lines[s][ln] += 1
        geom = f.get('geometry') or {}
        pts = []
        try:
            _flat(geom.get('coordinates') or [], pts)
        except Exception:
            pts = []
        for pt in pts:
            grid[_cell(pt[0], pt[1])].append((pt[0], pt[1], ln))
            line_pts[ln].append(pt)
        if pts:
            for nm, pt in ((p.get('from'), pts[0]), (p.get('to'), pts[-1])):
                if nm:
                    endpoint[nm].append((pt[0], pt[1], ln))
    for ln, c in cand.items():
        line_color[ln] = c.most_common(1)[0][0]
    return station_lines, line_color, line_segs, (endpoint, grid, line_op, line_pts)


def _lines_near_grid(grid, lon, lat, radius_km=NEAR_LINE):
    """线路几何距站点 radius_km 以内的线路名集合"""
    reach = int(math.ceil(radius_km / 100.0 / GRID)) + 1
    cx, cy = _cell(lon, lat)
    hit = Counter()
    for ix in range(cx - reach, cx + reach + 1):
        for iy in range(cy - reach, cy + reach + 1):
            for (x, y, ln) in grid.get((ix, iy), ()):
                d = _km(lon, lat, x, y)
                if d <= radius_km:
                    hit[ln] += 1
    return hit


def _dist_to_line(line, lon, lat, geo, cap=1e9):
    """站点到某线路几何的最近距离(km), 超出 cap 立即返回 cap"""
    if not geo or lon is None or lat is None:
        return 0.0
    pts = geo[3].get(line) or ()
    if not pts:
        return float('inf')     # 数据集没有这条线的几何 -> 无法校验, 按"远"处理
    k = math.cos(math.radians(lat))
    best = float('inf')
    for (x, y) in pts:
        d = math.hypot((x - lon) * k * 111.0, (y - lat) * 111.0)
        if d < best:
            best = d
            if best < 0.05:
                break
    return best


def left_ok(line, lon, lat, geo):
    """数据集里没有该线路几何时(无坐标可比), 不拦截都道府県覆盖"""
    return not geo or not (geo[3].get(line)) or lon is None


def _op_bonus(line, op, geo):
    """同运营方优先(共站枢纽: 东京站 JR 与地铁不会互相顶掉)"""
    if not op or not geo:
        return 0
    ops = geo[2].get(line)
    return 1 if ops and ops.get(op) else 0


def _candidates(station_name, base, station_lines, line_segs, lon, lat, op, geo):
    """候选线路 -> 权重。

    有坐标: 端点同名且<=NEAR_ENDPOINT 的线路优先; 若无, 用几何邻域
    (覆盖长区间跳过中间站的情况)。无坐标: 退回纯站名统计(旧行为)。
    """
    if lon is None or lat is None or not geo:
        full = station_lines.get(station_name) or station_lines.get(base) or {}
        c = {l: v for l, v in full.items() if '新幹線' not in l}
        return c or dict(full)

    endpoint, grid = geo[0], geo[1]
    near = {}
    for nm in (station_name, base):
        for (x, y, ln) in endpoint.get(nm, ()):
            if _km(lon, lat, x, y) <= NEAR_ENDPOINT:
                near[ln] = near.get(ln, 0) + 2      # 同名且位置吻合, 权重最高
    if not near:
        near = dict(_lines_near_grid(grid, lon, lat))   # 线路几何经过该站
    if not near:
        # 几何邻域也没找到: 退回站名统计, 但剔除地理上明显无关的线路
        full = station_lines.get(station_name) or station_lines.get(base) or {}
        full = {l: v for l, v in full.items() if '新幹線' not in l} or dict(full)
        ok = {l: v for l, v in full.items()
              if _dist_to_line(l, lon, lat, geo) <= 50.0}
        return ok

    c = {l: v for l, v in near.items() if '新幹線' not in l}
    if not c:
        return dict(near)          # 新干线专属站(新函館北斗等): 保留新干线色
    return c


def resolve(station_name, station_lines, line_color, line_segs,
            station_override=None, line_override=None, pref=None,
            lon=None, lat=None, op=None, geo=None):
    """返回 (line 或 None, color)。color 一定可用(无匹配时回退藏青)。

    给出 lon/lat 时启用地理约束: 只接受端点同名**且位置吻合**的线路,
    没有则在「几何经过该站的线路」里挑。这样能消灭同名站跨县串线
    (池田(熊本県) 误挂北海道根室線之类)。
    """
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
        # 都道府県覆盖也要过地理校验, 否则同名站会被钉到远方的线上
        if _dist_to_line(forced, lon, lat, geo, cap=60.0) <= 60.0 \
                or left_ok(forced, lon, lat, geo):
            return forced, (lo.get(forced) or line_color.get(forced) or DEFAULT_INK)

    line = so.get(station_name) or so.get(base)
    if line and line not in line_color:
        line = None            # 覆盖项在这个数据集里没色 -> 走自动
    elif line:
        # 覆盖表是按「站名」写的, 同名异地站(神田/田町/大宮)会被误钉:
        # 有坐标时要求线路确实经过该站, 否则交给地理归因
        if _dist_to_line(line, lon, lat, geo, cap=25.0) > 25.0:
            line = None

    if not line:
        c = _candidates(station_name, base, station_lines, line_segs,
                        lon, lat, op, geo)
        if c:
            skip_grey = {l: v for l, v in c.items() if not _grey(line_color.get(l))}
            c = skip_grey or c
            line = max(c, key=lambda x: (c[x], _op_bonus(x, op, geo),
                                         -line_segs.get(x, 0)))

    color = lo.get(line) or line_color.get(line) or DEFAULT_INK
    return line, color


def apply(rows, geojson, key_name='name', pref_key='pref'):
    """原地给 worklist 行补 line / line_color 字段"""
    station_lines, line_color, line_segs, geo = build_index(geojson)
    n_line = n_color = 0
    for r in rows:
        line, color = resolve(r.get(key_name) or '', station_lines, line_color, line_segs,
                              pref=r.get(pref_key), lon=r.get('lon'), lat=r.get('lat'),
                              op=r.get('op'), geo=geo)
        r['line'] = line or ''
        r['line_color'] = color
        if line:
            n_line += 1
        if color != DEFAULT_INK:
            n_color += 1
    return rows, dict(total=len(rows), with_line=n_line, with_color=n_color)
