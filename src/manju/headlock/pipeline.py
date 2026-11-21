# -*- coding: utf-8 -*-
"""头锁插件：pipeline

阶段 1  头部骨架 + ControlNet → 头的位置和大小被锁定
阶段 3  按同一 profile 生成蒙版 → 局部重绘换脸（不需检测）
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
from manju.headlock.skeleton import make_skeleton
from manju.headlock.masker import make_mask

HOST = 'http://127.0.0.1:8188'
CKPT = 'animagine-xl-4.0.safetensors'
CN_MODEL = 'controlnet-openpose-sdxl.safetensors'

W, H = 832, 1216
STEPS, CFG = 28, 7.0

STYLE = "masterpiece, best quality, very aesthetic, absurdres, anime style, detailed face, detailed eyes"
NEG = ("lowres, bad anatomy, bad hands, extra fingers, deformed, faceless, "
       "watermark, text, signature, logo, photo, photorealistic, 3d render, "
       "extra person, three people, crowd, multiple views, blurry, sketch, "
       "body only, no head")


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


def _ipadapter(wf, ref_name, weight):
    wf["10"] = {"class_type": "IPAdapterModelLoader",
                "inputs": {"ipadapter_file": "ip-adapter-plus_sdxl_vit-h.safetensors"}}
    wf["11"] = {"class_type": "CLIPVisionLoader",
                "inputs": {"clip_name": "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors"}}
    wf["12"] = {"class_type": "LoadImage",
                "inputs": {"image": ref_name, "upload": "image"}}
    wf["13"] = {"class_type": "IPAdapterAdvanced",
                "inputs": {"weight": weight, "weight_type": "linear",
                           "combine_embeds": "concat", "start_at": 0.0, "end_at": 0.85,
                           "embeds_scaling": "V only",
                           "model": ["4", 0], "ipadapter": ["10", 0],
                           "image": ["12", 0], "clip_vision": ["11", 0]}}
    return ["13", 0]


def _controlnet(wf, pose_name, pos_ref, neg_ref, strength, end_percent):
    wf["20"] = {"class_type": "LoadImage",
                "inputs": {"image": pose_name, "upload": "image"}}
    wf["21"] = {"class_type": "ControlNetLoader",
                "inputs": {"control_net_name": CN_MODEL}}
    wf["22"] = {"class_type": "ControlNetApplyAdvanced",
                "inputs": {"strength": strength,
                           "start_percent": 0.0, "end_percent": end_percent,
                           "positive": pos_ref, "negative": neg_ref,
                           "control_net": ["21", 0], "image": ["20", 0]}}
    return ["22", 0], ["22", 1]


def build_stage1(prompt, neg, seed, pose_name, ref_name=None, ipa_weight=0.55,
                 cn_strength=1.0, cn_end=0.75):
    """阶段1：头部骨架锁位置 + IP-Adapter 锁长相"""
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": W, "height": H, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["4", 1]}},
    }
    model_ref = _ipadapter(wf, ref_name, ipa_weight) if ref_name else ["4", 0]
    pos, negc = _controlnet(wf, pose_name, ["6", 0], ["7", 0],
                            cn_strength, cn_end)
    wf["3"] = {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": STEPS, "cfg": CFG,
                          "sampler_name": "dpmpp_2m", "scheduler": "karras",
                          "denoise": 1.0, "model": model_ref,
                          "positive": pos, "negative": negc,
                          "latent_image": ["5", 0]}}
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}}
    wf["9"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "hl1", "images": ["8", 0]}}
    return wf


def build_stage3(img_name, mask_name, prompt, neg, seed, ref_name,
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
              "inputs": {"filename_prefix": "hl3", "images": ["8", 0]}},
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
    OUT = r'D:\deepseek文件区\manju\_头锁出图'
    WORK = r'D:\deepseek文件区\manju\_头锁工作区'
    CROPS = r'D:\deepseek文件区\manju\_裁脸参考图'
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)

    print('=' * 74)
    print('头锁插件 验证：头部骨架 + ControlNet')
    print('=' * 74)

    ref_yb = upload_file(os.path.join(CROPS, 'head_月白_2.png'), 'yb_h.png')['name']

    YB = (STYLE + ", 1girl, brown hair, low ponytail, gentle brown eyes, soft face, "
          "beige hanfu, cross-collar robe, standing, facing viewer")

    # 只测 3 个景别，看头部骨架是否生效
    for st in ("远景", "中景", "近景"):
        prof = get_profile(st, 1)
        print()
        print('[%s] %s' % (st, describe(prof)))

        sk_path = os.path.join(WORK, 'sk_%s.png' % st)
        make_skeleton(prof, W, H, out_path=sk_path)
        sk_up = upload_file(sk_path, 'sk_%s.png' % st)['name']

        wf = build_stage1(YB, NEG, 20261101, sk_up, ref_name=ref_yb,
                          ipa_weight=0.55, cn_strength=1.0, cn_end=0.75)
        p, info = run('S1_%s' % st, wf, OUT)
        if p:
            print('  阶段1 %.0fs -> %s' % (info, os.path.basename(p)))
        else:
            print('  阶段1 失败:', str(info)[:120])
            continue

        # 蒙版（同一 profile，不需要检测）
        mask_path = os.path.join(WORK, 'mk_%s.png' % st)
        make_mask(prof, W, H, expand=0.35, out_path=mask_path)
        mk_up = upload_file(mask_path, 'mk_%s.png' % st)['name']
        img_up = upload_file(p, 'in_%s.png' % st)['name']

        wf3 = build_stage3(img_up, mk_up, YB + ", detailed face, looking at viewer",
                           NEG, 20261102, ref_yb, 0.90, 0.85)
        p3, info3 = run('S3_%s' % st, wf3, OUT)
        if p3:
            print('  阶段3 %.0fs' % info3)
        else:
            print('  阶段3 失败:', str(info3)[:120])

    print()
    print('输出:', OUT)
