# -*- coding: utf-8 -*-
"""飞书告警通知（复用监控台那套逻辑，独立可用）。"""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from . import config


def _sign(ts, secret):
    s = "%s\n%s" % (ts, secret)
    return base64.b64encode(
        hmac.new(s.encode("utf-8"), digestmod=hashlib.sha256).digest()).decode()


# 飞书机器人安全设置里的「自定义关键词」，消息必须包含才放行
# 配在 .env 的 FEISHU_KEYWORD；为空则不处理
KEYWORD = getattr(config, "FEISHU_KEYWORD", "") or ""


def _ensure_keyword(text):
    """确保文本包含飞书要求的关键词。"""
    if not KEYWORD:
        return text
    if KEYWORD in text:
        return text
    return "%s %s" % (KEYWORD, text)


def send(title, content, level="info"):
    """推送一条卡片。level: info / success / warn / error"""
    if not config.FEISHU_WEBHOOK:
        return False, "未配置 FEISHU_WEBHOOK"

    color = {"info": "blue", "success": "green",
             "warn": "orange", "error": "red"}.get(level, "blue")

    # 标题和内容都补关键词，双保险
    title = _ensure_keyword(title)
    content = _ensure_keyword(content)

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": color},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": "漫剧工厂 · %s" % time.strftime("%Y-%m-%d %H:%M:%S")}]},
            ],
        },
    }

    if config.FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(ts, config.FEISHU_SECRET)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(config.FEISHU_WEBHOOK, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            ok = data.get("code", data.get("StatusCode")) == 0
            return ok, data.get("msg", "")
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def review_card(project_name, chapter, video_path):
    """生成后的「请审阅」卡片。"""
    text = ("**项目**：%s\n**第 %s 集** 已生成完毕\n**文件**：%s\n\n"
            "请预览确认，无误后手动发布。" %
            (project_name, chapter, video_path))
    return send("🎬 新剧集待审阅", text, "success")


def fail_card(project_name, chapter, reason):
    """失败告警卡片。"""
    text = ("**项目**：%s\n**第 %s 集** 生成失败\n\n**原因**：%s\n\n"
            "修复后可在面板点「重试」。" % (project_name, chapter, reason))
    return send("⚠️ 生成失败", text, "error")
