#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""印章渲染 v2 —— 自动主色 + 品牌署名"""
import sys, os, math, argparse
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, '..', 'outputs')):
    if os.path.exists(os.path.join(p, 'stamp_typography.py')):
        sys.path.insert(0, p); break
from stamp_typography import (load_art, to_transparent, F, adv, line_width,
                              draw_arc_ko, draw_line_ko)
from PIL import Image, ImageDraw, ImageFilter


def ink_color(art_path, deepen=0.94, src_img=None):
    """从印章主图提取印泥主色: 「饱和×暗」高分区的中位数。
       自动跟随每张图的主色调; 太浅时压深保证可读。"""
    im = (src_img or Image.open(art_path)).convert('RGB')
    arr = np.asarray(im.filter(ImageFilter.MedianFilter(3))).astype(np.float32)
    lum = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    m = lum < 200
    if not m.any():
        return (30, 45, 90)
    ink, ilum = arr[m], lum[m]
    mx, mn = ink.max(axis=1), ink.min(axis=1)
    sat = (mx - mn) / np.maximum(mx, 1)
    score = sat * (1 - ilum / 255.0)
    sel = ink[score >= np.percentile(score, 85)]
    c = np.median(sel, axis=0)
    # 亮度兜底: 提取色若偏浅(<95)则加深, 保证文字与图案对比
    l = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    k = deepen if l <= 130 else max(0.55, 95.0 / max(l, 1))
    c = c * k
    return tuple(int(min(255, max(0, v))) for v in c)


def fit_arc_span(text, radius, f_factory, track_deg, max_deg):
    """按可用角度上限自动选择字号: 字数多的站名自动缩小, 保证不挤满环带"""
    for k in range(100, 30, -4):
        f = f_factory(k)
        track_px = track_deg * math.pi / 180 * radius
        w = sum(f.getbbox(c)[2] - f.getbbox(c)[0] for c in text) + track_px * (len(text) - 1)
        deg = math.degrees(w / radius)
        if deg <= max_deg:
            return f, k, deg
    f = f_factory(30)
    return f, 30, 999


def render_v2(art_path, out_base, top, name, brand='MiniJapanRail',
              size=2048, layout='arc', brand_pos='below',
              ink=None, with_text=True, save_plain=True):
    """brand_pos: below(站名弧下方) | sides(左右竖排) | outer(最外圈小弧)"""
    src, cx, cy, R = load_art(art_path)
    base, CX, CY, RR = to_transparent(src, size, cx, cy, R)
    if save_plain:
        base.save(out_base + '.png')
    if not with_text:
        return out_base + '.png'

    icol = ink or ink_color(art_path, src_img=src)
    im = base.copy()

    # ── 上弧: 运营方 ──
    r_top = RR * 0.872
    f_top, kt, dgt = fit_arc_span(top, r_top, lambda k: F('sans', 'Medium', k),
                                  2.0, max_deg=104)
    f_top = F('sans', 'Medium', min(kt, int(size * 0.050)))
    gk = max(4, int(size * 0.0095))         # 挖空留白(比例), 保证浓密纹理上文字可读
    draw_arc_ko(im, top, CX, CY, r_top, f_top, icol, track_deg=2.0, grow=gk, blur=2.4)

    # ── 下弧: 站名 ──
    if layout == 'arc':
        txt = name + '駅'
        r_name = RR * 0.770
        f_name, ksz, deg = fit_arc_span(txt, r_name,
                                        lambda k: F('serif', 'Bold', k),
                                        2.2, max_deg=74)
        # 字号不超过基准, 也不小于可读下限
        ksz = min(ksz, int(size * 0.062))
        f_name = F('serif', 'Bold', ksz)
        draw_arc_ko(im, txt, CX, CY, r_name, f_name, icol,
                    track_deg=2.2, center_deg=90, flip=True, grow=gk, blur=2.6)
    elif layout == 'classic':
        draw_line_ko(im, name + '駅', CX, CY + RR * 0.695,
                     F('serif', 'Bold', int(size * 0.126)), icol,
                     track=int(size * 0.011), grow=6, blur=1.3)

    # ── 品牌署名 ──
    if brand:
        f_b = F('sans', 'Medium', int(size * 0.023))
        if brand_pos == 'below':
            # 站名弧外侧下方, 留出间距(站名止于 0.834R, 品牌中心 0.905R)
            draw_arc_ko(im, brand, CX, CY, RR * 0.872, f_b, icol,
                        track_deg=0.75, center_deg=90, flip=True,
                        grow=max(3, int(size * 0.006)), blur=1.8)
        elif brand_pos == 'outer':
            # 最外圈顶部: 运营方(0.872R)之外, 品牌 0.937R
            draw_arc_ko(im, brand, CX, CY, RR * 0.937, f_b, icol,
                        track_deg=0.85, center_deg=270, grow=3, blur=0.9)
        elif brand_pos == 'band':
            # 底部横排小字(不沿弧), 位于站名下方正中
            draw_line_ko(im, brand, CX, CY + RR * 0.905, f_b, icol,
                         track=int(size * 0.004), grow=max(3, int(size*0.008)), blur=1.2)
        elif brand_pos == 'sides':
            # 左右两侧竖排
            for side in (-1, 1):
                _vtext_rot(im, brand, side, CX, CY, RR, f_b, icol)

    im.save(out_base + '.text.png')
    return out_base + '.text.png', icol


def _vtext_rot(im, text, side, CX, CY, RR, f, ink):
    """左右两侧竖排: side=-1 左侧, +1 右侧; 逐字沿垂直方向排列并逆时针旋转 90 度"""
    chars = list(text)
    gaps = 3
    hs = [f.getbbox(c)[3] - f.getbbox(c)[1] for c in chars]
    total = sum(hs) + gaps * (len(chars) - 1)
    x = CX + side * RR * 0.875
    y = CY - total / 2
    for ch, h in zip(chars, hs):
        b = f.getbbox(ch)
        w = b[2] - b[0]
        tile = Image.new('RGBA', (w + 10, h + 10), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((5 - b[0], 5 - b[1]), ch, font=f, fill=tuple(ink) + (255,))
        tile = tile.rotate(90 if side < 0 else -90, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(tile, (int(x - tile.width / 2), int(y + h / 2 - tile.height / 2)))
        y += h + gaps


def _vtext(im, text, cx, cy, f, ink, vertical=True):
    """竖排文字(从上到下), 用于左右两侧"""
    total = sum(f.getbbox(c)[3] - f.getbbox(c)[1] for c in text) + len(text) * 3
    y = cy - total / 2
    d = ImageDraw.Draw(im)
    for ch in text:
        b = f.getbbox(ch)
        w, h = b[2] - b[0], b[3] - b[1]
        d.text((cx - w / 2 - b[0], y - b[1]), ch, font=f, fill=tuple(ink) + (255,))
        y += h + 3


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('art'); ap.add_argument('out')
    ap.add_argument('--top', default='JR東日本')
    ap.add_argument('--name', default='東京')
    ap.add_argument('--brand', default='MiniJapanRail')
    ap.add_argument('--brand-pos', default='below', choices=['below', 'outer', 'band', 'sides', 'off'])
    ap.add_argument('--size', type=int, default=2048)
    ap.add_argument('--layout', default='arc')
    ap.add_argument('--ink', default='', help='留空=自动提取主图颜色')
    a = ap.parse_args()
    ink = None
    if a.ink:
        h = a.ink.lstrip('#'); ink = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    bp = 'below' if a.brand_pos == 'off' else a.brand_pos
    brand = '' if a.brand_pos == 'off' else a.brand
    out, c = render_v2(a.art, a.out, a.top, a.name, brand, a.size, a.layout, bp, ink)
    print(out, 'ink=#%02x%02x%02x' % c)
