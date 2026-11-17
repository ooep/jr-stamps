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


# ─────────── 线路色渲染 ───────────
def _hex2rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _to_hsl(rgb):
    r, g, b = [v / 255.0 for v in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    sat = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = ((g - b) / d + (6 if g < b else 0)) / 6
    elif mx == g:
        h = ((b - r) / d + 2) / 6
    else:
        h = ((r - g) / d + 4) / 6
    return h, sat, l


def _from_hsl(h, sat, l):
    if sat <= 0:
        v = int(round(l * 255)); return (v, v, v)
    q = l * (1 + sat) if l < 0.5 else l + sat - l * sat
    p = 2 * l - q

    def f(t):
        t = t % 1.0
        if t < 1 / 6: return p + (q - p) * 6 * t
        if t < 1 / 2: return q
        if t < 2 / 3: return p + (q - p) * (2 / 3 - t) * 6
        return p
    return tuple(int(round(max(0, min(1, f(h + d))) * 255)) for d in (1 / 3, 0, -1 / 3))


def readable_ink(hex_color, target_lum=84, sat_boost=1.0):
    """把线路色压成可读的墨色: 保留色相, 亮度拉到 target_lum, 饱和度可选增强"""
    h, s, l = _to_hsl(_hex2rgb(hex_color))
    s = min(1.0, s * sat_boost + 0.08)
    # 由亮度目标反解 L
    tl = target_lum / 255.0
    cand = _from_hsl(h, s, tl)
    if abs(0.299 * cand[0] + 0.587 * cand[1] + 0.114 * cand[2] - target_lum) > 12:
        lo, hi = 0.0, tl
        for _ in range(24):
            mid = (lo + hi) / 2
            c = _from_hsl(h, s, mid)
            lm = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
            if lm > target_lum:
                hi = mid
            else:
                lo = mid
        cand = _from_hsl(h, s, (lo + hi) / 2)
    return cand


def line_tint(im, hex_color, keep_accent=True, sat_boost=1.0,
              gamma=None, white_amt=0.22, deepen=1.0):
    """把(深蓝墨)印章图案整体重着色为该线路的饱和色。

    做法不是"混色", 而是**油墨覆盖**: 以墨密度 d 为权重, 从近乎白的纸色
    压到线路色本体, 再对 d 做 gamma 压深, 让中间调偏向满色 —— 这样细密的
    木刻排线仍然保持浓墨观感, 不会糊成水彩。
    """
    base = np.asarray(im.convert('RGBA')).astype(np.float32)
    rgb, al = base[..., :3], base[..., 3]
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    tgt = np.array(_hex2rgb(hex_color), dtype=np.float32)
    op = al > 40
    if op.sum() < 200:
        return im
    lo = float(np.percentile(lum[op], 3))
    hi = float(np.percentile(lum[op], 97))
    d = np.clip((hi - lum) / max(6.0, hi - lo), 0, 1)

    # 自适应: 让每张图的平均墨量观感一致(原图对比度低的会被压深)
    if gamma is None or gamma <= 0:
        m = float(d[op].mean())
        gamma = 0.95 if m <= 1e-6 else float(np.clip(
            np.log(0.55) / np.log(max(1e-3, min(0.999, m))), 0.55, 1.35))
    d = np.power(d, gamma) * deepen          # 压深中间调 -> 更浓的墨

    # 纸端只混少量线路色(保持干净), 墨端直接到线路色甚至略深
    light = 255 + (tgt - 255) * white_amt
    dark = tgt * 0.86
    out = light + (dark - light) * np.clip(d, 0, 1)[..., None]

    if sat_boost != 1.0:
        # 保色相提升饱和度: 相对灰度做一次外推
        g = out @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        out = g[..., None] + (out - g[..., None]) * sat_boost

    if keep_accent:
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        warm = (r > b + 26) & (r > 90) & op
        if warm.sum() > 30:
            acc = np.stack([np.full_like(r, 190), np.full_like(r, 42), np.full_like(r, 34)], -1)
            k = warm[..., None].astype(np.float32) * 0.95
            out = out * (1 - k) + acc * k

    out = np.clip(out, 0, 255)
    return Image.fromarray(np.dstack([out, al]).astype(np.uint8), 'RGBA')


def ink_color(art_path, deepen=0.94, src_img=None):
    """从印章主图提取印泥主色: 「饱和×暗」高分区的中位数。
       自动跟随每张图的主色调; 太浅时压深保证可读。"""
    if src_img is not None:
        im = src_img
    else:
        im = Image.open(art_path)
    has_alpha = im.mode in ('RGBA', 'LA') or 'transparency' in im.info
    al = np.asarray(im.convert('RGBA'))[..., 3] if has_alpha else None
    im = im.convert('RGB')
    arr = np.asarray(im.filter(ImageFilter.MedianFilter(3))).astype(np.float32)
    lum = arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if al is not None and float(np.mean(al < 30)) > 0.02:
        # 原生透明底: 只取有墨(alpha 高)的区域, 否则透明区的黑色会把主色拉黑
        m = (al > 200) & (lum < 210)
        if m.sum() < 500:
            m = (al > 120) & (lum < 210)
    else:
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
              ink=None, with_text=True, save_plain=True, line_color=None,
              sat_boost=1.0):
    """brand_pos: below(站名弧下方) | sides(左右竖排) | outer(最外圈小弧)"""
    src, cx, cy, R = load_art(art_path)
    base, CX, CY, RR = to_transparent(src, size, cx, cy, R)
    if line_color:
        base = line_tint(base, line_color, sat_boost=sat_boost)
    if save_plain:
        base.save(out_base + '.png')
    if not with_text:
        return out_base + '.png'

    if ink:
        icol = ink
    elif line_color:
        icol = readable_ink(line_color, target_lum=84, sat_boost=sat_boost)
    else:
        icol = ink_color(art_path, src_img=src)
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
