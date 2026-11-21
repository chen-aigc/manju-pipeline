# -*- coding: utf-8 -*-
"""大模型调度（OpenAI 兼容协议：DeepSeek / Kimi / 通义 / 本地 Ollama 通用）。

特点：
    · 只用标准库，不依赖 openai SDK
    · 强制 JSON 输出 + 自动修复常见的格式问题
    · 失败重试（指数退避），并统计 token 用量
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import config


class LLMError(Exception):
    pass


SYSTEM_PROMPT = """你是一位资深竖屏短剧导演兼分镜师，把小说改编成 60~90 秒的漫剧。

## 最重要的规则：镜头要多、要短

- 一集必须拆成 **60~90 个镜头（shot）**，每个镜头对应一张图和 1~2 秒画面。
- 每个镜头只承载 **一句话**（旁白或一句台词），超过 18 字必须拆开。
- 高潮段落（打斗、爆发、反转）镜头要更密：**每个动作 1 秒，一个动作至少拆 3 个镜头**。

## 景别必须轮换，不能一直是同一种

每个镜头的 `shot_type` 只能是这六种之一，且要交错使用：

| shot_type | 含义 | 用途 |
|---|---|---|
| `远景` | 大场景，人很小 | 交代环境、地点转换 |
| `中景` | 半身 | 对话 |
| `特写` | 面部或局部（手、眼睛、剑） | 情绪爆发 |
| `空镜` | 纯景物，没有人 | 转场、停顿、渲染气氛 |
| `死物` | 道具特写（断剑、退婚书、油灯） | 强调关键物件 |
| `过肩` | 从一人肩后看另一人 | 对峙、对话 |

**每 5 个镜头内至少出现 3 种不同景别。** 不允许连续 3 个镜头同一景别。

## 景别配比（硬性，必须落在区间内）

| 景别 | 占比 | 40 镜对应 |
|---|---|---|
| 中景 | 25~30% | 10~12 个 |
| 特写 | 20~25% | 8~10 个 |
| 远景 | 15~20% | 6~8 个 |
| 空镜 | 10~15% | 4~6 个 |
| 过肩 | 8~12% | 3~5 个 |
| 死物 | 5~10% | 2~4 个 |

## 特写的适用范围（重要）

特写 **只用于脸部情绪**（震惊、隐忍、冷笑）。

**以下内容不要用「特写」**，改用括号里的景别：

| 想拍的内容 | 用什么 | 原因 |
|---|---|---|
| 手部动作（擦伤口、拧布、缝衣、握剑） | 中景 或 死物 | 手部特写本地模型画不出，会变成半身像 |
| 道具细节（剑、灯、碗、信） | 死物 | 道具特写最稳 |
| 环境细节（雪、雨、门缝） | 空镜 | 纯景最好画 |

**先记这条，再定景别。**

**特写不能超过 25%**：整集全是脸会失去空间感，观众会疲劳。
特写只用在情绪爆发点（一句话的转折、质问、决裂），不要用来凑数。

## 角色要求

- **必须有配角**：不要整集只有主角，至少 2 个配角有台词或明确出场。
- 每个镜头列出 `characters`，没有人物就写空数组（空镜/死物）。
- 人物动作要具体：不要写「站着」，要写「握拳、后退半步、猛地抬头、把剑横在身前」。

## 画面必须和台词严格对应（最容易出错的地方）

`image_prompt` 必须画出**这一句台词正在发生的事**，不是「大概的场景」。

**硬性检查**：如果把这句台词和画面提示词放一起，别人能看出它们不是同一件事，就是错的。

## 动作要写到「能画出来」的程度

只写在做什么不够，模型会画成人站着。必须写清：

| 要素 | 反例 | 正例 |
|---|---|---|
| 谁的身体部位 | wiping his hand | her right hand on his left hand |
| 拿的具体物件 | holding something | holding a wet cloth |
| 对方身上有什么 | —— | his knuckles cracked, dried blood |

**再加两个必填字段：**

- props_in_frame：画面里必须看见的物件清单（湿布、木盆、灯、断剑）
- env_motion：环境里一个**正在动**的东西（风吹发丝、烛火晃、雪落、尘浮）
- depth：前景/中景/后景各是什么（让画面有层次）

**这三个字段是画面信息量的来源，不写就只有人+树。**

## 角色状态必须逐镜记录（跨镜连贯的关键）

`character_state` 是**每个出场角色**在本镜的状态，用来保证：
- 上一镜脸上的血，下一镜还在
- 换了衣服要有剧情理由
- 淋湿的头发不会下一镜自己干了

**每一镜都要写全，哪怕和上一镜一样。** 这是给管线做一致性检查用的。

| 字段 | 要求 | 示例 |
|---|---|---|
| `clothing` | 本镜穿的什么，10 字内 | 青绿交领汉服 / 月白长衫 / 黑甲 / 白披风 |
| `wounds` | 可见伤痕数组，没有就写 [] | ["右脸血痕"] / ["左肩纱布"] |
| `dirt` | 污迹数组，没有就写 [] | ["肩上积雪"] / ["双手泥污"] |
| `hair_state` | 头发状态 6 字内 | 束髻 / 散落 / 淋湿 / 戴冠 |
| `emotion` | 本镜情绪 2~4 字 | 隐忍 / 讥讽 / 决然 |

**连贯性要求：**
- 伤痕和污迹必须持续，直到剧情里明确「包扎」「洗净」「换衣」
- 服装不变，除非剧情里明确「换装」
- 发型不变，除非剧情里明确「梳洗」

**如果第二镜起角色换了装却没写换装动作，就是错的。**


## 画面 vs 台词对应

| 台词 | ✅ 正确画面 | ❌ 错误画面 |
|---|---|---|
| 签了它，滚出林家 | 女人把退婚书甩在桌上，手指指向门口 | 两人站在庭院里对视 |
| 窗外风声骤紧 | 木窗被风吹得晃动，窗纸作响，屋内油灯焰心歪斜 | 少年站在树林空地上 |
| 这剑……你从何处得来？ | 老者瞳孔放大，身体前倾，手指停在半空 | 老者负手站在远处山崖 |
| 三年了，我受尽白眼 | 少年低头跪地，周围几双穿着靴子的脚围住他 | 少年独自在山顶眺望 |

**英文画面描述**：`image_prompt_en` 必须是**英文短语**（20 词以内），
只写看得见的东西：人物、动作、道具、环境、光线。
不要写心理活动。**武器写 `straight broken sword`，绝不能写 scythe。**

**地点连续性**：`scene` 字段写本镜头所在的具体地点（如「破屋屋内」「院墙外」）。
**连续 3 个镜头以内，scene 不能跳变**，除非旁白明确说了「走出」「来到」「转身」等位移动作。
观众要从画面上看出「还在原地」，不能一会儿屋里一会儿山野。

## 其他

1. 只输出 JSON，不要解释、不要 markdown 代码块。
2. 台词口语化，符合人物身份，不要播音腔。
3. 结尾留钩子。
4. 剧情必须平滑承接上一集结尾。
5. 武器要写准确：断剑就是「straight broken sword」，**不能画成镰刀或弯刀**。

## 输出 JSON 结构

{
  "chapter": 1,
  "title": "第一章 标题",
  "shots": [
    {
      "index": 1,
      "scene": "本镜头所在的具体地点，如 破屋屋内",
      "shot_type": "远景",
      "narration": "旁白（可空字符串，不超过 18 字）",
      "dialogues": [{"role": "角色名", "text": "台词（不超过 18 字）", "emotion": "震惊"}],
      "image_prompt": "画面描述（中文）：人物+动作+场景+光线。要写清这一句正在发生什么",
      "image_prompt_en": "同上的英文版，20 词以内，逗号分隔的短语，不要整句。例：young man sitting at wooden table, wiping rusty broken sword, single oil lamp, dim interior",
      "action_en": "英文动作，必须写到「谁+哪只手+拿什么+对方身上有什么」（12 词以内）。反例：wiping his hand（太抽象）。正例：her right hand holding wet cloth on his left hand, cracked knuckles, dried blood",
      "emotion": "本镜头情绪（2~4 字）：震惊/愤怒/隐忍/冷笑/恐惧/茫然/讥讽/决然",
      "emotion_en": "情绪的英文表情（3 词以内，必须写）：shocked expression, wide eyes / angry expression, furrowed brows",
      "camera": "机位（6 字以内）：缓推/拉远/固定/横移/俯视/仰视",
      "props_in_frame": ["画面里必须出现的具体物件，如：湿布、木盆、油灯、断剑"],
      "env_motion": "环境里一个正在动的东西（英文短语 6 词内）：wind moving hair / candle flickering / dust floating / snow falling",
      "depth": "前后景分层（英文短语 10 词内）：foreground 落叶, midground 女子, background 塔门",
      "prop": "本镜头出现的关键道具（没有写空字符串）",
      "characters": ["出场角色名"],
      "character_state": {
        "角色名": {
          "clothing": "该角色本镜的服装（中文 10 字内）：青绿交领汉服/月白长衫/黑甲/白披风",
          "wounds": ["该角色本镜可见的伤痕，如 右脸血痕、左肩纱布（没有写空数组）"],
          "dirt": ["污迹，如 肩上积雪、双手泥污（没有写空数组）"],
          "hair_state": "头发状态（6 字内）：束髻/散落/淋湿/戴冠",
          "emotion": "本镜情绪（2~4 字）"
        }
      },
      "act": "动作关键词，如：甩出退婚书"
    }
  ],
  "summary": "本章 150~200 字剧情摘要",
  "new_foreshadowing": ["伏笔"],
  "character_updates": [{"name": "角色名", "state": "本章结束后的状态", "appearance": "外貌补充"}]
}"""


def _post(url, payload, api_key, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def chat(messages, temperature=0.8, max_tokens=None, json_mode=True, retries=3):
    """调用大模型，返回 (内容文本, usage)。"""
    if not config.LLM_API_KEY:
        raise LLMError("未配置 LLM_API_KEY，请先在 .env 里填写")

    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            data = _post(url, payload, config.LLM_API_KEY, config.LLM_TIMEOUT)
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return content, usage
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(400).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            last_err = "HTTP %s：%s" % (exc.code, detail[:200])
            # 4xx（除 429）不重试
            if 400 <= exc.code < 500 and exc.code != 429:
                raise LLMError(last_err)
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)

        if attempt < retries:
            time.sleep(min(8, 2 ** attempt))

    raise LLMError("调用失败（重试 %d 次）：%s" % (retries, last_err))


def _extract_json(text):
    """从模型回复里抠出 JSON（容忍代码块、前后废话、被截断）。

    三级回落：
        1. 直接解析
        2. 截取 { ... }
        3. 修复尾逗号 / 补全被截断的括号
    """
    if not text:
        raise LLMError("模型返回为空")

    t = text.strip()
    # 去掉 ```json ... ```
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    if m:
        t = m.group(1).strip()

    # 1. 直接解析
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass

    # 2. 截取第一个 { 到最后一个 }
    start, end = t.find("{"), t.rfind("}")
    candidate = ""
    if start >= 0 and end > start:
        candidate = t[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(fixed)
            except Exception:  # noqa: BLE001
                pass

    # 3. 修复被截断的 JSON
    #    从第一个 { 开始，统计未闭合的 { [ 和字符串状态，补齐
    if start >= 0:
        src = t[start:]
        repaired = _repair_truncated_json(src)
        if repaired:
            try:
                data = json.loads(repaired)
                # 重要：截断修复会丢数据，必须显式警告
                lost = len(src) - len(repaired)
                print("[LLM] ⚠️ JSON 被截断，已修复（丢弃约 %d 字符）" % lost)
                if isinstance(data, dict):
                    shots = data.get("shots")
                    if isinstance(shots, list):
                        print("[LLM] ⚠️ 修复后只剩 %d 个镜头"
                              "（可能不完整，建议重跑）" % len(shots))
                return data
            except Exception:  # noqa: BLE001
                pass

    raise LLMError("无法解析 JSON（长度 %d）：%s … %s"
                   % (len(t), t[:200], t[-100:]))


def _repair_truncated_json(src):
    """尝试修复被截断的 JSON。

    做法：
        1. 扫描字符，跟踪字符串/转义状态和括号栈
        2. 如果最后一个元素没写完，回退到上一个完整的对象
        3. 补齐未闭合的括号

    Returns:
        修复后的字符串，失败返回 None
    """
    if not src.startswith("{"):
        return None

    in_str = False
    escape = False
    stack = []           # 括号栈
    # 记录每个 { 或 [ 之后的"安全截断点"（即逗号后、元素边界）
    safe_cuts = []

    i = 0
    n = len(src)
    while i < n:
        ch = src[i]

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
                # 字符串开始前是个安全点
                safe_cuts.append(i)
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
                # 闭合后是安全点
                safe_cuts.append(i + 1)
            elif ch == ",":
                safe_cuts.append(i + 1)
        i += 1

    # 情况 A：括号已闭合，只是字符串没闭合 → 补个引号
    if not stack and in_str:
        return src + '"'

    # 情况 B：括号没闭合 → 回退到最后一个安全点，再补齐括号
    if stack:
        # 从后往前找最后一个安全点
        for cut in reversed(safe_cuts):
            if cut > n:
                continue
            head = src[:cut].rstrip()
            if not head:
                continue
            # 去掉尾逗号
            head = re.sub(r",\s*$", "", head)

            # 重新扫描 head 计算还差什么括号
            s2 = False
            e2 = False
            st2 = []
            for c in head:
                if s2:
                    if e2:
                        e2 = False
                    elif c == "\\":
                        e2 = True
                    elif c == '"':
                        s2 = False
                else:
                    if c == '"':
                        s2 = True
                    elif c in "{[":
                        st2.append(c)
                    elif c in "}]":
                        if st2:
                            st2.pop()

            # 如果 head 里有未闭合的字符串，先补引号
            tail = ""
            if s2:
                tail += '"'
            # 补齐括号（反向）
            for c in reversed(st2):
                tail += "}" if c == "{" else "]"

            repaired = head + tail
            try:
                json.loads(repaired)
                return repaired
            except Exception:  # noqa: BLE001
                continue

    return None


def generate_script(source_text, memory, chapter=None, target_shots=70):
    """把小说文本 / 一句话主题 → 分镜脚本 JSON。

    输出上限说明：
        每镜有 10 个字段，40 镜的 JSON 约 9000+ token，
        超过多数模型单次输出上限（DeepSeek 8192），会被截断成非法 JSON。
        所以镜头数超过 BATCH 时自动分批生成，再合并。
    """
    ctx = memory.context_for_prompt()
    ch = chapter if chapter is not None else (memory.data["plot"].get("chapter", 0) + 1)

    prev_tail = ""
    summaries = memory.data.get("rolling_summary") or []
    if summaries:
        prev_tail = str(summaries[-1])[:200]

    want = int(target_shots or 40)
    BATCH = 14                     # 每批镜头数（约 3200 token 输出，安全）
    shot_plan = []
    if want <= BATCH + 4:
        shot_plan = [(1, want)]
    else:
        start = 1
        while start <= want:
            end = min(start + BATCH - 1, want)
            shot_plan.append((start, end))
            start = end + 1

    all_shots = []
    usage_total = {}
    summary = ""
    foreshadow = []
    char_updates = []
    head = None

    for bi, (lo, hi) in enumerate(shot_plan, 1):
        user = []
        if ctx:
            user.append(ctx)
        if prev_tail:
            user.append("【上一集结尾】\n%s\n（本章开头请从这里自然承接，不要跳跃）"
                        % prev_tail)
        user.append("【本章要改编的内容】\n%s" % (source_text or "").strip())

        if len(shot_plan) == 1:
            user.append("【要求】写第 %d 集，分镜数量 %d 个左右，严格按要求输出 JSON。"
                        % (ch, want))
        else:
            already = "、".join(str(s.get("narration") or s.get("scene") or "")
                               for s in all_shots[-3:]) or "（无）"
            user.append(
                "【分批要求】这是一集 %d 个镜头的拆分任务，本批只写 **第 %d~%d 个镜头**"
                "（共 %d 个）。index 必须从 %d 连续编到 %d。\n"
                "【前情】本批之前已有的最后几个镜头：%s\n"
                "【注意】不要重复前面写过的内容，直接接着往下演。"
                "只输出这一批的 shots 数组，JSON 结构不变。"
                % (want, lo, hi, hi - lo + 1, lo, hi, already))

        content, usage = chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user)},
        ])
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                usage_total[k] = usage_total.get(k, 0) + v

        part = _extract_json(content)
        if part is None:
            raise LLMError("剧本生成失败：第 %d~%d 镜的返回不是合法 JSON" % (lo, hi))

        if head is None:
            head = part
        shots = part.get("shots") or []
        if not shots:
            raise LLMError("剧本生成失败：第 %d~%d 镜没有返回分镜" % (lo, hi))
        # 重新编号，防止模型自己乱编
        for i, s in enumerate(shots):
            s["index"] = lo + i
        all_shots.extend(shots)

        if not summary:
            summary = str(part.get("summary") or "")
        foreshadow.extend(part.get("new_foreshadowing") or [])
        char_updates.extend(part.get("character_updates") or [])

    data = dict(head or {})
    data["chapter"] = ch
    data["shots"] = all_shots
    data.setdefault("title", "第%d集" % ch)
    data["summary"] = summary
    data["new_foreshadowing"] = foreshadow
    data["character_updates"] = char_updates

    if not data["shots"]:
        raise LLMError("模型没有返回任何分镜")

    if len(shot_plan) > 1:
        print("[剧本] 分 %d 批生成，共 %d 个镜头"
              % (len(shot_plan), len(all_shots)))

    return data, usage_total


def summarize(text, max_chars=200):
    """把一章内容压成摘要（用于滚动记忆）。"""
    content, _ = chat([
        {"role": "system",
         "content": "你是剧情编辑。把下面这集剧情压缩成不超过 %d 字的中文摘要，"
                    "只保留关键事件和人物状态变化，不要评价、不要分点。" % max_chars},
        {"role": "user", "content": text[:6000]},
    ], json_mode=False, temperature=0.3, max_tokens=500)
    return content.strip()
