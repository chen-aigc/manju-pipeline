# -*- coding: utf-8 -*-
"""头锁插件 v2：用「区域条件」代替 ControlNet 锁头部

原理：
    ControlNet（骨架）在当前模型上无效，
    但 ConditioningSetAreaPercentage 已验证有效（分区构图成功过）。

    所以把头部单独放一个区域条件：
        背景层   满幅
        头部层   限定在 (cx±w/2, cy±h/2) 的框里
    框的位置和大小来自同一套 profile，
    因此头会被"钉"在指定位置。

阶段 3 的蒙版也用同一 profile，坐标天然对齐。
"""
import sys
import os
import json
import time
import urllib.request
import urllib.parse

sys.stdout.reconfigure(encoding='utf-8')

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from manju.headlock.profile import get_profile, describe
from manju.headlock.masker import make_mask, head_box

HOST = 'http://127.0.0.1:8188'
CKPT = 'animagine-xl-4.0.safetensors'

W, H = 832, 1216
STEPS, CFG = 28, 7.0

STYLE = "masterpiece, best quality, very aesthetic, absurdres, anime style, detailed face, detailed eyes"
SOLID = "solid colors, opaque painting, clear lineart"

SCENE = ("old chinese room, mud walls, wooden beams, paper window, "
         "low wooden table, warm lamplight, interior, detailed background")

NEG = ("lowres, bad anatomy, bad hands, extra fingers, deformed, faceless, "
       "watermark, text, signature, logo, photo, photorealistic, 3d render, "
       "japanese room, tatami, torii, modern room, modern furniture, "
       "extra person, three people, crowd, multiple views, blurry, sketch")

YB = ("1girl, brown hair, low ponytail, gentle brown eyes, soft face, "
      "beige hanfu, cross-collar robe, serene expression")


def post(path, payload):
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def get(path):
    return json.loads(urllib.request.urlopen(HOST + path, timeout=30).read())


def upload_file(src, name):
    with open(src, 'rb') as f:
        content = f.read()
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    body = b''
    body += ('--%s\r\n' % boundary).encode()
    body += ('Content-Disposition: form-data; name="image"; filename="%s"\r\n'
             % name).encode()
    body += b'Content-Type: image/png\r\n\r\n' + content + b'\r\n'
    body += ('--%s\r\n' % boundary).encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += ('--%s--\r\n' % boundary).encode()
    req = urllib.request.Request(
        HOST + '/upload/image', data=body,
        headers={'Content-Type': 'multipart/form-data; boundary=%s' % boundary})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def area_for_head(prof):
    """把头部参数换算成 ConditioningSetAreaPercentage 的 (x, y, w, h)

    注意：区域条件控制的是"这个提示词在哪个框内生效"，
    框要比头大一圈，给脖子和肩膀留空间，否则会画出"孤零零一颗头"。
    """
    head_h = prof["size"]
    head_w = head_h * 0.78
    # 区域框：宽度 = 头宽的 3.2 倍（含肩），高度 = 头高的 3.0 倍（含上半身）
    fw = min(1.0, head_w * 3.2)
    fh = min(1.0, head_h * 3.0)
    x = max(0.0, prof["cx"] - fw / 2)
    y = max(0.0, min(1.0 - fh, prof["cy"] - head_h * 0.55))
    return x, y, fw, fh


def build_headlock(prompt, neg, seed, prof, ref_name=None,
                   ipa_weight=0.60, bg_strength=0.85, head_strength=1.25):
    """阶段1：头部用区域条件锁位置，背景满幅"""
    x, y, fw, fh = area_for_head(prof)

    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": W, "height": H, "batch_size": 1}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["4", 1]}},
        # 背景层：铺满整个画面
        "tB": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "%s, %s, %s, empty room, no people"
                                  % (STYLE, SOLID, SCENE), "clip": ["4", 1]}},
        "aB": {"class_type": "ConditioningSetAreaPercentage",
               "inputs": {"conditioning": ["tB", 0], "width": 1.0, "height": 1.0,
                          "x": 0.0, "y": 0.0, "strength": bg_strength}},
        # 头部层：限定在框内
        "tH": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "%s, %s, %s" % (STYLE, SOLID, prompt),
                          "clip": ["4", 1]}},
        "aH": {"class_type": "ConditioningSetAreaPercentage",
               "inputs": {"conditioning": ["tH", 0],
                          "width": fw, "height": fh,
                          "x": x, "y": y, "strength": head_strength}},
        "m1": {"class_type": "ConditioningCombine",
               "inputs": {"conditioning_1": ["aB", 0], "conditioning_2": ["aH", 0]}},
    }

    model_ref = ["4", 0]
    if ref_name:
        wf["10"] = {"class_type": "IPAdapterModelLoader",
                    "inputs": {"ipadapter_file": "ip-adapter-plus_sdxl_vit-h.safetensors"}}
        wf["11"] = {"class_type": "CLIPVisionLoader",
                    "inputs": {"clip_name": "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors"}}
        wf["12"] = {"class_type": "LoadImage",
                    "inputs": {"image": ref_name, "upload": "image"}}
        wf["13"] = {"class_type": "IPAdapterAdvanced",
                    "inputs": {"weight": ipa_weight, "weight_type": "linear",
                               "combine_embeds": "concat", "start_at": 0.0, "end_at": 0.85,
                               "embeds_scaling": "V only",
                               "model": ["4", 0], "ipadapter": ["10", 0],
                               "image": ["12", 0], "clip_vision": ["11", 0]}}
        model_ref = ["13", 0]

    wf["3"] = {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": STEPS, "cfg": CFG,
                          "sampler_name": "dpmpp_2m", "scheduler": "karras",
                          "denoise": 1.0, "model": model_ref,
                          "positive": ["m1", 0], "negative": ["7", 0],
                          "latent_image": ["5", 0]}}
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}}
    wf["9"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "hlk", "images": ["8", 0]}}
    return wf, (x, y, fw, fh)


def build_redraw(img_name, mask_name, prompt, neg, seed, ref_name,
                 ipa_weight=0.90, denoise=0.85, grow=8):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "50": {"class_type": "LoadImage", "inputs": {"image": img_name, "upload": "image"}},
        "51": {"class_type": "LoadImage", "inputs": {"image": mask_name, "upload": "image"}},
        "52": {"class_type": "ImageToMask", "inputs": {"image": ["51", 0], "channel": "red"}},
        "53": {"class_type": "GrowMask",
               "inputs": {"mask": ["52", 0], "expand": grow, "tapered_corners": True}},
        "54": {"class_type": "VAEEncodeForInpaint",
               "inputs": {"pixels": ["50", 0], "vae": ["4", 2],
                          "mask": ["53", 0], "grow_mask_by": 6}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["4", 1]}},
        "10": {"class_type": "IPAdapterModelLoader",
               "inputs": {"ipadapter_file": "ip-adapter-plus_sdxl_vit-h.safetensors"}},
        "11": {"class_type": "CLIPVisionLoader",
               "inputs": {"clip_name": "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": ref_name, "upload": "image"}},
        "13": {"class_type": "IPAdapterAdvanced",
               "inputs": {"weight": ipa_weight, "weight_type": "linear",
                          "combine_embeds": "concat", "start_at": 0.0, "end_at": 1.0,
                          "embeds_scaling": "V only",
                          "model": ["4", 0], "ipadapter": ["10", 0],
                          "image": ["12", 0], "clip_vision": ["11", 0]}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": STEPS, "cfg": CFG,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": denoise, "model": ["13", 0],
                         "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["54", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "hlk2", "images": ["8", 0]}},
    }


def run(name, wf, outdir):
    t0 = time.time()
    r = post('/prompt', {'prompt': wf})
    pid = r.get('prompt_id')
    if not pid:
        return None, '提交失败: ' + str(r)[:180]
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
                                          str(m[1].get('exception_message'))[:140])
                return None, msg
            imgs = hh[pid].get('outputs', {}).get('9', {}).get('images', [])
            if not imgs:
                return None, '无输出'
            i = imgs[0]
            q = urllib.parse.urlencode({'filename': i['filename'],
                                        'subfolder': i.get('subfolder', ''),
                                        'type': i.get('type', 'output')})
            os.makedirs(outdir, exist_ok=True)
            dst = os.path.join(outdir, name + '.png')
            urllib.request.urlretrieve('%s/view?%s' % (HOST, q), dst)
            return dst, round(time.time() - t0, 1)
    return None, '超时'


if __name__ == '__main__':
    OUT = r'D:\deepseek文件区\manju\_头锁v2'
    WORK = r'D:\deepseek文件区\manju\_头锁v2工作区'
    CROPS = r'D:\deepseek文件区\manju\_裁脸参考图'
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)

    print('=' * 74)
    print('头锁 v2：区域条件锁头部')
    print('=' * 74)

    ref = upload_file(os.path.join(CROPS, 'head_月白_2.png'), 'yb_v2.png')['name']

    for st in ("远景", "中景", "近景", "特写"):
        prof = get_profile(st, 1)
        x, y, fw, fh = area_for_head(prof)
        print()
        print('[%s] %s' % (st, describe(prof)))
        print('    区域框 x=%.2f y=%.2f w=%.2f h=%.2f' % (x, y, fw, fh))

        wf, _ = build_headlock(YB, NEG, 20261201, prof, ref_name=ref)
        p, info = run('L1_%s' % st, wf, OUT)
        if not p:
            print('    失败:', str(info)[:120])
            continue
        print('    出图 %.0fs' % info)

        # 阶段3：同一 profile 出蒙版
        mp = os.path.join(WORK, 'mk_%s.png' % st)
        make_mask(prof, W, H, expand=0.30, out_path=mp)
        mk_up = upload_file(mp, 'mk_%s.png' % st)['name']
        img_up = upload_file(p, 'in_%s.png' % st)['name']

        wf3 = build_redraw(img_up, mk_up,
                           STYLE + ", " + SOLID + ", " + YB + ", detailed face",
                           NEG, 20261202, ref, 0.90, 0.85)
        p3, info3 = run('L3_%s' % st, wf3, OUT)
        print('    重绘 %s' % ('%.0fs' % info3 if p3 else '失败 ' + str(info3)[:80]))

    print()
    print('输出:', OUT)
