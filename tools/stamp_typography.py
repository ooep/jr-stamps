#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stamp_typography.py — 駅スタンプ排版引擎

特性:
  * CJK 字体: 系统 Noto Sans/Serif CJK JP, 7 档字重 (index=0 为 JP 字形)
  * 弧形文字: 逐字沿圆排布并旋转, 下弧自动反转字序保持可读
  * 字距控制: 横排按 px, 弧排按角度(degree), 拉开呼吸感
  * 挖空法: 文字区域图案 alpha 清零(模拟印版留白), 再写同色墨字
            → 成品为纯单色印章, 放在任何底色上都干净
  * 透明度抠图: 亮度映射(纸底→透明, 油墨→不透明, 中间线性过渡)

依赖: Pillow, numpy
字体: /usr/share/fonts/opentype/noto/NotoSansCJK-{w}.ttc, NotoSerifCJK-{w}.ttc
"""
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

SANS  = '/usr/share/fonts/opentype/noto/NotoSansCJK-{w}.ttc'
SERIF = '/usr/share/fonts/opentype/noto/NotoSerifCJK-{w}.ttc'

def F(kind, weight, sz):
    tpl = SANS if kind == 'sans' else SERIF
    return ImageFont.truetype(tpl.format(w=weight), sz, index=0)

def adv(f, ch):
    b = f.getbbox(ch)
    return b[2] - b[0]

def line_width(f, text, track):
    if not text: return 0
    return sum(adv(f, c) for c in text) + track * (len(text) - 1)

def draw_line(im, text, cx, y_mid, f, fill, track=0, stroke=0, sfill=None):
    if not text: return
    d = ImageDraw.Draw(im)
    b0 = f.getbbox(text)
    x = cx - line_width(f, text, track) / 2
    y = y_mid - (b0[3] - b0[1]) / 2
    kw = dict(stroke_width=stroke, stroke_fill=sfill) if stroke and sfill else {}
    for ch in text:
        b = f.getbbox(ch)
        d.text((x - b[0], y - b[1]), ch, font=f, fill=fill, **kw)
        x += adv(f, ch) + track

def draw_arc(im, text, cx, cy, radius, f, fill, track_deg=0.0, center_deg=270.0,
             flip=False, stroke=0, sfill=None):
    """flip=False:字头朝外(上弧);True:字头朝圆心(下弧可读)"""
    if flip:
        text = text[::-1]
    n = len(text)
    if not n: return
    track_px = track_deg * math.pi / 180 * radius
    widths = [adv(f, c) for c in text]
    total_px = sum(widths) + track_px * (n - 1)
    total_deg = math.degrees(total_px / radius)
    start = center_deg - total_deg / 2
    acc = 0.0
    for i, ch in enumerate(text):
        w = widths[i]
        mid = start + math.degrees((acc + w / 2) / radius)
        acc += w + track_px
        a = math.radians(mid)
        x, y = cx + radius * math.cos(a), cy + radius * math.sin(a)
        rot = (mid - 270.0) if not flip else (mid - 90.0)
        b = f.getbbox(ch)
        cw, chh = max(1, b[2]-b[0]), max(1, b[3]-b[1])
        pad = 14 + stroke * 2
        tile = Image.new('RGBA', (cw + pad*2, chh + pad*2), (0,0,0,0))
        td = ImageDraw.Draw(tile)
        kw = dict(stroke_width=stroke, stroke_fill=sfill) if stroke and sfill else {}
        td.text((pad - b[0], pad - b[1]), ch, font=f, fill=fill, **kw)
        tile = tile.rotate(-rot, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(tile, (int(round(x - tile.width/2)), int(round(y - tile.height/2))))

def load_art(path):
    src = Image.open(path).convert('RGB')
    if src.size[0] != src.size[1]:
        s = max(src.size); sq = Image.new('RGB', (s,s), (255,255,255))
        sq.paste(src, ((s-src.size[0])//2, (s-src.size[1])//2)); src = sq
    arr = np.asarray(src.filter(ImageFilter.MedianFilter(3))).astype(np.float32)
    lum = arr @ np.array([0.299,0.587,0.114], dtype=np.float32)
    ys, xs = np.where(lum < 200)
    return src, (xs.min()+xs.max())/2, (ys.min()+ys.max())/2, max(xs.max()-xs.min(), ys.max()-ys.min())/2

def to_transparent(src, size, cx, cy, R, hi=185, lo=115):
    sc = size / src.size[0]
    im = src.resize((size, size), Image.LANCZOS)
    arr = np.asarray(im).astype(np.float32)
    lum = arr @ np.array([0.299,0.587,0.114], dtype=np.float32)
    a = np.clip((hi - lum) / max(1, hi - lo), 0, 1) * 255
    out = Image.fromarray(np.dstack([arr.astype(np.uint8), a.astype(np.uint8)]), 'RGBA')
    m = Image.new('L', (size, size), 0)
    C, CY, RR = cx*sc, cy*sc, R*sc
    ImageDraw.Draw(m).ellipse((C-RR, CY-RR, C+RR, CY+RR), fill=255)
    ar = np.asarray(out).copy(); ar[...,3] = np.minimum(ar[...,3], np.asarray(m))
    return Image.fromarray(ar, 'RGBA'), C, CY, RR

def bg_lum(im, box):
    x0,y0,x1,y1 = [int(v) for v in box]
    x0,y0 = max(0,x0), max(0,y0); x1,y1 = min(im.width,x1), min(im.height,y1)
    if x1<=x0 or y1<=y0: return 255.0
    a = np.asarray(im.crop((x0,y0,x1,y1)))
    l = a[...,:3].astype(np.float32) @ np.array([0.299,0.587,0.114], dtype=np.float32)
    op = a[...,3] > 128
    return float(np.median(l[op])) if op.any() else 255.0

def ink_for(lumv, dark=(36,53,94), light=(250,245,232)):
    """暗底→浅字(带深描边), 亮底→深字"""
    if lumv < 150:
        return tuple(light)+(255,), tuple(max(0,c-18) for c in dark)+(115,), 2
    return tuple(dark)+(255,), None, 0


def draw_line_haloed(im, text, cx, y_mid, f, ink, track=0,
                     halo=(250,245,232), halo_alpha=205, grow=7, blur=2.4):
    """先做柔和奶油底衬(膨胀+模糊),再写墨色字 —— 任何背景下都清晰,且不显贴纸"""
    if not text: return
    tw = line_width(f, text, track)
    asc, desc = f.getmetrics()
    pad = grow * 3
    W, H = int(tw) + pad * 2, asc + desc + pad * 2
    mask = Image.new('L', (W, H), 0)
    md = ImageDraw.Draw(mask)
    x = pad
    for ch in text:
        b = f.getbbox(ch)
        md.text((x - b[0], pad - b[1]), ch, font=f, fill=255)
        x += adv(f, ch) + track
    # 膨胀 + 模糊
    mask = mask.filter(ImageFilter.MaxFilter(grow * 2 + 1)).filter(ImageFilter.GaussianBlur(blur))
    layer = Image.new('RGBA', (W, H), tuple(halo) + (0,))
    layer.putalpha(mask.point(lambda v: int(v * halo_alpha / 255)))
    ox = int(cx - W / 2)
    oy = int(y_mid - (asc + desc) / 2)
    im.alpha_composite(layer, (ox, oy))
    # 写墨色字
    d = ImageDraw.Draw(im)
    b0 = f.getbbox(text)
    x = cx - tw / 2
    y = y_mid - (b0[3] - b0[1]) / 2
    for ch in text:
        b = f.getbbox(ch)
        d.text((x - b[0], y - b[1]), ch, font=f, fill=tuple(ink) + (255,))
        x += adv(f, ch) + track


def draw_arc_haloed(im, text, cx, cy, radius, f, ink, track_deg=0.0,
                    center_deg=270.0, flip=False, halo=(250,245,232),
                    halo_alpha=200, grow=5, blur=2.0):
    """弧形版留白底衬"""
    if flip:
        text = text[::-1]
    n = len(text)
    if not n: return
    track_px = track_deg * math.pi / 180 * radius
    widths = [adv(f, c) for c in text]
    total_px = sum(widths) + track_px * (n - 1)
    total_deg = math.degrees(total_px / radius)
    start = center_deg - total_deg / 2
    acc = 0.0
    items = []
    for i, ch in enumerate(text):
        w = widths[i]
        mid = start + math.degrees((acc + w / 2) / radius)
        acc += w + track_px
        a = math.radians(mid)
        x, y = cx + radius * math.cos(a), cy + radius * math.sin(a)
        rot = (mid - 270.0) if not flip else (mid - 90.0)
        b = f.getbbox(ch)
        cw, chh = max(1, b[2]-b[0]), max(1, b[3]-b[1])
        pad = 14 + grow * 3
        tile = Image.new('RGBA', (cw + pad*2, chh + pad*2), (0,0,0,0))
        td = ImageDraw.Draw(tile)
        td.text((pad - b[0], pad - b[1]), ch, font=f, fill=tuple(ink) + (255,))
        items.append((tile, x, y, rot, pad, b, cw, chh))
    # 底衬层
    for tile, x, y, rot, pad, b, cw, chh in items:
        m = tile.split()[3].copy()
        m = m.filter(ImageFilter.MaxFilter(grow * 2 + 1)).filter(ImageFilter.GaussianBlur(blur))
        lay = Image.new('RGBA', tile.size, tuple(halo) + (0,))
        lay.putalpha(m.point(lambda v: int(v * halo_alpha / 255)))
        lay = lay.rotate(-rot, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(lay, (int(round(x - lay.width/2)), int(round(y - lay.height/2))))
    # 文字层
    for tile, x, y, rot, pad, b, cw, chh in items:
        t2 = tile.rotate(-rot, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(t2, (int(round(x - t2.width/2)), int(round(y - t2.height/2))))


# ══════════ 挖空法(推荐):文字区图案 alpha 清零 → 写同色墨字 ══════════
def _ko(im, m, ox, oy):
    """用 mask m 在 (ox,oy) 处挖空 im 的 alpha"""
    arr = np.asarray(im).copy()
    x0, y0 = max(0, ox), max(0, oy)
    x1, y1 = min(im.width, ox + m.width), min(im.height, oy + m.height)
    if x1 <= x0 or y1 <= y0: return
    sm = np.asarray(m)[y0-oy:y1-oy, x0-ox:x1-ox].astype(np.float32) / 255.0
    arr[y0:y1, x0:x1, 3] = (arr[y0:y1, x0:x1, 3].astype(np.float32) * (1 - sm)).astype(np.uint8)
    im.paste(Image.fromarray(arr, 'RGBA'))


def _text_mask(text, f, track, grow, blur, pad_extra=8):
    tw = line_width(f, text, track)
    asc, desc = f.getmetrics()
    pad = grow * 3 + pad_extra
    W, H = int(tw) + pad * 2, asc + desc + pad * 2
    m = Image.new('L', (W, H), 0)
    md = ImageDraw.Draw(m)
    x = pad
    for ch in text:
        b = f.getbbox(ch)
        md.text((x - b[0], pad - b[1]), ch, font=f, fill=255)
        x += adv(f, ch) + track
    if grow:
        m = m.filter(ImageFilter.MaxFilter(grow * 2 + 1))
    if blur:
        m = m.filter(ImageFilter.GaussianBlur(blur))
    return m, W, H, asc, desc


def draw_line_ko(im, text, cx, y_mid, f, ink, track=0, grow=7, blur=1.2):
    """横排:挖空 + 墨字"""
    if not text: return
    m, W, H, asc, desc = _text_mask(text, f, track, grow, blur)
    ox, oy = int(round(cx - W / 2)), int(round(y_mid - H / 2))
    _ko(im, m, ox, oy)
    draw_line(im, text, cx, y_mid, f, tuple(ink) + (255,), track)


def draw_arc_ko(im, text, cx, cy, radius, f, ink, track_deg=0.0, center_deg=270.0,
                flip=False, grow=5, blur=1.1):
    """弧形:逐字挖空 + 逐字墨字"""
    if flip: text = text[::-1]
    n = len(text)
    if not n: return
    track_px = track_deg * math.pi / 180 * radius
    widths = [adv(f, c) for c in text]
    total_px = sum(widths) + track_px * (n - 1)
    start = center_deg - math.degrees(total_px / radius) / 2
    acc = 0.0
    items = []
    for i, ch in enumerate(text):
        w = widths[i]
        mid = start + math.degrees((acc + w / 2) / radius)
        acc += w + track_px
        a = math.radians(mid)
        x, y = cx + radius * math.cos(a), cy + radius * math.sin(a)
        rot = (mid - 270.0) if not flip else (mid - 90.0)
        b = f.getbbox(ch)
        cw, chh = max(1, b[2]-b[0]), max(1, b[3]-b[1])
        pad = 14 + grow * 3
        tile = Image.new('RGBA', (cw + pad*2, chh + pad*2), (0,0,0,0))
        ImageDraw.Draw(tile).text((pad - b[0], pad - b[1]), ch, font=f, fill=(0,0,0,255))
        items.append((ch, tile, x, y, rot, b, pad))
    for ch, tile, x, y, rot, b, pad in items:
        m = tile.split()[3].copy()
        m = m.filter(ImageFilter.MaxFilter(grow*2+1)).filter(ImageFilter.GaussianBlur(blur))
        mr = m.rotate(-rot, resample=Image.BICUBIC, expand=True)
        _ko(im, mr, int(round(x - mr.width/2)), int(round(y - mr.height/2)))
    for ch, tile, x, y, rot, b, pad in items:
        t2 = Image.new('RGBA', tile.size, (0,0,0,0))
        ImageDraw.Draw(t2).text((pad - b[0], pad - b[1]), ch, font=f, fill=tuple(ink)+(255,))
        t3 = t2.rotate(-rot, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(t3, (int(round(x - t3.width/2)), int(round(y - t3.height/2))))
