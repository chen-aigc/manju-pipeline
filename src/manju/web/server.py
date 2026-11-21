# -*- coding: utf-8 -*-
"""FastAPI 后端：任务提交、进度、预览、修补、设置。

路由一览：
    GET  /                      首页
    GET  /api/health            健康检查
    GET  /api/projects          项目列表
    POST /api/jobs              提交任务
    GET  /api/jobs              任务列表
    GET  /api/jobs/{id}         任务详情（轮询用）
    POST /api/jobs/{id}/cancel  取消
    GET  /api/projects/{name}   项目详情（集数、成本）
    GET  /api/projects/{name}/{chapter}          单集详情
    POST /api/projects/{name}/{chapter}/retry    重做出错项
    POST /api/projects/{name}/{chapter}/upload   上传人工素材
    GET  /api/settings          读配置
    POST /api/settings          改配置
    GET  /media/{path}          视频/图片（支持 Range）
"""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..core import integrity
from ..core.auth import auth, ROLES, ROLE_INFO, PERMISSIONS
from .jobs import JobManager

STATIC_DIR = Path(__file__).parent / "static"
PROJECTS = Path(config.PROJECTS_DIR)

app = FastAPI(title="漫剧工厂", docs_url="/api/docs")
manager = JobManager(PROJECTS)


# ---------------------------------------------------------------- 鉴权
# 不需要登录就能访问的路径
AUTH_WHITELIST = {
    "/", "/index.html", "/favicon.ico",
    "/api/health", "/api/auth/status", "/api/auth/login",
    "/api/docs", "/openapi.json",
}

# 路径前缀 → 权限点
PATH_PERMISSIONS = [
    # 只读
    ("/api/health", "health.read"),
    ("/api/projects", "project.read"),
    ("/api/stats", "project.read"),
    ("/api/estimate", "project.read"),
    ("/api/jobs", "job.read"),
    ("/api/progress", "job.read"),
    ("/api/memory", "project.read"),
    ("/api/logs", "job.read"),
    ("/api/plugins", "plugins.read"),
    ("/api/capabilities", "plugins.read"),
    ("/api/gpu", "gpu.read"),
    ("/api/spend", "spend.read"),
    ("/api/alerts", "spend.read"),
    ("/api/versions", "version.read"),
    ("/api/snapshots", "snapshot.read"),
    ("/api/radio", "plugins.read"),
    ("/media", "media.read"),
    ("/thumb", "media.read"),
]


def _client_token(request):
    """从请求里取 token（header 优先，其次 query）。"""
    h = request.headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.query_params.get("_token") or ""


def _perm_for(method, path):
    """按方法+路径推断所需权限。

    重要：敏感路径（用户管理、删除类）必须先于通用只读判断拦截，
    否则 GET /api/users 会被当成普通只读请求放行。
    """
    # ---------- 1. 敏感路径优先（不分方法） ----------
    if path.startswith("/api/users"):
        return "user.write"
    if path.startswith("/api/auth/enabled"):
        return "user.write"
    if path.startswith("/api/auth/permissions"):
        # 权限矩阵可以让所有角色看，但修改只能 admin
        return "project.read" if method in ("GET", "HEAD") else "user.write"
    if path.startswith("/api/auth/password"):
        return "project.read"        # 改自己的密码，登录即可
    if path.startswith("/api/snapshots") and method == "DELETE":
        return "snapshot.delete"
    if path.startswith("/api/projects") and method == "DELETE":
        return "project.delete"
    if path.startswith("/api/memory") and method == "DELETE":
        return "project.delete"

    # ---------- 2. 写操作细分 ----------
    if method not in ("GET", "HEAD", "OPTIONS"):
        if path.startswith("/api/jobs"):
            if "/cancel" in path:
                return "job.cancel"
            if "/priority" in path:
                return "job.priority"
            if "/concurrency" in path:
                return "job.concurrency"
            return "job.submit"
        if path.startswith("/api/settings"):
            return "settings.write"
        if path.startswith("/api/alerts/budget"):
            return "budget.write"
        if "/ratelimit" in path:
            return "ratelimit.write"
        if "/upload" in path:
            return "upload.write"
        if "/retry" in path:
            return "job.submit"
        if "/choose" in path:
            return "version.choose"
        if "/story" in path:
            return "version.write"
        if "/audit" in path:
            return "audit.read"
        if path.startswith("/api/versions"):
            return "version.write"
        if path.startswith("/api/snapshots"):
            if "/restore" in path:
                return "snapshot.restore"
            return "snapshot.create"
        if path.startswith("/api/radio"):
            return "plugins.read"
        return "job.submit"

    # ---------- 3. 只读 ----------
    for prefix, perm in PATH_PERMISSIONS:
        if path == prefix or path.startswith(prefix + "/"):
            return perm
    return "project.read"


# ---------------------------------------------------------------- 限流
class _HttpLimiter:
    """HTTP 层限流：滑动窗口 + 每 IP 独立。

    默认：
        普通接口 120 次/分钟
        登录接口 10 次/分钟（防爆破）
        健康检查 600 次/分钟（监控要高频）
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}          # (ip, bucket) -> [timestamp, ...]

    def _bucket_for(self, path):
        if path.startswith("/api/health"):
            return "health"
        if path.startswith("/api/auth/login"):
            return "login"
        return "api"

    def _limit_for(self, bucket):
        try:
            from .. import config
            return {
                "health": int(getattr(config, "HTTP_RATE_HEALTH", 600)),
                "login": int(getattr(config, "HTTP_RATE_LOGIN", 10)),
                "api": int(getattr(config, "HTTP_RATE_API", 600)),
            }.get(bucket, 600)
        except Exception:  # noqa: BLE001
            return {"health": 600, "login": 10, "api": 600}.get(bucket, 600)

    def check(self, ip, path):
        """Returns (ok, retry_after_sec)."""
        bucket = self._bucket_for(path)
        limit = self._limit_for(bucket)
        now = time.time()
        window = 60.0

        key = (ip, bucket)
        with self._lock:
            arr = self._hits.get(key)
            if arr is None:
                arr = []
                self._hits[key] = arr
            # 清掉窗口外的
            while arr and now - arr[0] > window:
                arr.pop(0)

            if len(arr) >= limit:
                retry = int(window - (now - arr[0])) + 1
                return False, retry

            arr.append(now)

            # 防内存泄漏：太多了就清掉一批
            if len(self._hits) > 5000:
                self._hits = {k: v for k, v in self._hits.items() if v}

        return True, 0

    def stats(self):
        with self._lock:
            return {
                "tracked_keys": len(self._hits),
                "total_hits": sum(len(v) for v in self._hits.values()),
            }


http_limiter = _HttpLimiter()


@app.middleware("http")
async def ratelimit_middleware(request: Request, call_next):
    """HTTP 限流（在鉴权之前）。"""
    path = request.url.path
    # 静态资源和 media 不限制（浏览器会并发拉很多）
    if path.startswith("/static/") or path.startswith("/media/") \
            or path.startswith("/thumb/"):
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    ok, retry = http_limiter.check(ip, path)
    if not ok:
        return JSONResponse(
            {"detail": "请求过于频繁，请 %d 秒后再试" % retry,
             "retry_after": retry},
            status_code=429,
            headers={"Retry-After": str(retry)})

    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """统一鉴权中间件。"""
    path = request.url.path
    method = request.method

    # 白名单：完全不用登录
    if path in AUTH_WHITELIST:
        return await call_next(request)

    # /api/auth/* 需要 token 但不需要权限检查（改密码、登出、查询自己）
    if path == "/api/auth/me" or path.startswith("/api/auth/password") \
            or path.startswith("/api/auth/logout"):
        if not auth.enabled():
            request.state.user = {"name": "local", "role": "admin",
                                  "display": "本机"}
            return await call_next(request)
        user = auth.verify(_client_token(request))
        if not user:
            return JSONResponse(
                {"detail": "未登录或登录已过期", "need_login": True},
                status_code=401)
        request.state.user = user
        return await call_next(request)

    # 只有真正公开的 /api/auth/* 走白名单
    # 注意：/api/auth/enabled、/api/auth/permissions 需要权限检查，
    # 不能在这里放过，否则任何人都能关掉鉴权。
    if path in ("/api/auth/status", "/api/auth/login"):
        return await call_next(request)

    # 鉴权关闭：直接放行
    if not auth.enabled():
        request.state.user = {"name": "local", "role": "admin",
                              "display": "本机"}
        return await call_next(request)

    # 校验 token
    tok = _client_token(request)
    user = auth.verify(tok)
    if not user:
        return JSONResponse(
            {"detail": "未登录或登录已过期", "need_login": True},
            status_code=401)

    request.state.user = user

    # 权限检查
    perm = _perm_for(method, path)
    if not auth.can(user, perm):
        return JSONResponse(
            {"detail": "权限不足：需要 %s（当前角色 %s）"
                       % (perm, user.get("role")),
             "perm": perm},
            status_code=403)

    return await call_next(request)


# ---------------------------------------------------------------- 登录 / 用户管理
@app.get("/api/auth/status")
def auth_status():
    """鉴权状态（不需要登录）。"""
    return {
        "enabled": auth.enabled(),
        "roles": [{"name": r, "desc": ROLE_INFO.get(r, "")} for r in ROLES],
        "has_users": len(auth.list_users()) > 0,
    }


@app.post("/api/auth/login")
def auth_login(payload: dict = Body(...)):
    """登录，返回 token。"""
    name = (payload.get("username") or "").strip()
    pw = payload.get("password") or ""
    tok, info = auth.login(name, pw)
    if not tok:
        raise HTTPException(401, info)
    return {"ok": True, "token": tok, "user": info}


@app.get("/api/auth/me")
def auth_me(request: Request):
    """当前用户信息。"""
    return getattr(request.state, "user", None) or {"role": "admin"}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    """登出。"""
    tok = _client_token(request)
    auth.logout(tok)
    return {"ok": True}


@app.post("/api/auth/password")
def auth_change_password(request: Request, payload: dict = Body(...)):
    """改自己的密码。"""
    user = getattr(request.state, "user", None) or {}
    name = user.get("name")
    if not name:
        raise HTTPException(401, "未登录")
    ok, msg = auth.set_password(name, payload.get("new_password") or "")
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "msg": msg, "need_relogin": True}


# ---------------------------------------------------------------- 用户管理（仅 admin）
@app.get("/api/users")
def users_list():
    return {"users": auth.list_users()}


@app.post("/api/users")
def users_create(payload: dict = Body(...)):
    ok, msg = auth.add_user(
        payload.get("username") or "",
        payload.get("password") or "",
        payload.get("role") or "viewer",
        payload.get("display") or "",
        bool(payload.get("must_change")),
    )
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "msg": msg}


@app.patch("/api/users/{name}")
def users_update(name, payload: dict = Body(...)):
    ok, msg = auth.update_user(
        name,
        role=payload.get("role"),
        display=payload.get("display"),
        disabled=payload.get("disabled"),
        must_change=payload.get("must_change"),
    )
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "msg": msg}


@app.delete("/api/users/{name}")
def users_delete(name, confirm_token: str = ""):
    """删除用户（需要确认 token）。"""
    from ..core.danger import danger

    ok, info = danger.consume(confirm_token, {"name": name})
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    ok, msg = auth.delete_user(name)
    if not ok:
        raise HTTPException(400, msg)
    danger.notify("user.delete", {"name": name}, {"msg": msg})
    danger.log("user.delete", {"name": name}, {"msg": msg})
    return {"ok": True, "msg": msg}


@app.post("/api/users/{name}/password")
def users_set_password(name, payload: dict = Body(...)):
    ok, msg = auth.set_password(name, payload.get("new_password") or "")
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "msg": msg}


@app.get("/api/auth/permissions")
def auth_permissions():
    """权限矩阵（给界面显示）。"""
    return {
        "roles": [{"name": r, "desc": ROLE_INFO.get(r, "")} for r in ROLES],
        "matrix": {p: list(roles) for p, roles in PERMISSIONS.items()},
    }


@app.post("/api/auth/enabled")
def auth_set_enabled(payload: dict = Body(...)):
    """开关鉴权（仅 admin）。本地单机可关。"""
    on = bool(payload.get("enabled"))
    return {"ok": True, "enabled": auth.set_enabled(on)}



# ---------------------------------------------------------------- 工具
def _proj_dir(name):
    p = PROJECTS / name
    if not p.exists():
        raise HTTPException(404, "项目不存在：%s" % name)
    return p


def _chapter_dir(name, chapter):
    try:
        num = int(chapter)
    except (TypeError, ValueError):
        raise HTTPException(404, "不是有效的集数：%s" % chapter)
    d = _proj_dir(name) / ("chapter_%03d" % num)
    if not d.exists():
        raise HTTPException(404, "第 %s 集不存在" % chapter)
    return d


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _safe_rel(path):
    """确保请求的文件在项目目录内。"""
    p = (PROJECTS / path).resolve()
    if PROJECTS.resolve() not in p.parents and p != PROJECTS.resolve():
        raise HTTPException(403, "越权访问")
    return p


# ---------------------------------------------------------------- 页面
@app.get("/", response_class=HTMLResponse)
def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return HTMLResponse("<h1>前端未安装</h1><p>缺少 static/index.html</p>",
                            status_code=500)
    return HTMLResponse(idx.read_text(encoding="utf-8"))


_HEALTH_CACHE = {"ts": 0.0, "data": None}
_HEALTH_TTL = 8.0          # 秒，别每次都探 ComfyUI


def _comfy_url():
    """ComfyUI 地址（兼容两种配置名）。"""
    return (getattr(config, "COMFY_URL", "")
            or getattr(config, "COMFY_HOST", "")
            or "http://127.0.0.1:8188").rstrip("/")


def _probe_comfy(timeout=0.6):
    """探 ComfyUI 是否在线。超时压到 0.6 秒，避免页面被拖住。"""
    try:
        import httpx
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            return c.get(_comfy_url() + "/system_stats").status_code == 200
    except Exception:  # noqa: BLE001
        return False


@app.get("/api/health")
def health(fresh: int = 0):
    import time as _t
    now = _t.time()
    if (not fresh) and _HEALTH_CACHE["data"] is not None \
            and now - _HEALTH_CACHE["ts"] < _HEALTH_TTL:
        return _HEALTH_CACHE["data"]

    from ..plugins import registry
    out = {"ok": True, "projects_dir": str(PROJECTS)}
    try:
        out["providers"] = registry.registry_snapshot()
    except Exception as exc:  # noqa: BLE001
        out["providers_error"] = str(exc)

    out["comfyui"] = _probe_comfy()
    out["llm"] = bool(config.LLM_API_KEY)
    out["running"] = manager._running
    out["jobs"] = len(manager.jobs)

    # 磁盘占用（走缓存，全盘 rglob 不做第二次）
    disk = {}
    try:
        import shutil as _sh
        total, used, free = _sh.disk_usage(PROJECTS)
        disk = {"total_gb": round(total / 1024 ** 3, 1),
                "free_gb": round(free / 1024 ** 3, 1)}
        prev = (_HEALTH_CACHE.get("disk") or {})
        if prev and now - prev.get("ts", 0) < 120:
            disk["projects_mb"] = prev["mb"]
        else:
            size = sum(f.stat().st_size for f in PROJECTS.rglob("*")
                       if f.is_file())
            disk["projects_mb"] = round(size / 1024 / 1024, 1)
            _HEALTH_CACHE["disk"] = {"ts": now, "mb": disk["projects_mb"]}
        disk["limit_mb"] = config.PROJECT_DISK_MAX_MB
        disk["over_limit"] = disk["projects_mb"] > config.PROJECT_DISK_MAX_MB
    except Exception:  # noqa: BLE001
        pass
    out["disk"] = disk

    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["data"] = out
    return out


@app.post("/api/health/fix")
def health_fix():
    """跑一次自动修复（建目录、装缺失依赖），返回日志。"""
    from ..setup_check import run_setup
    import contextlib
    import io

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = run_setup(fix=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "体检失败：%s" % exc)
    return {"ok": True, "exit": code, "log": buf.getvalue()[-5000:]}


@app.post("/api/estimate")
def estimate_cost(payload: dict = Body(default={})):
    """跑之前先算这集要花多少钱。

    单价来源：
        可灵 3.0   720p $0.084/s 1080p $0.112/s 4K $0.42/s（汇率 7.15）
        可灵出图   $0.028/张
        火山 TTS   6.5 元/万字符
        剧本       DeepSeek 按 token 估、GPT-4o 级按 0.64 元/集
    未公开牌价的（即梦/Vidu/海螺）标 estimate=True，界面会提示是估值。
    """
    shots = max(1, min(500, int(payload.get("shots") or 40)))
    sec_per = max(0.5, min(15.0, float(payload.get("seconds") or 3.0)))
    waste = max(0.0, min(1.0, float(payload.get("waste") or 0) / 100.0))

    image_p = payload.get("image") or config.IMAGE_PROVIDER
    video_p = payload.get("video") or config.VIDEO_PROVIDER
    tts_p = payload.get("tts") or config.TTS_PROVIDER
    script_p = payload.get("script") or config.SCRIPT_PROVIDER
    res = (payload.get("resolution") or "1080p").lower()

    total_sec = shots * sec_per
    tries = 1.0 + waste          # 生成次数（含废片重试）

    # ---------- 剧本 ----------
    script_price = {
        "deepseek": 0.05, "qwen": 0.08, "glm": 0.02, "doubao": 0.06,
        "siliconflow": 0.15, "kimi": 0.30, "openai": 0.64,
        "ollama": 0.0, "mock": 0.0, "manual": 0.0,
    }
    script_yuan = script_price.get(script_p, 0.10)
    script_est = script_p not in ("deepseek", "openai", "glm", "ollama", "mock")

    # ---------- 图生视频（每秒） ----------
    # 可灵按分辨率不同价（官方美元价 × 7.15）
    # 可灵官方按秒计费（美元 × 7.15）；5 秒起售，所以短镜头会浪费
    kling_by_res = {"720p": 0.60, "1080p": 0.80, "4k": 3.00}
    kling_min_sec = 5.0
    # 可灵最短 5 秒、即梦最短 4 秒 —— 短镜头要按起售时长计费
    min_sec = 5.0 if video_p == "kling" else (4.0 if video_p == "jimeng" else 1.0)
    per_shot = max(float(sec_per), min_sec)
    billed_sec = per_shot * shots * tries

    jimeng_price = {  # 火山方舟牌价（元/秒）
        "mini": (0.26, 0.26), "fast": (0.62, 0.62),
        "seedance-2-5": (1.51, 3.74), "seedance-2-0": (0.99, 2.48),
        "seedance-1-5": (0.99, 2.48), "lite-i2v": (0.60, 0.60),
    }
    jm = config.JIMENG_MODEL or ""
    jp = jimeng_price.get("seedance-2-0", (0.99, 2.48))
    for kw, pair in jimeng_price.items():
        if kw in jm.lower():
            jp = pair
            break
    jm_price = jp[1] if res == "1080p" else jp[0]

    vid_price = {
        "null": 0.0, "manual": 0.0,
        "kling": kling_by_res.get(res, 0.80),
        "jimeng": jm_price, "vidu": 0.30, "hailuo": 0.30,
        "blender": 0.02,        # 只烧电
    }
    per_sec = vid_price.get(video_p, 0.50)
    vid_yuan = per_sec * billed_sec
    vid_est = video_p in ("vidu", "hailuo")

    # ---------- 出图：分辨率越高越贵（云端） ----------
    if image_p == "openai_image":
        base_img = float(config.OPENAI_IMAGE_PRICE or 0.20)
        per_img = base_img * {"720p": 1.0, "1080p": 1.0, "4k": 2.0}.get(res, 1.0)
    else:
        per_img = {"comfyui": 0.0325, "sdwebui": 0.03, "placeholder": 0.0,
                   "manual": 0.0, "realistic": 0.05, "sdxl": 0.08
                   }.get(image_p, 0.10)
    img_yuan = per_img * shots * tries          # 废片要重画

    # ---------- 配音 ----------
    chars = shots * 25                          # 每镜约 25 字
    if tts_p == "edge":
        tts_yuan, tts_note = 0.0, "Edge 免费"
    elif tts_p == "volc":
        tts_yuan, tts_note = chars * 6.5 / 10000, "火山 6.5 元/万字符"
    elif tts_p == "mock":
        tts_yuan, tts_note = 0.0, "静音测试"
    else:
        tts_yuan, tts_note = 0.0, "人工录音"

    # ---------- 合计 ----------
    known = script_yuan + img_yuan + vid_yuan + tts_yuan
    items = [
        {"key": "script", "label": "剧本", "yuan": round(script_yuan, 4),
         "detail": "%s · 整集一次" % script_p, "estimate": script_est},
        {"key": "image", "label": "出图", "yuan": round(img_yuan, 4),
         "detail": "%d 张 × %.4f 元（含 %.0f%% 重画）" % (round(shots * tries), per_img, waste * 100),
         "estimate": image_p == "openai_image"},
        {"key": "video", "label": "图生视频", "yuan": round(vid_yuan, 4),
         "detail": "每镜 %.0f 秒 × %d 镜 ≈ %.0f 秒计费 × %.2f 元/秒" % (
             per_shot, shots, billed_sec, per_sec),
         "estimate": vid_est},
        {"key": "tts", "label": "配音", "yuan": round(tts_yuan, 4),
         "detail": "%s · 约 %d 字" % (tts_note, chars), "estimate": False},
        {"key": "bgm", "label": "背景音乐", "yuan": 0.0,
         "detail": "自动合成免费", "estimate": False},
        {"key": "edit", "label": "剪辑", "yuan": 0.0,
         "detail": "本地 FFmpeg", "estimate": False},
    ]

    has_estimate = any(i["estimate"] for i in items)
    return {
        "shots": shots,
        "seconds": sec_per,
        "total_seconds": round(total_sec, 1),
        "waste_percent": waste * 100,
        "items": items,
        "total_yuan": "%.2f" % known,
        # 有估值项时给一个 ±25% 区间，别让人以为是精确值
        "low_yuan": "%.2f" % (known * 0.75) if has_estimate else "%.2f" % known,
        "high_yuan": "%.2f" % (known * 1.25) if has_estimate else "%.2f" % known,
        "per_second_yuan": "%.4f" % (known / total_sec if total_sec else 0),
        "has_estimate": has_estimate,
        "combo": {"script": script_p, "image": image_p,
                  "video": video_p, "tts": tts_p},
    }


@app.post("/api/maintain/cleanup")
def cleanup(payload: dict = Body(default={})):
    """清理历史文件：每处 history 只留最近 keep 份；_trash 里超 7 天的删掉。"""
    keep = int(payload.get("keep") or 3)
    dry = bool(payload.get("dry_run"))
    freed = 0
    removed = 0
    dirs = []

    for proj in (PROJECTS.iterdir() if PROJECTS.exists() else []):
        if not proj.is_dir() or proj.name.startswith("_"):
            continue
        for hist in proj.rglob("history"):
            if not hist.is_dir():
                continue
            files = sorted(hist.glob("*"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            n = 0
            for f in files[keep:]:
                try:
                    sz = f.stat().st_size if f.is_file() else 0
                    if not dry:
                        if f.is_dir():
                            import shutil as _sh
                            _sh.rmtree(f)
                        else:
                            f.unlink()
                    freed += sz
                    removed += 1
                    n += 1
                except OSError:
                    pass
            if n:
                dirs.append("%s (%d 个)" % (hist.relative_to(PROJECTS), n))

    trash = PROJECTS / "_trash"
    if trash.exists():
        import time as _t
        now = _t.time()
        for d in list(trash.iterdir()):
            try:
                if now - d.stat().st_mtime <= 7 * 86400:
                    continue
                import shutil as _sh
                sz = (sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                      if d.is_dir() else d.stat().st_size)
                if not dry:
                    if d.is_dir():
                        _sh.rmtree(d)
                    else:
                        d.unlink()
                freed += sz
                removed += 1
            except OSError:
                pass

    return {"ok": True, "dry_run": dry, "removed": removed,
            "freed_mb": round(freed / 1024 / 1024, 1),
            "dirs": dirs[:20]}


# ---------------------------------------------------------------- 项目
@app.get("/api/projects")
def list_projects():
    out = []
    if not PROJECTS.exists():
        return out
    for d in sorted(PROJECTS.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        chapters = []
        cover = None
        cover_thumb = None
        latest = 0.0
        for ch in sorted(d.glob("chapter_*")):
            n = ch.name.replace("chapter_", "")
            try:
                num = int(n)
            except ValueError:
                continue
            video = ch / ("chapter_%03d.mp4" % num)
            cost = _read_json(ch / "cost.json", {}) or {}
            todos = _read_json(ch / "manual_todo" / "todos.json", []) or []
            pack = (ch / "pack").exists()

            # 待人工剪辑
            pending = False
            st = _read_json(ch / "state.json", {}) or {}
            if (st.get("items", {}).get("edit") or {}).get("status") == "degraded":
                pending = True

            chapters.append({
                "chapter": num,
                "video": bool(video.exists()),
                "video_size": video.stat().st_size if video.exists() else 0,
                "cost": (cost.get("summary") or {}).get("total_min_yuan"),
                "todos": len(todos),
                "pack": pack,
                "pending": pending,
            })

            # 封面：取第一集第一个镜头
            if cover is None:
                for img in sorted((ch / "images").glob("shot_*.png")):
                    cover = ("/media/%s/%s/images/%s"
                             % (name_quote(d.name), ch.name, img.name))
                    cover_thumb = ("/thumb/%s/%s/images/%s"
                                   % (name_quote(d.name), ch.name, img.name))
                    break

            try:
                latest = max(latest, ch.stat().st_mtime)
            except OSError:
                pass

        mem = _read_json(d / "memory.json", {}) or {}
        out.append({
            "name": d.name,
            "chapters": chapters,
            "cover": cover,
            "cover_thumb": cover_thumb,
            "updated_at": _fmt_ts(latest),
            "characters": len(mem.get("characters") or {}),
        })
    return out


def name_quote(name):
    from urllib.parse import quote
    return quote(name)


def _fmt_ts(ts):
    if not ts:
        return ""
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts))


@app.get("/api/stats")
def global_stats():
    """首页概览：项目数、集数、成片数、累计成本。"""
    projects = 0
    chapters = 0
    videos_total = 0
    todos_total = 0
    chars_total = 0
    cost_si = 0          # 用整数丝累计，避免浮点误差
    recent = []

    if PROJECTS.exists():
        for d in PROJECTS.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            projects += 1
            mem = _read_json(d / "memory.json", {}) or {}
            chars_total += len(mem.get("characters") or {})

            for ch in d.glob("chapter_*"):
                chapters += 1
                num = ch.name.replace("chapter_", "")
                try:
                    num = int(num)
                except ValueError:
                    continue
                if (ch / ("chapter_%03d.mp4" % num)).exists():
                    videos_total += 1
                todos = _read_json(ch / "manual_todo" / "todos.json", []) or []
                todos_total += len(todos)
                c = _read_json(ch / "cost.json", {}) or {}
                summary = c.get("summary") or {}
                try:
                    cost_si += int(round(float(
                        summary.get("total_min_yuan") or 0) * 10000))
                except (TypeError, ValueError):
                    pass

    return {
        "projects": projects,
        "chapters": chapters,
        "videos": videos_total,
        "characters": chars_total,
        "todos": todos_total,
        "cost_yuan": "%.4f" % (cost_si / 10000.0),
    }


@app.get("/api/projects/{name}")
def project_detail(name):
    d = _proj_dir(name)
    mem = _read_json(d / "memory.json", {}) or {}
    return {
        "name": name,
        "characters": list((mem.get("characters") or {}).keys()),
        "props": list((mem.get("props") or {}).keys()),
        "chapter": (mem.get("plot") or {}).get("chapter", 0),
    }


@app.get("/api/projects/{name}/{chapter}")
def chapter_detail(name, chapter):
    d = _chapter_dir(name, chapter)
    num = int(chapter)
    script = _read_json(d / "script.json", {}) or {}
    st = _read_json(d / "state.json", {}) or {}
    cost = _read_json(d / "cost.json", {}) or {}
    todos = _read_json(d / "manual_todo" / "todos.json", []) or []
    video = d / ("chapter_%03d.mp4" % num)

    # 视频元信息
    v_dur = v_size = 0.0
    if video.exists():
        v_size = video.stat().st_size
        try:
            from ..tts import probe_duration
            v_dur = probe_duration(video)
        except Exception:  # noqa: BLE001
            v_dur = 0.0

    shots = []
    cur = 0.0
    for s in script.get("shots") or []:
        idx = int(s.get("index") or 0)
        subs = s.get("sub_shots") or []
        dur = sum(float(x.get("duration") or 0) for x in subs) or \
            float(s.get("_duration") or 0)
        text = (s.get("narration") or "").strip()
        for dl in s.get("dialogues") or []:
            if dl.get("text"):
                text = (text + " / " if text else "") + \
                    "%s：%s" % (dl.get("role", ""), dl["text"])
        items = st.get("items", {}) or {}
        shots.append({
            "index": idx,
            "shot_type": s.get("shot_type", ""),
            "scene": s.get("scene", ""),
            "text": text[:80],
            "duration": round(dur, 2),
            "start": round(cur, 2),
            "sub_shots": len(subs),
            "image": (d / "images" / ("shot_%02d.png" % idx)).exists(),
            "video": (d / "videos" / ("shot_%02d.mp4" % idx)).exists(),
            "status": (items.get("image_%02d" % idx) or {}).get("status", ""),
            "vstatus": (items.get("video_%02d" % idx) or {}).get("status", ""),
        })
        cur += dur

    # 字幕预览
    srt_preview = []
    srt_path = d / ("chapter_%03d.srt" % num)
    if srt_path.exists():
        txt = srt_path.read_text(encoding="utf-8", errors="replace")
        for block in txt.strip().split("\n\n")[:400]:
            lines = [x for x in block.splitlines() if x.strip()]
            if len(lines) >= 3:
                srt_preview.append({"time": lines[1], "text": " ".join(lines[2:])})

    stats = {}
    for s in shots:
        stats[s["status"] or "unknown"] = stats.get(s["status"] or "unknown", 0) + 1

    # 分辨率探测（从 ffmpeg 的输出里读，没有 ffprobe）
    vw = vh = 0
    if video.exists():
        try:
            import re
            import subprocess
            from ..tts import ffmpeg_path
            r = subprocess.run([ffmpeg_path(), "-i", str(video)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            m = re.search(r"(\d{2,4})x(\d{3,4})", r.stderr or "")
            if m:
                vw, vh = int(m.group(1)), int(m.group(2))
        except Exception:  # noqa: BLE001
            pass

    native = (vw == 2160 and vh == 3840)
    return {
        "name": name,
        "chapter": num,
        "video": video.exists(),
        "video_url": ("/media/%s/chapter_%03d/chapter_%03d.mp4"
                      % (name, num, num)) if video.exists() else None,
        "video_duration": round(v_dur, 2),
        "video_size": v_size,
        "video_w": vw,
        "video_h": vh,
        "is_4k": vw >= 2160,
        "upscaled": vw >= 2160 and not native,
        "cost": cost.get("summary") or {},
        "records": cost.get("records") or [],
        "todos": todos,
        "shots": shots,
        "shot_stats": stats,
        "shot_count": len(shots),
        "srt": srt_path.exists(),
        "srt_preview": srt_preview[:200],
        "pack": (d / "pack").exists(),
        "report": (d / "report.xlsx").exists(),
        "total_duration": round(cur, 2),
    }


# ---------------------------------------------------------------- 任务
@app.post("/api/jobs")
def create_job(payload: dict = Body(...)):
    raw_project = payload.get("project")
    if raw_project is not None and not isinstance(raw_project, str):
        raise HTTPException(400, "项目名必须是文本")
    project = (raw_project or "").strip()
    if not project:
        raise HTTPException(400, "缺少项目名")
    # 统一名称校验（拒绝路径穿越 + shell 元字符 + 保留名）
    from ..core.validate import validate_name
    ok, why = validate_name(project, "项目名")
    if not ok:
        raise HTTPException(400, why)

    # stages 长度限制
    stages_in = payload.get("stages")
    if stages_in is not None:
        if not isinstance(stages_in, (list, tuple)):
            raise HTTPException(400, "stages 必须是数组")
        if len(stages_in) > 16:
            raise HTTPException(400, "stages 最多 16 项（给了 %d）"
                                % len(stages_in))
        valid_stages = {"script", "image", "video", "audio", "bgm",
                        "edit", "report"}
        bad = [s for s in stages_in if s not in valid_stages]
        if bad:
            raise HTTPException(400, "未知阶段：%r（可选 %s）"
                                % (bad[:3], "/".join(sorted(valid_stages))))

    start = int(payload.get("chapter") or 1)
    count = int(payload.get("count") or 1)
    count = max(1, min(30, count))          # 一次最多 30 集
    chapters = list(range(start, start + count))

    # 本次任务指定的引擎（可选，留空用配置默认值）
    providers = payload.get("providers")
    if providers is not None:
        if not isinstance(providers, dict):
            raise HTTPException(400, "providers 必须是对象")
        from ..plugins import registry as _reg
        _reg.load_all()
        allowed = {
            "script": set(_reg.registry_snapshot().get("script") or []),
            "image": set(_reg.registry_snapshot().get("image") or []),
            "video": set(_reg.registry_snapshot().get("video") or []),
            "tts": set(_reg.registry_snapshot().get("tts") or []),
            "bgm": set(_reg.registry_snapshot().get("bgm") or []),
            "edit": set(_reg.registry_snapshot().get("edit") or []),
        }
        bad = []
        for k, v in providers.items():
            if k not in allowed:
                bad.append("%s（未知类别）" % k)
            elif v not in allowed[k]:
                bad.append("%s=%s（不存在该实现）" % (k, v))
        if bad:
            raise HTTPException(400, "无效的引擎指定：%s" % "；".join(bad))
        # 只保留非空值
        providers = {k: v for k, v in providers.items() if v} or None

    job = manager.submit(
        project, chapters,
        source_text=payload.get("topic") or payload.get("source_text") or "",
        target_shots=int(payload.get("target_shots") or 40),
        stages=payload.get("stages"),
        priority=payload.get("priority") or "normal",
        providers=providers,
    )
    return job.to_dict()


# ---------------------------------------------------------------- 危险操作确认
@app.post("/api/confirm/request")
def confirm_request(payload: dict = Body(...)):
    """申请危险操作的确认 token（不会执行任何操作）。"""
    from ..core.danger import danger, OPS

    op = (payload.get("op") or "").strip()
    params = payload.get("params") or {}
    if not op:
        raise HTTPException(400, "缺少 op（可选：%s）" % ", ".join(OPS))

    user = ""
    try:
        from ..core.auth import auth
        # 从 header 拿当前用户（中间件已经认证过）
        pass
    except Exception:  # noqa: BLE001
        pass

    # 按操作类型生成预览
    preview = _build_preview(op, params)
    result, err = danger.request(op, params, user=payload.get("user") or "",
                                 preview=preview)
    if result is None:
        raise HTTPException(400, err)
    return result


def _build_preview(op, params):
    """为不同操作生成预览。"""
    try:
        if op in ("resource.cleanup", "resource.lru"):
            from ..core.resources import resources
            if op == "resource.cleanup":
                r = resources.cleanup(
                    dry_run=True,
                    max_safety=params.get("max_safety") or "safe",
                    kinds=params.get("kinds"))
            else:
                r = resources.lru_evict(
                    target_free_mb=int(params.get("target_free_mb") or 500),
                    dry_run=True)
            return {
                "freed_mb": r.get("freed_mb", 0),
                "affected_count": r.get("removed_count", 0),
                "summary": "将删除 %d 个文件/目录（先进回收站）"
                           % r.get("removed_count", 0),
                "sample": [x.get("path") for x in (r.get("removed") or [])[:5]],
            }

        if op == "batch.delete":
            from ..core.assets_mgr import BatchOps
            name = params.get("project") or ""
            ch = int(params.get("chapter") or 1)
            shots = params.get("shots") or []
            d = _proj_dir(name) / ("chapter_%03d" % ch) if name else None
            if d and d.exists():
                bo = BatchOps(d)
                pv = bo.preview("delete", shots, extra={})
                return {
                    "freed_mb": pv.get("total_size_mb", 0),
                    "affected_count": pv.get("count", 0),
                    "summary": "将把 %d 个镜头的产物移入回收站" % pv.get("count", 0),
                    "sample": ["镜头 %d" % x["index"] for x in (pv.get("items") or [])[:10]],
                }
            return {"summary": "项目或集不存在"}

        if op in ("trash.purge", "trash.clear"):
            from ..core.trash import trash
            tid = params.get("trash_id") or ""
            if tid:
                it = trash.get(tid)
                if it:
                    return {
                        "freed_mb": it.get("total_mb", 0),
                        "affected_count": len(it.get("items") or []),
                        "summary": "将永久删除 %d 个文件（%.1f MB），无法还原"
                                   % (len(it.get("items") or []),
                                      it.get("total_mb", 0)),
                        "sample": [x.get("name") for x in (it.get("items") or [])[:8]],
                    }
            st = trash.stats()
            return {
                "freed_mb": st.get("total_mb", 0),
                "affected_count": st.get("count", 0),
                "summary": "将清空整个回收站（%d 个条目，%.1f MB），无法还原"
                           % (st.get("count", 0), st.get("total_mb", 0)),
            }

        if op == "project.delete":
            from ..core.resources import size_of
            name = params.get("name") or ""
            p = _proj_dir(name) if name else None
            if p and p.exists():
                mb = size_of(p) / 1024 / 1024
                return {"freed_mb": round(mb, 1),
                        "summary": "将删除项目 %s（%.1f MB）" % (name, mb)}
            return {"summary": "项目 %s 不存在" % name}

        if op == "chapter.delete":
            name = params.get("name") or ""
            ch = int(params.get("chapter") or 1)
            p = _proj_dir(name) / ("chapter_%03d" % ch) if name else None
            if p and p.exists():
                from ..core.resources import size_of
                mb = size_of(p) / 1024 / 1024
                return {"freed_mb": round(mb, 1),
                        "summary": "将删除 %s 第 %d 集（%.1f MB）" % (name, ch, mb)}
            return {"summary": "目录不存在"}

        if op == "snapshot.delete":
            return {"summary": "将删除快照 %s" % (params.get("dir") or "")}

        if op == "snapshot.restore":
            return {"summary": "将用快照 %s 覆盖当前文件"
                               % (params.get("dir") or "")}

        if op == "script.rollback":
            return {"summary": "将把剧本回滚到版本 %s"
                               % (params.get("version") or "")}

        if op == "user.delete":
            return {"summary": "将删除用户 %s" % (params.get("name") or "")}

        if op == "alerts.clear":
            from ..core.alerting import alerts
            n = alerts.stats().get("total", 0)
            return {"affected_count": n, "summary": "将清空 %d 条告警" % n}

        if op == "auth.disable":
            return {"summary": "关闭后任何人都能访问本系统"}

    except Exception as exc:  # noqa: BLE001
        return {"summary": "预览失败：%r" % exc}
    return {}


@app.post("/api/confirm/verify")
def confirm_verify(payload: dict = Body(...)):
    """校验 token（不消费）。"""
    from ..core.danger import danger

    tok = payload.get("token") or ""
    ok, info = danger.verify(tok, payload.get("params"))
    return {"ok": ok, "info": info}


@app.get("/api/confirm/pending")
def confirm_pending():
    """当前挂起的确认。"""
    from ..core.danger import danger
    return {"pending": danger.pending()}


@app.get("/api/confirm/history")
def confirm_history(limit: int = 50):
    """危险操作历史。"""
    from ..core.danger import danger
    return {"history": danger.history(limit)}


@app.post("/api/confirm/cancel")
def confirm_cancel(payload: dict = Body(...)):
    from ..core.danger import danger
    ok, msg = danger.cancel(payload.get("token") or "")
    return {"ok": ok, "msg": msg}


# ---------------------------------------------------------------- 素材资产
@app.get("/api/assets/{name}/{chapter}")
def assets_list(name, chapter):
    """一集的全部素材清单（逐镜）。"""
    from ..core.assets_mgr import AssetInventory

    d = _chapter_dir(name, chapter)
    inv = AssetInventory(d)

    # 读剧本/状态/一致性报告
    script = None
    sp = d / "script.json"
    if sp.exists():
        try:
            script = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    story = None
    stp = d / "story_state.json"
    if stp.exists():
        try:
            story = json.loads(stp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    cons = None
    cp = d / "consistency_report.json"
    if cp.exists():
        try:
            cons = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    items = inv.list(script=script, story_state=story, consistency=cons)
    return {
        "name": name,
        "chapter": int(chapter),
        "summary": inv.summary(items),
        "shots": items,
    }


@app.get("/api/assets/{name}/{chapter}/{shot_id}")
def assets_shot(name, chapter, shot_id):
    """单镜素材详细信息（含所有变体）。"""
    from ..core.assets_mgr import AssetInventory
    from ..core.versioning import ShotVersions
    import re as _re

    d = _chapter_dir(name, chapter)
    m = _re.search(r"(\d+)", shot_id)
    if not m:
        raise HTTPException(400, "镜头名格式错误，如 shot_017")
    idx = int(m.group(1))

    inv = AssetInventory(d)
    items = inv.list()
    hit = [x for x in items if x["index"] == idx]
    if not hit:
        return {"name": name, "chapter": int(chapter), "index": idx,
                "exists": False, "shot": None, "variants": []}

    # 图片变体
    sv_img = ShotVersions(d, kind="image")
    variants_img = sv_img.list("shot_%03d" % idx)
    chosen_img = sv_img.chosen("shot_%03d" % idx)

    # 视频变体
    sv_vid = ShotVersions(d, kind="video")
    variants_vid = sv_vid.list("shot_%03d" % idx)

    return {
        "name": name, "chapter": int(chapter), "index": idx,
        "exists": True,
        "shot": hit[0],
        "variants": variants_img,
        "chosen": chosen_img,
        "video_variants": variants_vid,
    }


@app.post("/api/assets/{name}/{chapter}/{shot_id}/replace")
def assets_replace(name, chapter, shot_id, payload: dict = Body(...)):
    """用本地文件替换某镜素材。

    payload:
        kind: "image" | "video"
        source_path: 本地文件路径（服务端可访问的路径）
        backup: 是否备份旧版（默认 true）
    """
    from ..core.assets_mgr import AssetReplacer
    from ..core.danger import danger
    import re as _re

    d = _chapter_dir(name, chapter)
    m = _re.search(r"(\d+)", shot_id)
    if not m:
        raise HTTPException(400, "镜头名格式错误")
    idx = int(m.group(1))

    kind = payload.get("kind") or "image"
    src = payload.get("source_path") or ""
    if not src:
        raise HTTPException(400, "缺少 source_path")
    if not Path(src).exists():
        raise HTTPException(400, "源文件不存在：%s" % src)

    backup = payload.get("backup", True)
    rp = AssetReplacer(d)

    if kind == "image":
        ok, result = rp.replace_still(idx, src, backup=backup)
    else:
        ok, result = rp.replace_video(idx, src, backup=backup)

    if not ok:
        raise HTTPException(400, str(result))

    # 飞书通知
    try:
        danger.notify("asset.replace",
                      {"project": name, "chapter": chapter,
                       "shot": shot_id, "kind": kind},
                      {"msg": "已替换 %s 的%s" % (shot_id,
                                                 "静帧" if kind == "image" else "视频")})
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "result": result}


@app.post("/api/assets/{name}/{chapter}/{shot_id}/upload")
async def assets_upload(name, chapter, shot_id,
                        file: UploadFile = File(...),
                        kind: str = "image"):
    """上传文件直接替换某镜素材（multipart）。"""
    from ..core.assets_mgr import AssetReplacer
    import re as _re
    import tempfile

    d = _chapter_dir(name, chapter)
    m = _re.search(r"(\d+)", shot_id)
    if not m:
        raise HTTPException(400, "镜头名格式错误")
    idx = int(m.group(1))

    # 存到临时文件
    ext = Path(file.filename or "upload.png").suffix or ".png"
    tmp = Path(tempfile.mkdtemp()) / ("upload" + ext)
    try:
        with tmp.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        file.file.close()

    rp = AssetReplacer(d)
    if kind == "image":
        ok, result = rp.replace_still(idx, tmp, backup=True)
    else:
        ok, result = rp.replace_video(idx, tmp, backup=True)

    try:
        tmp.unlink()
    except OSError:
        pass

    if not ok:
        raise HTTPException(400, str(result))
    return {"ok": True, "result": result}


@app.post("/api/assets/{name}/{chapter}/{shot_id}/restore")
def assets_restore(name, chapter, shot_id, payload: dict = Body(...)):
    """把某个变体还原为正式素材（需要确认 token）。"""
    from ..core.assets_mgr import AssetReplacer
    from ..core.danger import danger
    import re as _re

    d = _chapter_dir(name, chapter)
    m = _re.search(r"(\d+)", shot_id)
    if not m:
        raise HTTPException(400, "镜头名格式错误")
    idx = int(m.group(1))

    version = (payload.get("version") or "").strip()
    kind = payload.get("kind") or "image"
    if not version:
        raise HTTPException(400, "缺少 version")

    # 回滚前置判断
    from ..core.rollback_guard import guard
    chk = guard.shot(d, "shot_%03d" % idx, version, kind=kind)

    if not chk.ok:
        raise HTTPException(409, {
            "detail": "还原被拒绝",
            "blockers": chk.blockers,
            "info": chk.info,
        })
    if chk.skip:
        return {"ok": True, "skipped": True,
                "msg": chk.skip_reason, "check": chk.to_dict()}

    # 确认保护
    params = {"project": name, "chapter": int(chapter),
              "shot": shot_id, "version": version, "kind": kind}
    tok = payload.get("confirm_token") or ""
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    rp = AssetReplacer(d)
    ok, msg = rp.restore_variant(idx, version, kind=kind)
    if not ok:
        raise HTTPException(400, msg)

    try:
        danger.notify("asset.restore", params, {"msg": msg})
        danger.log("asset.restore", params, {"msg": msg})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "msg": msg, "check": chk.to_dict()}


# ---------------------------------------------------------------- 批量操作
@app.get("/api/batch/ops")
def batch_ops_list():
    """可用的批量操作。"""
    from ..core.assets_mgr import BatchOps
    return {"ops": [{"name": k, "desc": v}
                    for k, v in BatchOps.OPS.items()]}


@app.post("/api/batch/{name}/{chapter}/preview")
def batch_preview(name, chapter, payload: dict = Body(...)):
    """预览批量操作影响。"""
    from ..core.assets_mgr import BatchOps

    d = _chapter_dir(name, chapter)
    op = payload.get("op") or ""
    shots = payload.get("shots") or []
    extra = payload.get("extra") or {}

    if not op:
        raise HTTPException(400, "缺少 op")
    if not shots:
        raise HTTPException(400, "缺少 shots（镜头序号列表）")

    bo = BatchOps(d, _proj_dir(name))
    return bo.preview(op, [int(x) for x in shots], extra=extra)


@app.post("/api/batch/{name}/{chapter}/execute")
def batch_execute(name, chapter, payload: dict = Body(...)):
    """执行批量操作。

    delete 需要 confirm_token。
    """
    from ..core.assets_mgr import BatchOps
    from ..core.danger import danger

    d = _chapter_dir(name, chapter)
    op = payload.get("op") or ""
    shots = [int(x) for x in (payload.get("shots") or [])]
    extra = payload.get("extra") or {}

    if not op:
        raise HTTPException(400, "缺少 op")
    if not shots:
        raise HTTPException(400, "缺少 shots")

    # 删除和选用版本都需要确认（会覆盖当前文件）
    if op in ("delete", "choose"):
        tok = payload.get("confirm_token") or ""
        params = {"project": name, "chapter": int(chapter),
                  "shots": sorted(shots)}
        ok, info = danger.consume(tok, params)
        if not ok:
            raise HTTPException(403, "需要确认：%s" % info)

    bo = BatchOps(d, _proj_dir(name))
    result = bo.execute(op, shots, extra=extra,
                        user=payload.get("user") or "")

    # 通知
    try:
        danger.notify("batch." + op,
                      {"project": name, "chapter": chapter,
                       "shots": shots},
                      {"msg": "成功 %d，失败 %d"
                              % (len(result.get("success") or []),
                                 len(result.get("failed") or [])),
                       "freed_mb": result.get("freed_mb", 0)})
        danger.log("batch." + op, {"shots": shots}, result)
    except Exception:  # noqa: BLE001
        pass

    return result


# ---------------------------------------------------------------- 回收站
@app.get("/api/trash")
def trash_list(limit: int = 100):
    """回收站列表 + 统计。"""
    from ..core.trash import trash

    return {
        "items": trash.list(limit=limit),
        "stats": trash.stats(),
    }


@app.get("/api/trash/{tid}")
def trash_get(tid):
    """回收站某条目详情。"""
    from ..core.trash import trash

    item = trash.get(tid)
    if not item:
        raise HTTPException(404, "条目不存在")
    return item


@app.post("/api/trash/{tid}/restore")
def trash_restore(tid, payload: dict = Body(default={})):
    """还原回收站条目到原位置（需要确认 token）。"""
    from ..core.trash import trash
    from ..core.danger import danger

    # 还原前置判断
    from ..core.rollback_guard import guard
    item = trash.get(tid)
    chk = guard.trash(item)
    if not chk.ok:
        raise HTTPException(409, {
            "detail": "还原被拒绝",
            "blockers": chk.blockers,
            "info": chk.info,
        })

    params = {"trash_id": tid}
    tok = (payload or {}).get("confirm_token") or ""
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    ok, result = trash.restore(tid)
    if not ok:
        raise HTTPException(400, result)

    try:
        danger.notify("trash.restore", {"trash_id": tid},
                      {"msg": "还原 %d 个文件" % result.get("restored", 0)})
        danger.log("trash.restore", {"trash_id": tid}, result)
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "result": result, "check": chk.to_dict()}


@app.delete("/api/trash/{tid}")
def trash_purge(tid, confirm_token: str = ""):
    """永久删除回收站条目（需要确认 token）。"""
    from ..core.trash import trash
    from ..core.danger import danger

    params = {"trash_id": tid}
    ok, info = danger.consume(confirm_token, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    ok, msg = trash.purge(tid)
    if not ok:
        raise HTTPException(400, msg)
    danger.notify("trash.purge", params, {"msg": msg})
    danger.log("trash.purge", params, {"msg": msg})
    return {"ok": True, "msg": msg}


@app.post("/api/trash/purge-expired")
def trash_purge_expired():
    """清理过期条目。"""
    from ..core.trash import trash

    result = trash.purge_expired()
    return {"ok": True, **result}


@app.delete("/api/trash")
def trash_clear(confirm_token: str = ""):
    """清空回收站（需要确认 token）。"""
    from ..core.trash import trash
    from ..core.danger import danger

    ok, info = danger.consume(confirm_token, {"action": "clear_all"})
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    result = trash.clear()
    danger.notify("trash.clear", {}, result)
    danger.log("trash.clear", {}, result)
    return {"ok": True, **result}


# ---------------------------------------------------------------- 回滚辅助
def _cancel_all_running(wait_seconds=30):
    """取消所有运行中的任务，等它们停下来。

    Returns:
        (ok, msg, cancelled_ids)
    """
    from ..core.runtime import runtime

    ids = runtime.running_jobs()
    if not ids:
        return True, "没有运行中的任务", []

    cancelled = []
    for jid in ids:
        try:
            ok, msg = manager.cancel(jid)
            if ok:
                cancelled.append(jid)
        except Exception as exc:  # noqa: BLE001
            print("[回滚] 取消 %s 失败：%r" % (jid, exc))

    # 等它们真的停下来
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not runtime.is_busy():
            return True, "已取消 %d 个任务" % len(cancelled), cancelled
        time.sleep(0.5)

    # 超时了
    still = runtime.running_jobs()
    return False, "取消超时，仍有 %d 个任务在跑" % len(still), cancelled


# ---------------------------------------------------------------- 回滚预检查
@app.post("/api/rollback/check")
def rollback_check(payload: dict = Body(...)):
    """回滚前的预检查（不执行任何操作）。"""
    from ..core.rollback_guard import guard
    from ..core.runtime import runtime

    kind = (payload.get("kind") or "").strip()
    name = (payload.get("name") or "").strip()
    chapter = int(payload.get("chapter") or 1)

    # 调试信息
    debug = {
        "running_jobs": runtime.running_jobs(),
        "is_busy": runtime.is_busy(),
        "describe": runtime.describe(),
    }

    if kind == "script":
        ch_dir = _chapter_dir(name, chapter)
        chk = guard.script(ch_dir, payload.get("version") or "")
        return {"kind": kind, **chk.to_dict(), "_debug": debug}

    if kind == "shot":
        ch_dir = _chapter_dir(name, chapter)
        chk = guard.shot(ch_dir, payload.get("shot") or "",
                         payload.get("version") or "",
                         kind=payload.get("asset_kind") or "image")
        return {"kind": kind, **chk.to_dict(), "_debug": debug}

    if kind == "trash":
        from ..core.trash import trash
        item = trash.get(payload.get("trash_id") or "")
        chk = guard.trash(item)
        return {"kind": kind, **chk.to_dict(), "_debug": debug}

    if kind == "snapshot":
        from ..core.versioning import ProjectSnapshots
        ps = ProjectSnapshots(_proj_dir(name))
        manifest = ps.get(payload.get("dir") or "")
        ch_dir = _chapter_dir(name, chapter)
        chk = guard.snapshot(manifest, ch_dir)
        return {"kind": kind, **chk.to_dict(), "_debug": debug}

    raise HTTPException(400, "未知的 kind：%s（可选 script/shot/trash/snapshot）"
                        % kind)


# ---------------------------------------------------------------- 回滚策略
@app.get("/api/rollback/policy")
def rollback_policy_get():
    """当前回滚策略 + 可选策略列表。"""
    from ..core.rollback_policy import policy

    return policy.summary()


@app.post("/api/rollback/policy")
def rollback_policy_set(payload: dict = Body(...)):
    """设置回滚策略。

    payload:
        default: "block" | "wait" | "cancel" | "force"
        per_op:  {"script": "block", "shot": "wait", ...}
    """
    from ..core.rollback_policy import policy

    return {"ok": True, **policy.set(
        default=payload.get("default"),
        per_op=payload.get("per_op"),
    )}


@app.get("/api/rollback/waiting")
def rollback_waiting():
    """等待队列（wait 策略下排队的回滚）。"""
    from ..core.rollback_policy import policy

    return {"waiting": policy.list_waiting()}


@app.post("/api/rollback/waiting/{qid}/cancel")
def rollback_waiting_cancel(qid):
    """取消排队中的回滚。"""
    from ..core.rollback_policy import policy

    ok, msg = policy.cancel_waiting(qid)
    return {"ok": ok, "msg": msg}


# ---------------------------------------------------------------- 3D 场景
@app.get("/api/3d/scenes")
def scenes_3d():
    """列出可用的 3D 场景与渲染器。"""
    from ..core.extensions import probe_3d
    from ..plugins import registry as reg

    reg.load_all(force=True)
    snap = reg.registry_snapshot()
    info = probe_3d()

    renderers = []
    for name in snap.get("scene3d", []):
        cls = reg.get_class("scene3d", name)
        if not cls:
            continue
        try:
            inst = cls()
            renderers.append({
                "name": name,
                "available": bool(inst.available()),
                "engines": inst.supported_engines()
                if hasattr(inst, "supported_engines") else [],
            })
        except Exception as exc:  # noqa: BLE001
            renderers.append({"name": name, "available": False,
                              "error": str(exc)[:100]})

    return {
        "available": info["available"],
        "blender_exe": info["blender_exe"],
        "note": info["note"],
        "scenes": info["scenes"],
        "renderers": renderers,
    }


@app.post("/api/3d/render")
def render_3d(payload: dict = Body(...)):
    """渲染一个 3D 镜头。

    payload:
        scene_path:  .blend 文件路径（或场景名）
        scene_name:  场景名（scene_path 为空时用）
        camera:      {"fps":24, "duration_sec":3,
                      "keyframes":[{"frame":0,"location":[x,y,z],
                                    "rotation":[rx,ry,rz],"lens":50}]}
        spec:        {"width":1080,"height":1920,"fps":24,
                      "samples":64,"engine":"BLENDER_EEVEE",
                      "transparent":true}
        out_name:    输出子目录名（默认 "3d"）
        dry_run:     true 只校验不渲染
    """
    from ..core.extensions import (
        CameraPath, RenderSpec, SceneAsset,
    )

    scene_path = (payload.get("scene_path") or "").strip()
    scene_name = (payload.get("scene_name") or "scene").strip()

    if not scene_path:
        raise HTTPException(400, "缺少 scene_path（.blend 文件路径）")

    scene = SceneAsset(name=scene_name, blend_path=scene_path)
    if not scene.exists():
        raise HTTPException(400, "场景文件不存在：%s" % scene_path)

    # 相机
    cam_data = payload.get("camera") or {}
    camera = CameraPath(
        name=cam_data.get("name") or "default",
        fps=int(cam_data.get("fps") or 24),
        duration_sec=float(cam_data.get("duration_sec") or 3.0),
    )
    for kf in (cam_data.get("keyframes") or []):
        try:
            camera.add_keyframe(
                int(kf.get("frame", 0)),
                tuple(kf.get("location") or (0, 0, 5)),
                tuple(kf.get("rotation") or (0, 0, 0)),
                float(kf.get("lens", 50)),
            )
        except Exception:  # noqa: BLE001
            pass

    if not camera.keyframes:
        # 默认：静止相机
        camera.add_keyframe(0, (0, -5, 3), (70, 0, 0))

    # 渲染参数
    spec_data = payload.get("spec") or {}
    spec = RenderSpec(
        width=int(spec_data.get("width") or 1080),
        height=int(spec_data.get("height") or 1920),
        fps=int(spec_data.get("fps") or camera.fps),
        samples=int(spec_data.get("samples") or 64),
        engine=spec_data.get("engine") or "BLENDER_EEVEE",
        transparent=bool(spec_data.get("transparent", True)),
    )

    if payload.get("dry_run"):
        return {
            "ok": True, "dry_run": True,
            "scene": scene.name,
            "frames": int(camera.duration_sec * camera.fps),
            "resolution": "%dx%d" % (spec.width, spec.height),
            "engine": spec.engine,
        }

    # 真正渲染
    from ..plugins import base as pbase

    out_name = (payload.get("out_name") or "3d").strip()

    try:
        from ..core.orchestrator import Orchestrator
        d = _proj_dir(payload.get("project") or "")
        orc = Orchestrator(
            payload.get("project"), int(payload.get("chapter") or 1),
            project_dir=d)
        orc.bind_job(payload.get("job_id") or "")
        result = orc.render_3d(scene, camera, spec, out_name=out_name)
    except pbase.ProviderError as exc:
        raise HTTPException(500, "3D 渲染失败：%s" % exc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "3D 渲染异常：%r" % exc)

    return {
        "ok": True,
        "frames": result.data.get("frames", 0) if result.data else 0,
        "duration": result.duration,
        "frames_dir": str(result.frames_dir) if result.frames_dir else None,
        "video": str(result.video) if result.video else None,
        "data": result.data,
    }


# ---------------------------------------------------------------- 电台
@app.get("/api/radio/providers")
def radio_providers():
    """可用的电台渲染器。"""
    from ..plugins import registry as reg

    reg.load_all(force=True)
    snap = reg.registry_snapshot()

    out = []
    for name in snap.get("radio", []):
        cls = reg.get_class("radio", name)
        if not cls:
            continue
        try:
            inst = cls()
            out.append({"name": name, "available": bool(inst.available())})
        except Exception as exc:  # noqa: BLE001
            out.append({"name": name, "available": False,
                        "error": str(exc)[:100]})
    return {"providers": out, "count": len(out)}


@app.post("/api/radio/render")
def render_radio(payload: dict = Body(...)):
    """渲染一期电台。

    payload:
        script: {"title":"", "host":"", "voice":"",
                 "bgm":"", "bgm_volume":0.25,
                 "segments":[{"kind":"speech","text":"...","voice":""},
                             {"kind":"silence","duration":1.0}]}
        out_name: 输出子目录名（默认 "radio"）
        dry_run:  true 只校验不渲染
    """
    from ..core.extensions import RadioPipeline, RadioScript, RadioSegment

    script_data = payload.get("script") or {}
    if not isinstance(script_data, dict):
        raise HTTPException(400, "script 必须是对象")

    # 用 RadioPipeline.from_dict 构造脚本
    try:
        script = RadioPipeline.from_dict(script_data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "script 格式错误：%r" % exc)

    segs = script.segments
    if not segs:
        raise HTTPException(400, "segments 不能为空")
    if len(segs) > 200:
        raise HTTPException(400, "segments 最多 200 段")

    if payload.get("dry_run"):
        return {
            "ok": True, "dry_run": True,
            "title": script.title,
            "segments": len(segs),
            "estimated_duration": round(script.total_duration_estimate(), 1),
        }

    out_name = (payload.get("out_name") or "radio").strip()

    try:
        from ..core.orchestrator import Orchestrator
        d = _proj_dir(payload.get("project") or "")
        orc = Orchestrator(
            payload.get("project"), int(payload.get("chapter") or 1),
            project_dir=d)
        orc.bind_job(payload.get("job_id") or "")
        result = orc.render_radio(script, out_name=out_name)
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print("[电台] 渲染失败:\n%s" % tb)
        raise HTTPException(500, "电台渲染失败：%r\n%s"
                            % (exc, tb.split("\n")[-3:]
                               if False else tb[-600:]))

    return {
        "ok": True,
        "audio": str(result.audio) if result.audio else None,
        "subtitle": str(result.subtitle) if result.subtitle else None,
        "duration": result.duration,
        "segments": result.segments,
    }


@app.get("/api/resources")
def resources_scan():
    """扫描资源占用（磁盘、可清理项、配额告警）。"""
    from ..core.resources import resources

    extra = []
    comfy = getattr(config, "COMFY_MODELS_DIR", "")
    if comfy and Path(comfy).exists():
        extra.append(("ComfyUI 模型", comfy))

    rep = resources.summary(extra_dirs=extra)

    # 配额告警也走统一告警系统
    try:
        from ..core.alerting import alerts
        alerts.load_from_config()
        for a in rep.get("quota_alerts") or []:
            alerts.emit(a["kind"], "disk", a["level"], a["msg"],
                        value=a.get("value"), limit=a.get("limit"))
    except Exception:  # noqa: BLE001
        pass

    return rep


@app.post("/api/resources/cleanup")
def resources_cleanup(payload: dict = Body(default={})):
    """清理资源。

    真删必须带 confirm_token（通过 /api/confirm/request 获取）。
    dry_run=true 时不需要 token（只预览）。
    """
    from ..core.resources import resources
    from ..core.danger import danger

    dry = payload.get("dry_run", True)
    max_safety = payload.get("max_safety") or "safe"
    kinds = payload.get("kinds") or None

    if dry:
        # 只预览，不需要确认
        return resources.cleanup(dry_run=True, max_safety=max_safety,
                                 kinds=kinds)

    # 真删：必须带确认 token
    tok = payload.get("confirm_token") or ""
    params = {"max_safety": max_safety, "kinds": kinds}
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    result = resources.cleanup(dry_run=False, max_safety=max_safety,
                               kinds=kinds,
                               user=payload.get("user") or "")
    # 通知 + 记录
    danger.notify("resource.cleanup", params, result,
                  user=payload.get("user") or "")
    danger.log("resource.cleanup", params, result,
               user=payload.get("user") or "")
    return result


@app.post("/api/resources/lru")
def resources_lru(payload: dict = Body(default={})):
    """按 LRU 淘汰。真删必须带 confirm_token。"""
    from ..core.resources import resources
    from ..core.danger import danger

    target = int(payload.get("target_free_mb") or 500)
    dry = payload.get("dry_run", True)

    if dry:
        return resources.lru_evict(target_free_mb=target, dry_run=True)

    tok = payload.get("confirm_token") or ""
    params = {"target_free_mb": target}
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    result = resources.lru_evict(target_free_mb=target, dry_run=False,
                                 user=payload.get("user") or "")
    danger.notify("resource.lru", params, result, user=payload.get("user") or "")
    danger.log("resource.lru", params, result, user=payload.get("user") or "")
    return result


@app.post("/api/resources/policy")
def resources_policy(payload: dict = Body(default={})):
    """修改清理策略/配额。"""
    from ..core.resources import resources

    return {"ok": True, "policy": resources.set_policy(**payload)}


@app.get("/api/jobs/{job_id}/progress")
def job_progress(job_id, full: bool = False):
    """详细进度：每阶段的每个细项（含耗时、成本、日志、ETA）。

    full=true 时返回全部细项明细；默认只返回摘要。
    """
    from ..core.progress import tracker

    snap = tracker.snapshot(job_id, include_items=full)
    if snap is None:
        raise HTTPException(404, "没有这个任务的进度记录")
    return snap


@app.get("/api/jobs/{job_id}/progress/summary")
def job_progress_summary(job_id):
    """精简进度（给轮询用，不含细项明细）。"""
    from ..core.progress import tracker

    snap = tracker.summary(job_id)
    if snap is None:
        raise HTTPException(404, "没有这个任务的进度记录")
    return snap


@app.get("/api/progress/all")
def progress_all():
    """所有正在追踪的任务。"""
    from ..core.progress import tracker

    out = []
    for jid in tracker.list_jobs():
        s = tracker.summary(jid)
        if s:
            out.append(s)
    return {"count": len(out), "jobs": out}


@app.get("/api/gpu")
def gpu_status():
    """实时 GPU 状态（显存、利用率、温度、功耗、进程）。"""
    from ..core.gpu import gpu_monitor

    snap = gpu_monitor.snapshot()

    # 显存告警
    try:
        from ..core.spend import spend
        spend.load_budget_from_config()
        vram = (snap.get("summary") or {}).get("mem_used_mb", 0)
        snap["alerts"] = spend.check_alerts(vram_mb=vram)
    except Exception:  # noqa: BLE001
        snap["alerts"] = []

    return snap


@app.get("/api/gpu/task/{job_id}")
def gpu_task_usage(job_id):
    """某任务的算力消耗。"""
    from ..core.gpu import gpu_monitor

    usage = gpu_monitor.task_usage(job_id)
    if usage is None:
        return {"active": False, "job_id": job_id}
    return {"active": True, "job_id": job_id, **usage}


@app.get("/api/spend/{job_id}")
def spend_job(job_id):
    """单任务成本明细（含每镜）。"""
    from ..core.spend import spend

    return spend.job_summary(job_id)


@app.get("/api/spend/{job_id}/{shot_id}")
def spend_shot(job_id, shot_id):
    """单镜头成本明细。"""
    from ..core.spend import spend

    return spend.shot_summary(job_id, shot_id)


@app.get("/api/spend")
def spend_overview(days: int = 7):
    """总览：今日、最近 N 天、所有任务的汇总。"""
    from ..core.spend import spend

    spend.load_budget_from_config()
    daily = spend.daily_summary(days=days)
    jobs = []
    for jid in spend.all_jobs():
        j = spend.job_summary(jid)
        jobs.append({
            "job_id": jid,
            "total": j["total"],
            "shot_count": j["shot_count"],
            "avg_per_shot": j["avg_per_shot"],
            "breakdown": j["breakdown"],
        })
    jobs.sort(key=lambda x: x["total"], reverse=True)
    return {
        "daily": daily,
        "jobs": jobs[:50],
        "budget": spend.budget,
        "alerts": spend.check_alerts(),
    }


@app.get("/api/alerts")
def alerts_all():
    """所有当前告警（成本 + 显存 + 历史）。"""
    from ..core.gpu import gpu_monitor
    from ..core.spend import spend
    from ..core.alerting import alerts

    alerts.load_from_config()
    spend.load_budget_from_config()

    # 1. 预算检查（自动产生告警）
    vram = (gpu_monitor.snapshot().get("summary") or {}).get("mem_used_mb", 0)
    budget_alerts = spend.check_alerts(vram_mb=vram)
    for a in budget_alerts:
        alerts.emit(a["kind"], "auto", a["level"], a["msg"],
                    value=a.get("value"), limit=a.get("limit"))

    # 2. 返回历史 + 统计
    return {
        "count": alerts.stats()["unacked"],
        "alerts": alerts.list(limit=100, only_unacked=True),
        "history": alerts.list(limit=200),
        "stats": alerts.stats(),
        "budget": spend.budget,
        "vram_mb": round(vram, 1),
        "config": alerts.config_summary(),
    }


@app.post("/api/alerts/ack")
def alerts_ack(payload: dict = Body(default={})):
    """确认告警（单条或全部）。"""
    from ..core.alerting import alerts

    if payload.get("all"):
        ok, msg = alerts.ack_all()
    else:
        aid = payload.get("id") or ""
        ok, msg = alerts.ack(aid)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "msg": msg}


@app.post("/api/alerts/mute")
def alerts_mute(payload: dict = Body(...)):
    """静音某类告警。"""
    from ..core.alerting import alerts

    kind = payload.get("kind") or ""
    secs = int(payload.get("seconds") or 3600)
    if payload.get("unmute"):
        ok, msg = alerts.unmute(kind)
    else:
        if not kind:
            raise HTTPException(400, "缺少 kind")
        ok, msg = alerts.mute(kind, secs)
    return {"ok": ok, "msg": msg, "config": alerts.config_summary()}


@app.post("/api/alerts/config")
def alerts_config(payload: dict = Body(default={})):
    """改告警配置（级别/通道/冷却）。"""
    from ..core.alerting import alerts

    alerts.configure(
        min_level=payload.get("min_level"),
        feishu=payload.get("feishu"),
        webhook_url=payload.get("webhook_url"),
        cooldown=payload.get("cooldown"),
    )
    return {"ok": True, "config": alerts.config_summary()}


@app.post("/api/alerts/test")
def alerts_test(payload: dict = Body(default={})):
    """发一条测试告警。"""
    from ..core.alerting import alerts

    alerts.load_from_config()
    alerts.reset_cooldowns()
    r = alerts.emit(
        payload.get("kind") or "test",
        "manual_test",
        payload.get("level") or "warn",
        payload.get("message") or "【测试】这是一条测试告警",
        source="manual")
    return {"ok": True, "result": r}


@app.delete("/api/alerts")
def alerts_clear(confirm_token: str = ""):
    """清空告警历史（需要确认 token）。"""
    from ..core.alerting import alerts
    from ..core.danger import danger

    ok, info = danger.consume(confirm_token, {})
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    n = alerts.stats().get("total", 0)
    ok, msg = alerts.clear()
    danger.notify("alerts.clear", {}, {"msg": "清空 %d 条" % n})
    danger.log("alerts.clear", {}, {"msg": "清空 %d 条" % n})
    return {"ok": ok, "msg": msg}


@app.post("/api/alerts/budget")
def set_budget(payload: dict = Body(...)):
    """修改告警阈值。"""
    from ..core.spend import spend

    spend.set_budget(
        per_shot=payload.get("per_shot") or 0,
        per_job=payload.get("per_job") or 0,
        daily=payload.get("daily") or 0,
        vram_mb=payload.get("vram_mb") or 0,
    )
    return {"ok": True, "budget": spend.budget}


@app.get("/api/versions/{name}/{chapter}")
def versions_overview(name, chapter):
    """版本总览：剧本版本、镜头变体、快照。"""
    from ..core.versioning import VersionManager

    proj = _proj_dir(name)
    vm = VersionManager(proj, chapter=int(chapter))
    return vm.overview()


@app.get("/api/versions/{name}/{chapter}/script")
def versions_script(name, chapter):
    """剧本版本列表。"""
    from ..core.versioning import VersionManager

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    return {"versions": vm.list_script_versions()}


@app.post("/api/versions/{name}/{chapter}/script/archive")
def versions_script_archive(name, chapter, payload: dict = Body(default={})):
    """手动归档当前剧本。"""
    from ..core.versioning import VersionManager

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    meta = vm.archive_script(
        reason=payload.get("reason") or "手动归档",
        source=payload.get("source") or "")
    if not meta:
        raise HTTPException(404, "剧本文件不存在")
    return {"ok": True, "version": meta}


@app.get("/api/versions/{name}/{chapter}/script/{version_id}")
def versions_script_read(name, chapter, version_id):
    """读某个版本的剧本内容。"""
    from ..core.versioning import VersionManager

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    fv = vm.file_versions("script.json")
    content = fv.read(version_id)
    if content is None:
        raise HTTPException(404, "版本不存在")
    return {"version": version_id, "content": content}


@app.get("/api/versions/{name}/{chapter}/script/{va}/diff/{vb}")
def versions_script_diff(name, chapter, va, vb):
    """两个版本的差异。"""
    from ..core.versioning import VersionManager

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    fv = vm.file_versions("script.json")
    return {"diff": fv.diff(va, vb)}


@app.post("/api/versions/{name}/{chapter}/script/{version_id}/rollback")
def versions_script_rollback(name, chapter, version_id, payload: dict = Body(default={})):
    """回滚剧本到某版本（需要确认 token）。"""
    from ..core.versioning import VersionManager
    from ..core.danger import danger

    # 回滚前置判断
    from ..core.rollback_guard import guard
    from ..core.rollback_policy import policy as rpolicy
    ch_dir = _chapter_dir(name, chapter)

    # 允许请求里临时指定策略
    pol = (payload or {}).get("policy") or rpolicy.get("script")
    chk = guard.script(ch_dir, version_id, policy=pol)

    if not chk.ok:
        raise HTTPException(409, {
            "detail": "回滚被拒绝",
            "blockers": chk.blockers,
            "info": chk.info,
            "policy": pol,
            "hint": "可用策略：wait（排队）/ cancel（先取消）/ force（强制）",
        })
    if chk.skip:
        return {"ok": True, "skipped": True,
                "msg": chk.skip_reason, "check": chk.to_dict()}

    # 按策略处理运行中的任务
    if chk.job_running:
        if pol == "cancel":
            ok, msg, cancelled = _cancel_all_running()
            if not ok:
                raise HTTPException(409, {
                    "detail": "取消任务失败：%s" % msg,
                    "cancelled": cancelled,
                })
            chk.info["cancelled_jobs"] = cancelled
        elif pol == "wait":
            from ..core.rollback_policy import policy as rp

            def _do_rollback():
                vm2 = VersionManager(_proj_dir(name), chapter=int(chapter))
                return vm2.rollback_script(version_id)

            qid = rp.enqueue("script.rollback",
                             {"project": name, "version": version_id},
                             _do_rollback,
                             user=(payload or {}).get("user") or "")
            return {"ok": True, "queued": True, "queue_id": qid,
                    "msg": "已排队，等任务完成后自动回滚",
                    "check": chk.to_dict()}
        # force：直接继续

    params = {"project": name, "version": version_id}
    tok = (payload or {}).get("confirm_token") or ""
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    ok, msg = vm.rollback_script(version_id)
    if not ok:
        raise HTTPException(400, msg)
    danger.notify("script.rollback", params, {"msg": msg})
    danger.log("script.rollback", params, {"msg": msg})
    return {"ok": True, "msg": msg, "check": chk.to_dict()}


@app.get("/api/versions/{name}/{chapter}/shots/{shot_id}")
def versions_shot(name, chapter, shot_id, kind: str = "image"):
    """某镜头的所有版本。"""
    from ..core.versioning import VersionManager

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    sv = vm.shot_versions(kind)
    return {
        "shot_id": shot_id,
        "kind": kind,
        "versions": sv.list(shot_id),
        "chosen": sv.chosen(shot_id),
    }


@app.post("/api/versions/{name}/{chapter}/shots/{shot_id}/choose")
def versions_shot_choose(name, chapter, shot_id, payload: dict = Body(...)):
    """选用某版本（需要确认 token）。"""
    from ..core.versioning import VersionManager
    from ..core.danger import danger

    ver = (payload.get("version") or "").strip()
    kind = payload.get("kind") or "image"
    if not ver:
        raise HTTPException(400, "缺少 version")

    params = {"project": name, "chapter": int(chapter),
              "shot": shot_id, "version": ver, "kind": kind}
    tok = payload.get("confirm_token") or ""
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    vm = VersionManager(_proj_dir(name), chapter=int(chapter))
    ok, msg = vm.choose_shot_version(shot_id, ver, kind=kind)
    if not ok:
        raise HTTPException(400, msg)

    try:
        danger.notify("asset.choose", params, {"msg": msg})
        danger.log("asset.choose", params, {"msg": msg})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "msg": msg}


@app.get("/api/snapshots/{name}")
def snapshots_list(name):
    """项目快照列表。"""
    from ..core.versioning import ProjectSnapshots

    ps = ProjectSnapshots(_proj_dir(name))
    return {"snapshots": ps.list()}


@app.post("/api/snapshots/{name}")
def snapshots_create(name, payload: dict = Body(default={})):
    """创建快照。"""
    from ..core.versioning import VersionManager

    chapter = int(payload.get("chapter") or 1)
    vm = VersionManager(_proj_dir(name), chapter=chapter)
    ok, result = vm.create_snapshot(
        name=payload.get("name") or "",
        note=payload.get("note") or "")
    if not ok:
        raise HTTPException(400, result)
    return {"ok": True, "manifest": result}


@app.post("/api/snapshots/{name}/{snapshot_dir}/restore")
def snapshots_restore(name, snapshot_dir, payload: dict = Body(default={})):
    """从快照还原（需要确认 token）。"""
    from ..core.versioning import ProjectSnapshots
    from ..core.danger import danger

    # 还原前置判断
    from ..core.rollback_guard import guard

    ps = ProjectSnapshots(_proj_dir(name))
    manifest = ps.get(snapshot_dir)
    ch_dir = _chapter_dir(name, chapter) if isinstance(chapter, int) else None
    if ch_dir is None:
        ch_dir = _proj_dir(name) / "chapter_001"
    chk = guard.snapshot(manifest, ch_dir)

    if not chk.ok:
        raise HTTPException(409, {
            "detail": "还原被拒绝",
            "blockers": chk.blockers,
            "info": chk.info,
        })

    params = {"project": name, "dir": snapshot_dir}
    tok = (payload or {}).get("confirm_token") or ""
    ok, info = danger.consume(tok, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    ok, msg = ps.restore(snapshot_dir)
    if not ok:
        raise HTTPException(400, msg)
    danger.notify("snapshot.restore", params, {"msg": msg})
    danger.log("snapshot.restore", params, {"msg": msg})
    return {"ok": True, "msg": msg, "check": chk.to_dict()}


@app.delete("/api/snapshots/{name}/{snapshot_dir}")
def snapshots_delete(name, snapshot_dir, confirm_token: str = ""):
    """删除快照（需要确认 token）。"""
    from ..core.versioning import ProjectSnapshots
    from ..core.danger import danger

    params = {"project": name, "dir": snapshot_dir}
    ok, info = danger.consume(confirm_token, params)
    if not ok:
        raise HTTPException(403, "需要确认：%s" % info)

    ps = ProjectSnapshots(_proj_dir(name))
    ok, msg = ps.delete(snapshot_dir)
    if not ok:
        raise HTTPException(400, msg)
    danger.notify("snapshot.delete", params, {"msg": msg})
    danger.log("snapshot.delete", params, {"msg": msg})
    return {"ok": True, "msg": msg}


@app.post("/api/jobs/{job_id}/priority")
def set_job_priority(job_id, payload: dict = Body(...)):
    """调整排队中任务的优先级。"""
    pri = (payload.get("priority") or "normal").strip()
    ok, msg = manager.set_priority(job_id, pri)
    if not ok:
        raise HTTPException(400, msg)
    job = manager.get(job_id)
    return {"ok": True, "msg": msg, "job": job.to_dict() if job else None}


@app.post("/api/jobs/concurrency")
def set_concurrency(payload: dict = Body(...)):
    """动态调整并发数。"""
    n = int(payload.get("max_concurrent") or 1)
    ok, msg = manager.set_max_concurrent(n)
    return {"ok": ok, "msg": msg, "max_concurrent": manager.max_concurrent}


@app.get("/api/ratelimit")
def ratelimit_status():
    """限速状态。"""
    if manager.limiter is None:
        return {"enabled": False, "config": {}}
    return {
        "enabled": True,
        "status": manager.limiter.status(),
        "config": {
            "qps": manager.limiter.qps,
            "concurrency": manager.limiter.concurrency,
            "daily": manager.limiter.daily,
            "default_qps": manager.limiter.default_qps,
            "default_concurrency": manager.limiter.default_concurrency,
            "default_daily": manager.limiter.default_daily,
        },
    }


@app.post("/api/ratelimit/reset")
def ratelimit_reset():
    """清零日配额计数（测试用）。"""
    if manager.limiter is None:
        raise HTTPException(400, "限速器未启用")
    manager.limiter.reset_daily()
    return {"ok": True}


@app.get("/api/jobs")
def list_jobs(limit: int = 50):
    return manager.list(limit)


@app.get("/api/jobs/queue")
def job_queue():
    """队列状态：正在跑的任务、排队中的任务及位置。"""
    return manager.queue_state()


@app.get("/api/jobs/{job_id}")
def job_detail(job_id):
    j = manager.get(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")

    data = j.to_dict()

    # 补充这一集的实时数据（state / 成本 / 日志尾巴 / 产物）
    try:
        ch = PROJECTS / j.project / ("chapter_%03d" % j.chapter)
        if ch.exists():
            st = _read_json(ch / "state.json", {}) or {}
            items = st.get("items") or {}

            counts = {}
            for v in items.values():
                k = v.get("status", "unknown")
                counts[k] = counts.get(k, 0) + 1

            by_stage = {}
            for key, v in items.items():
                stage = key.split("_")[0]
                d = by_stage.setdefault(stage, {"done": 0, "ok": 0,
                                                "degraded": 0, "failed": 0})
                d["done"] += 1
                s = v.get("status")
                if s == "ok":
                    d["ok"] += 1
                elif s == "degraded":
                    d["degraded"] += 1
                elif s == "failed":
                    d["failed"] += 1

            cost = _read_json(ch / "cost.json", {}) or {}
            todos = _read_json(ch / "manual_todo" / "todos.json", []) or []

            events = []
            ev_path = ch / "logs" / "events.jsonl"
            if ev_path.exists():
                lines = ev_path.read_text(encoding="utf-8",
                                          errors="replace").splitlines()
                for line in lines[-40:]:
                    try:
                        events.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue

            video = ch / ("chapter_%03d.mp4" % j.chapter)
            data["state_counts"] = counts
            data["by_stage"] = by_stage
            data["cost"] = cost.get("summary") or {}
            data["cost_records"] = (cost.get("records") or [])[-40:]
            data["todos"] = todos
            data["events"] = events
            data["has_video"] = video.exists()
            data["video_size"] = video.stat().st_size if video.exists() else 0
            data["has_report"] = (ch / "report.xlsx").exists()
            data["has_pack"] = (ch / "pack").exists()
    except Exception as exc:  # noqa: BLE001
        data["enrich_error"] = str(exc)

    return data


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id):
    ok, msg = manager.cancel(job_id)
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------- 修补
@app.delete("/api/projects/{name}")
def delete_project(name, confirm: str = ""):
    """删除整个项目。必须带 confirm=项目名，防止误删。"""
    d = _proj_dir(name)
    if confirm != name:
        raise HTTPException(400, "危险操作：需要 confirm=%s 才删除" % name)

    from ..core import atomic
    # 不直接删，先移到 _trash/，7 天内可手工找回
    trash = PROJECTS / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    import time as _t
    dest = trash / ("%s_%s" % (name, _t.strftime("%Y%m%d_%H%M%S")))
    try:
        d.rename(dest)
    except OSError as exc:
        raise HTTPException(500, "删除失败：%s" % exc)
    return {"ok": True, "moved_to": str(dest.relative_to(PROJECTS))}


@app.delete("/api/projects/{name}/{chapter}")
def delete_chapter(name, chapter, confirm: str = ""):
    d = _chapter_dir(name, chapter)
    num = int(chapter)
    if confirm != str(num):
        raise HTTPException(400, "危险操作：需要 confirm=%d 才删除" % num)

    trash = _proj_dir(name) / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    import time as _t
    dest = trash / ("%s_%s" % (d.name, _t.strftime("%Y%m%d_%H%M%S")))
    try:
        d.rename(dest)
    except OSError as exc:
        raise HTTPException(500, "删除失败：%s" % exc)
    return {"ok": True, "moved_to": str(dest.relative_to(PROJECTS))}


@app.post("/api/projects/{name}/rename")
def rename_project(name, payload: dict = Body(...)):
    new = (payload.get("to") or "").strip()
    if not new:
        raise HTTPException(400, "缺少新名字")
    if any(c in new for c in "\\/:*?\"<>|"):
        raise HTTPException(400, "新名字不能包含特殊字符")
    if new == name:
        return {"ok": True, "name": new}

    src = _proj_dir(name)
    dst = PROJECTS / new
    if dst.exists():
        raise HTTPException(400, "已存在同名项目：%s" % new)
    try:
        src.rename(dst)
    except OSError as exc:
        raise HTTPException(500, "重命名失败：%s" % exc)
    return {"ok": True, "name": new}


@app.get("/api/export/{name}")
def export_project_info(name):
    """导出项目清单（给人看/存档用）。"""
    d = _proj_dir(name)
    mem = _read_json(d / "memory.json", {}) or {}
    rows = []
    for ch in sorted(d.glob("chapter_*")):
        num = ch.name.replace("chapter_", "")
        try:
            num = int(num)
        except ValueError:
            continue
        video = ch / ("chapter_%03d.mp4" % num)
        cost = _read_json(ch / "cost.json", {}) or {}
        todos = _read_json(ch / "manual_todo" / "todos.json", []) or []
        rows.append({
            "chapter": num,
            "video": video.exists(),
            "size_mb": round(video.stat().st_size / 1048576, 2) if video.exists() else 0,
            "cost_yuan": (cost.get("summary") or {}).get("total_min_yuan"),
            "todos": len(todos),
        })
    return {
        "name": name,
        "characters": list((mem.get("characters") or {}).keys()),
        "props": list((mem.get("props") or {}).keys()),
        "chapters": rows,
        "total_cost": "%.4f" % sum(float(r["cost_yuan"] or 0) for r in rows),
    }
    """把出错项的状态清掉，重新跑一轮（只补缺的）。"""
    d = _chapter_dir(name, chapter)
    st_path = d / "state.json"
    st = _read_json(st_path, {}) or {}
    items = st.get("items") or {}
    n = 0
    for k, v in items.items():
        if v.get("status") in ("failed", "degraded", "unknown"):
            v["status"] = "pending"
            n += 1
    if n:
        from ..core import atomic
        atomic.atomic_write_text(
            st_path, json.dumps(st, ensure_ascii=False, indent=2))

    job = manager.submit(name, chapter,
                         stages=["image", "video", "audio", "bgm", "edit", "report"])
    return {"ok": True, "reset": n, "job": job.to_dict()}


@app.post("/api/projects/{name}/{chapter}/retry")
def retry_failed(name, chapter):
    """把出错项的状态清掉，重新跑一轮（只补缺的）。"""
    d = _chapter_dir(name, chapter)
    st_path = d / "state.json"
    st = _read_json(st_path, {}) or {}
    items = st.get("items") or {}
    n = 0
    for k, v in items.items():
        if v.get("status") in ("failed", "degraded", "unknown"):
            v["status"] = "pending"
            n += 1
    if n:
        from ..core import atomic
        atomic.atomic_write_text(
            st_path, json.dumps(st, ensure_ascii=False, indent=2))

    job = manager.submit(name, int(chapter),
                         stages=["image", "video", "audio",
                                 "bgm", "edit", "report"])
    return {"ok": True, "reset": n, "job": job.to_dict()}


@app.post("/api/projects/{name}/{chapter}/upload")
async def upload_manual(name, chapter, kind: str = "image",
                        shot: int = 0, file: UploadFile = File(...)):
    """上传人工素材到 manual/。"""
    d = _chapter_dir(name, chapter)
    sub = {"image": "images", "video": "videos_ok", "audio": "audio",
           "script": ""}.get(kind)
    if sub is None:
        raise HTTPException(400, "未知类型：%s" % kind)

    ext = Path(file.filename or "").suffix.lower()
    allow = {
        "image": {".png", ".jpg", ".jpeg", ".webp"},
        "video": {".mp4", ".mov"},
        "audio": {".mp3", ".wav", ".m4a"},
        "script": {".json"},
    }[kind]
    if ext not in allow:
        raise HTTPException(400, "不支持的格式：%s" % ext)

    target_dir = d / "manual" / sub if sub else d / "manual"
    target_dir.mkdir(parents=True, exist_ok=True)
    name_part = "script%s" % ext if kind == "script" else \
        ("shot_%02d%s" % (int(shot), ext))
    target = target_dir / name_part

    from ..core import atomic
    tmp = atomic.tmp_path(target)
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(file.file, fh)

    ok, why = integrity.validate(tmp, {"image": "image", "video": "video",
                                      "audio": "audio",
                                      "script": "json"}[kind])
    if not ok:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "文件校验失败：%s" % why)

    tmp.replace(target)
    return {"ok": True, "path": str(target.relative_to(PROJECTS))}


# ---------------------------------------------------------------- 记忆
MEM_SECTIONS = ("characters", "props", "world", "visual", "production", "plot")


@app.get("/api/memory/{name}")
def get_memory(name):
    d = _proj_dir(name)
    path = d / "memory.json"
    mem = _read_json(path, None)
    if mem is None:
        raise HTTPException(404, "没有记忆文件")
    return {
        "name": name,
        "corrupt": bool(mem.get("corrupt")),
        "characters": mem.get("characters") or {},
        "props": mem.get("props") or {},
        "world": mem.get("world") or {},
        "visual": mem.get("visual") or {},
        "plot": mem.get("plot") or {},
        "rolling_summary": mem.get("rolling_summary") or [],
        "updated_at": mem.get("updated_at", ""),
        "sections": list(MEM_SECTIONS),
    }


@app.post("/api/memory/{name}")
def update_memory(name, payload: dict = Body(...)):
    """局部更新记忆：只覆盖传进来的字段。"""
    d = _proj_dir(name)
    path = d / "memory.json"
    from .. import memory as mem_mod
    from ..core import atomic

    mem = mem_mod.Memory(path)
    allowed = set(MEM_SECTIONS)
    changed = []
    for k, v in (payload or {}).items():
        if k not in allowed:
            continue
        mem.data[k] = v
        changed.append(k)

    if not changed:
        raise HTTPException(400, "没有可更新的字段")

    # 备份旧文件，再原子保存
    if path.exists():
        atomic.archive_stale(path, d / "history")
    mem.save()
    return {"ok": True, "changed": changed, "updated_at": mem.data.get("updated_at")}


@app.delete("/api/memory/{name}/characters/{char}")
def delete_character(name, char):
    d = _proj_dir(name)
    path = d / "memory.json"
    from .. import memory as mem_mod
    from ..core import atomic

    mem = mem_mod.Memory(path)
    if char not in (mem.data.get("characters") or {}):
        raise HTTPException(404, "没有这个角色")
    atomic.archive_stale(path, d / "history")
    del mem.data["characters"][char]
    mem.save()
    return {"ok": True, "deleted": char}


@app.post("/api/memory/{name}/characters")
def add_character(name, payload: dict = Body(...)):
    """新增角色。"""
    d = _proj_dir(name)
    path = d / "memory.json"
    from .. import memory as mem_mod
    from ..core import atomic

    char = (payload.get("name") or "").strip()
    if not char:
        raise HTTPException(400, "角色名不能为空")

    mem = mem_mod.Memory(path)
    if char in (mem.data.get("characters") or {}):
        raise HTTPException(400, "角色「%s」已存在" % char)

    if path.exists():
        atomic.archive_stale(path, d / "history")
    mem.ensure_character(
        char,
        appearance=payload.get("appearance_prompt") or payload.get("appearance") or "",
        voice=payload.get("voice") or "",
    )
    c = mem.data["characters"][char]
    if payload.get("appearance_en"):
        c["appearance_en"] = payload["appearance_en"]
    if payload.get("seed"):
        c["seed"] = int(payload["seed"])
    if payload.get("state"):
        c["state"] = payload["state"]
    mem.save()
    return {"ok": True, "name": char}


@app.delete("/api/memory/{name}/props/{prop}")
def delete_prop(name, prop):
    d = _proj_dir(name)
    path = d / "memory.json"
    from .. import memory as mem_mod
    from ..core import atomic

    mem = mem_mod.Memory(path)
    if prop not in (mem.data.get("props") or {}):
        raise HTTPException(404, "没有这个道具")
    atomic.archive_stale(path, d / "history")
    del mem.data["props"][prop]
    mem.save()
    return {"ok": True, "deleted": prop}


@app.post("/api/memory/{name}/props")
def add_prop(name, payload: dict = Body(...)):
    d = _proj_dir(name)
    path = d / "memory.json"
    from .. import memory as mem_mod
    from ..core import atomic

    prop = (payload.get("name") or "").strip()
    if not prop:
        raise HTTPException(400, "道具名不能为空")

    mem = mem_mod.Memory(path)
    if prop in (mem.data.get("props") or {}):
        raise HTTPException(400, "道具「%s」已存在" % prop)

    if path.exists():
        atomic.archive_stale(path, d / "history")
    mem.ensure_prop(
        prop,
        appearance_en=payload.get("appearance_en") or "",
        holder=payload.get("holder") or "",
        location=payload.get("location") or "",
    )
    mem.save()
    return {"ok": True, "name": prop}


# ---------------------------------------------------------------- 日志
@app.get("/api/projects/{name}/{chapter}/logs")
def chapter_logs(name, chapter, limit: int = 300, status: str = ""):
    d = _chapter_dir(name, chapter)
    path = d / "logs" / "events.jsonl"
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue

    if status:
        events = [e for e in events if e.get("status") == status]

    total = len(events)
    events = events[-limit:]
    return {
        "name": name, "chapter": int(chapter),
        "total": total,
        "events": events,
        "files": {
            "events": str(path) if path.exists() else None,
            "report": str(d / "report.xlsx") if (d / "report.xlsx").exists() else None,
            "cost": str(d / "cost.json") if (d / "cost.json").exists() else None,
        },
    }


@app.get("/api/logs/{name}")
def project_logs(name, limit: int = 200):
    """跨集汇总日志。"""
    d = _proj_dir(name)
    events = []
    for ch in sorted(d.glob("chapter_*")):
        p = ch / "logs" / "events.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    events.sort(key=lambda e: e.get("ts", ""))
    return {"name": name, "total": len(events), "events": events[-limit:]}


@app.get("/api/projects/{name}/{chapter}/story")
def chapter_story(name, chapter, shot: str = ""):
    """剧情状态（服装/伤痕跨镜传递）。"""
    from ..core.story_state import StoryState

    d = _chapter_dir(name, chapter)
    sp = d / "story_state.json"
    st = StoryState(sp, d / "state_rules.json")

    if shot:
        rec = st.get_shot(shot)
        if not rec:
            raise HTTPException(404, "没有 %s 的记录" % shot)
        return {"name": name, "chapter": int(chapter),
                "shot": shot, "record": rec}

    return {
        "name": name, "chapter": int(chapter),
        "exists": sp.exists(),
        "path": str(sp),
        "shots": st.data.get("shots") or {},
        "summary": st.summary(),
        "rules": st.rules,
    }


@app.get("/api/projects/{name}/{chapter}/story/inherit")
def chapter_story_inherit(name, chapter, chars: str = ""):
    """模拟下一镜会继承什么。"""
    from ..core.story_state import StoryState

    d = _chapter_dir(name, chapter)
    st = StoryState(d / "story_state.json", d / "state_rules.json")
    names = [c.strip() for c in chars.split(",") if c.strip()]
    addon, warns = st.build_prompt("__next__", names)
    return {"inherit": addon, "warnings": warns}


@app.delete("/api/projects/{name}/{chapter}/story")
def chapter_story_reset(name, chapter):
    """清除剧情状态。"""
    d = _chapter_dir(name, chapter)
    sp = d / "story_state.json"
    existed = sp.exists()
    if existed:
        sp.unlink()
    bak = d / "story_state.json.bak"
    if bak.exists():
        bak.unlink()
    return {"ok": True, "removed": existed}


@app.post("/api/projects/{name}/{chapter}/audit")
def chapter_audit(name, chapter, mode: str = "relative",
                  cutoff: float = 0.0):
    """整集一致性审核。

    mode: relative（找离群的）| absolute（按阈值）
    cutoff: relative 模式是离群比例（0 = 用默认 0.15）
            absolute 模式是相似度阈值（0 = 用默认 0.55）
    """
    from ..core.consistency import ConsistencyChecker

    d = _chapter_dir(name, chapter)
    img_dir = d / "images"
    if not img_dir.exists():
        raise HTTPException(404, "没有图片目录")

    shots = []
    for p in sorted(img_dir.glob("shot_*.png")) + sorted(img_dir.glob("shot_*.jpg")):
        shots.append({"id": p.stem, "path": str(p)})

    if not shots:
        raise HTTPException(404, "没有找到 shot_*.png / shot_*.jpg")

    ck = ConsistencyChecker(None)
    report = ck.audit_batch(
        shots,
        cutoff=(cutoff if cutoff > 0 else None),
        mode=mode,
    )

    # 写报告
    rp = d / "consistency_report.json"
    rp.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                  encoding="utf-8")

    return {
        "name": name, "chapter": int(chapter),
        "mode": mode,
        "scanned": len(shots),
        "report": report.to_dict(),
        "summary": report.summary(),
        "path": str(rp),
    }


@app.get("/api/projects/{name}/{chapter}/audit")
def chapter_audit_read(name, chapter):
    """读最近一次一致性审核结果。"""
    d = _chapter_dir(name, chapter)
    rp = d / "consistency_report.json"
    if not rp.exists():
        return {"exists": False}
    try:
        data = json.loads(rp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "报告解析失败：%r" % exc) from exc
    return {"exists": True, "path": str(rp), **data}


@app.get("/api/plugins")
def plugins_list():
    """插件清单 + 接口规范校验。"""
    from ..plugins import registry, interface

    registry.load_all(force=True)
    snapshot = registry.registry_snapshot()
    checks = interface.check_all()

    kinds = {}
    for kind, names in snapshot.items():
        kinds[kind] = []
        for n in names:
            errs = checks.get("%s/%s" % (kind, n), [])
            kinds[kind].append({"name": n, "ok": not errs, "errors": errs})

    total = sum(len(v) for v in kinds.values())
    bad = sum(1 for v in kinds.values() for x in v if not x["ok"])
    return {"kinds": kinds, "total": total, "failed": bad,
            "summary": interface.report()}


@app.get("/api/capabilities")
def capabilities():
    """能力清单：3D / 电台等扩展接口是否就绪。"""
    from ..plugins import registry
    from ..core import extensions as ext

    registry.load_all(force=True)
    snap = registry.registry_snapshot()

    p3d = ext.probe_3d()
    pradio = ext.probe_radio()

    return {
        "3d": {
            "available": p3d["available"],
            "enabled": bool(p3d["blender_exe"]),
            "blender_exe": p3d["blender_exe"],
            "scenes": p3d["scenes"],
            "video_providers": snap.get("video", []),
            "note": p3d["note"],
        },
        "radio": {
            "available": pradio["available"],
            "enabled": True,
            "tts": pradio["tts"],
            "bgm": pradio["bgm"],
            "note": pradio["note"],
        },
    }


@app.get("/api/radio/schema")
def radio_schema():
    """电台稿的 schema（给前端表单用）。"""
    return {
        "fields": {
            "title": "节目标题",
            "host": "主播名",
            "voice": "主播音色（如 zh-CN-XiaoxiaoNeural）",
            "bgm": "全片垫乐文件路径",
            "bgm_volume": "垫乐音量（0~1）",
        },
        "segment_kinds": {
            "speech": "口播（填 text + 可选 voice）",
            "music":  "垫乐段落（填 music + volume + fade_in/out）",
            "sfx":    "音效（填 music 路径）",
            "silence": "静默（填 duration 秒数）",
        },
        "voice_options": [
            {"id": "zh-CN-XiaoxiaoNeural", "desc": "女声·温柔"},
            {"id": "zh-CN-XiaoyiNeural", "desc": "女声·年轻"},
            {"id": "zh-CN-YunxiNeural", "desc": "男声·清朗"},
            {"id": "zh-CN-YunjianNeural", "desc": "男声·低沉"},
            {"id": "zh-CN-YunyangNeural", "desc": "男声·新闻"},
            {"id": "zh-CN-YunxiaNeural", "desc": "男声·少年"},
        ],
    }


@app.post("/api/radio/preview")
def radio_preview(payload: dict = Body(...)):
    """电台稿预览：只做估算，不实际合成。"""
    from ..core import extensions as ext

    try:
        rs = ext.RadioPipeline.from_dict(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "电台稿解析失败：%r" % exc) from exc

    return {
        "script": rs.to_dict(),
        "estimated_duration": round(rs.total_duration_estimate(), 1),
        "segment_count": len(rs.segments),
        "note": "预览只做时长估算，实际合成需要 TTS + ffmpeg",
    }


def _list_3d_scenes():
    """扫描 3D 场景文件。"""
    out = []
    for base in (Path(config.ASSETS_DIR) / "scenes",
                 Path(config.PROJECTS_DIR)):
        if not base.exists():
            continue
        for p in base.rglob("*.blend"):
            out.append(str(p))
    return out[:50]


# ---------------------------------------------------------------- 设置
EDITABLE = (
    "SCRIPT_PROVIDER", "IMAGE_PROVIDER", "VIDEO_PROVIDER", "TTS_PROVIDER",
    "BGM_PROVIDER", "EDIT_PROVIDER", "LOG_PROVIDER",
    "OUTPUT_RESOLUTION", "OUTPUT_UPSCALE_SHARPEN", "IMAGE_GEN_SIZE",
    "VIDEO_BUDGET_MAX", "COST_ALERT", "SCRIPT_BUDGET", "IMAGE_BUDGET",
    "TTS_BUDGET", "MEMORY_MAX_CHARS", "STRICT_MODE", "AUTO_FALLBACK",
    "EDIT_EXPORT_PACK", "PROJECT_DISK_MAX_MB",
    # 画风
    "IMAGE_STYLE_SUFFIX",
    # 云端模型
    "DOUBAO_IMAGE_MODEL", "DOUBAO_IMAGE_SIZE",
    "JIMENG_MODEL", "JIMENG_RESOLUTION", "JIMENG_DURATION",
    # 配音
    "VOLC_TTS_VOICE", "TTS_VOICE_WANBAI", "TTS_VOICE_LINYUE",
)


@app.get("/api/settings")
def get_settings():
    from ..plugins import registry
    return {
        "values": {k: getattr(config, k, None) for k in EDITABLE},
        "providers": registry.registry_snapshot(),
        "env_path": str(config.ROOT / ".env"),
        "style_presets": getattr(config, "IMAGE_STYLE_PRESETS", {}),
    }


# ---------------------------------------------------------------- 素材包
@app.get("/api/manual/pending/file")
def manual_pending_file(project: str = "", chapter: int = 1,
                        file: str = ""):
    """读 _待确认/ 里的单个文件（给缩略图用）。"""
    from fastapi.responses import FileResponse

    if not project or not file:
        raise HTTPException(400, "缺参数")
    if Path(file).name != file:
        raise HTTPException(400, "文件名不允许带路径")

    ch_dir = PROJECTS / project / ("chapter_%03d" % int(chapter))
    p = ch_dir / "manual" / "_待确认" / file
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "文件不存在")

    return FileResponse(str(p))


@app.get("/api/manual/pending")
def manual_pending(project: str = "", chapter: int = 1):
    """列出 _待确认/ 里的文件，并为每个推荐最像的镜号。

    做法：
        图片 → 和现有 images/ 里的 40 张图逐张算相似度，取最高的
        音频 → 直接用字幕/台词文本匹配文件名（不可靠，返回空）
        视频 → 抽首帧再和图片比
    """
    import tempfile

    if not project:
        raise HTTPException(400, "缺少项目名")

    ch_dir = PROJECTS / project / ("chapter_%03d" % int(chapter))
    pending_dir = ch_dir / "manual" / "_待确认"
    img_dir = ch_dir / "images"

    if not pending_dir.exists():
        return {"count": 0, "items": [], "path": str(pending_dir)}

    files = [f for f in pending_dir.iterdir() if f.is_file()]
    if not files:
        return {"count": 0, "items": [], "path": str(pending_dir)}

    # 现有图片指纹（只算一次）
    ref_fps = {}
    try:
        from ..core.consistency import fingerprint
        if img_dir.exists():
            for p in img_dir.glob("shot_*.png"):
                try:
                    idx = int(p.stem.split("_")[1])
                    ref_fps[idx] = fingerprint(p)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    items = []
    for f in sorted(files):
        info = {
            "file": f.name,
            "size": f.stat().st_size,
            "kind": _guess_kind(f.suffix.lower()),
            "suggest": None,
            "similarity": None,
            "thumb_ok": False,
        }

        # 图片：算相似度
        if info["kind"] == "image" and ref_fps:
            try:
                from ..core.consistency import fingerprint, similarity
                fp = fingerprint(f)
                best_idx, best_sim = None, 0.0
                for idx, ref in ref_fps.items():
                    s = similarity(fp, ref)
                    if s > best_sim:
                        best_idx, best_sim = idx, s
                if best_idx and best_sim >= 0.62:
                    info["suggest"] = best_idx
                    info["similarity"] = round(best_sim, 3)
                info["thumb_ok"] = True
            except Exception:  # noqa: BLE001
                pass

        # 视频：抽首帧再算
        elif info["kind"] == "video" and ref_fps:
            try:
                tmp = Path(tempfile.mkdtemp(prefix="_pnd_"))
                frame = tmp / "f.jpg"
                from ..core.video_check import _extract_frame
                if _extract_frame(f, 0.0, frame) and frame.exists():
                    from ..core.consistency import fingerprint, similarity
                    fp = fingerprint(frame)
                    best_idx, best_sim = None, 0.0
                    for idx, ref in ref_fps.items():
                        s = similarity(fp, ref)
                        if s > best_sim:
                            best_idx, best_sim = idx, s
                    if best_idx and best_sim >= 0.62:
                        info["suggest"] = best_idx
                        info["similarity"] = round(best_sim, 3)
                    info["thumb_ok"] = True
                import shutil as _sh
                _sh.rmtree(tmp, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

        items.append(info)

    return {
        "count": len(items),
        "items": items,
        "path": str(pending_dir),
        "total_shots": len(ref_fps),
    }


@app.post("/api/manual/claim")
def manual_claim(payload: dict = Body(...)):
    """把 _待确认/ 里的一个文件认领到指定镜号。

    请求：{"project": "...", "chapter": 1, "file": "乱名.png",
           "shot": 5, "kind": "auto", "line": 1}
    """
    import re
    import shutil as _sh

    project = str(payload.get("project") or "").strip()
    filename = str(payload.get("file") or "").strip()
    if not project or not filename:
        raise HTTPException(400, "缺少项目名或文件名")

    if Path(filename).name != filename:
        raise HTTPException(400, "文件名不允许带路径")

    try:
        chapter = int(payload.get("chapter") or 1)
        shot = int(payload.get("shot") or 0)
        line = int(payload.get("line") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "参数必须是数字")

    if shot <= 0 or shot > 999:
        raise HTTPException(400, "镜号范围 1-999")

    ch_dir = PROJECTS / project / ("chapter_%03d" % chapter)
    src = ch_dir / "manual" / "_待确认" / filename
    if not src.exists():
        raise HTTPException(404, "文件不存在")

    ext = src.suffix.lower()
    kind = str(payload.get("kind") or "auto")

    if kind == "auto":
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            kind = "image"
        elif ext in (".mp4", ".mov", ".webm", ".mkv"):
            kind = "video"
        else:
            kind = "audio"

    if kind == "image":
        dst = ch_dir / "manual" / "images" / ("shot_%02d%s" % (shot, ext))
    elif kind == "video":
        dst = ch_dir / "manual" / "videos_ok" / ("shot_%02d%s" % (shot, ext))
    else:
        dst = ch_dir / "manual" / "audio" / (
            "s%02d_%02d%s" % (shot, line, ext))

    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists():
        raise HTTPException(409, "目标已存在：%s" % dst.name)

    try:
        _sh.move(str(src), str(dst))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "移动失败：%r" % exc)

    return {"ok": True, "moved_to": str(dst),
            "rel": "%s/%s" % (dst.parent.name, dst.name)}


@app.post("/api/manual/pending/clear")
def manual_pending_clear(payload: dict = Body(...)):
    """清空 _待确认/（移到回收站）。"""
    import shutil as _sh

    project = str(payload.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "缺少项目名")

    try:
        chapter = int(payload.get("chapter") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "集数必须是数字")

    ch_dir = PROJECTS / project / ("chapter_%03d" % chapter)
    pending = ch_dir / "manual" / "_待确认"

    if not pending.exists():
        return {"ok": True, "deleted": 0}

    n = len([f for f in pending.iterdir() if f.is_file()])

    # 走回收站
    try:
        from ..core.trash import TrashManager
        tm = TrashManager(PROJECTS)
        paths = [f for f in pending.iterdir() if f.is_file()]
        if paths:
            tm.move(paths, op="清理待确认")
    except Exception:  # noqa: BLE001
        _sh.rmtree(pending, ignore_errors=True)

    return {"ok": True, "deleted": n}


def _guess_kind(ext: str) -> str:
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return "image"
    if ext in (".mp4", ".mov", ".webm", ".mkv"):
        return "video"
    return "audio"


@app.post("/api/manual/organize")
def organize_manual(payload: dict = Body(...)):
    """整理 manual/_inbox/ 里的人工素材。

    请求：{"project": "...", "chapter": 1, "dry": false}
    返回：{ok, moved, conflicts, pending, detail}
    """
    import re
    import shutil as _sh

    project = str(payload.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "缺少项目名")

    try:
        chapter = int(payload.get("chapter") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "集数必须是数字")

    dry = bool(payload.get("dry", False))

    ch_dir = PROJECTS / project / ("chapter_%03d" % chapter)
    if not ch_dir.exists():
        raise HTTPException(404, "项目不存在")

    manual = ch_dir / "manual"
    inbox = manual / "_inbox"

    inbox.mkdir(parents=True, exist_ok=True)
    for sub in ("images", "videos_ok", "audio", "_待确认"):
        (manual / sub).mkdir(parents=True, exist_ok=True)

    files = [f for f in inbox.iterdir() if f.is_file()]
    if not files:
        return {"ok": True, "moved": 0, "conflicts": 0, "pending": 0,
                "detail": "inbox 是空的，把文件拖进 %s 再试"
                          % str(inbox)}

    EXT_MAP = {
        ".png": "images", ".jpg": "images", ".jpeg": "images",
        ".webp": "images", ".bmp": "images",
        ".mp4": "videos_ok", ".mov": "videos_ok", ".webm": "videos_ok",
        ".mkv": "videos_ok",
        ".mp3": "audio", ".wav": "audio", ".m4a": "audio",
        ".aac": "audio", ".flac": "audio", ".ogg": "audio",
    }

    def _shot(name):
        stem = Path(name).stem.lower()
        m = re.match(r'^s\s*0*(\d{1,3})[_\-\s]0*(\d{1,2})', stem)
        if m:
            return int(m.group(1))
        for pat in (r'shot[_\-\s]*0*(\d{1,3})',
                    r'第?\s*0*(\d{1,3})\s*镜',
                    r'镜\s*0*(\d{1,3})'):
            m = re.search(pat, stem)
            if m:
                return int(m.group(1))
        if re.fullmatch(r'0*\d{1,3}', stem):
            return int(stem)
        m = re.match(r'^0*(\d{1,3})[_\-\.]', stem)
        if m:
            return int(m.group(1))
        m = re.search(r'[_\-\.]0*(\d{1,3})$', stem)
        if m:
            return int(m.group(1))
        return None

    def _line(name):
        stem = Path(name).stem.lower()
        m = re.match(r'^s\s*0*(\d{1,3})[_\-\s]0*(\d{1,2})', stem)
        if m:
            return int(m.group(2)) or 1
        m = re.search(r'[_\-\s]0*(\d{1,2})$', stem)
        if m:
            return int(m.group(1)) or 1
        return 1

    moved = 0
    conflicts = 0
    pending = 0
    details = []

    for f in sorted(files):
        ext = f.suffix.lower()
        target = EXT_MAP.get(ext)
        shot = _shot(f.name)

        if not target or shot is None:
            pending += 1
            if not dry:
                dst = manual / "_待确认" / f.name
                n = 2
                while dst.exists():
                    dst = manual / "_待确认" / (
                        "%s_%d%s" % (f.stem, n, f.suffix))
                    n += 1
                try:
                    _sh.move(str(f), str(dst))
                except Exception:  # noqa: BLE001
                    pass
            details.append({"file": f.name, "result": "待确认",
                            "reason": "识别不出镜号"
                            if shot is None else "不支持的后缀"})
            continue

        if target == "audio":
            ln = _line(f.name)
            newname = "s%02d_%02d%s" % (shot, ln, ext)
        else:
            newname = "shot_%02d%s" % (shot, ext)

        dst = manual / target / newname
        if dst.exists():
            conflicts += 1
            n = 2
            while dst.exists():
                dst = manual / target / (
                    "%s_%d%s" % (Path(newname).stem, n, ext))
                n += 1

        if dry:
            details.append({"file": f.name, "result": "会移到 %s/%s"
                            % (target, dst.name)})
        else:
            try:
                _sh.move(str(f), str(dst))
                moved += 1
                details.append({"file": f.name,
                                "result": "%s/%s" % (target, dst.name)})
            except Exception as exc:  # noqa: BLE001
                details.append({"file": f.name,
                                "result": "失败：%s" % str(exc)[:60]})

    return {"ok": True, "moved": moved, "conflicts": conflicts,
            "pending": pending, "dry": dry,
            "inbox": str(inbox), "details": details[:100]}


@app.get("/api/manual/inbox")
def manual_inbox(project: str = "", chapter: int = 1):
    """查看 _inbox 里有多少待整理的文件。"""
    if not project:
        raise HTTPException(400, "缺少项目名")

    ch_dir = PROJECTS / project / ("chapter_%03d" % int(chapter))
    inbox = ch_dir / "manual" / "_inbox"

    if not inbox.exists():
        return {"exists": False, "count": 0, "path": str(inbox)}

    files = [f for f in inbox.iterdir() if f.is_file()]
    return {
        "exists": True,
        "count": len(files),
        "path": str(inbox),
        "files": [f.name for f in files[:20]],
    }


@app.post("/api/pack/export")
def export_pack_api(payload: dict = Body(...)):
    """导出素材包给外部剪辑团队。

    请求：{"project": "...", "chapter": 1, "with_video": true, "zip": true}
    """
    from ..core.pack_export import export_pack

    project = str(payload.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "缺少项目名")

    try:
        chapter = int(payload.get("chapter") or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "集数必须是数字")

    chapter_dir = PROJECTS / project / ("chapter_%03d" % chapter)
    if not chapter_dir.exists():
        raise HTTPException(404, "项目不存在：%s 第 %d 集"
                            % (project, chapter))

    try:
        return export_pack(
            project, chapter, chapter_dir,
            zip_it=bool(payload.get("zip", True)),
            with_video=bool(payload.get("with_video", True)),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "导出失败：%r" % exc)


@app.get("/api/pack/download/{project}/{chapter}")
def download_pack(project: str, chapter: int):
    """下载已导出的素材包 zip。"""
    from fastapi.responses import FileResponse

    base = PROJECTS / project / ("chapter_%03d" % chapter)
    zips = sorted(base.glob("素材包_*.zip"))
    if not zips:
        raise HTTPException(404, "素材包不存在，请先导出")

    z = zips[-1]
    return FileResponse(str(z), media_type="application/zip",
                        filename=z.name)


@app.get("/api/usage")
def get_usage(project: str = "", chapter: int = 0,
              stage: str = "", provider: str = "",
              since: str = "", until: str = "",
              limit: int = 200):
    """用量明细 + 汇总。"""
    from ..core import usage_db

    ch = int(chapter) if chapter else None
    rows = usage_db.query(project=project, chapter=ch, stage=stage,
                          provider=provider, since=since, until=until,
                          limit=max(1, min(2000, int(limit or 200))))
    st = usage_db.stats(project=project, chapter=ch,
                        since=since, until=until)
    return {
        "rows": rows,
        "stats": st,
        "health": usage_db.health(),
    }


@app.get("/api/usage/summary")
def get_usage_summary(project: str = "", chapter: int = 0,
                      since: str = "", until: str = ""):
    """只要汇总（页面顶部卡片用）。"""
    from ..core import usage_db
    ch = int(chapter) if chapter else None
    return usage_db.stats(project=project, chapter=ch,
                          since=since, until=until)


@app.get("/api/style")
def get_style():
    """当前画风 + 可用预设。"""
    return {
        "current": getattr(config, "IMAGE_STYLE_SUFFIX", "") or "",
        "presets": getattr(config, "IMAGE_STYLE_PRESETS", {}),
    }


@app.post("/api/style")
def set_style(payload: dict = Body(...)):
    """改画风。传 preset="水墨" 用预设，或 suffix="自定义文本"。"""
    preset = str(payload.get("preset") or "").strip()
    suffix = payload.get("suffix")

    presets = getattr(config, "IMAGE_STYLE_PRESETS", {})
    if preset:
        if preset not in presets:
            raise HTTPException(400, "未知画风：%s（可选 %s）"
                                % (preset, " / ".join(presets)))
        suffix = presets[preset]

    suffix = (suffix or "").strip()
    # 长度限制，避免拼出超长 prompt
    if len(suffix) > 300:
        raise HTTPException(400, "画风描述最多 300 字（给了 %d）"
                            % len(suffix))

    env_path = Path(config.ROOT) / ".env"
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    out = []
    seen = False
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln else ""
        if key == "IMAGE_STYLE_SUFFIX" and not ln.lstrip().startswith("#"):
            out.append("IMAGE_STYLE_SUFFIX=%s" % suffix)
            seen = True
        else:
            out.append(ln)
    if not seen:
        out.append("IMAGE_STYLE_SUFFIX=%s" % suffix)

    from ..core import atomic
    atomic.atomic_write_text(env_path, "\n".join(out) + "\n")
    config.IMAGE_STYLE_SUFFIX = suffix

    return {"ok": True, "current": suffix,
            "preset": preset or None}


@app.post("/api/settings")
def set_settings(payload: dict = Body(...)):
    """写回 .env（保留未知行）。"""
    env_path = Path(config.ROOT) / ".env"
    values = {k: str(v) for k, v in (payload or {}).items() if k in EDITABLE}

    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    seen = set()
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln else ""
        if key in values and not ln.lstrip().startswith("#"):
            out.append("%s=%s" % (key, values[key]))
            seen.add(key)
        else:
            out.append(ln)
    for k, v in values.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))

    from ..core import atomic
    atomic.atomic_write_text(env_path, "\n".join(out) + "\n")

    # 热更新当前进程的配置对象（重启后彻底生效）
    for k, v in values.items():
        cur = getattr(config, k, None)
        try:
            if isinstance(cur, bool):
                setattr(config, k, v.lower() in ("1", "true", "yes"))
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(config, k, int(float(v)))
            elif isinstance(cur, float):
                setattr(config, k, float(v))
            else:
                setattr(config, k, v)
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True, "written": values,
            "note": "部分配置需重启服务生效"}


# ---------------------------------------------------------------- 媒体
THUMB_CACHE = PROJECTS.parent / ".cache" / "thumbs"
THUMB_WIDTH = 320
THUMB_QUALITY = 78


def _thumb_for(src: Path):
    """按需生成缩略图并缓存。返回缩略图路径，失败则回原图。"""
    try:
        from PIL import Image
        st = src.stat()
        key = "%s_%d_%d.jpg" % (src.stem, int(st.st_mtime), THUMB_WIDTH)
        # 目录层级也带上，避免不同项目同名冲突
        sub = src.parent.parent.name + "_" + src.parent.name
        dst = THUMB_CACHE / sub / key
        if dst.exists() and dst.stat().st_size > 512:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            nh = max(1, int(h * THUMB_WIDTH / w))
            im = im.resize((THUMB_WIDTH, nh), Image.LANCZOS)
            tmp = dst.with_suffix(".tmp.jpg")
            im.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
            tmp.replace(dst)
        return dst
    except Exception:  # noqa: BLE001
        return src


@app.get("/thumb/{path:path}")
def media_thumb(path: str):
    """缩略图。界面列表用这个，别直接拉 2MB 原图。"""
    src = (PROJECTS / path).resolve()
    try:
        src.relative_to(PROJECTS.resolve())
    except ValueError:
        raise HTTPException(403, "越界路径")
    if not src.is_file():
        raise HTTPException(404, "文件不存在")

    dst = _thumb_for(src)
    return FileResponse(str(dst), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/media/{path:path}")
def media(path: str):
    p = _safe_rel(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p))     # Starlette 自动支持 Range


# 静态资源（前端 js/css）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def serve(host="127.0.0.1", port=8501):
    import uvicorn
    config.ensure_dirs()
    uvicorn.run(app, host=host, port=port, log_level="info")
