# -*- coding: utf-8 -*-
"""全局配置：从 .env 读取，带默认值。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # D:\...\manju
PROJECTS_DIR = ROOT / "projects"
ASSETS_DIR = ROOT / "assets"


def _load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def env(key, default=""):
    return os.environ.get(key, default) or default


# ---------------- 大模型（OpenAI 兼容协议）----------------
LLM_BASE_URL = env("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = env("LLM_MODEL", "deepseek-chat")
LLM_API_KEY = env("LLM_API_KEY", "")
LLM_TIMEOUT = int(env("LLM_TIMEOUT", "90"))
LLM_MAX_TOKENS = int(env("LLM_MAX_TOKENS", "4000"))

# ---------------- 语音 ----------------
# 角色 → edge-tts 音色。未登记的角色走 DEFAULT_VOICE
VOICE_MAP = {
    # ---- 本剧角色 ----
    "林越": "zh_male_qingshuangnanda_uranus_bigtts",
    "月白": "zh_female_tianmeixiaoyuan_uranus_bigtts",
    "旁白": "zh_male_yuanboxiaoshu_uranus_bigtts",
    # ---- 通用角色类型 ----
    "男主": "zh_male_yangguangqingnian_uranus_bigtts",
    "女主": "zh_female_shuangkuaisisi_uranus_bigtts",
    "反派": "zh_male_beijingxiaoye_uranus_bigtts",
    "老者": "zh_male_yuanboxiaoshu_uranus_bigtts",
    "少年": "zh_male_qingshuangnanda_uranus_bigtts",
    "少女": "zh_female_tianmeixiaoyuan_uranus_bigtts",
    "将军": "zh_male_yuanboxiaoshu_uranus_bigtts",
    "系统": "zh_female_gaolengyujie_uranus_bigtts",
    "家丁甲": "zh_male_yangguangqingnian_uranus_bigtts",
    "林越妻子": "zh_female_tianmeixiaoyuan_uranus_bigtts",
}

# 名字里带这些字，按男性音色
MALE_HINTS = ("爷", "叔", "伯", "父", "哥", "郎", "生", "丁", "甲", "乙", "丙",
              "老者", "长老", "王", "将军", "护卫", "弟子")
FEMALE_HINTS = ("女", "娘", "姐", "妹", "姑", "婆", "妃", "后", "妻", "母")
DEFAULT_VOICE = "zh_male_yuanboxiaoshu_uranus_bigtts"
TTS_RATE = env("TTS_RATE", "+0%")     # 语速，可写 -10% / +15%
TTS_VOLUME = env("TTS_VOLUME", "+0%")

# ---------------- 画面 ----------------
COMFY_HOST = env("COMFY_HOST", "http://127.0.0.1:8188")
COMFY_WORKFLOW = env("COMFY_WORKFLOW", str(ROOT / "assets" / "workflow_api.json"))
IMAGE_DRAFT_W, IMAGE_DRAFT_H = 720, 1280     # 抽卡尺寸（原生 9:16）
IMAGE_FINAL_W, IMAGE_FINAL_H = 1080, 1920    # 直接到视频分辨率（9:16）

# ---------------- 成片输出分辨率 ----------------
# 720p / 1080p / 4k。4k 走本地 Lanczos 超分（免费），
# 想真原生 4K 就把 VIDEO_PROVIDER 换成 kling 并在 resolution 里指定。
OUTPUT_RESOLUTION = env("OUTPUT_RESOLUTION", "1080p")
OUTPUT_SIZES = {
    "720p": (720, 1280),
    "1080p": (1080, 1920),
    "4k": (2160, 3840),
}
OUTPUT_UPSCALE_SHARPEN = env("OUTPUT_UPSCALE_SHARPEN", "0.5")   # 放大后锐化强度

# 抽卡尺寸：draft=720×1280（快，约 13 秒/张）｜final=1080×1920（慢，约 23 秒/张）
# 成片最后会统一到 OUTPUT_RESOLUTION，所以抽卡用 draft 通常够用
IMAGE_GEN_SIZE = env("IMAGE_GEN_SIZE", "draft")
IMAGE_GEN_SIZES = {
    "draft": (720, 1280),
    "final": (1080, 1920),
}


def image_gen_size():
    """返回抽卡用的 (宽, 高)。"""
    if IMAGE_MODEL == "sdxl":
        return (SDXL_FINAL_W, SDXL_FINAL_H) if IMAGE_GEN_SIZE == "final" else (SDXL_DRAFT_W, SDXL_DRAFT_H)
    return IMAGE_GEN_SIZES.get(IMAGE_GEN_SIZE, (720, 1280))
IMAGE_MAX_RETRY = int(env("IMAGE_MAX_RETRY", "12"))
IMAGE_QUALITY_MIN_VAR = float(env("IMAGE_QUALITY_MIN_VAR", "600"))

# ---------------- 视频 ----------------
GRADE_STRENGTH = 1.0                 # 电影调色强度
VIDEO_W, VIDEO_H = 1080, 1920      # 抖音竖屏
VIDEO_FPS = int(env("VIDEO_FPS", "30"))
ZOOM_SPEED = float(env("ZOOM_SPEED", "0.0008"))   # Ken Burns 每秒缩放增量
USE_NVENC = env("USE_NVENC", "1") == "1"

# ---------------- 通知 ----------------
FEISHU_WEBHOOK = env("FEISHU_WEBHOOK", "")
FEISHU_SECRET = env("FEISHU_SECRET", "")
# 飞书机器人「自定义关键词」：消息必须包含才放行
FEISHU_KEYWORD = env("FEISHU_KEYWORD", "告警漫剧")

# ---------------- 调度 ----------------
# 凌晨 00:30 之后跑，走低价时段（配合操作系统的计划任务使用）
BATCH_HOUR = int(env("BATCH_HOUR", "0"))
BATCH_MINUTE = int(env("BATCH_MINUTE", "30"))

# ---------------- 插件供应商（改这里换技术栈）----------------
SCRIPT_PROVIDER = env("SCRIPT_PROVIDER", "deepseek")
IMAGE_PROVIDER = env("IMAGE_PROVIDER", "comfyui")
VIDEO_PROVIDER = env("VIDEO_PROVIDER", "null")
TTS_PROVIDER = env("TTS_PROVIDER", "edge")
BGM_PROVIDER = env("BGM_PROVIDER", "synth")
EDIT_PROVIDER = env("EDIT_PROVIDER", "rhythm")
LOG_PROVIDER = env("LOG_PROVIDER", "excel")

# ---- 扩展能力：3D 场景 ----
SCENE3D_PROVIDER = env("SCENE3D_PROVIDER", "placeholder")
SCENE3D_FALLBACK = env("SCENE3D_FALLBACK", "placeholder")
BLENDER_EXE = env("BLENDER_EXE", "")

# ---- 扩展能力：电台 ----
RADIO_PROVIDER = env("RADIO_PROVIDER", "simple")
RADIO_FALLBACK = env("RADIO_FALLBACK", "manual")

# ---- 外部工具 ----
FFMPEG_EXE = env("FFMPEG_EXE", "")
FFPROBE_EXE = env("FFPROBE_EXE", "")

MEMORY_PROVIDER = env("MEMORY_PROVIDER", "json_file")

# ---------- 各家大模型的 Key（用哪个填哪个）----------
DASHSCOPE_API_KEY = env("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = env("DASHSCOPE_BASE_URL", "")
MOONSHOT_API_KEY = env("MOONSHOT_API_KEY", "")
MOONSHOT_BASE_URL = env("MOONSHOT_BASE_URL", "")
ZHIPU_API_KEY = env("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = env("ZHIPU_BASE_URL", "")
ARK_API_KEY = env("ARK_API_KEY", "")
ARK_BASE_URL = env("ARK_BASE_URL", "")
SILICONFLOW_API_KEY = env("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = env("SILICONFLOW_BASE_URL", "")
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
OPENAI_BASE_URL = env("OPENAI_BASE_URL", "")
OLLAMA_HOST = env("OLLAMA_HOST", "")
OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", "")

# ---------- 图像 ----------
# 豆包 Seedream（火山方舟），复用 ARK_API_KEY / JIMENG_BASE_URL
# 实测画风准确度比本地 SD1.5 高 5 分，汉服结构能画对
DOUBAO_IMAGE_MODEL = env("DOUBAO_IMAGE_MODEL", "doubao-seedream-5-5-261028")
DOUBAO_IMAGE_SIZE = env("DOUBAO_IMAGE_SIZE", "1080x1920")   # 竖屏
DOUBAO_IMAGE_PRICE = env("DOUBAO_IMAGE_PRICE", "0.20")      # 元/张
DOUBAO_WATERMARK = env("DOUBAO_WATERMARK", "off")           # off=去掉"豆包AI生成"

# 全局画风后缀：所有出图自动拼接在 prompt 后面
# 空字符串 = 不加
# 例：中国水墨画风格，宣纸质感，水墨笔触明显，写意留白，低饱和浅灰调
IMAGE_STYLE_SUFFIX = env("IMAGE_STYLE_SUFFIX", "")

# 首跑时自动生成角色定妆照（用于跨镜一致性）
# 关掉的话需要手动放 chapter_dir/characters/<角色名>.png
AUTO_CHAR_REF = env("AUTO_CHAR_REF", "1").lower() not in ("0", "false", "off")

# 出完图后自动发现离群镜头并用定妆照重做
AUTO_FIX_OUTLIERS = env("AUTO_FIX_OUTLIERS", "0").lower() not in (
    "0", "false", "off")

# 视频生成后自动做一致性校验（检测人物崩坏/画面漂移）
AUTO_CHECK_VIDEO = env("AUTO_CHECK_VIDEO", "1").lower() not in (
    "0", "false", "off")

# 视频校验阈值
VIDEO_FIRST_SIM = env("VIDEO_FIRST_SIM", "0.82")   # 第一帧 vs 输入图
VIDEO_DRIFT_SIM = env("VIDEO_DRIFT_SIM", "0.55")   # 中间/结尾 vs 第一帧

# 预设画风（Web 设置页里可一键选）
IMAGE_STYLE_PRESETS = {
    "水墨": ("中国水墨画风格，宣纸质感，水墨笔触明显，写意留白，"
             "低饱和浅灰调"),
    "日系": ("日系动画风格，赛璐璐上色，清新明快，柔和光源，"
             "细腻线条"),
    "写实": ("电影级写实摄影，浅景深，自然光，真实皮肤质感，"
             "35mm 胶片质感"),
    "国漫": ("中国动画风格，厚涂笔触，色彩饱满，光影层次丰富，"
             "电影感构图"),
    "古风": ("古风插画，工笔重彩，绢本设色，细腻纹理，典雅色调"),
}

SD_WEBUI_URL = env("SD_WEBUI_URL", "http://127.0.0.1:7860")
OPENAI_IMAGE_API_KEY = env("OPENAI_IMAGE_API_KEY", "")
OPENAI_IMAGE_BASE_URL = env("OPENAI_IMAGE_BASE_URL", "")
OPENAI_IMAGE_MODEL = env("OPENAI_IMAGE_MODEL", "")
OPENAI_IMAGE_PRICE = env("OPENAI_IMAGE_PRICE", "0.05")

# ---------- 图生视频 ----------
KLING_API_KEY = env("KLING_API_KEY", "")
KLING_BASE_URL = env("KLING_BASE_URL", "https://api-singapore.klingai.com")
KLING_MODEL = env("KLING_MODEL", "kling-2.6")
KLING_DURATION = int(env("KLING_DURATION", "5"))        # 只能 5 或 10
KLING_RESOLUTION = env("KLING_RESOLUTION", "1080p")     # 720p / 1080p
KLING_AUDIO = env("KLING_AUDIO", "off")                 # off / native
KLING_IMAGE_FORMAT = env("KLING_IMAGE_FORMAT", "data_uri")  # data_uri / base64

# 兼容旧配置名（早期按 JWT 写的，已经不用了）
KLING_ACCESS_KEY = env("KLING_ACCESS_KEY", "")
KLING_SECRET_KEY = env("KLING_SECRET_KEY", "")
JIMENG_API_KEY = env("JIMENG_API_KEY", "")
JIMENG_BASE_URL = env("JIMENG_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
JIMENG_MODEL = env("JIMENG_MODEL", "doubao-seedance-2-0-mini-260615")
JIMENG_RESOLUTION = env("JIMENG_RESOLUTION", "720p")
JIMENG_DURATION = int(env("JIMENG_DURATION", "4"))     # 最短 4 秒，比可灵省
JIMENG_AUDIO = env("JIMENG_AUDIO", "off")             # off / native
JIMENG_PRICE_PER_SEC = env("JIMENG_PRICE_PER_SEC", "0.60")
VIDU_API_KEY = env("VIDU_API_KEY", "")
HAILUO_API_KEY = env("HAILUO_API_KEY", "")
HAILUO_GROUP_ID = env("HAILUO_GROUP_ID", "")

# ---------- 语音 ----------
VOLC_TTS_APPID = env("VOLC_TTS_APPID", "")
VOLC_TTS_TOKEN = env("VOLC_TTS_TOKEN", "")

# ---------- 扩展位：真人 / 3D / 对口型（接口已留，未实现）----------
# 填了才算「可用」，不填时下拉里会显示但会提示未配置
REALISTIC_MODEL = env("REALISTIC_MODEL", "")        # 写实底模文件名
SDXL_MODEL = env("SDXL_MODEL", "animagine-xl-4.0.safetensors")   # SDXL 底模文件名

# ---------- 画面模型开关 ----------
# sd15 : Counterfeit-V2.5，约 11 秒/张，风格细腻但构图服从弱
# sdxl : Animagine XL 4.0，约 28 秒/张，特写/空镜/双人强得多
#        ⚠ 必须以 --lowvram 启动 ComfyUI，否则加载模型时进程直接崩
IMAGE_MODEL = env("IMAGE_MODEL", "sd15")             # sd15 | sdxl
SDXL_DRAFT_W, SDXL_DRAFT_H = 768, 1344               # 9:16 附近，64 的倍数
SDXL_FINAL_W, SDXL_FINAL_H = 832, 1472
SDXL_STEPS = int(env("SDXL_STEPS", "26"))
SDXL_CFG = float(env("SDXL_CFG", "6.0"))
# 整集一致性：同一 base seed + 固定步长偏移
# 实测同一集用固定 seed 家族，人物脸和画风都更稳
SDXL_BASE_SEED = int(env("SDXL_BASE_SEED", "20261030"))
SDXL_SEED_STRIDE = int(env("SDXL_SEED_STRIDE", "7919"))

# 混合出图：这些镜头类型用 SDXL，其余用 SD1.5
# 只放"无人物"类型 —— 纯景物镜不会暴露画风差异，也不会换脸
# SDXL IP-Adapter（角色一致性）
# 实测 weight=0.75 时跨镜发型/服装/脸型最稳
SDXL_IPADAPTER = env("SDXL_IPADAPTER", "ip-adapter-plus_sdxl_vit-h.safetensors")
SDXL_CLIP_VISION = env("SDXL_CLIP_VISION",
                       "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors")
SDXL_IPADAPTER_WEIGHT = float(env("SDXL_IPADAPTER_WEIGHT", "0.55"))   # 单人镜；过高会把"定妆照构图"也搬进画面
SDXL_IPADAPTER_WEIGHT_PAIR = float(env("SDXL_IPADAPTER_WEIGHT_PAIR", "0.50"))
# 生效区间：end_at 提前结束，让提示词的场景/构图有机会接管
#   实测 1.0 时参考图构图（人物居中、背景空白）会污染整张图
SDXL_IPADAPTER_END_AT = float(env("SDXL_IPADAPTER_END_AT", "0.65"))

# 角色定妆照目录（IP-Adapter 参考图）
# 文件名约定：<角色名>_<序号>.png 自动匹配；双人镜用 双人_月白林越.png
CHARS_DIR = env("CHARS_DIR",
                str(ROOT / "projects" / "断剑月白" / "chapter_001" / "chars"))
PAIR_REF_NAME = env("PAIR_REF_NAME", "双人_月白林越.png")

SDXL_SHOT_TYPES = set(
    t.strip() for t in env("SDXL_SHOT_TYPES", "空镜").split(",") if t.strip()
)
BLENDER_EXE = env("BLENDER_EXE", "")                # blender 可执行文件
BLENDER_BLEND_FILE = env("BLENDER_BLEND_FILE", "")  # 3D 场景文件
BLENDER_SAMPLES = int(env("BLENDER_SAMPLES", "64"))

LIPSYNC_PROVIDER = env("LIPSYNC_PROVIDER", "")      # heygen/jianying/tencent
LIPSYNC_API_KEY = env("LIPSYNC_API_KEY", "")
LIPSYNC_PRICE_MIN = env("LIPSYNC_PRICE_MIN", "1.0")

VOICE_CLONE_PROVIDER = env("VOICE_CLONE_PROVIDER", "")
VOICE_CLONE_ID = env("VOICE_CLONE_ID", "")

# 降级链（逗号分隔）
SCRIPT_FALLBACK = env("SCRIPT_FALLBACK", "")
IMAGE_FALLBACK = env("IMAGE_FALLBACK", "placeholder")
VIDEO_FALLBACK = env("VIDEO_FALLBACK", "null")
TTS_FALLBACK = env("TTS_FALLBACK", "mock")
BGM_FALLBACK = env("BGM_FALLBACK", "none")
EDIT_FALLBACK = env("EDIT_FALLBACK", "")
LOG_FALLBACK = env("LOG_FALLBACK", "console")

# ---------------- 任务锁 ----------------
LOCK_TIMEOUT_MIN = int(env("LOCK_TIMEOUT_MIN", "3"))
LOCK_WAIT_MAX_MIN = int(env("LOCK_WAIT_MAX_MIN", "10"))
ALLOW_PARALLEL_SAME_PROJECT = env("ALLOW_PARALLEL_SAME_PROJECT", "0") == "1"

# ---------------- 重试与降级 ----------------
HEAD_CHECK_RETRY = int(env("HEAD_CHECK_RETRY", "3"))
VIDEO_MAX_RETRY = int(env("VIDEO_MAX_RETRY", "3"))
AUTO_FALLBACK = env("AUTO_FALLBACK", "1") == "1"
STRICT_MODE = env("STRICT_MODE", "0") == "1"

# ---------------- 预算（分项，单位元）----------------
SCRIPT_BUDGET = float(env("SCRIPT_BUDGET", "5"))
IMAGE_BUDGET = float(env("IMAGE_BUDGET", "10"))
TTS_BUDGET = float(env("TTS_BUDGET", "20"))
VIDEO_BUDGET_MAX = float(env("VIDEO_BUDGET_MAX", "100"))
COST_ALERT = float(env("COST_ALERT", "80"))

# ---------------- 磁盘与归档 ----------------
DISK_WARN_PERCENT = int(env("DISK_WARN_PERCENT", "85"))
PROJECT_DISK_MAX_MB = int(env("PROJECT_DISK_MAX_MB", "2048"))
AUTO_CLEAN_HISTORY = env("AUTO_CLEAN_HISTORY", "0") == "1"
HISTORY_KEEP_DAYS = int(env("HISTORY_KEEP_DAYS", "30"))
HISTORY_MAX_FILES = int(env("HISTORY_MAX_FILES", "100"))

# ---------------- 记忆 ----------------
MEMORY_MAX_CHARS = int(env("MEMORY_MAX_CHARS", "1200"))
MEMORY_CORRUPT_POLICY = env("MEMORY_CORRUPT_POLICY", "degrade")

# ---------------- 剧情连贯 ----------------
# 状态继承（伤痕/服装跨镜保持）+ 生成后一致性检查
# 设 0 可关闭
STORY_HOOKS_ENABLED = env("STORY_HOOKS", "1") == "1"

# ---------------- 插件调用监控 ----------------
# off=关闭  entry=只打入口  exit=只打出口(默认)  all=入口+出口  verbose=含返回值
PLUGIN_TRACE = env("PLUGIN_TRACE", "exit")

# ---------------- 任务队列 ----------------
# 同时跑几个任务（单卡 GPU 建议 1；纯云端可以 2~3）
JOB_MAX_CONCURRENT = int(env("JOB_MAX_CONCURRENT", "1"))

# ---------------- 限速 ----------------
# 格式："key=值,key2=值2"，key 可以是 "image/doubao" 或 "image/*"
# QPS：每秒最多几次调用
RATE_QPS = env("RATE_QPS", "")
# 并发：同时最多几个在飞
RATE_CONCURRENCY = env("RATE_CONCURRENCY", "")
# 日配额：每天最多几次
RATE_DAILY = env("RATE_DAILY", "")
# 全局默认（0 = 不限）
RATE_DEFAULT_QPS = float(env("RATE_DEFAULT_QPS", "0"))
RATE_DEFAULT_CONCURRENCY = int(env("RATE_DEFAULT_CONCURRENCY", "0"))
RATE_DEFAULT_DAILY = int(env("RATE_DEFAULT_DAILY", "0"))

# ---------------- 成本告警阈值 ----------------
# 0 = 不限。超阈值会在 Web 和日志里提示，但不会中断任务
ALERT_PER_SHOT = float(env("ALERT_PER_SHOT", "2.0"))     # 单镜成本上限（元）
ALERT_PER_JOB = float(env("ALERT_PER_JOB", "200"))       # 单任务上限（元）
ALERT_DAILY = float(env("ALERT_DAILY", "300"))           # 日累计上限（元）
ALERT_VRAM_MB = float(env("ALERT_VRAM_MB", "5800"))      # 显存上限（MB，6GB 卡留 344MB 余量）

# ---------------- 告警通知 ----------------
# 低于此级别不推送（info / warn / error / critical）
ALERT_MIN_LEVEL = env("ALERT_MIN_LEVEL", "warn")
# 同一告警的去重冷却（秒）
ALERT_COOLDOWN_SEC = int(env("ALERT_COOLDOWN_SEC", "300"))
# 自定义 Webhook（留空则不启用）
ALERT_WEBHOOK_URL = env("ALERT_WEBHOOK_URL", "")
# 飞书机器人复用 FEISHU_WEBHOOK / FEISHU_SECRET

# ---------------- 缓存与资源管理 ----------------
# 配额（MB，0 = 不限）
RES_QUOTA_PROJECT_MB = int(env("RES_QUOTA_PROJECT_MB", "2048"))
RES_QUOTA_TOTAL_MB = int(env("RES_QUOTA_TOTAL_MB", "8192"))
# 清理策略
RES_LOG_KEEP_DAYS = int(env("RES_LOG_KEEP_DAYS", "14"))
RES_FRAMES_KEEP_DAYS = int(env("RES_FRAMES_KEEP_DAYS", "7"))
RES_SCRIPT_VERSIONS_KEEP = int(env("RES_SCRIPT_VERSIONS_KEEP", "10"))
RES_SHOT_VARIANTS_KEEP = int(env("RES_SHOT_VARIANTS_KEEP", "5"))
RES_SNAPSHOTS_KEEP = int(env("RES_SNAPSHOTS_KEEP", "10"))
# ComfyUI 模型目录（用于统计占用）
COMFY_MODELS_DIR = env("COMFY_MODELS_DIR", r"D:\ComfyUI\ComfyUI\models")

# ---------------- 回收站 ----------------
# 删除操作先进回收站，保留 N 天后自动清理
TRASH_KEEP_DAYS = int(env("TRASH_KEEP_DAYS", "7"))
# 是否启用回收站（0 = 删除即永久删除）
TRASH_ENABLED = env("TRASH_ENABLED", "1") == "1"

# ---------------- 导出 ----------------
EDIT_EXPORT_PACK = env("EDIT_EXPORT_PACK", "1") == "1"


def ensure_dirs():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
