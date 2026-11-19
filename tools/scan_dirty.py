#!/usr/bin/env python3
"""扫描已生成的成品, 找出「透明底不干净」的站。

判据: 四角 8x8 区块的中位 alpha > 0 即视为脏底。
（正常成品角 alpha 必为 0；脏底来自 AI 假透明(alpha≈14) 被当成原生 alpha 后算成 1~3。）

用法:
  python3 tools/scan_dirty.py --stamps stamps --out tools/redo.json [--workers 8]
"""
import argparse
import json
import os
import re
import sys
from multiprocessing import Pool

import numpy as np
from PIL import Image

SID = re.compile(r'st(\d{5})\.text\.png$')


def corner_median(path):
    """返回四角 8x8 区块的 alpha 中位数（取最大的一角）。"""
    try:
        im = Image.open(path)
        im.load()
        a = np.asarray(im.convert('RGBA'))[..., 3]
    except Exception:
        return None
    h, w = a.shape
    k = 8
    corners = (a[:k, :k], a[:k, w - k:], a[h - k:, :k], a[h - k:, w - k:])
    return max(float(np.median(c)) for c in corners)


def scan_one(path):
    m = SID.search(os.path.basename(path))
    if not m:
        return None
    med = corner_median(path)
    if med is None or med <= 0:
        return None
    return int(m.group(1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stamps', default='stamps')
    ap.add_argument('--out', default='tools/redo.json')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--limit', type=int, default=0, help='仅扫前 N 个(调试用)')
    a = ap.parse_args()

    paths = []
    for root, _, files in os.walk(a.stamps):
        for f in files:
            if f.endswith('.text.png'):
                paths.append(os.path.join(root, f))
    paths.sort()
    if a.limit:
        paths = paths[:a.limit]
    print(f'待扫描 {len(paths)} 张成品', flush=True)

    dirty = []
    step = max(1, len(paths) // 20)
    with Pool(a.workers) as pool:
        for i, sid in enumerate(pool.imap_unordered(scan_one, paths, chunksize=32)):
            if sid is not None:
                dirty.append(sid)
            if (i + 1) % step == 0:
                print(f'  {i + 1}/{len(paths)} | 脏底 {len(dirty)}', flush=True)

    dirty.sort()
    print(f'\u626b\u63cf\u5b8c\u6210: \u810f\u5e95 {len(dirty)} / {len(paths)}', flush=True)
    if dirty:
        os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(dirty, f)
        print(f'\u6e05\u5355\u5df2\u5199\u5165 {a.out}', flush=True)
    elif os.path.exists(a.out):
        os.remove(a.out)
        print('\u5168\u90e8\u5e72\u51c0, \u5df2\u79fb\u9664\u65e7\u6e05\u5355', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
