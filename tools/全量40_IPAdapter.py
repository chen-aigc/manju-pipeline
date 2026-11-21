# -*- coding: utf-8 -*-
"""40 镜全量：SDXL + IP-Adapter（按镜头自动选参考图）

流程：
    上传所有参考图到 ComfyUI input
    → 每镜按 shot_type/characters 选参考图
    → build_sdxl_workflow(ref_image=...)
    → 生成、保存、记录
"""
import sys
import os
import json
import time
import urllib.request
import urllib.parse

sys.path.insert(0, r'D:\deepseek文件区\manju\src')
sys.stdout.reconfigure(encoding='utf-8')

from manju.image_gen import (build_prompts, build_sdxl_workflow,
                             ref_image_for_shot, list_char_refs, is_alive)
import manju.config as config

HOST = 'http://127.0.0.1:8188'
OUT = r'D:\deepseek文件区\manju\_SDXL_IPAdapter_40_v2'
META = os.path.join(OUT, '_meta.json')
SCRIPT = r'D:\deepseek文件区\manju\projects\断剑月白\chapter_001\script_优化版2.json'
os.makedirs(OUT, exist_ok=True)

W, H = config.SDXL_DRAFT_W, config.SDXL_DRAFT_H
WEIGHT_CHAR = 0.75      # 单人
WEIGHT_PAIR = 0.70      # 双人


def post(path, payload):
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def get(path):
    return json.loads(urllib.request.urlopen(HOST + path, timeout=30).read())


def upload_image(src_path, name):
    with open(src_path, 'rb') as f:
        content = f.read()
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    body = b''
    body += ('--%s\r\n' % boundary).encode()
    body += ('Content-Disposition: form-data; name="image"; filename="%s"\r\n'
             % name).encode()
    body += b'Content-Type: image/png\r\n\r\n'
    body += content
    body += b'\r\n'
    body += ('--%s\r\n' % boundary).encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += ('--%s--\r\n' % boundary).encode()
    req = urllib.request.Request(
        HOST + '/upload/image', data=body,
        headers={'Content-Type': 'multipart/form-data; boundary=%s' % boundary})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def run(name, wf):
    t0 = time.time()
    r = post('/prompt', {'prompt': wf})
    pid = r.get('prompt_id')
    if not pid:
        return None, '提交失败: ' + str(r)[:150]
    for _ in range(900):
        time.sleep(2)
        try:
            hh = get('/history/%s' % pid)
        except Exception:
            continue
        if pid in hh:
            st = hh[pid].get('status', {})
            if st.get('status_str') == 'error':
                msg = ''
                for m in st.get('messages', []):
                    if m[0] == 'execution_error':
                        msg = '%s: %s' % (m[1].get('node_type'),
                                          str(m[1].get('exception_message'))[:120])
                return None, msg
            imgs = hh[pid].get('outputs', {}).get('9', {}).get('images', [])
            if not imgs:
                return None, '无输出图'
            i = imgs[0]
            q = urllib.parse.urlencode({'filename': i['filename'],
                                        'subfolder': i.get('subfolder', ''),
                                        'type': i.get('type', 'output')})
            dst = os.path.join(OUT, name + '.png')
            urllib.request.urlretrieve('%s/view?%s' % (HOST, q), dst)
            return dst, round(time.time() - t0, 1)
    return None, '超时'


if __name__ == '__main__':
    if not is_alive():
        print('ComfyUI 没启动（需 --lowvram）')
        sys.exit(1)

    j = json.load(open(SCRIPT, encoding='utf-8'))
    shots = j['shots']

    # 1) 上传所有参考图
    refs, pair = list_char_refs()
    if not refs:
        print('没找到角色参考图，请检查', config.CHARS_DIR)
        sys.exit(1)
    up_map = {}
    for cname, p in refs.items():
        n = upload_image(p, 'ref_%s.png' % cname)
        up_map[cname] = n.get('name', 'ref_%s.png' % cname)
    pair_name = None
    if pair:
        pair_name = upload_image(pair, 'ref_pair.png').get('name', 'ref_pair.png')

    print('=' * 76)
    print('40 镜全量：SDXL + IP-Adapter')
    print('  角色参考: %s' % ', '.join('%s→%s' % kv for kv in up_map.items()))
    print('  双人参考: %s' % pair_name)
    print('  weight: 单人 %.2f / 双人 %.2f' % (WEIGHT_CHAR, WEIGHT_PAIR))
    print('=' * 76)

    # 断点续跑
    done = {}
    if os.path.exists(META):
        for r in json.load(open(META, encoding='utf-8')):
            if r.get('ok') and os.path.exists(r.get('path', '')):
                done[r['index']] = r
    if done:
        print('已完成 %d 张，继续补齐' % len(done))

    meta = list(done.values())
    t_all = time.time()

    for s in shots:
        idx = s['index']
        if idx in done:
            continue
        tag = ref_image_for_shot(s)
        if tag is None:
            ref = None
            weight = 0.0
        elif tag == '__PAIR__':
            ref = pair_name
            weight = WEIGHT_PAIR
        else:
            cname = tag.replace('__CHAR__', '')
            ref = up_map.get(cname)
            weight = WEIGHT_CHAR

        pos, neg = build_prompts(s)
        wf = build_sdxl_workflow(ref_image=ref)
        if '13' in wf:
            wf['13']['inputs']['weight'] = weight
        wf["6"]["inputs"]["text"] = pos
        wf["7"]["inputs"]["text"] = neg
        wf["3"]["inputs"]["seed"] = config.SDXL_BASE_SEED + idx * config.SDXL_SEED_STRIDE
        wf["5"]["inputs"]["width"] = W
        wf["5"]["inputs"]["height"] = H

        rec = {'index': idx, 'scene': s['scene'], 'shot_type': s['shot_type'],
               'characters': s.get('characters') or [], 'ref': ref,
               'weight': weight, 'words': len(pos.split()),
               'positive': pos, 'ok': False, 'path': None,
               'seconds': None, 'error': None}

        print('[%2d/40] #%-2d %-8s %-5s ref=%-16s' % (
            len(done) + 1, idx, s['scene'], s['shot_type'], ref or '无'))
        p, info = run('shot%02d' % idx, wf)
        if p:
            rec.update(ok=True, path=p, seconds=info)
            print('        OK %.0fs' % info)
        else:
            rec['error'] = info
            print('        FAIL', str(info)[:110])
        meta.append(rec)
        json.dump(sorted(meta, key=lambda x: x['index']),
                  open(META, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    ok = [m for m in meta if m['ok']]
    print()
    print('=' * 76)
    print('完成 %d/40   %.1f 分钟   平均 %.1f 秒/张' % (
        len(ok), (time.time() - t_all) / 60,
        sum(m['seconds'] or 0 for m in ok) / max(1, len(ok))))
    print('输出:', OUT)
