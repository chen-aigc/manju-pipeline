# -*- coding: utf-8 -*-
"""画面生成：ComfyUI API 对接 + 自动质检 + 无限重试（上限保护）。

设计要点：
    · 先用 512x768 低分辨率抽卡，合格后再高清修复放大
    · IP-Adapter + 角色专属 Seed 保证同一角色长得一样
    · 质检三条：能打开、不是纯色/黑屏、方差够高（有细节）
    · 最大重试次数可配，防止死循环
"""
import copy
import json
import random
import re
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config


class ComfyError(Exception):
    pass


# --------------------------------------------------------------------------
# 基础 HTTP
# --------------------------------------------------------------------------
def _get(path, timeout=10):
    url = config.COMFY_HOST.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _post(path, payload, timeout=30):
    url = config.COMFY_HOST.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def is_alive():
    try:
        _get("/system_stats", timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 工作流模板操作
# --------------------------------------------------------------------------
def load_workflow(path=None):
    p = Path(path or config.COMFY_WORKFLOW)
    if not p.exists():
        raise ComfyError(
            "找不到 ComfyUI 工作流文件：%s\n"
            "请在 ComfyUI 里搭好工作流，用「保存(API格式)」导出，放到这个路径。" % p)
    return json.loads(p.read_text(encoding="utf-8"))


def build_sdxl_workflow(ckpt=None, ref_image=None):
    """SDXL 工作流（代码内构造，不依赖 assets 里的 json）。

    与 SD1.5 的差别：
      · 不挂 hanfuSong LoRA（那是 SD1.5 的，SDXL 用不了）
      · 可选挂 IP-Adapter（SDXL 版，用于角色一致性）
      · ref_image 为 str：单人镜，用 IPAdapterAdvanced
      · ref_image 为 list（2 张）：双人镜，用 ImageBatch + IPAdapterBatch
      · weight 见 config.SDXL_IPADAPTER_WEIGHT
      · 尺寸 768×1344（9:16 附近，64 的倍数）
    """
    ckpt = ckpt or getattr(config, "SDXL_CKPT", "animagine-xl-4.0.safetensors")
    steps = int(getattr(config, "SDXL_STEPS", 26))
    cfg = float(getattr(config, "SDXL_CFG", 6.0))
    # ⚠ 不能用 config.image_gen_size()：那个会跟随 IMAGE_MODEL，
    #   在 sd15 模式下会返回 720×1280，SDXL 用这个尺寸会崩。
    if getattr(config, "IMAGE_GEN_SIZE", "draft") == "final":
        w, h = (getattr(config, "SDXL_FINAL_W", 832),
                getattr(config, "SDXL_FINAL_H", 1472))
    else:
        w, h = (getattr(config, "SDXL_DRAFT_W", 768),
                getattr(config, "SDXL_DRAFT_H", 1344))
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "manju_xl", "images": ["8", 0]}},
    }

    model_ref = ["4", 0]
    refs = []
    if isinstance(ref_image, (list, tuple)):
        refs = [r for r in ref_image if r]
    elif ref_image:
        refs = [ref_image]

    if refs:
        # IP-Adapter SDXL：模型 → 参考图 → 融合，再进 KSampler
        wf["10"] = {"class_type": "IPAdapterModelLoader",
                    "inputs": {"ipadapter_file": getattr(
                        config, "SDXL_IPADAPTER",
                        "ip-adapter-plus_sdxl_vit-h.safetensors")}}
        wf["11"] = {"class_type": "CLIPVisionLoader",
                    "inputs": {"clip_name": getattr(
                        config, "SDXL_CLIP_VISION",
                        "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors")}}

        # 逐个加载参考图
        img_refs = []
        for k, r in enumerate(refs):
            nid = str(20 + k)
            wf[nid] = {"class_type": "LoadImage",
                       "inputs": {"image": r, "upload": "image"}}
            img_refs.append([nid, 0])

        base_w = float(getattr(config, "SDXL_IPADAPTER_WEIGHT", 0.75))

        if len(img_refs) == 1:
            # 单人：常规 IPAdapterAdvanced
            wf["13"] = {"class_type": "IPAdapterAdvanced",
                        "inputs": {"weight": base_w,
                                   "weight_type": "linear",
                                   "combine_embeds": "concat",
                                   "start_at": 0.0,
                                   "end_at": float(getattr(
                                       config, "SDXL_IPADAPTER_END_AT", 0.65)),
                                   "embeds_scaling": "V only",
                                   "model": ["4", 0],
                                   "ipadapter": ["10", 0],
                                   "image": img_refs[0],
                                   "clip_vision": ["11", 0]}}
            model_ref = ["13", 0]
        else:
            # 双人：ImageBatch 合并两张参考图，再用 IPAdapterBatch
            wf["30"] = {"class_type": "ImageBatch",
                        "inputs": {"image1": img_refs[0],
                                   "image2": img_refs[1]}}
            wf["13"] = {"class_type": "IPAdapterBatch",
                        "inputs": {"weight": base_w,
                                   "weight_type": "linear",
                                   "start_at": 0.0,
                                   "end_at": float(getattr(
                                       config, "SDXL_IPADAPTER_END_AT", 0.65)),
                                   "embeds_scaling": "V only",
                                   "encode_batch_size": 2,
                                   "model": ["4", 0],
                                   "ipadapter": ["10", 0],
                                   "image": ["30", 0],
                                   "clip_vision": ["11", 0]}}
            model_ref = ["13", 0]

    wf["3"] = {"class_type": "KSampler",
               "inputs": {"seed": 123456789, "steps": steps, "cfg": cfg,
                          "sampler_name": "dpmpp_2m", "scheduler": "karras",
                          "denoise": 1.0, "model": model_ref,
                          "positive": ["6", 0], "negative": ["7", 0],
                          "latent_image": ["5", 0]}}
    wf["8"]["inputs"]["samples"] = ["3", 0]
    return wf


def find_nodes(workflow, class_type):
    """按 class_type 找出所有节点 id。"""
    return [nid for nid, node in workflow.items()
            if node.get("class_type") == class_type]


def set_input(workflow, node_id, key, value):
    workflow[node_id].setdefault("inputs", {})[key] = value


def apply_prompt(workflow, positive, negative=None, seed=None, width=None, height=None):
    """把提示词 / seed / 尺寸写进工作流。

    只认标准节点：
        CLIPTextEncode（正向/反向靠位置区分）
        KSampler（seed）
        EmptyLatentImage（宽高）
    """
    wf = copy.deepcopy(workflow)

    # --- 找 KSampler，写 seed
    samplers = find_nodes(wf, "KSampler")
    if not samplers:
        raise ComfyError("工作流里没有 KSampler 节点")
    if seed is not None:
        for nid in samplers:
            set_input(wf, nid, "seed", int(seed))
            # 固定 seed 后关闭 randomize 行为
            set_input(wf, nid, "control_after_generate", "fixed")

    # --- 尺寸
    latents = find_nodes(wf, "EmptyLatentImage")
    if latents and (width and height):
        for nid in latents:
            set_input(wf, nid, "width", int(width))
            set_input(wf, nid, "height", int(height))

    # --- 文本：按 KSampler 的 positive / negative 引用定位
    if samplers:
        ks = wf[samplers[0]].get("inputs", {})
        pos_ref = ks.get("positive")
        neg_ref = ks.get("negative")

        def _ref_id(ref):
            if isinstance(ref, list) and ref:
                return str(ref[0])
            return None

        pid, nid_ = _ref_id(pos_ref), _ref_id(neg_ref)
        if pid and pid in wf and positive:
            set_input(wf, pid, "text", positive)
        if nid_ and nid_ in wf and negative is not None:
            set_input(wf, nid_, "text", negative)
    return wf


# --------------------------------------------------------------------------
# 质检
# --------------------------------------------------------------------------
def head_is_cut(path, band=0.06, skin_thr=0.18, dark_thr=0.45):
    """检测图片顶部是否被切头（改进版，降低误判）。

    旧版只看顶部 6% 横条，把「深色树枝/废墟」误当成头发，
    导致大量正常图被判切头 → 无谓重试 → 换 seed → 换脸。

    新版三条同时成立才算切头：
        1. 只看中间 50% 宽度（人头一般在中间）
        2. 顶部两段都偏深（头发有连续性，树枝通常不连续）
        3. 或顶部中间出现明显肤色（脸顶被切）

    返回 (cut, 说明)
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return False, ""

    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size

        # 中间 50% 宽度
        x0, x1 = int(w * 0.25), int(w * 0.75)
        b1_h = max(2, int(h * band))          # 顶部 0~6%
        b2_h = max(2, int(h * band * 2))      # 第二段 6%~18%

        def ratios(y0, y1):
            crop = im.crop((x0, y0, x1, y1))
            px = list(crop.getdata())
            n = len(px) or 1
            skin = dark = 0
            for (r, g, b) in px:
                if r > 150 and 95 < g < 205 and 80 < b < 190 and r > g > b:
                    skin += 1
                if r < 70 and g < 70 and b < 80:
                    dark += 1
            return skin / n, dark / n

        skin1, dark1 = ratios(0, b1_h)
        skin2, dark2 = ratios(b1_h, b1_h + b2_h)

        # 顶部中间明显肤色 → 脸被切
        if skin1 >= 0.30:
            return True, "顶部中间肤色 %.0f%%" % (skin1 * 100)

        # 两段都深色 → 头发占满顶部
        if dark1 >= 0.62 and dark2 >= 0.45:
            return True, "顶部连续深色（%.0f%%/%.0f%%）" % (dark1 * 100, dark2 * 100)

        return False, ""
    except Exception:  # noqa: BLE001
        return False, ""


def check_image(path, min_var=None):
    """返回 (ok, reason)。"""
    path = Path(path)
    if not path.exists() or path.stat().st_size < 1024:
        return False, "文件不存在或过小"

    try:
        from PIL import Image
        import numpy as np  # 可能没装，退化为手算
    except Exception:  # noqa: BLE001
        try:
            from PIL import Image
            img = Image.open(path).convert("L")
            img.thumbnail((128, 128))
            px = list(img.getdata())
            n = len(px)
            mean = sum(px) / max(1, n)
            var = sum((p - mean) ** 2 for p in px) / max(1, n)
            thr = min_var if min_var is not None else config.IMAGE_QUALITY_MIN_VAR
            if var < thr:
                return False, "画面过于平坦（方差 %.0f < %.0f）" % (var, thr)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, "无法读取图片：%r" % (exc,)

    try:
        from PIL import Image
        import numpy as np
        img = Image.open(path).convert("L")
        arr = np.asarray(img, dtype="float32")
        var = float(arr.var())
        mean = float(arr.mean())
        if mean < 10:
            return False, "整体过暗（均值 %.0f）" % mean
        if mean > 248:
            return False, "整体过亮（均值 %.0f）" % mean
        thr = min_var if min_var is not None else config.IMAGE_QUALITY_MIN_VAR
        if var < thr:
            return False, "画面过于平坦（方差 %.0f < %.0f）" % (var, thr)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, "质检失败：%r" % (exc,)


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def load_ipadapter_workflow(path=None):
    """加载带 IP-Adapter 的工作流。"""
    p = Path(path or (Path(config.ASSETS_DIR) / "workflow_ipadapter.json"))
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def set_reference_image(workflow, ref_name):
    """把 LoadImage 节点的图片设成参考图。"""
    for nid in find_nodes(workflow, "LoadImage"):
        set_input(workflow, nid, "image", ref_name)
    return workflow


def upload_reference(src_path, name="ref.png"):
    """把参考图（定妆照）上传到 ComfyUI 的 input 目录。

    ComfyUI 的 LoadImage 只能读它自己 input 目录里的文件，
    所以每次换角色都要重新上传，覆盖同名文件即可。
    """
    src = Path(src_path)
    if not src.exists():
        raise ComfyError("参考图不存在：%s" % src)

    import mimetypes
    boundary = "----manju%d" % random.randint(10 ** 8, 10 ** 9)
    ctype = mimetypes.guess_type(src.name)[0] or "image/png"
    data = src.read_bytes()

    body = b""
    body += ("--%s\r\n" % boundary).encode()
    body += ('Content-Disposition: form-data; name="image"; filename="%s"\r\n'
             % name).encode()
    body += ("Content-Type: %s\r\n\r\n" % ctype).encode()
    body += data + b"\r\n"
    body += ("--%s\r\n" % boundary).encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += ("--%s--\r\n" % boundary).encode()

    url = config.COMFY_HOST.rstrip("/") + "/upload/image"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type",
                   "multipart/form-data; boundary=%s" % boundary)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise ComfyError("上传参考图失败：%s" % exc) from exc

    return info.get("name") or name


def has_reference():
    """检查 ComfyUI 里有没有 ref.png。"""
    try:
        data = _get("/object_info/LoadImage", timeout=10)
        files = data["LoadImage"]["input"]["required"]["image"][0]
        return "ref.png" in files
    except Exception:  # noqa: BLE001
        return False


def queue_prompt(workflow, client_id=None):
    client_id = client_id or str(uuid.uuid4())
    resp = _post("/prompt", {"prompt": workflow, "client_id": client_id})
    return resp.get("prompt_id"), client_id


def wait_for(history_id, timeout=300, interval=1.5):
    """轮询直到出图。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = _get("/history/%s" % history_id, timeout=10)
            if history_id in h:
                entry = h[history_id]
                outs = entry.get("outputs", {})
                files = []
                for node_out in outs.values():
                    for img in node_out.get("images", []):
                        files.append(img)
                if files:
                    return files
                # 有状态说明失败
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise ComfyError("ComfyUI 执行报错：%s" % status)
        except ComfyError:
            raise
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    raise ComfyError("等待出图超时（%ss）" % timeout)


def download_image(image_info, save_to):
    """从 ComfyUI 取回图片。"""
    q = urllib.parse.urlencode({
        "filename": image_info.get("filename", ""),
        "subfolder": image_info.get("subfolder", ""),
        "type": image_info.get("type", "output"),
    })
    url = config.COMFY_HOST.rstrip("/") + "/view?" + q
    save_to = Path(save_to)
    save_to.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as resp:
        save_to.write_bytes(resp.read())
    return save_to


# 强制动漫风格的前缀与负向词（SD1.5 基础模型偏写实，必须显式压制）
STYLE_PREFIX = "masterpiece, best quality, anime style, "
# SDXL（Animagine 系）需要它自己的质量词；
# 实测加 flat lighting / cel shading / clean lineart 会变成平涂版画，千万别加。
STYLE_PREFIX_SDXL = ("masterpiece, best quality, very aesthetic, absurdres, "
                     "anime style, ")

# 场景 → 光效（戏剧性光影的来源）
LIGHTING = [
    (("深夜", "夜里", "夜色", "月", "冷月"), "cold moonlight, blue rim light, deep shadows, chiaroscuro"),
    (("油灯", "烛", "火光", "篝火"), "warm candlelight, flickering glow, strong light and shadow contrast, chiaroscuro"),
    (("清晨", "黎明", "破晓", "晨雾"), "volumetric morning mist, god rays, soft backlight, hazy light shafts"),
    (("黄昏", "夕阳", "落日"), "golden hour backlight, long shadows, warm orange rim light"),
    (("雨", "湿", "水"), "rain-wet surfaces, reflective ground, moody overcast light"),
    (("雷", "闪", "电"), "dramatic lightning flash, hard edge lighting, high contrast"),
    (("大厅", "堂", "殿"), "interior dramatic lighting, shafts of light from high windows, dust motes"),
    (("破屋", "小屋", "屋"), "dim interior, single light source, strong falloff, vignette"),
    (("院", "庭院", "山", "林", "野"), "natural daylight, dappled light through leaves, atmospheric haze"),
]

# 材质表现（特定环境下的质感）
MATERIALS = [
    (("剑", "断剑", "刀"), "weathered steel texture, rust patina, metallic specular highlight"),
    (("衣", "袍", "裳", "服"), "silk fabric sheen, fine embroidered pattern, cloth folds with soft shadows"),
    (("桌", "木", "门", "窗", "梁"), "aged wood grain texture, worn edges, matte surface"),
    (("墙", "石", "砖", "路"), "rough stone texture, cracks and moss, weathered surface"),
    (("灯笼", "灯", "烛"), "paper lantern translucency, warm subsurface glow"),
    (("饼", "碗", "陶"), "coarse ceramic texture, glazed highlights"),
    (("雾",), "volumetric fog layers, soft edge falloff"),
]

# 统一收尾：强化空间层次和质感
CINEMA_SUFFIX = "dramatic lighting, detailed textures, depth of field"


def _match(table, text):
    out = []
    for keys, val in table:
        if any(k in text for k in keys):
            out.append(val)
    return out



STYLE_NEGATIVE = ("photo, photorealistic, realistic, 3d render, "
                  "real person, human photo, portrait photo, "
                  "watermark, text, logo, signature, username, "
                  "kanji, chinese characters, japanese text, letters, words, "
                  "stamp, seal, emblem, banner, subtitle, "
                  "collage, multiple views, split image, grid, "
                  "lowres, blurry, bad anatomy, bad hands, extra fingers, "
                  "worst quality, low quality, cropped head, out of frame, face cut off, "
                  "head out of frame, top of head cropped, cut off at top, missing head, "
                  "scythe, curved blade, sickle, hook sword, polearm, "
                  "dark dull clothes, all black clothing, overly dark, underexposed, "
                  "off center subject, subject at edge of frame, tiny figure, "
                  "extra hands, extra arms, three hands, multiple limbs, "
                  "fused fingers, malformed hands, deformed face, "
                  "plain background, empty background, solid color background, "
                  "flat lighting, dull colors, low contrast, no shadows, "
                  "low detail, blurry textures, washed out")

# 中文 → 英文的常用词映射（SD1.5 不认中文）
ZH_MAP = {
    "夜晚": "night", "白天": "day", "清晨": "morning", "黄昏": "dusk",
    "少年": "young man", "少女": "young girl", "男子": "man", "女子": "woman",
    "老者": "old man", "黑发": "black hair", "白发": "white hair",
    "长发": "long hair", "短发": "short hair",
    "古装": "ancient chinese costume", "长衫": "hanfu robe", "黑袍": "black robe",
    "庭院": "courtyard", "大厅": "hall", "街道": "street", "仓库": "warehouse",
    "剑": "sword", "断剑": "broken sword", "灯笼": "lantern",
    "特写": "close-up", "中景": "medium shot", "全景": "wide shot",
    "电影感": "cinematic", "月光": "moonlight", "灯光": "lighting",
    "眼神": "eyes", "坚定": "determined", "惊讶": "surprised",
    "青色": "blue-green", "清瘦": "slender", "倔强": "determined",
    "少年": "teenage boy", "少女": "teenage girl", "红衣": "red dress",
    "华服": "ornate dress", "女子": "woman", "男子": "man",
    "长须": "long beard", "黑袍": "black robe", "负手": "hands behind back",
    "月光": "moonlight", "烛火": "candlelight", "油灯": "oil lamp",
    "破旧": "dilapidated", "小屋": "small room", "大厅": "grand hall",
    "庭院": "courtyard", "门口": "doorway", "家丁": "servant",
    "断剑": "broken sword", "锈迹": "rusty", "剑": "sword",
    "全景": "wide shot", "中景": "medium shot", "特写": "close-up",
    "惊愕": "shocked", "震惊": "astonished", "低头": "looking down",
    "擦拭": "wiping", "紧握": "gripping", "笑": "laughing",
    "薄雾": "mist", "仙山": "immortal mountain", "楼阁": "pavilion",
    "青石板": "stone path", "布": "cloth", "桌": "table",
    "剪影": "silhouette", "光晕": "glow", "蓝": "blue",
    "光效": "light effect", "科技感": "tech effect", "暗调": "dark tone",
    "虚化": "blurred", "人影": "figures", "夜色": "night",
    "清晨": "dawn", "深夜": "late night", "冷": "cold",
}


# 角色固定外貌（英文），写进每张图的提示词开头，保证一致性
CHAR_LOOK = {
    "林越": ("(male:1.4), (black hair:1.5), (spiky hair:1.2), 1boy, "
             "masculine jaw, adam apple, flat chest, "
             "light jade green chinese hanfu robe, white inner collar, "
             "slender build, sharp eyes"),
    "老者": ("(white hair:1.3), (long beard:1.2), 1old man, black robe, "
             "sharp piercing eyes"),
    "林越妻子": ("1girl, long black hair, red ornate chinese dress, "
                 "arrogant expression"),
    "家丁甲": ("1man, brown servant clothes, laughing expression"),
    "系统": "floating blue holographic text, glowing",
    "月白": ("1girl, (brown hair:1.3), (long hair:1.1), "
              "hair tied with cloth string, (gentle brown eyes:1.2), "
              "soft round face, kind expression, beige hanfu, "
              "cross-collar robe, linen, pale skirt, cloth sash"),
    "月白2": "1girl, brown hair, ponytail",
}

# 双人同框专用：短句 + 位置绑定。
# SD1.5 处理多人时会把两段外观"混"成一个，
# 1girl 还会压过 1boy。所以这里用「谁在左、谁在右」把特征钉死，
# 并显式写 one male one female。
CHAR_LOOK_2P = {
    "林越":     "(male:1.3) 1boy black spiky hair jade green hanfu",
    "月白":     "1girl brown low ponytail beige hanfu",
    "老者":     "1old man white hair long beard black robe",
    "林越妻子": "1girl long black hair red dress",
    "家丁甲":   "1man brown servant clothes",
}
MALE_SET = {"林越", "老者", "家丁甲"}

# 特写专用：只留脸/发/性别，不写服装。
# 写全身汉服时 SD 会忍不住画全身，特写就变成中景（#2/#13/#16/#38 都是这么废的）。
CHAR_FACE = {
    "林越":   "(male:1.4) 1boy black spiky hair, sharp eyes",
    "月白":   "1girl brown hair, gentle brown eyes, soft round face",
    "老者":   "1old man white hair, long beard, piercing eyes",
    "林越妻子": "1girl long black hair, arrogant eyes",
    "家丁甲": "1man brown hair",
}


# 颜色词：LLM 写的服装颜色经常和记忆里的设定冲突
COLOR_WORDS = (
    "blue", "red", "green", "black", "white", "yellow", "purple",
    "jade", "crimson", "scarlet", "azure", "golden", "silver",
    "brown", "grey", "gray", "pink", "orange",
)
CLOTH_WORDS = ("robe", "hanfu", "dress", "clothes", "garment", "shirt",
               "cloak", "coat", "armor", "sleeve")


def _strip_conflicting_color(text, chars):
    """有记忆外观时，把动作描述里的服装颜色词去掉，避免双重描述。

    例：记忆是「青绿汉服」，LLM 却写 "young man in blue robe"
    → 去掉 blue 后变成 "young man in robe"，外观由记忆统一控制。
    """
    if not chars or not text:
        return text
    words = text.split()
    lower = [w.lower().strip(",.") for w in words]
    if not any(c in CLOTH_WORDS for c in lower):
        return text            # 没提衣服，不用管
    keep, dropped = [], 0
    for w in words:
        if w.lower().strip(",.") in COLOR_WORDS:
            dropped += 1
            continue
        keep.append(w)
    if not dropped:
        return text
    out = " ".join(keep)
    return re.sub(r"\s{2,}", " ", out).strip(" ,")


def character_look(names, limit=None):
    """把角色名映射成固定英文外貌串（可按词数压缩）。"""
    out = []
    for n in names or []:
        v = CHAR_LOOK.get(str(n).strip())
        if v:
            out.append(v)
    s = ", ".join(out)
    if limit:
        ws = s.split()
        if len(ws) > limit:
            s = " ".join(ws[:limit])
    return s


EMOTION_KEYS = {
    "怒": "angry expression, furrowed brows",
    "惊": "shocked expression, wide eyes",
    "哭": "crying, tears in eyes",
    "笑": "smiling, relaxed",
    "笑": "smiling",
    "冷": "cold expression, narrowed eyes",
    "痛": "pained expression, gritted teeth",
    "惧": "fearful expression",
    "嘲": "sneering",
    "沉": "somber expression",
    "肃": "serious expression",
    "茫": "dazed expression",
    "狠": "fierce expression",
    "紧张": "tense expression",
    "震惊": "shocked expression, wide eyes",
    "愤怒": "angry expression, furrowed brows",
}


def _infer_emotion(shot):
    """从旁白/动作里推断情绪（没有结构化字段时的兜底）。"""
    text = " ".join(str(shot.get(k) or "") for k in
                     ("narration", "action_en", "image_prompt_en", "act"))
    for kw, en in EMOTION_KEYS.items():
        if kw and kw in text:
            return en
    # 对白里的感叹号/问号给个基础情绪
    if "！" in text or "!" in text:
        return "intense expression"
    return ""


def zh_to_en(text, max_len=350):
    """把中文提示词里的关键词替换成英文，其余保留。

    SD1.5 对中文几乎无响应，不做转换等于白写提示词。
    """
    out = str(text or "")
    for zh, en in ZH_MAP.items():
        out = out.replace(zh, " " + en + " ")
    # 清掉残留的中文字符（模型不认，留着只会浪费 token）
    import re as _re
    out = _re.sub(r"[\u4e00-\u9fff]+", " ", out)
    out = _re.sub(r"\s+", " ", out).strip(" ,")
    return out[:max_len]


MAX_WORDS = 62      # 约 60 token，留出安全余量（CLIP 上限 77）


def _max_words():
    """SDXL 有两个文本编码器，可以放宽到 75 词；SD1.5 仍是 62。"""
    if getattr(config, "IMAGE_MODEL", "sd15") == "sdxl":
        return 75
    return MAX_WORDS


def _clean(text, max_words):
    """清掉中文标点残留，压成英文短语。"""
    import re as _re
    t = str(text or "")
    # 去掉中文标点
    t = _re.sub(r"[，。！？；：、…（）【】《》,;:!?()\[\]]", " ", t)
    # 只保留 ASCII 字母数字和已有英文
    t = _re.sub(r"[^A-Za-z0-9\-\s]", " ", t)
    words = [w for w in t.split() if w]
    return " ".join(words[:max_words])


# ============================================================================
# SDXL 专用提示词构造
# ----------------------------------------------------------------------------
# SDXL（Animagine 系）是自然语言模型：
#   · 不要 (xxx:1.3) 这种权重堆叠——会干扰，且不同实现支持度不一致
#   · 不要 "visible:" / "motion:" / "depth layers:" 这类结构化前缀
#   · 45~55 词最稳，超过 70 词开始互相干扰
# 场景/人物/构图全部写成短句自然语言。
# ============================================================================

SCENE_SDXL = (
    ("破屋屋内", "inside an old chinese room, mud walls, wooden beams, paper window, low wooden table"),
    ("屋内", "inside an old chinese room, mud walls, wooden beams, paper window"),
    ("院门内", "inside a chinese courtyard, stone paved ground, wooden gate behind, snow on the ground"),
    ("院门外", "outside a chinese courtyard gate at night, wooden gate, stone steps, snow covered ground"),
    ("院", "a chinese courtyard, stone wall, wooden gate"),
    ("塔内", "inside a ruined stone pagoda, wooden beams, dust in the air"),
    ("塔", "a ruined stone pagoda, cracked walls, rubble"),
    ("街", "an ancient chinese street, wooden shops, stone road"),
    ("林", "a dense forest, tall trees, mist"),
    ("山", "mountain ridge, rocky cliff, distant peaks"),
)

CHAR_SDXL = {
    "林越": "a young man with black spiky hair in jade green hanfu",
    "月白": "a young woman with brown ponytail in beige hanfu",
    "老者": "an old man with white hair and long beard in black robe",
    "林越妻子": "a woman with long black hair in red dress",
    "家丁甲": "a man in brown servant clothes",
}

COMP_SDXL = {
    "远景": "wide shot",
    "中景": "medium shot, waist up",
    "近景": "medium close-up, chest up",
    "特写": "close-up portrait, face fills the frame",
    "过肩": "over the shoulder shot",
    "空镜": "empty scene, no people",
    "死物": "close-up of objects, no people",
}

# 风格锚定：整集用同一串，压住画风漂移。
# ⚠ 不要写 flat lighting / cel shading / clean lineart ——
#   实测会把 SDXL 推成平涂版画，比 SD1.5 还差。
# 锚定要「正面受光 + 清晰面部」，否则 SDXL 会画成背光剪影。
# 禁用词（实测都变差）：
#   anime screencap / film still  → 远景剪影
#   muted natural colors          → 整体灰暗
#   flat lighting / cel shading   → 平涂版画
STYLE_TAIL_SDXL = ("2d anime illustration, consistent art style, "
                   "detailed shading, clean colors, "
                   "faces clearly lit, soft key light, sharp focus")

NEG_SDXL = (
    "lowres, bad anatomy, bad hands, extra fingers, deformed face, "
    "watermark, text, signature, logo, "
    "photo, photorealistic, 3d render, "
    "japanese room, tatami, shoji screen, torii, japanese shrine gate, "
    "modern room, modern furniture, bed, mattress, sofa, tiled floor, glass window, "
    "extra person, three people, crowd, multiple views"
)


def _scene_sdxl(scene, wide=False):
    """场景锚点。

    ⚠️ 室内 + wide shot 很容易被画成欧式高大厅堂，
       所以室内远景要显式写 small humble room / low ceiling。
    """
    for k, v in SCENE_SDXL:
        if k in scene:
            indoor = not any(x in scene for x in ("院", "街", "林", "山", "外"))
            if wide and indoor:
                return v + ", small humble room, low ceiling, cramped space"
            return v
    return ""


def _light_sdxl(text, outdoor=False):
    """光照描述。

    ⚠️ 雪只在室外写：室内写 snow，SDXL 会画成满屏白羽毛。
    """
    if outdoor and any(k in text for k in ("雪", "snow")):
        return "falling snow, cold blue night air"
    if any(k in text for k in ("夜", "night", "月")):
        return "night, cold moonlight"
    if any(k in text for k in ("灯", "烛", "lamp", "candle")):
        return "warm lamplight, soft glow"
    return "soft warm light"


def build_prompts_sdxl(shot):
    """SDXL 版提示词：短句、自然语言、不堆权重。"""
    scene = str(shot.get("scene") or "")
    st = str(shot.get("shot_type") or "")
    chars, seen = [], set()
    for c in (shot.get("characters") or []):
        c = str(c).strip()
        if c and c not in seen and c in CHAR_SDXL:
            seen.add(c)
            chars.append(c)
    if st in ("空镜", "死物"):
        chars = []

    body = []

    # ① 场景（远景要额外收紧室内空间）
    sc = _scene_sdxl(scene, wide=(st == "远景"))
    if sc:
        body.append(sc)

    # ② 人物
    if len(chars) == 1:
        body.append(CHAR_SDXL[chars[0]])
    elif len(chars) >= 2:
        body.append("%s on the left, %s on the right, both adults, same height"
                    % (CHAR_SDXL[chars[0]], CHAR_SDXL[chars[1]]))

    # ③ 构图
    comp = COMP_SDXL.get(st, "medium shot")
    if len(chars) >= 2 and st not in ("空镜", "死物"):
        comp = "two people, " + comp
    body.append(comp)

    # ④ 道具（最多 3 个）
    props = shot.get("props_in_frame") or []
    if isinstance(props, str):
        props = [props]
    ps = []
    for p in list(props)[:3]:
        p = str(p).strip()
        if not p:
            continue
        e = zh_to_en(p, 30) if any(ord(c) > 127 for c in p) else p
        e = _clean(e, 3)
        if e:
            ps.append(e)
    if ps:
        # 近景/中景里道具容易挡脸，加位置限定
        if st in ("近景", "特写"):
            body.append(", ".join(ps) + " in the background, not covering the face")
        else:
            body.append(", ".join(ps))

    # ⑤ 动作（短）
    act = str(shot.get("action_en") or "").strip()
    if act:
        body.append(_clean(act, 8))

    # ⑥ 光影
    raw = " ".join(str(shot.get(k) or "") for k in
                   ("image_prompt", "narration", "action_en", "scene"))
    outdoor = any(x in scene for x in ("院门", "街", "林", "山", "外"))
    body.append(_light_sdxl(raw, outdoor=outdoor))

    # 风格尾缀必须放前面：68 词上限会砍掉末尾，放最后等于没写
    positive = ("masterpiece, best quality, very aesthetic, absurdres, anime style, "
                + STYLE_TAIL_SDXL + ", "
                + ", ".join(b for b in body if b))
    w = positive.split()
    if len(w) > 68:
        positive = " ".join(w[:68])
    positive = re.sub(r"\s{2,}", " ", positive).strip().rstrip(",")
    return positive, NEG_SDXL


def ref_image_for_shot(shot):
    """按镜头角色自动挑 IP-Adapter 参考图。

    返回 None（不挂）/ 图片文件名（已上传到 ComfyUI input 的名字）。
    调用前需保证文件已在 ComfyUI 的 input 目录（由 ensure_refs_uploaded 负责）。
    """
    chars = [str(x).strip() for x in (shot.get("characters") or []) if str(x).strip()]
    st = str(shot.get("shot_type") or "")
    if st in ("空镜", "死物") or not chars:
        return None
    if len(chars) >= 2:
        return "__PAIR__"
    return "__CHAR__" + chars[0]


def list_char_refs():
    """扫描 CHARS_DIR，返回 {角色名: 文件路径} 与双人参考图路径。"""
    d = Path(getattr(config, "CHARS_DIR", ""))
    out, pair = {}, None
    if not d.exists():
        return out, pair
    for p in sorted(d.glob("*.png")):
        stem = p.stem
        if stem.startswith("_"):
            continue
        # 双人参考图：文件名以 双人 开头即为双人图（不看后缀数字）
        pair_name = getattr(config, "PAIR_REF_NAME", "双人_月白林越")
        if stem == pair_name or stem.startswith("双人"):
            pair = p
            continue
        # <角色名>_<序号> ：取序号最大的一张
        if "_" in stem:
            name, _, num = stem.rpartition("_")
            if num.isdigit():
                if name not in out or int(num) > int(out[name][1]):
                    out[name] = (p, num)
    return {k: v[0] for k, v in out.items()}, pair


def build_prompts_model(shot, model):
    """按指定模型构造提示词（不改全局 IMAGE_MODEL，便于混合出图）。"""
    if model == "sdxl":
        return build_prompts_sdxl(shot)
    old = getattr(config, "IMAGE_MODEL", "sd15")
    try:
        config.IMAGE_MODEL = "sd15"
        return build_prompts(shot)
    finally:
        config.IMAGE_MODEL = old


def model_for_shot(shot):
    """混合出图：无人物镜头走 SDXL，其余走 SD1.5。"""
    st = str(shot.get("shot_type") or "")
    types = getattr(config, "SDXL_SHOT_TYPES", set())
    if st in types:
        return "sdxl"
    return "sd15"


def build_prompts(shot, memory=None):
    """生成 (正向, 负向) 提示词。

    IMAGE_MODEL=sdxl 时走 build_prompts_sdxl()（短句自然语言版）；
    否则走下面的 SD1.5 逻辑（权重堆叠 + 结构化前缀）。
    """
    if getattr(config, "IMAGE_MODEL", "sd15") == "sdxl":
        return build_prompts_sdxl(shot)

    """SD1.5 版：严格控制在 CLIP 77 token 以内。

    CLIP 只读前 77 个 token，超出的会被整段丢弃。
    配额分配（总 62 词，留余量给 BPE 分词误差）：
        style 6 / comp 6 / action 12 / emotion 5 / look 16 / light 5 / prop 4

    ⚠️ 动作和情绪必须排在人物外观之前。
    以前的顺序把 look 放前面，46 词里外观占 18 词，
    动作直接被截掉 —— 这是"画面和剧情不匹配"的主因。
    """
    raw = (shot.get("image_prompt") or shot.get("narration")
           or shot.get("image_prompt_en") or "")
    scene = str(shot.get("scene") or "").strip()

    # 场景锚点：把中文地点翻成具体视觉名词。
    # 不做这一步，模型只会画「人+树」，塔/门/废墟全没有。
    # ⚠️ 顺序至关重要：循环遇到第一个命中的关键词就 break。
    #    所以「破屋屋内」必须排在「破屋」之前，否则室内镜头会拿到外景描述
    #    ——这是 23/40 镜场景崩掉的直接原因（旧版把「破屋屋内」判成了带
    #    broken roof 的室外破房子）。
    SCENE_MAP = (
        # ---- 室内优先（本剧 35/40 镜在屋内，必须最精确）----
        ("破屋屋内", "(ancient chinese room interior:1.3), mud walls, wooden beams, "
                     "paper window, oil lamp"),
        ("屋内灯下", "(inside old chinese room:1.3), paper window, wooden beams, "
                     "oil lamp on table, warm glow"),
        ("屋内", "(ancient chinese room interior:1.3), mud walls, wooden beams, "
                 "paper window, oil lamp"),
        ("室内", "(ancient chinese room interior:1.2), mud walls, wooden beams, paper window"),
        # ---- 院内 / 门外 ----
        ("院门内", "(inside courtyard:1.2), stone paved ground, wooden gate behind, "
                   "mud wall, snow on ground"),
        ("院门外", "(outside courtyard gate:1.3), wooden gate, stone steps, "
                   "snow covered ground, mud wall"),
        ("院门", "(courtyard gate:1.2), wooden gate, stone step, swept ground"),
        ("院", "(courtyard:1.1), stone wall, wooden gate"),
        # ---- 塔 ----
        ("塔顶", "(top of ruined pagoda:1.2), broken roof tiles, open sky"),
        ("塔门", "(stone doorway of ruined pagoda:1.3), dark arch, broken lintel, moss"),
        ("塔下", "(base of ruined stone pagoda:1.3), cracked walls, rubble, dead trees"),
        ("塔内", "(pagoda interior:1.2), wooden beams, dust motes, dim light"),
        ("废塔", "(ruined stone pagoda:1.2), cracked walls, rubble"),
        # ---- 其他 ----
        ("屋外", "(outside wooden house:1.2), dirt yard, stone wall"),
        ("街", "(ancient street:1.2), wooden shops, stone road"),
        ("林", "(dense forest:1.2), tall trees, mist between trunks"),
        ("山", "(mountain ridge:1.2), rocky cliff, distant peaks"),
        # ---- 时间/天气（放最后，只兜底）----
        ("夜", "night scene, moonlight, deep shadows"),
        ("晨", "dawn, mist, pale cold light"),
        ("雨", "rain, wet ground, dripping eaves"),
    )
    scene_anchors = ""
    for kw, en in SCENE_MAP:
        if kw in scene:
            scene_anchors = en
            break
    if not scene_anchors and scene:
        scene_anchors = _clean(zh_to_en(scene, 120), 8)

    # 特效锚点：剧本里的光/震/落石，SD1.5 只有给具体名词才画得出来
    fx_text = " ".join(str(shot.get(k) or "") for k in
                       ("action_en", "narration", "image_prompt", "act"))
    FX_MAP = (
        ("银光", "glowing silver light, radiant beam, magical glow"),
        ("光柱", "vertical beam of light piercing the sky"),
        ("发光", "glowing effect, emissive light"),
        ("光", "glowing light, light rays"),
        ("震", "shaking ground, falling debris, dust cloud"),
        ("砖", "falling bricks, crumbling stones"),
        ("烟", "smoke and dust in the air"),
        ("风", "wind blowing, swirling leaves"),
        ("雨", "rain streaks, wet reflections"),
        ("雪", "(falling snow:1.3), snowflakes, snow covered ground, cold breath"),
        ("火", "fire glow, embers"),
        # 英文键：action_en / image_prompt_en 里写的是英文，
        # 只给中文键的话「雪夜」这类天气永远匹配不到（旧版 #1 没画出雪就是这个原因）
        ("snow", "(falling snow:1.3), snowflakes, snow covered ground, cold breath"),
        ("rain", "rain streaks, wet reflections"),
        ("wind", "wind blowing, swirling leaves"),
        ("fire", "fire glow, embers"),
        ("blood", "blood stains, blood splatter"),
        ("light", "glowing light, light rays"),
    )
    fx = ""
    for kw, en in FX_MAP:
        if kw in fx_text:
            fx = en
            break

    chars = shot.get("characters") or []
    # 死物/空镜在语义上就是「没有人」：即使剧本列了角色也要忽略，
    # 否则会画出一个人物特写而不是道具
    st0 = str(shot.get("shot_type") or "")
    if st0 in ("死物", "空镜"):
        chars = []
    char_count = len(chars)

    # 双人同框：两个人都必须写，且各分 9 词。
    # 旧版只取 chars[:1]，第二个人完全没有描述 →
    # 模型要么只画一个人，要么把两个人画成同一张脸。
    # 双人同框：短句 + 位置绑定（左/右），并显式声明一男一女。
    # 这样 SD1.5 才不会把两段外观混成一个人，也不会被 1girl 带偏性别。
    # 特写/近景：换成头肩外观，避免被画成中景全身
    _is_face_shot = st0 in ("特写", "近景") and char_count == 1
    if _is_face_shot:
        _fn = str(chars[0]).strip()
        look = CHAR_FACE.get(_fn, character_look(chars[:1], limit=10))
    elif char_count >= 2:
        def _c2p(n):
            return CHAR_LOOK_2P.get(str(n).strip(), '')

        a, b = str(chars[0]).strip(), str(chars[1]).strip()
        la, lb = _c2p(a), _c2p(b)
        a_male = a in MALE_SET
        b_male = b in MALE_SET
        if a_male != b_male:
            pair = "one male one female, "
        elif a_male:
            pair = "2boys, "
        else:
            pair = "2girls, "
        seg = [pair.rstrip(', ')]
        if la:
            seg.append(la + " left")
        if lb:
            seg.append(lb + " right")
        look = ", ".join(seg)
    else:
        look = character_look(chars[:1], limit=16)

    st = str(shot.get("shot_type") or "")
    # 室内/室外要分开描述远景：室内写 "vast landscape" 会把屋子画成野外
    is_interior = any(k in scene for k in ("屋内", "室内", "屋", "房", "殿", "厅", "塔内", "家"))
    comp_map = {
        # 远景不写 distant figure：和 no people 冲突时会画出人
        "远景": ("wide shot, full room visible, interior space, environment dominant"
                 if is_interior
                 else "wide establishing shot, environment dominant, vast landscape"),
        "中景": "medium shot, waist up, face clearly visible",
        "近景": "medium close-up, chest up, face prominent",
        "特写": "(headshot:1.4), portrait, face only, cropped at shoulders, detailed eyes",
        "空镜": "empty room, background art, no characters",
        "死物": "object close-up, props only, shallow depth, bare wooden floor",
        "过肩": "over the shoulder shot, blurred foreground shoulder",
    }
    comp = comp_map.get(st, "medium shot, waist up")

    # 双人同框的构图约束：不写的话模型会画成单人，或者两个人叠在一起。
    # 用紧凑版 —— 双人镜头还要留给「动作」足够预算，否则剧情会被裁掉。
    if char_count >= 2:
        # 权重必须给足：(2people:1.4) 实测才能稳定出现两个人，
        # 光写 2people 时 SD1.5 仍只画一个。
        two_comp = {
            "远景": "(2people:1.4), two people, wide shot",
            "中景": "(2people:1.4), two people, medium shot",
            "近景": "(2people:1.4), two people, medium close-up",
            "特写": "(2people:1.4), two people, close-up on faces",
            "过肩": "(2people:1.4), two people, over the shoulder shot",
            "空镜": "(2people:1.4), two people, wide shot",
            "死物": "(2people:1.4), two people, medium shot",
        }
        comp = two_comp.get(st, "2people, medium shot")

    # 特写要分「脸」和「手/道具」：否则手部特写会被画成胸像
    eye_text = " ".join(str(shot.get(k) or "") for k in
                         ("action_en", "image_prompt_en", "narration", "act")).lower()
    HAND_KEYS = ("hand", "finger", "wrist", "palm", "knuckle", "blood",
                 "wound", "cloth", "basin", "needle", "thread",
                 "mend", "sew", "wipe")
    FACE_KEYS = ("looking up", "looking at", "face", "expression", "eyes",
                 "frown", "gaze", "staring", "lips", "tear")
    # 有明确脸部线索时优先画脸，避免"抬头看他"被判成手部特写
    face_hint = any(k in eye_text for k in FACE_KEYS)
    if st == "特写" and any(k in eye_text for k in HAND_KEYS) and not face_hint:
        comp = ("close-up of hands, detailed fingers, skin texture, veins, "
                "shallow depth of field, hands fill the frame")
        hand_shot = True
    else:
        hand_shot = False

    # ---- 机位：把剧本的 camera 字段翻成构图词，打破"永远正面站立" ----
    CAMERA_MAP = {
        "俯视": "high angle, looking down",
        "俯拍": "high angle, looking down",
        "仰视": "low angle, looking up",
        "仰拍": "low angle, looking up",
        "横移": "side view, profile",
        "侧移": "side view, profile",
        "缓推": "eye level, slight depth",
        "推近": "eye level, slight depth",
        "拉远": "wider framing, pulled back",
        "固定": "eye level, locked framing",
        "跟随": "tracking shot, side view",
        "旋转": "dutch angle, tilted frame",
    }
    cam_zh = str(shot.get("camera") or "").strip()
    cam = CAMERA_MAP.get(cam_zh, "")
    if cam_zh and not cam:
        for k, v in CAMERA_MAP.items():
            if k in cam_zh:
                cam = v
                break

    # 构图反向约束：SD1.5 对 close-up 响应弱，必须显式排除全身
    if hand_shot:
        extra_neg = (", full body, wide shot, long shot, two people, "
                     "distant, standing figure, (upper body:1.2)")
    elif st in ("特写", "近景"):
        extra_neg = (", full body, wide shot, long shot, standing figure, "
                     "distant, small face, multiple people, (shoulders:1.3), "
                     "(torso:1.3), hands, (child:1.3), "
                     "back view, from behind, (turned away:1.3), "
                     "tiny figure, distant figure")
    elif st == "中景":
        # 中景要看清脸，排除背影和过小人物
        extra_neg = (", back view, from behind, (turned away:1.2), "
                     "tiny figure, distant figure, full body")
    elif st == "死物":
        extra_neg = ", person, face, hands, full body"
    else:
        extra_neg = ""

    # ---- 动作：优先 LLM 的结构化字段，其次英文描述 ----
    action = ""
    for key in ("action_en", "image_prompt_en"):
        v = str(shot.get(key) or "").strip()
        if v:
            action = _clean(v, 12)
            break
    if not action:
        act = str(shot.get("act") or shot.get("narration") or "")
        action = _clean(zh_to_en(act, 200), 10)

    # 动作里若带服装颜色，会和记忆外观打架 → 去掉颜色词
    action = _strip_conflicting_color(action, chars)

    # ---- 情绪：没有字段就从旁白/动作里推断 ----
    emotion = str(shot.get("emotion_en") or "").strip()
    if not emotion:
        emotion = _infer_emotion(shot)

    _style = (STYLE_PREFIX_SDXL
              if getattr(config, "IMAGE_MODEL", "sd15") == "sdxl"
              else STYLE_PREFIX)
    parts = [_style.strip().rstrip(",")]

    # 物件清单紧跟风格词，保证不被截断（这是画面信息量的核心）
    props = shot.get("props_in_frame") or []
    if isinstance(props, str):
        props = [props]
    if props:
        en_props = []
        for pz in props[:3]:
            pz = str(pz).strip()
            if not pz:
                continue
            t = zh_to_en(pz, 40) if any(ord(c) > 127 for c in pz) else pz
            t = _clean(t, 3)
            if t:
                en_props.append(t)
        if en_props:
            # SDXL 认自然语言，"visible: xxx" 这种冒号前缀会被当成平面图层
            if getattr(config, "IMAGE_MODEL", "sd15") == "sdxl":
                parts.append(", ".join(en_props))
            else:
                parts.append("visible: " + ", ".join(en_props))

    # 无人物镜头：显式禁止画人（二次元模型会自己塞人）
    if not chars:
        # ⚠️ 不能写 "scenery, landscape"：这两个词会把画面推向写实风光照，
        #    实测空镜出来是现代木椅 + 瓷砖地 + 日式庭院。
        parts.insert(0, "empty interior, no people, background art")
        # 古风锚点：不加的话空镜会跑成现代走廊/水泥楼
        parts.append("ancient chinese architecture, stone ruins, "
                     "weathered mud walls, bare plank floor, hanfu era setting")
        if st == "死物":
            parts.append("still life, props on old wooden table, "
                         "detailed fabric texture, no people")
        parts.append("(no humans:1.4), environment only")

    # 成年约束：SD1.5 动漫底模容易把 1boy 画成小孩
    if "1boy" in look or "1man" in look:
        look = look + ", adult"
        extra_neg += ", (child:1.4), (kid:1.4), chibi, loli, (loli:1.4)"

    # 中景/近景/特写：强制人物面向镜头，否则会画成背影
    if chars and char_count < 2 and st in ("中景", "近景", "特写") and not hand_shot:
        parts.append("(face visible:1.2)")
        # 没有坐/跪/躺的动作时，强制站姿，避免模型全画成盘腿坐
        low = (" ".join(str(shot.get(k) or "") for k in
                         ("action_en", "image_prompt_en", "narration", "act"))
               ).lower()
        SEATED = ("sitting", "sit ", "seated", "kneel", "kneeling",
                  "crouch", "lying", "lie ", "kneel down", "stooped")
        if not any(w in low for w in SEATED):
            parts.append("(standing:1.2)")

    if hand_shot:
        # 手部特写：保留人物外观（否则模型不知道手是什么），
        # 但用构图词把镜头压到手部，脸只允许露出一点边缘
        parts.append("close-up of hands, hands fill the frame, fingers in sharp focus")
        if action:
            parts.append(action)
        parts.append("detailed hands, skin texture, (face partially out of frame:1.2)")
        seg_list = []
    else:
        seg_list = [(comp, 6), (cam, 4), (action, 12), (emotion, 5)]
    for seg, budget in seg_list:
        if seg:
            parts.append(seg)

    env_motion = str(shot.get("env_motion") or "").strip()
    _is_sdxl = getattr(config, "IMAGE_MODEL", "sd15") == "sdxl"
    if env_motion and not _is_sdxl:
        # SD1.5 需要 "motion:" 这种结构化提示；SDXL 会把它当图层说明，跳过
        parts.append("motion: " + _clean(env_motion, 6))

    depth = str(shot.get("depth") or "").strip()
    if depth and not _is_sdxl:
        d_en = zh_to_en(depth, 80) if any(ord(c) > 127 for c in depth) else depth
        d_en = _clean(d_en, 8)
        if d_en:
            parts.append("depth layers: " + d_en)

    # 场景锚点 + 特效锚点（放在人物外观之前，权重更高）
    if scene_anchors:
        parts.append(scene_anchors)
    if fx:
        parts.append(fx)


    if look:
        parts.append(look)

    # 武器：强制直剑，避免画成镰刀
    if any(k in raw for k in ("剑", "刀")):
        parts.append("straight broken sword")

    lights = _match(LIGHTING, raw + " " + str(shot.get("act") or ""))
    light = _short_light(lights, raw)
    if light:
        parts.append(light)

    mats = _match(MATERIALS, raw)
    if mats:
        parts.append(_clean(mats[0], 4))

    parts.append(CINEMA_SUFFIX.strip())

    # ============================================================
    # 负面词：CLIP 只读前 77 token，超了整段丢弃。
    # 旧版把所有词直接拼接（NEGATIVE_COMPACT + extra_neg + 无人段），
    # 长度远超 77 token —— 结果 (person:1.9)、bathtub、japanese 这些
    # 真正起作用的词全在被截断的尾巴上，等于没写。
    # 下面改成「按优先级排序 + 限长」，保证关键约束一定排在最前面。
    # ============================================================
    MAX_NEG_WORDS = 58
    _neg_items = []

    # ① 无人镜头：先把人压死（最优先，否则空镜/死物必出人）
    if not chars:
        _neg_items.append((1, "(person:1.9), (human:1.9), (1girl:1.9), (1boy:1.9), "
                              "(girl:1.8), (woman:1.8), (man:1.8), (figure:1.5), "
                              "people, crowd, character, portrait, solo"))

    # ② 有男性角色：压女性化（Counterfeit 默认把 1boy 画成少女）
    if any(str(c).strip() in MALE_SET for c in chars):
        if char_count >= 2:
            # 双人混合时不能写 2girls（会把女生也删掉），只压"女性化特征"
            _neg_items.append((2, "(female:1.3), feminine face, long eyelashes, "
                                  "breasts, makeup, androgynous"))
        else:
            _neg_items.append((2, "(female:1.4), (2girls:1.4), feminine face, "
                                  "long eyelashes, breasts, makeup, androgynous"))

    # ③ 现代/日式元素（内景重灾区）
    _neg_items.append((3, "(bed:1.7), (mattress:1.6), (sofa:1.6), "  
                          "japanese room, tatami, shoji screen, modern room, "
                          "wallpaper, glass window, ceiling lamp, "
                          "pillow, bathtub, sink, washbasin, "
                          "cushion, rug, plush toy, tiled floor, "
                          "modern furniture, western furniture"))

    # ④ 构图反向约束（原来放在 extra_neg，容易被挤掉，挪到这里）
    if char_count < 2:
        _neg_items.append((4, "2girls, multiple people, crowd, two people"))

    # ④.5 SDXL 特别爱多画人，必须显式压
    if getattr(config, "IMAGE_MODEL", "sd15") == "sdxl":
        _neg_items.append((4, "extra person, three people, crowd, "
                              "extra figures in background"))

    # ⑤ 质量与画风
    _neg_items.append((5, "photo, photorealistic, 3d, watermark, text, signature, "
                          "logo, cropped head, out of frame, bad anatomy, "
                          "extra hands, extra fingers, blurry, lowres, "
                          "worst quality, low quality"))

    # ⑥ 镜头类型补充约束
    if st == "死物" or st == "空镜":
        _neg_items.append((4, "sitting, standing figure, person sitting"))
    elif st in ("特写", "近景") and not hand_shot:
        _neg_items.append((4, "full body, wide shot, standing figure, legs, feet"))

    _neg_items.sort(key=lambda x: x[0])
    _neg_acc, _neg_kept = 0, []
    for _p, _txt in _neg_items:
        for _piece in _txt.split(', '):
            _w = len(_piece.split())
            if _neg_acc + _w > MAX_NEG_WORDS:
                break
            _neg_kept.append(_piece)
            _neg_acc += _w
    neg = ", ".join(_neg_kept)

    # ---- 按优先级裁剪到 MAX_WORDS ----
    # 顺序：style > 物件 > 构图 > 场景 > 动作 > 动态 > 外观 > 其他
    # ⚠️ 场景锚点必须排在最前面几位。旧版把它归到 90（最低优先），
    #    预算一满就先把它丢掉 —— 结果 40 镜里几乎没有一镜带场景描述，
    #    模型只能自由发挥，于是"古塔变日式木屋""破屋变室外"。
    try:
        _priority = []
        for _seg in parts:
            _p = 90
            if _seg == parts[0]:
                _p = 1
            elif _seg.startswith("visible: "):
                _p = 2
            elif _seg.startswith(("scenery", "no people")):
                _p = 3
            elif any(k in _seg for k in ("shot", "close-up", "wide")):
                _p = 4
            elif scene_anchors and _seg == scene_anchors:
                _p = 5
            elif look and _seg == look:
                _p = 6          # 外观必须保命：丢了角色就画成路人
            elif action and _seg == action:
                _p = 7
            elif emotion and _seg == emotion:
                _p = 8
            elif _seg.startswith("motion: "):
                _p = 9
            elif _seg.startswith("depth layers: "):
                _p = 11         # 本地说不清景深，最后才保留
            elif "1boy" in _seg or "1girl" in _seg or "1man" in _seg or "1old" in _seg:
                _p = 6
            _priority.append((_p, _seg))
        _priority.sort(key=lambda x: x[0])
        _acc, _kept = 0, []
        for _p, _seg in _priority:
            _w = len(_seg.split())
            if _acc + _w <= _max_words():
                _kept.append(_seg)
                _acc += _w
        parts = _kept
    except Exception:
        pass

    positive = ", ".join(p for p in parts if p)
    positive = re.sub(r",\s*,", ",", positive).strip().rstrip(",")
    positive = re.sub(r"\s{2,}", " ", positive)
    words = positive.split()
    if len(words) > _max_words():
        positive = " ".join(words[:_max_words()]).rstrip(",")

    return positive, neg


def _short_light(lights, text):
    """挑一条短的光影描述。时间优先于室内——天亮不能被判成昏暗室内。"""
    if any(k in text for k in ("天亮", "清晨", "晨", "黎明", "破晓")):
        return "morning light, soft warm glow, hazy air"
    if any(k in text for k in ("黄昏", "夕阳", "落日", "傍晚")):
        return "golden hour, long shadows, warm rim light"
    if any(k in text for k in ("深夜", "夜", "月")):
        return "night, moonlight, rim light"
    if any(k in text for k in ("油灯", "灯", "烛")):
        return "candlelight, deep shadows"
    if lights:
        return min(lights, key=len)
    return "dramatic side lighting"


NEGATIVE_COMPACT = (
    "cropped head, face cut off, out of frame, "
    "scythe, curved blade, sickle, "
    "2girls, multiple people, crowd, "
    "white hair, silver hair, "
    "photo, photorealistic, 3d, "
    "watermark, text, signature, logo, "
    "extra hands, bad anatomy, extra fingers, "
    "lowres, blurry, worst quality, low quality"
)


def generate_one(image_prompt, negative_prompt=None, seed=None, size=None,
                 workflow=None, out_path=None, max_retry=None, use_ref=False,
                 require_head=True, keep_seed_on_retry=False, ref_image=None):
    """生成一张合格图片，自动换 seed 重试。

    返回 (图片路径, 使用的 seed, 尝试次数)
    """
    if not is_alive():
        raise ComfyError("ComfyUI 没启动，请先在 %s 跑起来" % config.COMFY_HOST)

    if workflow is not None:
        wf = workflow
    elif getattr(config, "IMAGE_MODEL", "sd15") == "sdxl":
        # SDXL：用 SDXL 版 IP-Adapter（需先上传参考图，名字由上层传入）
        wf = build_sdxl_workflow(ref_image=ref_image)
    elif use_ref and has_reference():
        wf = load_ipadapter_workflow()
        if wf is None:
            wf = load_workflow()
        else:
            wf = set_reference_image(wf, "ref.png")
    else:
        wf = load_workflow()

    if negative_prompt is None:
        negative_prompt = STYLE_NEGATIVE
    W, H = size or (config.image_gen_size()
                            if getattr(config, "IMAGE_MODEL", "sd15") == "sdxl"
                            else (config.IMAGE_DRAFT_W, config.IMAGE_DRAFT_H))
    retries = max_retry if max_retry is not None else config.IMAGE_MAX_RETRY
    base_seed = seed if seed is not None else random.randint(0, 2 ** 31 - 1)

    last_reason = ""
    for i in range(retries):
        # 有人物的镜头：重试时保持同一个 seed，只加强构图约束。
        # 否则 seed 一变，同一角色就换脸了（这是第 4 集换脸的原因）。
        if keep_seed_on_retry:
            use_seed = base_seed
            prompt_i = image_prompt
            if i > 0:
                prompt_i = (image_prompt +
                            ", (full head:1.25), head fully in frame, "
                            "wider framing, no cropping")
        else:
            use_seed = base_seed if i == 0 else base_seed + i * 7919
            prompt_i = image_prompt

        # 人物镜头永不换 seed：一致性比「切头」更重要。
        # 切头可以靠构图词缓解，换脸观众一眼就能看出来。

        w = apply_prompt(wf, prompt_i, negative_prompt, use_seed, W, H)
        try:
            pid, _ = queue_prompt(w)
            files = wait_for(pid)
        except Exception as exc:  # noqa: BLE001
            last_reason = repr(exc)
            continue

        target = out_path or Path(config.ASSETS_DIR) / ("tmp_%d.png" % use_seed)
        download_image(files[0], target)

        ok, reason = check_image(target)
        if ok:
            # 再查一次「顶部切头」——SD1.5 常常把头顶画到画外
            cut, why = (False, '') if not require_head else head_is_cut(target)
            if not cut:
                return Path(target), use_seed, i + 1
            # 人物镜头：同 seed 已经试到第 4 次还切头，就带着切头收下，
            # 不能为了构图把脸换掉
            if keep_seed_on_retry and i >= 3:
                print("    [出图] 同 seed 仍切头（%s），为保证人物一致直接采用" % why)
                return Path(target), use_seed, i + 1
            reason = "切头（%s）" % why
            ok = False

        last_reason = reason
        Path(target).unlink(missing_ok=True)

    raise ComfyError("重试 %d 次仍不合格，最后一次原因：%s" % (retries, last_reason))


def upscale_hires(image_path, out_path=None, scale=1.5):
    """高清修复：把合格图放大到成片尺寸。

    优先用 ffmpeg 缩放（不耗显存）；要真正的 AI 放大，
    在 ComfyUI 工作流里加 Upscale 节点后走 generate_one。
    """
    from .tts import ffmpeg_path
    import subprocess

    src = Path(image_path)
    dst = Path(out_path) if out_path else src.with_name(src.stem + "_hires.png")
    W, H = config.IMAGE_FINAL_W, config.IMAGE_FINAL_H
    # ffmpeg 不允许输入输出同一文件，先写临时文件再替换
    tmp = dst.with_name(dst.stem + "._tmp" + dst.suffix)
    cmd = [ffmpeg_path(), "-y", "-i", str(src),
           "-vf", "scale=%d:%d:flags=lanczos" % (W, H),
           str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0 or not tmp.exists():
        raise ComfyError("放大失败：%s" % (r.stderr or "")[-300:])
    if tmp.resolve() != src.resolve():
        src.unlink(missing_ok=True)
    tmp.replace(dst)
    return dst
