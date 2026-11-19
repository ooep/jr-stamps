#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_jr_stamps.py — 车站印章批量生产 → 推送到 ooep/jr-stamps

仓库结构（对齐 JR-images 的 draw_img 布局）:
    stamps/<NN_都道府県>/st<物理站ID>.png        无字透明素材
    stamps/<NN_都道府県>/st<物理站ID>.text.png   加字成品

流程:
    1. 取站图(JR-images draw_img, GitHub blobs API)
    2. Lupi 图生图 → 印章图案(async 任务轮询, 审查拦截自动降级重试)
    3. stamp_render 排版(下弧站名 + 上弧运营方 + MiniJapanRail 署名, 自动配色)
    4. 1024px / 32 色调色板 PNG(约 0.24MB/张)
    5. 每 BATCH 张用 Git Data API 合成 1 个 commit 推送, 随后删除本地文件

环境变量:
    LUPI_KEY    必需, Lupi API Key
    GH_TOKEN    必需, GitHub token(repo 权限)

用法:
    export LUPI_KEY='...' GH_TOKEN='...'
    python3 build_jr_stamps.py --limit 10            # 只做 10 站
    python3 build_jr_stamps.py                       # 全量(可多次运行, 自动跳过已完成)
    python3 build_jr_stamps.py --workers 3 --batch 5
"""
import argparse, base64, csv, io, json, math, os, sys, time
import urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PIL import Image
from stamp_render import render_v2
try:
    from line_theme import apply as apply_line_theme
except ImportError:                       # 工具目录不在 sys.path 时
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from line_theme import apply as apply_line_theme

GH_OWNER, GH_REPO = 'ooep', 'jr-stamps'
GH_API = 'https://api.github.com'
LUPI = 'https://lupi.mlgb7.com'
JRIMG_TREE = '2ff7d450c0915d5abf53b7a8c7b75108badf7218'   # JR-images draw_img 子树
BRAND = 'MiniJapanRail'
SIZE_OUT = 1024
PALETTE = 32

PROMPT = ('将这张车站插画改绘成一枚复古风格的圆形印章图案。'
          '极其重要：背景必须完全透明（PNG 透明通道，alpha=0），'
          '不要纸底、不要米色/白色/任何纯色背景，只有印章图案本身有颜色。'
          '深蓝色油墨的线条与色块刻画车站建筑与场景，天空中一枚小小的朱红色圆点作为点缀，'
          '油墨浓淡略有变化，带有手工盖章的粗糙质感；外围有圆形双线边框，'
          '画面中不要出现任何文字、字母或数字。正方形构图。')
PROMPT_FALLBACK = ('把参考图改绘成深蓝色单色的日式橡皮印章图案，木刻版画线条，带圆形边框，'
                   '背景完全透明（PNG alpha 透明，无任何底色），画面中不要出现任何文字。正方形构图。')

# 运营方官方名 → 印章用短名
# JR 六社属地(用于运营方消歧)
JR_TERRITORY = {
    '北海道旅客鉄道': {'北海道'},
    '東日本旅客鉄道': {'東京都', '神奈川県', '埼玉県', '千葉県', '茨城県', '栃木県', '群馬県',
                       '山梨県', '新潟県', '長野県', '山形県', '宮城県', '福島県', '岩手県',
                       '青森県', '秋田県', '静岡県'},
    '東海旅客鉄道': {'愛知県', '岐阜県', '静岡県', '三重県', '長野県', '滋賀県', '京都府',
                     '大阪府', '兵庫県'},
    '西日本旅客鉄道': {'福井県', '石川県', '富山県', '滋賀県', '京都府', '大阪府', '兵庫県',
                       '奈良県', '和歌山県', '三重県', '岡山県', '広島県', '山口県',
                       '鳥取県', '島根県', '福岡県'},
    '四国旅客鉄道': {'香川県', '徳島県', '愛媛県', '高知県'},
    '九州旅客鉄道': {'福岡県', '佐賀県', '長崎県', '熊本県', '大分県', '宮崎県', '鹿児島県'},
}

OP_SHORT = {
    '北海道旅客鉄道': 'JR北海道', '東日本旅客鉄道': 'JR東日本', '東海旅客鉄道': 'JR東海',
    '西日本旅客鉄道': 'JR西日本', '四国旅客鉄道': 'JR四国', '九州旅客鉄道': 'JR九州',
    '東京地下鉄': '東京メトロ', '東京都': '都営交通',
    '大阪市高速電気軌道': 'Osaka Metro', '名古屋市': '名古屋市営地下鉄',
    '札幌市': '札幌市営地下鉄', '福岡市': '福岡市地下鉄', '神戸市': '神戸市営地下鉄',
    '京都市': '京都市営地下鉄', '横浜市': '横浜市営地下鉄', '仙台市': '仙台市地下鉄',
    '近畿日本鉄道': '近鉄', '名古屋鉄道': '名鉄', '京浜急行電鉄': '京急',
    '京成電鉄': '京成', '京王電鉄': '京王', '小田急電鉄': '小田急',
    '東急電鉄': '東急', '相模鉄道': '相鉄', '西武鉄道': '西武',
    '東武鉄道': '東武', '阪急電鉄': '阪急', '阪神電気鉄道': '阪神',
    '京阪電気鉄道': '京阪', '南海電気鉄道': '南海', '西日本鉄道': '西鉄',
    '山陽電気鉄道': '山陽電車', '神戸電鉄': '神戸電鉄',
}
OP_SUFFIX = ('旅客鉄道株式会社', '株式会社')


def http(url, data=None, headers=None, method=None, timeout=180, raw=False, tries=4):
    """带网络重试(SSL 握手超时/连接中断自动重试)"""
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={'User-Agent': 'jr-stamps-builder/1.0', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise                                   # 业务错误不重试
        except Exception as e:                      # 网络类错误重试
            last = e
            time.sleep(2 + i * 3)
    raise last


def gh(path, data=None, method=None, token=None):
    hdr = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}
    body = json.dumps(data).encode() if data is not None else None
    if body:
        hdr['Content-Type'] = 'application/json'
    out = http(GH_API + path, data=body, headers=hdr, method=method)
    return json.loads(out) if out else {}


def op_short(name):
    if not name:
        return ''
    if name in OP_SHORT:
        return OP_SHORT[name]
    for suf in OP_SUFFIX:
        if name.endswith(suf):
            name = name[:-len(suf)]
    return name


# ───────────── Lupi ─────────────
class KeyPool:
    """多账号 Lupi key 轮转, 让并发可以按 key 数量线性扩容"""

    def __init__(self, keys):
        self.keys = [k for k in keys if k]
        self._i = 0
        self._lock = threading.Lock()

    def __len__(self):
        return len(self.keys)

    def get(self):
        with self._lock:
            if not self.keys:
                raise RuntimeError('无可用 Lupi key')
            k = self.keys[self._i % len(self.keys)]
            self._i += 1
            return k


def lupi_edit(key, art_bytes, prompt):
    boundary = '----b' + str(int(time.time() * 1000))
    parts = []
    for k, v in (('model', 'gpt-image-2.5'), ('prompt', prompt), ('n', '1'),
                 ('size', '1024x1024'), ('quality', 'high'),
                 ('background', 'transparent'), ('output_format', 'png'),
                 ('response_format', 'url'), ('async', 'true')):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="s.jpg"\r\n'
                  f'Content-Type: image/jpeg\r\n\r\n').encode() + art_bytes + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(parts)
    out = http(LUPI + '/v1/images/edits', data=body, timeout=180,
               headers={'Authorization': 'Bearer ' + key,
                        'Content-Type': f'multipart/form-data; boundary={boundary}'})
    d = json.loads(out)
    return d.get('task_id') or d.get('id')


def lupi_poll(key, tid, max_wait=900, interval=6):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        raw = http(LUPI + '/api/image-tasks?ids=' + tid, timeout=60,
                   headers={'Authorization': 'Bearer ' + key})
        it = (json.loads(raw).get('items') or [{}])[0]
        if it.get('terminal'):
            return it.get('status'), it
        time.sleep(interval)
    return 'timeout', {}


_CONSEC_FAIL = 0          # 连续失败计数(熔断用)


def gen_art(key, art_bytes, retries=4):
    """上游不稳定: 多轮重试 + 指数退避; 连续失败则整体退避"""
    global _CONSEC_FAIL
    chain = [PROMPT] * (retries + 1) + [PROMPT_FALLBACK] * 2
    for i, prompt in enumerate(chain):
        try:
            tid = lupi_edit(key, art_bytes, prompt)
            st, it = lupi_poll(key, tid)
            if st == 'success':
                url = (it.get('results') or [{}])[0].get('url')
                _CONSEC_FAIL = 0
                return http(url, timeout=240)
            print(f'      [{st} {i+1}/{len(chain)}] 重试', flush=True)
        except Exception as e:
            print(f'      [err {str(e)[:50]} {i+1}/{len(chain)}] 重试', flush=True)
        _CONSEC_FAIL += 1
        if _CONSEC_FAIL >= 6:                  # 上游整体故障 → 长退避
            wait = 120
            print(f'      连续失败 {_CONSEC_FAIL} 次, 全局退避 {wait}s', flush=True)
            time.sleep(wait)
            _CONSEC_FAIL = 0
        else:
            time.sleep(4 + i * 4)
    return None


# ───────────── 单站生产 ─────────────
def make_stamp(row, key_pool, gh_token, draw_sha, outdir):
    sid, name = row['id'], row['name']
    pdir = row['pdir']
    outp = os.path.join(outdir, pdir)
    os.makedirs(outp, exist_ok=True)
    plain = os.path.join(outp, f'{row["slug"]}.png')
    text = os.path.join(outp, f'{row["slug"]}.text.png')
    if os.path.exists(text):
        return None

    art_url = f'{GH_API}/repos/ooep/JR-images/git/blobs/{draw_sha[row["path"]]}'
    art = http(art_url, headers={'Authorization': 'Bearer ' + gh_token,
                                 'Accept': 'application/vnd.github.raw'}, timeout=180)
    ai = os.path.join('/tmp', f'ai_{sid}.png')
    with open(ai, 'wb') as f:
        f.write(art)
    try:
        png = gen_art(key_pool.get(), art)
        if not png:
            raise RuntimeError('gen_failed')
        aip = os.path.join(outdir, f'.ai_{sid}.png')
        with open(aip, 'wb') as f:
            f.write(png)
        render_v2(aip, os.path.join(outp, row['slug']), op_short(row['op']), name,
                  brand=BRAND, size=SIZE_OUT, layout='arc', brand_pos='below',
                  line_color=row.get('line_color'))
        os.remove(aip)
        # 调色板压缩
        for p in (plain, text):
            im = Image.open(p).convert('RGBA')
            im.quantize(colors=PALETTE, method=Image.FASTOCTREE).save(p, optimize=True)
        return (row, plain, text)
    finally:
        if os.path.exists(ai):
            os.remove(ai)


# ───────────── 推送 ─────────────
def push_contents(token, rpath, lpath, message):
    """Contents API 单文件提交(空仓库引导用)"""
    content = base64.b64encode(open(lpath, 'rb').read()).decode()
    body = {'message': message, 'content': content, 'branch': 'main'}
    q = urllib.parse.quote(rpath, safe='/')
    try:
        cur = gh(f'/repos/{GH_OWNER}/{GH_REPO}/contents/{q}?ref=main', token=token)
        body['sha'] = cur['sha']
    except (urllib.error.HTTPError, KeyError):
        pass
    gh(f'/repos/{GH_OWNER}/{GH_REPO}/contents/{q}', body, method='PUT', token=token)


def push_batch(token, items, dry=False):
    """items: [(repo_path, local_file), ...] → 单个 commit"""
    if not items or dry:
        return 0
    try:
        ref = gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/ref/heads/main', token=token)
        base_sha = ref['object']['sha']
        head = gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/commits/{base_sha}', token=token)
        base_tree = head['tree']['sha']
        parents = [base_sha]
    except urllib.error.HTTPError as e:
        if e.code not in (404, 409):
            raise
        # 空仓库: 先用 Contents API 建首提交, 再走正常批量流程
        first = True
        for rpath, lpath in items:
            if first:
                push_contents(token, rpath, lpath, 'init stamps')
                first = False
            else:
                push_contents(token, rpath, lpath, 'add stamp')
        return len(items)

    tree = []
    for rpath, lpath in items:
        blob = gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/blobs',
                  {'content': base64.b64encode(open(lpath, 'rb').read()).decode(),
                   'encoding': 'base64'}, method='POST', token=token)
        tree.append({'path': rpath, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    t = gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/trees',
           {'tree': tree, **({'base_tree': base_tree} if base_tree else {})},
           method='POST', token=token)
    c = gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/commits',
           {'message': f'add {len(items)} stamps', 'tree': t['sha'], 'parents': parents},
           method='POST', token=token)
    if parents:
        gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/refs/heads/main',
           {'sha': c['sha']}, method='PATCH', token=token)
    else:
        gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/refs', {'ref': 'refs/heads/main', 'sha': c['sha']},
           method='POST', token=token)
    return len(items)



# ───────────── 线路主题 ─────────────
SEG_URL = (f'{GH_API}/repos/ooep/floral-salad-8734/contents/'
           'mini-japanrail-3d/data/segments3d.geojson')


def ensure_coords(rows, token):
    """老清单没有经纬度时补上 —— 线路消歧依赖坐标, 否则会跨县串线"""
    lack = [r for r in rows if r.get('lon') is None or r.get('lat') is None]
    if not lack:
        return True
    try:
        sa = json.loads(http(f'{GH_API}/repos/ooep/JR-images/contents/data/stations_all.json',
                             headers={'Authorization': 'Bearer ' + token,
                                      'Accept': 'application/vnd.github.raw'}, timeout=180))['stations']
    except Exception as e:
        print('  坐标补全失败(%s), 线路消歧降级' % str(e)[:50], flush=True)
        return False
    pos = {x['id']: (x.get('lon'), x.get('lat')) for x in sa}
    for r in rows:
        if r.get('lon') is None or r.get('lat') is None:
            lon, lat = pos.get(r.get('id'), (None, None))
            r['lon'], r['lat'] = lon, lat
    got = sum(1 for r in rows if r.get('lon') is not None)
    print('  坐标补全: %d/%d 站' % (got, len(rows)), flush=True)
    return True


def enrich_lines(rows, cache_dir, token):
    """给每站补 line / line_color; segments3d 本地缓存, 避免每轮重下 11MB"""
    ensure_coords(rows, token)
    seg = os.path.join(cache_dir, 'segments3d.json')
    if os.path.exists(seg) and os.path.getsize(seg) > 1 << 20:
        try:
            g = json.load(open(seg, encoding='utf-8'))
        except Exception:
            g = None
    else:
        g = None
    if g is None:
        try:
            print('拉取线路段数据(segments3d)...', flush=True)
            raw = http(SEG_URL, headers={'Authorization': 'Bearer ' + token,
                                         'Accept': 'application/vnd.github.raw'}, timeout=300)
            g = json.loads(raw)
            os.makedirs(cache_dir, exist_ok=True)
            json.dump(g, open(seg, 'w', encoding='utf-8'), ensure_ascii=False)
        except Exception as e:
            keep = sum(1 for r in rows if r.get('line_color'))
            print('  线路数据不可用(%s), 保留已有配色 %d 站' % (str(e)[:60], keep), flush=True)
            for r in rows:
                r.setdefault('line', '')
                r.setdefault('line_color', '#1F3A66')
            return rows
    rows, stat = apply_line_theme(rows, g)
    print('线路主题: %d/%d 站匹配到线路 (%.0f%%)'
          % (stat['with_line'], stat['total'], stat['with_line'] * 100.0 / max(1, stat['total'])), flush=True)
    return rows


# ───────────── 自举: 构建站点清单 ─────────────
def prepare(cache_dir, token):
    """从 JR-images + 主站数据构建 worklist.json / drawsha.json"""
    os.makedirs(cache_dir, exist_ok=True)
    wl, ds = os.path.join(cache_dir, 'worklist.json'), os.path.join(cache_dir, 'drawsha.json')
    if os.path.exists(wl) and os.path.exists(ds):
        print('清单已存在:', wl)
        try:
            rows = json.load(open(wl, encoding='utf-8'))
        except Exception:
            rows = None
        if rows:
            before = {(r.get('id'), r.get('line_color')) for r in rows}
            rows = enrich_lines(rows, cache_dir, token)
            after = {(r.get('id'), r.get('line_color')) for r in rows}
            if before != after:
                changed = len(after - before) if len(after) >= len(before) else len(before - after)
                print('  配色规则更新, %d 站线路色已刷新' % max(changed, 1), flush=True)
                json.dump(rows, open(wl, 'w'), ensure_ascii=False)
        return wl, ds

    print('拉取 JR-images 站点表...', flush=True)
    sa = json.loads(http(f'{GH_API}/repos/ooep/JR-images/contents/data/stations_all.json',
                         headers={'Authorization': 'Bearer ' + token,
                                  'Accept': 'application/vnd.github.raw'}, timeout=180))['stations']
    print('拉取 draw_img 文件树...', flush=True)
    tree = None
    for i in range(4):
        try:
            tree = json.loads(http(f'{GH_API}/repos/ooep/JR-images/git/trees/{JRIMG_TREE}?recursive=1',
                                   headers={'Authorization': 'Bearer ' + token}, timeout=240))
            break
        except Exception as e:
            print('  retry', i, str(e)[:60]); time.sleep(4)
    sha = {x['path']: x['sha'] for x in tree['tree'] if x['type'] == 'blob'}
    print('拉取线路别名表...', flush=True)
    lines_map = json.loads(http(f'{GH_API}/repos/ooep/floral-salad-8734/contents/'
                                'mini-japanrail-3d/data/lines_map.json',
                                headers={'Authorization': 'Bearer ' + token,
                                         'Accept': 'application/vnd.github.raw'}, timeout=180))
    print('拉取站点运营方映射...', flush=True)
    g3 = json.loads(http(f'{GH_API}/repos/ooep/floral-salad-8734/contents/'
                         'mini-japanrail-3d/data/stations3d.geojson',
                         headers={'Authorization': 'Bearer ' + token,
                                  'Accept': 'application/vnd.github.raw'}, timeout=280))['features']

    pref_dir = {}
    for path in sha:
        d = path.split('/')[0]
        if '_' in d and d.split('_')[0].isdigit():
            pref_dir[d.split('_', 1)[1]] = d

    # 站点名 -> [(运营方, lon, lat)]  坐标匹配兜底
    byname = {}
    line_op = {}
    for f in g3:
        pr = f['properties']
        nm, op, ln = pr.get('station'), pr.get('op'), pr.get('line')
        if nm and op:
            byname.setdefault(nm, []).append((op, f['geometry']['coordinates']))
        if op and ln:
            line_op.setdefault(ln, {})[op] = line_op.get(ln, {}).get(op, 0) + 1
    line_op_top = {k: max(v, key=v.get) for k, v in line_op.items()}
    short2full = {}
    for full, shorts in lines_map.items():
        for s2 in shorts:
            short2full.setdefault(s2, full)

    def resolve_op(st):
        """优先级: 属地校验过的 JR 线路票 > 坐标同名匹配 > 普通线路票

        说明: 部分线路名(如 '函館本線')在两个数据源之间没有别名映射,
        此时 JR 票为空, 直接采用票数会被共站的私铁顶掉(函館→道南いさりび),
        所以先回退到坐标同名匹配。
        """
        votes = {}
        for l in st.get('lines') or []:
            for k in (l, short2full.get(l, l)):
                if k in line_op_top:
                    votes[line_op_top[k]] = votes.get(line_op_top[k], 0) + 1
                    break
        pref = st.get('pref')
        for n, o in sorted(((n, o) for o, n in votes.items() if o in JR_TERRITORY),
                           key=lambda t: -t[0]):
            if pref in JR_TERRITORY[o]:
                return o
        cands = byname.get(st['name_key']) or byname.get(st['name']) or []
        if len(cands) > 1:
            cands = sorted(cands, key=lambda c: math.hypot(c[1][0] - st['lon'], c[1][1] - st['lat']))
        if cands:
            return cands[0][0]
        return max(votes, key=votes.get) if votes else ''

    rows, miss = [], 0
    for st in sa:
        pdir = pref_dir.get(st.get('pref') or '')
        path = f'{pdir}/st{st["id"]:05d}.jpg' if pdir else None
        if not path or path not in sha:
            miss += 1; continue
        rows.append(dict(id=st['id'], slug=st['slug'], name=st['name_key'], pref=st['pref'],
                         pdir=pdir, path=path, op=resolve_op(st),
                         lon=st.get('lon'), lat=st.get('lat'),
                         riders=st.get('riders') or 0))
    rows.sort(key=lambda r: (r['pdir'], r['id']))
    rows = enrich_lines(rows, cache_dir, token)
    json.dump(rows, open(wl, 'w'), ensure_ascii=False)
    json.dump(sha, open(ds, 'w'))
    print(f'清单构建完成: {len(rows)} 站可用, 缺站图 {miss}')
    return wl, ds



def upstream_alive(key, art_path, timeout_min=5):
    """轻量探活: 固定参考图试生成 1 张"""
    if not os.path.exists(art_path):
        return None   # 无参考图则跳过探活(依赖 --fail-fast 保护)
    try:
        art = open(art_path, 'rb').read()
        tid = lupi_edit(key, art, PROMPT_FALLBACK)
        st, _ = lupi_poll(key, tid, max_wait=timeout_min * 60, interval=8)
        return st == 'success'
    except Exception as e:
        print('   探活异常:', str(e)[:60], flush=True)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', default=os.path.join(os.path.expanduser('~'), '.jr-stamps'),
                    help='清单与中间缓存目录')
    ap.add_argument('--prepare', action='store_true', help='仅构建站点清单后退出')
    ap.add_argument('--worklist', default='')
    ap.add_argument('--draw-sha', default='')
    ap.add_argument('--outdir', default='/tmp/jrstamps')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--batch', type=int, default=5)
    ap.add_argument('--offset', type=int, default=0)
    ap.add_argument('--prefer', default='riders', choices=['riders', 'id', 'none'],
                    help='排序: riders=客流优先(先做重要站)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--probe-only', action='store_true', help='仅探活后退出')
    ap.add_argument('--count-only', action='store_true', help='仅统计剩余站数后退出')
    ap.add_argument('--probe-art', default=os.path.join(HERE, '東京站_参考站图.jpg'))
    ap.add_argument('--fail-fast', action='store_true', help='首站失败即中止本轮')
    ap.add_argument('--force-list', default='',
                    help='JSON 数组(站 id): 忽略「远端已存在」强制重画, 并在推送时同步收缩该列表')
    a = ap.parse_args()

    raw_keys = os.environ.get('LUPI_KEYS') or os.environ.get('LUPI_KEY') or ''
    keys = [k.strip() for k in raw_keys.replace('\n', ',').replace(' ', ',').split(',') if k.strip()]
    key_pool = KeyPool(keys)
    token = os.environ.get('GH_TOKEN')
    if not keys or not token:
        sys.exit('E: 需要 LUPI_KEY(或 LUPI_KEYS) 与 GH_TOKEN 环境变量')

    cache = a.cache
    wl, ds = a.worklist, a.draw_sha
    if not wl or not ds:
        wl, ds = prepare(cache, token)
    if a.prepare:
        print('清单:', wl); return
    if a.probe_only:
        ok = upstream_alive(key_pool.get(), a.probe_art)
        print('上游探活:', {True: 'OK', False: 'FAIL', None: 'SKIP(无参考图)'}[ok], flush=True)
        sys.exit(0 if ok is not False else 1)

    rows = json.load(open(wl, encoding='utf-8'))
    if a.prefer == 'riders':
        rows.sort(key=lambda r: -(r.get('riders') or 0))
    draw_sha = json.load(open(ds))
    os.makedirs(a.outdir, exist_ok=True)

    # 先跳过已推送(远端已有 .text.png), 再取 offset/limit
    done = set()
    try:
        t = gh(f'/repos/{GH_OWNER}/{GH_REPO}/git/trees/main?recursive=1', token=token)
        done = {x['path'] for x in t.get('tree', []) if x['path'].endswith('.text.png')}
        print(f'远端已有 {len(done)} 张成品', flush=True)
    except Exception:
        print('远端为空仓库', flush=True)
    forced = set()
    if a.force_list and os.path.exists(a.force_list):
        try:
            forced = {int(x) for x in json.load(open(a.force_list, encoding='utf-8'))}
            print(f'重画清单: {len(forced)} 站(忽略已有成品)', flush=True)
        except Exception as e:
            print('  重画清单读取失败(%s), 忽略' % str(e)[:50], flush=True)
            forced = set()
    rows = [r for r in rows
            if f'stamps/{r["pdir"]}/{r["slug"]}.text.png' not in done or r['id'] in forced]
    if a.count_only:
        print(f'待生产 {len(rows)} 站')
        return
    rows = rows[a.offset:]
    if a.limit:
        rows = rows[:a.limit]
    print(f'待生产 {len(rows)} 站, workers={a.workers}, batch={a.batch}', flush=True)

    pending, done_cnt, fail, pushed_total = [], 0, 0, 0
    pending_forced = set()      # 本批完成的强制重画站, 推送成功后从清单剔除

    def flush(items, tag=''):
        """推送 + 同步收缩重画清单(清单本身也进同一个 commit, 自终止)"""
        nonlocal forced
        items = list(items)
        tmp = None
        if a.force_list and pending_forced and not a.dry_run:
            left = sorted(forced - pending_forced)
            tmp = os.path.join(a.outdir, '_redo.json')
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(left, f)
                items.append((a.force_list.replace(os.sep, '/'), tmp))
            except OSError:
                tmp = None
        n = push_batch(token, items, a.dry_run)
        print(f'  \u2191 {tag}\u63a8\u9001 {n} \u4e2a\u6587\u4ef6', flush=True)
        forced -= pending_forced
        pending_forced.clear()
        # 关键: 同步写回本地清单, 下一轮(新进程)才能读到收缩后的结果
        if a.force_list and not a.dry_run:
            try:
                with open(a.force_list, 'w', encoding='utf-8') as f:
                    json.dump(sorted(forced), f)
            except OSError:
                pass
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
        if a.force_list:
            print(f'  重画清单剩余 {len(forced)} 站', flush=True)
        if not a.dry_run:
            for _, p_ in items:
                if p_ == tmp:
                    continue
                try: os.remove(p_)
                except OSError: pass
        return n

    _ok_seen = False
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(make_stamp, r, key_pool, token, draw_sha, a.outdir): r for r in rows}
        for fu in as_completed(futs):
            r = futs[fu]
            try:
                res = fu.result()
                if res is None:
                    continue
                row, plain, text = res
                pending.append((f'stamps/{row["pdir"]}/{row["slug"]}.png', plain))
                pending.append((f'stamps/{row["pdir"]}/{row["slug"]}.text.png', text))
                if row['id'] in forced:
                    pending_forced.add(row['id'])
                done_cnt += 1
                _ok_seen = True
                el = time.time() - t_start
                print(f'[OK {done_cnt}] {row["name"]} ({row["pref"]}) op={op_short(row["op"])} '
                      f'| 累计 {el/60:.1f} 分 | 均 {el/max(1,done_cnt):.0f}s/站', flush=True)
            except Exception as e:
                fail += 1
                print(f'[FAIL] {r["name"]}: {str(e)[:80]}', flush=True)
                if a.fail_fast and not _ok_seen:
                    print('  fail-fast: 上游疑似故障, 中止本轮', flush=True)
                    for f2 in futs: f2.cancel()
                    break
                try:
                    with open(os.path.join(a.outdir, 'failed.json'), 'a', encoding='utf-8') as ft:
                        ft.write(json.dumps({'id': r['id'], 'name': r['name'],
                                             'why': str(e)[:120]}, ensure_ascii=False) + '\n')
                except OSError:
                    pass

            if len(pending) >= a.batch * 2:
                try:
                    pushed_total += flush(pending)
                    pending = []
                except Exception as e:
                    print(f'  ✗ 推送失败({str(e)[:60]}), 文件保留待重试', flush=True)

    if pending or (a.force_list and pending_forced):
        try:
            pushed_total += flush(pending, tag='收尾')
        except Exception as e:
            print(f'  ✗ 收尾推送失败({str(e)[:60]}), 文件保留', flush=True)
    print(f'完成 {done_cnt} 站, 失败 {fail}, 已推送文件 {pushed_total}', flush=True)


if __name__ == '__main__':
    main()
