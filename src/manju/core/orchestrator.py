# -*- coding: utf-8 -*-
"""编排层：把七个环节串起来，带状态、成本、失败清单、断点续跑。

设计：
    Orchestrator.run() 依次跑 script → image → video → audio → bgm → edit → report
    每一步都：
        1. 检查 state，已完成的跳过
        2. 调插件，记录成本
        3. 失败/降级写入 manual_todo
        4. 更新 state（原子落盘）
"""
import json
import shutil
import time
import traceback
from pathlib import Path

from . import atomic, integrity, lock, state
from .cost import Cost, CostLedger, fmt_yuan
from .. import config, memory as mem_mod, shots as shots_mod, subtitles
from .. import tts as tts_mod
from ..plugins import base as pbase, registry


STAGES = ("script", "image", "video", "audio", "bgm", "edit", "report")


class JobResult:
    def __init__(self):
        self.ok = False
        self.video = None
        self.pack = None
        self.report = None
        self.state = {}
        self.ledger = None
        self.todos = []
        self.errors = []
        self.needs_manual = False

    def summary(self):
        st = self.state.get("items", {})
        counts = {}
        for v in st.values():
            k = v.get("status", "unknown")
            counts[k] = counts.get(k, 0) + 1
        return {
            "ok": self.ok,
            "video": str(self.video) if self.video else None,
            "pack": str(self.pack) if self.pack else None,
            "report": str(self.report) if self.report else None,
            "counts": counts,
            "todos": len(self.todos),
            "cost": self.ledger.summary() if self.ledger else {},
            "errors": self.errors,
        }


class Orchestrator:
    def __init__(self, project_name, chapter, source_text="", target_shots=40,
                 providers=None, project_dir=None, resume=True,
                 cancel_event=None, on_stage=None):
        self.name = project_name
        self.chapter = int(chapter)
        self.source_text = source_text
        self.target_shots = int(target_shots or 40)
        self.resume = resume
        self.cancel_event = cancel_event      # threading.Event，可选
        self.on_stage = on_stage              # fn(stage, done, total)

        self.dir = Path(project_dir or (Path(config.PROJECTS_DIR) / project_name))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chapter_dir = self.dir / ("chapter_%03d" % self.chapter)
        for sub in ("images", "audio", "videos", "manual", "manual_todo"):
            (self.chapter_dir / sub).mkdir(parents=True, exist_ok=True)

        self.memory = mem_mod.Memory(self.dir / "memory.json")
        self.state = state.StateStore(self.chapter_dir / "state.json")
        self.ledger = CostLedger()
        self.result = JobResult()
        self.result.ledger = self.ledger

        # 剧情连贯钩子（状态继承 + 一致性检查）
        # 可用 STORY_HOOKS=0 关闭
        self.story_hooks = None
        if getattr(config, "STORY_HOOKS_ENABLED", True):
            try:
                from .story_hooks import StoryHooks
                chars_dir = Path(config.ASSETS_DIR) / "characters"
                self.story_hooks = StoryHooks(self.chapter_dir, chars_dir)
            except Exception as exc:  # noqa: BLE001
                print("[编排] 剧情钩子初始化失败（不影响主流程）：%s" % exc)

        self._providers = providers or {}
        self._log = None
        self._todos = []

        # 进度追踪用：任务 id（由 JobManager 注入，手动跑时为 None）
        self.job_id = ""
        self._progress = None

    # ---------------- 进度追踪 ----------------
    def bind_job(self, job_id):
        """绑定任务 id 到全局进度追踪器。"""
        self.job_id = str(job_id or "")
        if self.job_id:
            try:
                from .progress import tracker
                self._progress = tracker
            except Exception:  # noqa: BLE001
                self._progress = None
        return self

    def _prog(self):
        """取进度追踪器（未绑定时返回 None）。"""
        if self._progress is None or not self.job_id:
            return None
        return self._progress

    def _prog_stage(self, stage, total=0):
        """登记一个阶段（同时把所有细项写进 state，供前端展示）。"""
        p = self._prog()
        if p is None:
            return
        p.start_stage(self.job_id, stage, total=total, label=stage)
        p.log(self.job_id, "阶段 %s 开始（共 %d 项）" % (stage, total))

    def _prog_item(self, stage, item_key, label=""):
        p = self._prog()
        if p is None:
            return
        p.start_item(self.job_id, stage, item_key, label=label)

    def _prog_done(self, stage, item_key, ok=True, cost=0.0,
                   elapsed=None, error="", detail=None):
        p = self._prog()
        if p is None:
            return
        p.finish_item(self.job_id, stage, item_key, ok=ok, cost=cost,
                      elapsed=elapsed, error=error, detail=detail)

    # ---------------- 成本记账 ----------------
    def _spend(self, stage, shot_idx=None, amount=0.0):
        """记一笔成本到全局追踪器，并检查告警。"""
        if not amount or float(amount) <= 0:
            return
        try:
            from .spend import spend
        except Exception as exc:  # noqa: BLE001
            print("[成本] 导入 spend 失败：%r" % exc)
            return

        try:
            from .alerting import alerts, cost_alert

            amt = float(amount)
            job_id = self.job_id or "manual"
            shot_id = "shot_%03d" % int(shot_idx) if shot_idx is not None else None

            if shot_id:
                spend.add_shot_cost(job_id, shot_id, stage, amt)
            else:
                spend.add_job_cost(job_id, stage, amt)

            # 告警（走统一告警系统，带去重）
            spend.load_budget_from_config()
            alerts.load_from_config()
            b = spend.budget

            # 单镜超阈值
            if shot_id and b.get("per_shot", 0) > 0:
                s = spend.shot_summary(job_id, shot_id)
                if s["total"] > b["per_shot"]:
                    cost_alert(job_id, shot_id, s["total"], b["per_shot"])

            # 单任务超阈值
            if b.get("per_job", 0) > 0:
                j = spend.job_summary(job_id)
                if j["total"] > b["per_job"]:
                    alerts.emit(
                        "cost", "%s/total" % job_id, "error",
                        "任务 %s 累计 ¥%.3f 超过阈值 ¥%.2f"
                        % (job_id, j["total"], b["per_job"]),
                        value=round(j["total"], 4), limit=b["per_job"],
                        job_id=job_id)

            # 日累计超阈值
            if b.get("daily", 0) > 0:
                today = spend.daily_summary(days=1).get("today", 0)
                if today > b["daily"]:
                    alerts.emit(
                        "cost", "daily", "error",
                        "今日累计 ¥%.2f 超过阈值 ¥%.2f" % (today, b["daily"]),
                        value=round(today, 4), limit=b["daily"])
        except Exception as exc:  # noqa: BLE001
            print("[成本] 记账失败：%r" % exc)

    # ---------------- 剧情状态推断 ----------------
    def _infer_story_state(self, shot):
        """从分镜数据推断这一镜的角色状态。

        优先级：
            1. 分镜里显式的 story_state 字段（完整覆盖）
            2. 分镜里的 character_state 字段（剧本生成时写入，最常用）
            3. 分镜里的显式字段（clothing / wounds / dirt / hair_state / emotion）
            4. memory 里角色的 look / appearance
            5. 从分镜的角色描述文本里提取关键词
        """
        explicit = shot.get("story_state")
        if isinstance(explicit, dict):
            return explicit

        roles = shot.get("characters") or []
        if isinstance(roles, str):
            roles = [c.strip() for c in roles.split(",") if c.strip()]

        # 空镜也要记录（场景/光线/时间），只是没有角色状态
        if not roles:
            scene = shot.get("scene") or ""
            light = shot.get("light") or ""
            tday = shot.get("time_of_day") or ""
            if not (scene or light or tday):
                return None
            return {
                "characters": {},
                "scene": scene,
                "light": light,
                "time_of_day": tday,
                "location": shot.get("location") or "",
                "events": shot.get("events") or [],
                "empty": True,
            }

        # 剧本生成的逐角色状态（最准确）
        shot_cs = shot.get("character_state") or {}

        # 备选：从整镜描述里找服装关键词
        cn_fields = ("image_prompt", "image_prompt_cn", "action",
                     "description", "scene")
        desc = " ".join(str(shot.get(f) or "") for f in cn_fields)
        fallback_clothing = self._extract_clothing(desc)

        # 情绪：剧本字段优先
        shot_emotion = shot.get("emotion") or ""

        chars = {}
        for name in roles:
            st = {}

            # 1) 剧本给的逐角色状态（最高优先级）
            cs = shot_cs.get(name) if isinstance(shot_cs, dict) else None
            if isinstance(cs, dict):
                for f in ("clothing", "wounds", "dirt",
                          "hair_state", "emotion"):
                    v = cs.get(f)
                    if v not in (None, "", []):
                        st[f] = v

            # 2) 角色外观（来自 memory）
            if not st.get("clothing"):
                try:
                    ch = self.memory.get_character(name) or {}
                    look = ch.get("look") or ch.get("appearance") or ""
                    if look:
                        st["clothing"] = str(look)
                except Exception:  # noqa: BLE001
                    pass

            # 3) 分镜里显式给的字段
            for f in ("clothing", "wounds", "dirt", "hair_state", "emotion"):
                v = shot.get(f) or shot.get("%s_%s" % (name, f))
                if v and not st.get(f):
                    st[f] = v

            # 4) 兜底：从描述里提取
            if not st.get("clothing") and fallback_clothing:
                st["clothing"] = fallback_clothing
            if not st.get("emotion") and shot_emotion:
                st["emotion"] = shot_emotion

            # 5) 还是空就记占位
            if not st.get("clothing"):
                st["clothing"] = "（未指定）"

            if st:
                chars[name] = st

        if not chars:
            return None

        return {
            "characters": chars,
            "scene": shot.get("scene") or "",
            "light": shot.get("light") or "",
            "time_of_day": shot.get("time_of_day") or "",
            "location": shot.get("location") or "",
            "events": shot.get("events") or [],
        }

    # 服装关键词表（中文描述 → 标准名）
    CLOTHING_WORDS = (
        "汉服", "青绿", "月白", "白衣", "黑袍", "长袍", "长衫", "短打",
        "披风", "斗篷", "铠甲", "官服", "道袍", "襦裙", "交领", "束腰",
        "腰带", "布衣", "麻衣", "锦衣", "素衣",
    )

    def _extract_clothing(self, text):
        """从描述文本里找服装关键词。"""
        if not text:
            return ""
        hits = [w for w in self.CLOTHING_WORDS if w in text]
        return "/".join(dict.fromkeys(hits)) if hits else ""

    # ---------------- 插件获取 ----------------
    def p(self, kind, **bind):
        """取插件实例。

        self._providers 里可能存的是插件名（Web 传 {"image": "doubao"}），
        也可能是插件实例。这里统一转成实例并缓存。
        """
        if kind in self._providers:
            obj = self._providers[kind]
            if not isinstance(obj, str):
                return obj

            # 是名字：实例化并缓存
            cls = registry.get_class(kind, obj)
            if cls is None:
                raise RuntimeError(
                    "%s/%s 插件不存在（可用：%s）"
                    % (kind, obj, ", ".join(
                        registry.registry_snapshot().get(kind) or [])))
            inst = cls(**bind) if bind else cls()
            self._providers[kind] = inst
            return inst

        return registry.pick(kind)

    def _log_event(self, **ev):
        ev.setdefault("chapter", self.chapter)
        ev.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))

        # 无论用哪种日志插件，都写一份 jsonl 事件流，供 Web 日志页读取
        try:
            logs = self.chapter_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            line = json.dumps(ev, ensure_ascii=False)
            with open(logs / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

        try:
            lg = registry.pick("log")
        except Exception:  # noqa: BLE001
            return
        try:
            lg.log(ev)
        except Exception:  # noqa: BLE001
            pass
        if self._log is None:
            self._log = lg

    def _record(self, stage, provider, cost, shot=None, status="ok",
                msg="", elapsed=0.0,
                tokens_in=0, tokens_out=0, tokens_total=0,
                images=0, seconds=0.0, chars=0, model="",
                usage=None):
        """记一次调用。

        ledger / state / 事件日志 / 用量数据库 四处同时记。
        usage 是插件返回的原始用量字典（可选），会解出 token 等细项。
        """
        self.ledger.add(stage, provider, cost, shot=shot,
                        action="generate", status=status)
        si = cost.amount_si
        if si:
            self.state.add_cost(si)
        self._log_event(stage=stage, provider=provider, shot=shot,
                        status=status, elapsed=round(elapsed, 2),
                        cost_si=si, detail=msg,
                        cost_source=cost.source.value)

        # 用量数据库
        try:
            from . import usage_db

            si2 = int(cost.amount_si or 0)
            usage_db.record(
                stage=stage, provider=provider, model=model,
                project=self.name, chapter=self.chapter, shot=shot,
                job_id=getattr(self, "job_id", "") or "",
                tokens_in=int(tokens_in or 0),
                tokens_out=int(tokens_out or 0),
                tokens_total=int(tokens_total or 0),
                images=int(images or 0),
                seconds=float(seconds or 0),
                chars=int(chars or 0),
                cost_micro=si2 * 10000,   # 1 丝 = 0.0001 元 = 10000 微元
                cost_source=cost.source.value,
                status=status, elapsed=elapsed, error=msg)
        except Exception:  # noqa: BLE001
            pass

    def _todo(self, kind, shot, reason, extra=None):
        item = {"kind": kind, "shot": shot, "reason": reason,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if extra:
            item.update(extra)
        self._todos.append(item)

    # ---------------- 主流程 ----------------
    def run(self, stages=None):
        stages = stages or STAGES
        t_all = time.time()

        lk = lock.ProjectLock(self.dir, timeout_min=config.LOCK_TIMEOUT_MIN
                              if hasattr(config, "LOCK_TIMEOUT_MIN") else 3,
                              wait_max_min=0)
        lk._started_ts = t_all
        try:
            lk.acquire()
        except lock.LockBusy:
            raise

        try:
            self._prepare()

            # 从非 script 阶段开始跑时，先把已有剧本读进来
            script = None
            if stages and stages[0] != "script":
                script = self._load_script()

            for i, st in enumerate(stages):
                # 每个阶段之间检查取消
                if self.cancel_event is not None and self.cancel_event.is_set():
                    print("[编排] 收到取消信号，当前阶段结束后停止")
                    self.state.data["stage"] = "cancelled"
                    self.state.save()
                    self.result.errors.append("用户取消")
                    break

                if self.on_stage is not None:
                    try:
                        self.on_stage(st, i, len(stages))
                    except Exception:  # noqa: BLE001
                        pass

                fn = getattr(self, "_stage_" + st, None)
                if fn is None:
                    continue
                self.state.data["stage"] = st
                self.state.save()
                t0 = time.time()
                try:
                    out = fn(script) if st == "script" else fn(script)
                    if st == "script":
                        script = out
                except Exception as exc:  # noqa: BLE001
                    self.result.errors.append("%s: %s" % (st, exc))
                    print("[编排] %s 阶段失败：%s" % (st, exc))
                    if config.STRICT_MODE if hasattr(config, "STRICT_MODE") else False:
                        raise

                lk.heartbeat()
                print("[编排] %-6s 完成 %.1fs" % (st, time.time() - t0))

                if self.on_stage is not None:
                    try:
                        self.on_stage(st, i + 1, len(stages))
                    except Exception:  # noqa: BLE001
                        pass

            cancelled = (self.cancel_event is not None
                         and self.cancel_event.is_set())
            self.state.data["stage"] = "cancelled" if cancelled else "done"
            self.state.save()
            self.result.ok = bool(self.result.video and Path(self.result.video).exists())

        finally:
            lk.release()
            self._flush_todos()

        self.result.state = self.state.data
        self.result.todos = self._todos
        print("\n" + self.ledger.text())
        print("总耗时 %.1f 分钟" % ((time.time() - t_all) / 60))
        return self.result

    def _prepare(self):
        """启动清理：残留 .tmp、坏文件隔离、超时锁。"""
        rep = state.prepare_workdir(self.chapter_dir)
        if rep["tmp_removed"]:
            print("[编排] 清理残留临时文件 %d 个" % rep["tmp_removed"])
        for path, reason, dest in rep["quarantined"]:
            print("[编排] 隔离坏文件 %s（%s）" % (Path(path).name, reason))
            self._todo("file_corrupt", Path(path).stem, reason,
                       {"file": str(path), "moved_to": str(dest) if dest else ""})

        # 记忆损坏提示
        if getattr(self.memory, "corrupt", False):
            self._todo("memory_corrupt", None,
                       "memory.json 损坏，已降级为空记忆")

    # ---------------- 剧本 ----------------
    def _stage_script(self, _):
        prog = self._prog()
        if prog is not None:
            prog.start_stage(self.job_id, "script", total=1, label="剧本")
            prog.ensure_item(self.job_id, "script", "script", label="分镜脚本")

        sp = self.p("script")
        sp.bind(self.chapter_dir) if hasattr(sp, "bind") else None

        existing = self.chapter_dir / "script.json"

        # 复用策略：
        #   1. resume=True  → 总是复用已有剧本
        #   2. 没传 source_text（用户没写新剧情）→ 也复用，
        #      避免花钱重新生成一个无关的剧本
        #   3. 用户明确写了一段剧情 → 重新生成
        has_user_text = bool((self.source_text or "").strip())
        should_reuse = self.resume or not has_user_text

        if should_reuse and existing.exists():
            ok, why = integrity.validate_json(existing, required=["shots"])
            if ok:
                print("[编排] 复用已有剧本 %s" % existing.name)
                data = json.loads(existing.read_text(encoding="utf-8"))
                self.state.set_item("script", state.ItemStatus.OK,
                                    path=str(existing))
                if prog is not None:
                    prog.skip_item(self.job_id, "script", "script",
                                   reason="复用已有剧本")
                    prog.finish_stage(self.job_id, "script", ok=True)
                return data
            print("[编排] 剧本损坏（%s），重新生成" % why)
            atomic.quarantine(existing, self.chapter_dir)

        if prog is not None:
            prog.start_item(self.job_id, "script", "script", label="分镜脚本")
            prog.log(self.job_id, "生成分镜脚本", stage="script",
                     item="script")

        ## 按流水线自动归档旧剧本（防止重新生成覆盖掉好版本）
        try:
            from .versioning import FileVersions
            FileVersions(existing).archive(
                reason="重新生成前备份", source=sp.name)
        except Exception:  # noqa: BLE001
            pass

        t0 = time.time()
        res = sp.generate(self.source_text or "自动生成", self.memory,
                          self.chapter, self.target_shots)
        data = res.data
        data.setdefault("chapter", self.chapter)

        # 补 index
        for i, s in enumerate(data.get("shots") or [], 1):
            s.setdefault("index", i)

        atomic.atomic_write_text(
            existing, json.dumps(data, ensure_ascii=False, indent=2))

        self._record("script", sp.name, res.cost, elapsed=time.time() - t0)
        self._spend("script", shot_idx=None,
                    amount=float(getattr(res.cost, "yuan", 0) or 0))
        self.state.set_item("script", state.ItemStatus.OK, path=str(existing))
        try:
            self.memory.set_plot(chapter=self.chapter,
                                 progress=data.get("summary", "")[:200])
            if data.get("summary"):
                self.memory.add_summary("第%d章：%s" % (self.chapter,
                                                       data["summary"][:120]))
            self.memory.save()
        except Exception:  # noqa: BLE001
            pass

        if prog is not None:
            n_shots = len(data.get("shots") or [])
            prog.finish_item(self.job_id, "script", "script", ok=True,
                             cost=float(getattr(res.cost, "yuan", 0) or 0),
                             elapsed=time.time() - t0,
                             detail={"shots": n_shots, "provider": sp.name})
            prog.log(self.job_id, "分镜脚本完成（%d 镜）" % n_shots,
                     stage="script", item="script")
            prog.finish_stage(self.job_id, "script", ok=True)
        return data

    def _promote_manual_ref(self, shot, img_path):
        """把人工图登记为角色定妆照。

        只在两种情况登记：
            1. 该镜头只有一个角色
            2. 该角色还没有定妆照
        """
        roles = [str(r) for r in (shot.get("characters") or []) if r]
        if not roles:
            return

        chars_dir = self.chapter_dir / "characters"
        chars_dir.mkdir(parents=True, exist_ok=True)

        for rname in roles:
            dst = chars_dir / (self._safe_name(rname) + ".png")
            if dst.exists() and dst.stat().st_size > 1024:
                continue          # 已经有定妆照，不覆盖
            if len(roles) > 1:
                continue          # 双人镜头不确定是谁，跳过
            shutil.copy2(img_path, dst)
            print("[定妆照] 用人工图登记角色「%s」" % rname)
            return

    # ---------------- 角色定妆照 ----------------
    def _char_ref_path(self, role):
        """取某角色的定妆照路径。

        查找顺序：
            1. chapter_dir/characters/<角色名>.png    （项目内自动生成）
            2. assets/characters/<角色名>.png          （全局素材库）
            3. memory 里记录的 ref_image 路径
        """
        role = str(role or "").strip()
        if not role:
            return None

        # 1. 项目内定妆照
        d = self.chapter_dir / "characters"
        for ext in (".png", ".jpg", ".jpeg"):
            p = d / (self._safe_name(role) + ext)
            if p.exists() and p.stat().st_size > 1024:
                return p

        # 2. 全局素材库
        try:
            g = Path(config.ASSETS_DIR) / "characters"
            for ext in (".png", ".jpg", ".jpeg"):
                p = g / (self._safe_name(role) + ext)
                if p.exists() and p.stat().st_size > 1024:
                    return p
        except Exception:  # noqa: BLE001
            pass

        # 3. memory 记录
        try:
            ch = self.memory.get_character(role)
            cand = str((ch or {}).get("ref_image") or "")
            if cand and Path(cand).exists():
                return Path(cand)
        except Exception:  # noqa: BLE001
            pass

        return None

    @staticmethod
    def _safe_name(name):
        bad = set('\\/:*?"<>|')
        return "".join(c if c not in bad else "_" for c in str(name))[:40]

    def _ensure_char_refs(self, script):
        """确保剧本里所有角色都有定妆照（首跑时自动生成）。"""
        if not getattr(config, "AUTO_CHAR_REF", True):
            return

        try:
            from .characters import CharacterBook

            chars_dir = self.chapter_dir / "characters"
            chars_dir.mkdir(parents=True, exist_ok=True)

            style = getattr(config, "IMAGE_STYLE_SUFFIX", "") or ""
            book = CharacterBook(self.chapter_dir, self.p("image"), style)

            roles = CharacterBook._collect_roles(script)
            missing = [r for r in roles if not book.has(r)]
            if not missing:
                return

            print("[定妆照] 缺少 %d 个角色的定妆照，开始生成：%s"
                  % (len(missing), ", ".join(missing)))

            # 临时把输出目录指到定妆照目录
            ip = self.p("image")
            old_out = (ip.config or {}).get("out_path")
            old_shot = (ip.config or {}).get("shot_index")

            try:
                for rname in missing:
                    desc = roles.get(rname, "")
                    out = chars_dir / (book._safe(rname) + ".png")
                    ip.config.update({"out_path": str(out), "shot_index": 0})
                    r = book.ensure(rname, desc)
                    print("  %s %s"
                          % ("✅" if r else "❌", rname))
            finally:
                ip.config.update({
                    "out_path": old_out or "",
                    "shot_index": old_shot if old_shot is not None else 0,
                })
        except Exception as exc:  # noqa: BLE001
            print("[定妆照] 生成失败（继续）：%s" % exc)

    # ---------------- 出图 ----------------
    def _stage_image(self, script):
        if not script:
            raise RuntimeError("没有剧本")

        # 先确保角色定妆照齐全（首跑时自动生成）
        self._ensure_char_refs(script)

        ip = self.p("image")
        if hasattr(ip, "bind"):
            ip.bind(self.chapter_dir / "manual" / "images")

        images = self.chapter_dir / "images"
        all_shots = script.get("shots") or []
        total = len(all_shots)

        # 进度追踪
        prog = self._prog()
        if prog is not None:
            prog.start_stage(self.job_id, "image", total=total, label="出图")
            # 先把所有细项登记为 pending，前端能看到全貌
            for s in all_shots:
                prog.ensure_item(self.job_id, "image",
                                 "shot_%03d" % int(s["index"]),
                                 label="第%d镜 %s" % (
                                     int(s["index"]), s.get("shot_type") or ""))

        for shot in all_shots:
            idx = int(shot["index"])
            out = images / ("shot_%02d.png" % idx)
            key = "image_%02d" % idx
            item_key = "shot_%03d" % idx

            # 已完成且有效 → 跳过
            if self.state.is_done(key) and out.exists():
                ok, _ = integrity.validate_image(out)
                if ok:
                    if prog is not None:
                        prog.skip_item(self.job_id, "image", item_key,
                                       reason="已完成")
                    continue

            # 人工图优先
            manual = self._find_manual("images", idx, (".png", ".jpg"))
            if manual is not None:
                # 先复制过去再校验（校验要读目标文件）
                atomic.atomic_copy(manual, out)
                ok, why = integrity.validate_image(out)
                if not ok:
                    print("[人工] 镜 %02d 人工图无效（%s），改为自动生成"
                          % (idx, why))
                    self._todo("image", idx,
                               "人工图无效：%s" % why,
                               {"manual": str(manual), "out": str(out)})
                    try:
                        out.unlink()
                    except Exception:  # noqa: BLE001
                        pass
                    # 继续往下走，走自动生成
                else:
                    self.state.set_item(key, state.ItemStatus.OK,
                                        path=str(out), note="人工图")
                    self._record("image", "manual", Cost.zero(),
                                 shot=idx, elapsed=0, images=1)

                    # 人工图顺便登记为角色定妆照（最标准的参考）
                    try:
                        self._promote_manual_ref(shot, out)
                    except Exception:  # noqa: BLE001
                        pass

                    if prog is not None:
                        prog.skip_item(self.job_id, "image", item_key,
                                       reason="人工图")
                    continue

            # 开始出图
            if prog is not None:
                prog.start_item(self.job_id, "image", item_key,
                                label="第%d镜 %s" % (idx, shot.get("shot_type") or ""))
                prog.log(self.job_id, "出图 第%d镜 开始" % idx,
                         stage="image", item=item_key)

            seed = None
            roles = shot.get("characters") or []
            if roles:
                seed = self.memory.get_seed(roles[0])
                if seed is None:
                    ch, _ = self.memory.ensure_character(roles[0])
                    seed = ch["seed"]
                    self.memory.save()

            st = str(shot.get("shot_type") or "")
            need_head = bool(roles) and st not in ("死物", "空镜", "特写")

            from .. import image_gen
            pos, neg = image_gen.build_prompts(shot, self.memory)

            # 全局画风后缀（IMAGE_STYLE_SUFFIX）
            style = (getattr(config, "IMAGE_STYLE_SUFFIX", "") or "").strip()
            if style and style not in pos:
                pos = "%s，%s" % (pos.rstrip("，,。. "), style)

            # 场景色调统一（剧本里的 scenes 字段）
            try:
                from .scene_style import build_scene_clause
                clause = build_scene_clause(script, shot)
                if clause and clause not in pos:
                    pos = "%s，%s" % (pos.rstrip("，,。. "), clause)
            except Exception:  # noqa: BLE001
                pass

            # 剧情连贯：把上一镜的状态拼进提示词
            if self.story_hooks is not None:
                try:
                    pos, hook_warns = self.story_hooks.enrich_prompt(shot, pos)
                    if hook_warns:
                        for w in hook_warns:
                            print("[剧情] 镜 %02d: %s" % (idx, w))
                except Exception as exc:  # noqa: BLE001
                    print("[剧情] 状态继承失败（继续）：%s" % exc)

            # 角色参考图（定妆照）
            #
            # 两条路：
            #   ① 豆包/云端插件：直接把图作为 ref_image 参数传进去（走 image 字段）
            #   ② ComfyUI：上传到其输入目录，用 IP-Adapter 锁脸
            ref_used = False
            ref_image_path = None

            if roles:
                # 收集该镜头所有角色的定妆照（最多 3 张，避免提示混乱）
                cand_paths = []
                for rname in roles:
                    p = self._char_ref_path(rname)
                    if p and p not in cand_paths:
                        cand_paths.append(p)

                cand_paths = cand_paths[:3]

                if cand_paths:
                    ref_image_path = (cand_paths[0] if len(cand_paths) == 1
                                      else cand_paths)

                    # 云端插件：直接传路径（单张或多张）
                    if getattr(ip, "name", "") in ("doubao", "openai_image"):
                        ref_used = True
                    else:
                        # 本地 ComfyUI：只上传第一张
                        try:
                            from .. import image_gen as _ig
                            _ig.upload_reference(cand_paths[0], "ref.png")
                            ref_used = True
                            ref_image_path = cand_paths[0]
                        except Exception as exc:  # noqa: BLE001
                            print("[编排] 参考图上传失败（继续）：%s" % exc)
                            ref_used = False

            ip.config.update({
                "out_path": out, "shot_index": idx,
                "require_head": need_head,
                "max_retry": getattr(config, "IMAGE_MAX_RETRY", 6),
                "use_ref": ref_used,
            })

            t0 = time.time()
            try:
                res = ip.generate(pos, neg, seed,
                                  config.image_gen_size(),
                                  ref_image=(ref_image_path
                                             if ref_used and ref_image_path
                                             else None),
                                  keep_seed_on_retry=bool(roles) and not ref_used)
                ok, why = integrity.validate_image(out)
                if not ok:
                    raise pbase.ProviderError("出图校验失败：%s" % why,
                                              provider=ip.name)
                self.state.set_item(key, state.ItemStatus.OK, path=str(out),
                                    seed=res.seed, attempts=res.attempts)
                self._record("image", ip.name, res.cost, shot=idx,
                             elapsed=time.time() - t0,
                             images=1, model=getattr(ip, "model_name", "") or "",
                             usage=getattr(res, "usage", None))
                self._spend("image", shot_idx=idx,
                            amount=float(getattr(res.cost, "yuan", 0) or 0))

                # 一致性检查 + 剧情状态记录
                if self.story_hooks is not None:
                    try:
                        chk = self.story_hooks.record_and_check(
                            shot, out,
                            state=self._infer_story_state(shot))
                        if chk.get("level") in ("WARN", "FAIL"):
                            print("[一致] 镜 %02d: %s" % (idx, chk["level"]))
                            for item in chk.get("consistency") or []:
                                for n in item.get("notes") or []:
                                    print("         %s" % n)
                            self._record("image", ip.name, Cost.zero(),
                                         shot=idx, status="degraded",
                                         msg="一致性 %s" % chk["level"])
                    except Exception as exc:  # noqa: BLE001
                        print("[一致] 检查失败（继续）：%s" % exc)

                # 进度：完成
                if prog is not None:
                    prog.finish_item(
                        self.job_id, "image", item_key,
                        ok=True,
                        cost=float(getattr(res.cost, "yuan", 0) or 0),
                        elapsed=time.time() - t0,
                        detail={"seed": res.seed, "attempts": res.attempts,
                                "provider": ip.name,
                                "path": str(out)})
                    prog.log(self.job_id, "出图 第%d镜 完成" % idx,
                             stage="image", item=item_key)
            except Exception as exc:  # noqa: BLE001
                self.state.set_item(key, state.ItemStatus.FAILED,
                                    error=str(exc)[:200])
                self._record("image", ip.name, Cost.unknown(0, 325), shot=idx,
                             status="failed", msg=str(exc)[:120],
                             elapsed=time.time() - t0)
                self._todo("image", idx, str(exc)[:200],
                           {"prompt": pos, "out": str(out)})
                # 失败也可能已经花钱（比如 API 返回错误但计费）
                self._spend("image", shot_idx=idx, amount=0.0)
                if prog is not None:
                    prog.finish_item(
                        self.job_id, "image", item_key,
                        ok=False, elapsed=time.time() - t0,
                        error=str(exc)[:200])
                    prog.log(self.job_id, "出图 第%d镜 失败：%s"
                             % (idx, str(exc)[:60]),
                             stage="image", item=item_key)

        if prog is not None:
            prog.finish_stage(self.job_id, "image", ok=True)

        # 一致性汇总：跑完出图后自动审核一次并写报告
        self._run_consistency_audit(script)

        # 自动修复：发现离群镜头用定妆照重做（默认关闭，AUTO_FIX_OUTLIERS=1 开）
        try:
            from .auto_fix import audit_and_fix
            fix = audit_and_fix(self, script, max_rounds=1,
                                threshold="WARN")
            if fix.get("redo"):
                print("[一致] 自动重做了 %d 个离群镜头" % fix["redo"])
                # 重做后再审一次，更新报告
                self._run_consistency_audit(script)
        except Exception as exc:  # noqa: BLE001
            print("[一致] 自动修复失败（继续）：%s" % exc)

        print("[编排] 出图 %d 个镜头" % total)

    # ---------------- 一致性汇总审核 ----------------
    def _run_consistency_audit(self, script):
        """出图完成后跑一次整集审核，把结果写进 consistency_report.json。

        这不是可选项：每集都要留一份质量报告，
        否则"哪几张离群"这件事每次都要重新算。
        """
        try:
            from .consistency import ConsistencyChecker

            img_dir = self.chapter_dir / "images"
            shots = []
            for s in (script.get("shots") or []):
                idx = int(s["index"])
                p = img_dir / ("shot_%02d.png" % idx)
                if p.exists():
                    shots.append({"id": "shot_%02d" % idx, "path": str(p)})

            if len(shots) < 2:
                print("[一致] 镜头不足 2 个，跳过整集审核")
                return None

            ck = ConsistencyChecker(None)
            report = ck.audit_batch(shots, mode="relative")

            rp = self.chapter_dir / "consistency_report.json"
            atomic.atomic_write_text(
                rp,
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

            st = report.stats()
            print("[一致] 整集审核完成：OK=%d WARN=%d FAIL=%d → %s"
                  % (st.get("OK", 0), st.get("WARN", 0),
                     st.get("FAIL", 0), rp.name))

            # 记到日志
            if self.on_stage is not None:
                try:
                    self._record("image", "consistency", Cost.zero(),
                                 status="degraded" if st.get("FAIL") else "ok",
                                 msg="OK=%d WARN=%d FAIL=%d"
                                     % (st.get("OK", 0), st.get("WARN", 0),
                                        st.get("FAIL", 0)))
                except Exception:  # noqa: BLE001
                    pass
            return report
        except Exception as exc:  # noqa: BLE001
            print("[一致] 整集审核失败（继续）：%s" % exc)
            return None

    # ---------------- 视频 ----------------
    def _stage_video(self, script):
        if not script:
            raise RuntimeError("没有剧本")
        vp = self.p("video")
        if hasattr(vp, "bind"):
            vp.bind(self.chapter_dir / "manual" / "videos_ok",
                    self.chapter_dir / "manual_todo" / "videos.json")

        videos = self.chapter_dir / "videos"
        videos.mkdir(parents=True, exist_ok=True)
        degraded = 0

        pv0 = self._prog()
        if pv0 is not None:
            n_v = sum(1 for s in (script.get("shots") or [])
                      if (self.chapter_dir / "images"
                          / ("shot_%02d.png" % int(s["index"]))).exists())
            pv0.start_stage(self.job_id, "video", total=n_v, label="视频")
            pv0.log(self.job_id, "阶段 video 开始（共 %d 项）" % n_v)

        for shot in script.get("shots") or []:
            idx = int(shot["index"])
            img = self.chapter_dir / "images" / ("shot_%02d.png" % idx)
            if not img.exists():
                continue

            key = "video_%02d" % idx
            out = videos / ("shot_%02d.mp4" % idx)

            if self.state.status_of(key) in state.ItemStatus.DONE and out.exists():
                ok, _ = integrity.validate_video(out)
                if ok:
                    if pv0 is not None:
                        pv0.skip_item(self.job_id, "video",
                                      "shot_%03d" % idx, reason="已完成")
                    continue

            manual = self._find_manual("videos_ok", idx, (".mp4", ".mov"))
            if manual is not None:
                atomic.atomic_copy(manual, out)
                self.state.set_item(key, state.ItemStatus.OK, path=str(out),
                                    note="人工视频")
                if pv0 is not None:
                    pv0.skip_item(self.job_id, "video",
                                  "shot_%03d" % idx, reason="人工视频")
                continue

            vp.config.update({"out_path": out, "shot_index": idx})
            dur = float(shot.get("_duration") or 3.0)
            v_item = "shot_%03d" % idx
            pv = self._prog()
            if pv is not None:
                pv.start_item(self.job_id, "video", v_item,
                              label="第%d镜视频" % idx)
                pv.log(self.job_id, "视频 第%d镜 开始" % idx,
                       stage="video", item=v_item)
            t0 = time.time()
            try:
                vr = vp.generate(img, shot.get("image_prompt_en") or "",
                                 duration=min(5.0, max(2.0, dur)))
                if vr.path and Path(vr.path).exists():
                    ok, why = integrity.validate_video(out)
                    if not ok:
                        raise pbase.ProviderError("视频校验失败：%s" % why,
                                                  provider=vp.name)

                    # 一致性校验：视频有没有崩（默认开，AUTO_CHECK_VIDEO=0 关）
                    vchk = None
                    try:
                        if getattr(config, "AUTO_CHECK_VIDEO", True):
                            from .video_check import check_video
                            vchk = check_video(out, img)
                            if not vchk.get("ok"):
                                print("[视频校验] 镜 %02d 可疑：%s"
                                      % (idx, vchk.get("reason", "")))
                                self._todo("video", idx,
                                           "视频可能崩坏：%s"
                                           % vchk.get("reason", ""),
                                           {"video": str(out),
                                            "check": vchk})
                            elif vchk.get("warn"):
                                print("[视频校验] 镜 %02d 轻微漂移：%s"
                                      % (idx, vchk.get("reason", "")))
                    except Exception as exc:  # noqa: BLE001
                        print("[视频校验] 镜 %02d 检查失败（继续）：%s"
                              % (idx, exc))
                        vchk = None

                    self.state.set_item(key, state.ItemStatus.OK, path=str(out))
                    self._record("video", vp.name, vr.cost, shot=idx,
                                 elapsed=time.time() - t0,
                                 seconds=float(getattr(vr, "duration", 0) or dur),
                                 model=getattr(vp, "model_name", "") or "",
                                 usage=getattr(vr, "usage", None))
                    self._spend("video", shot_idx=idx,
                                amount=float(getattr(vr.cost, "yuan", 0) or 0))
                    if pv is not None:
                        pv.finish_item(
                            self.job_id, "video", v_item, ok=True,
                            cost=float(getattr(vr.cost, "yuan", 0) or 0),
                            elapsed=time.time() - t0,
                            detail={"provider": vp.name,
                                    "duration": getattr(vr, "duration", 0),
                                    "path": str(out)})
                        pv.log(self.job_id, "视频 第%d镜 完成" % idx,
                               stage="video", item=v_item)
                else:
                    # 降级：不生成视频，由初剪用静态图
                    degraded += 1
                    self.state.set_item(key, state.ItemStatus.DEGRADED,
                                        note="无视频，用静态图")
                    self._record("video", vp.name, vr.cost, shot=idx,
                                 status="degraded", msg="降级为静态图",
                                 elapsed=time.time() - t0)
                    if pv is not None:
                        pv.finish_item(self.job_id, "video", v_item, ok=True,
                                       elapsed=time.time() - t0,
                                       detail={"degraded": True,
                                               "reason": "用静态图"})
                        pv.log(self.job_id, "视频 第%d镜 降级为静态图" % idx,
                               stage="video", item=v_item)
                    self._todo("video", idx, "视频未生成，已降级为静态图",
                               {"image": str(img)})
            except Exception as exc:  # noqa: BLE001
                self.state.set_item(key, state.ItemStatus.DEGRADED,
                                    error=str(exc)[:200])
                self._record("video", vp.name, Cost.unknown(0, 1200), shot=idx,
                             status="failed", msg=str(exc)[:120],
                             elapsed=time.time() - t0)
                self._todo("video", idx, str(exc)[:200],
                           {"image": str(img), "prompt": shot.get("image_prompt_en")})
                if pv is not None:
                    pv.finish_item(self.job_id, "video", v_item, ok=False,
                                   elapsed=time.time() - t0,
                                   error=str(exc)[:200])
                    pv.log(self.job_id, "视频 第%d镜 失败：%s"
                           % (idx, str(exc)[:60]),
                           stage="video", item=v_item)

        if pv0 is not None:
            pv0.finish_stage(self.job_id, "video", ok=True)
        if degraded:
            print("[编排] 视频降级 %d 个（用静态图）" % degraded)

    # ---------------- 配音 ----------------
    def _stage_audio(self, script):
        if not script:
            raise RuntimeError("没有剧本")
        tp = self.p("tts")
        if hasattr(tp, "bind"):
            tp.bind(self.chapter_dir / "manual" / "audio")

        audio_dir = self.chapter_dir / "audio"
        all_files, cursor = [], 0.0
        any_new = False

        pa = self._prog()
        if pa is not None:
            n_lines = 0
            for _s in (script.get("shots") or []):
                try:
                    n_lines += len(shots_mod.shot_lines(_s))
                except Exception:  # noqa: BLE001
                    pass
            pa.start_stage(self.job_id, "audio", total=n_lines, label="配音")

        for shot in script.get("shots") or []:
            idx = int(shot["index"])
            lines = shots_mod.shot_lines(shot)
            if not lines:
                continue

            line_files, line_durs = [], []
            for k, (role, text, emo) in enumerate(lines, 1):
                out = audio_dir / ("s%02d_%02d.mp3" % (idx, k))
                key = "audio_%02d_%02d" % (idx, k)
                a_item = "s%02d_%02d" % (idx, k)

                if out.exists():
                    ok, _ = integrity.validate_audio(out)
                    if ok:
                        line_files.append(out)
                        line_durs.append(tts_mod.probe_duration(out))
                        if not self.state.is_done(key):
                            self.state.set_item(key, state.ItemStatus.OK,
                                                path=str(out), note="复用")
                        if pa is not None:
                            pa.skip_item(self.job_id, "audio", a_item,
                                         reason="复用")
                        continue

                # 人工音频优先：manual/audio/s01_01.mp3 或 s01_01_台词.mp3
                manual_audio = self._find_manual_audio(idx, k)
                if manual_audio is not None:
                    atomic.atomic_copy(manual_audio, out)
                    ok, why = integrity.validate_audio(out)
                    if ok:
                        print("[人工] 镜 %02d 第 %d 句用人工配音：%s"
                              % (idx, k, manual_audio.name))
                        line_files.append(out)
                        line_durs.append(tts_mod.probe_duration(out))
                        self.state.set_item(key, state.ItemStatus.OK,
                                            path=str(out), note="人工音频")
                        self._record("audio", "manual", Cost.zero(),
                                     shot=idx, elapsed=0,
                                     chars=len(text or ""))
                        if pa is not None:
                            pa.skip_item(self.job_id, "audio", a_item,
                                         reason="人工音频")
                        continue
                    else:
                        print("[人工] 镜 %02d 第 %d 句人工配音无效（%s），"
                              "改为自动生成" % (idx, k, why))
                        self._todo("audio", idx,
                                   "人工配音无效：%s" % why,
                                   {"manual": str(manual_audio)})
                        try:
                            out.unlink()
                        except Exception:  # noqa: BLE001
                            pass

                if pa is not None:
                    pa.start_item(self.job_id, "audio", a_item,
                                  label="第%d镜 第%d句" % (idx, k))
                    pa.log(self.job_id, "配音 第%d镜 第%d句 开始" % (idx, k),
                           stage="audio", item=a_item)

                if role and role != "旁白":
                    voice = tts_mod.voice_for(role, self.memory,
                                              fallback_role="旁白")
                else:
                    voice = tts_mod.voice_for("旁白", self.memory,
                                              fallback_role="旁白")
                rate, pitch = tts_mod.emotion_params(emo)

                tp.config.update({"out_path": out})
                t0 = time.time()
                try:
                    res = tp.synthesize(text, voice, rate=rate, pitch=pitch)
                    # 一律以文件实测时长为准：插件报的可能是估算值，
                    # 两次运行用不同来源会让 script.json 反复变化
                    d = tts_mod.probe_duration(out) or res.duration
                    line_files.append(out)
                    line_durs.append(d)
                    any_new = True
                    self.state.set_item(key, state.ItemStatus.OK, path=str(out))
                    self._record("audio", tp.name, res.cost, shot=idx,
                                 elapsed=time.time() - t0)
                    self._spend("audio", shot_idx=idx,
                                amount=float(getattr(res.cost, "yuan", 0) or 0))
                    if pa is not None:
                        pa.finish_item(
                            self.job_id, "audio", a_item, ok=True,
                            cost=float(getattr(res.cost, "yuan", 0) or 0),
                            elapsed=time.time() - t0,
                            detail={"voice": voice, "duration": round(d, 2),
                                    "text": text[:40]})
                        pa.log(self.job_id,
                               "配音 第%d镜 第%d句 完成 %.1fs" % (idx, k, d),
                               stage="audio", item=a_item)
                except Exception as exc:  # noqa: BLE001
                    self.state.set_item(key, state.ItemStatus.FAILED,
                                        error=str(exc)[:200])
                    self._todo("audio", idx, str(exc)[:200],
                               {"text": text, "out": str(out)})
                    self._record("audio", tp.name, Cost.unknown(0, 0), shot=idx,
                                 status="failed", msg=str(exc)[:120],
                                 elapsed=time.time() - t0)
                    if pa is not None:
                        pa.finish_item(self.job_id, "audio", a_item, ok=False,
                                       elapsed=time.time() - t0,
                                       error=str(exc)[:200])
                        pa.log(self.job_id, "配音 第%d镜 第%d句 失败" % (idx, k),
                               stage="audio", item=a_item)

            if not line_files:
                continue

            shot_audio = audio_dir / ("shot_%02d.mp3" % idx)
            if any_new or not shot_audio.exists():
                tts_mod.concat_audio(line_files, shot_audio)
            total = sum(line_durs)
            shot["_duration"] = total

            subs = shots_mod.build_sub_shots(shot, total,
                                             line_durations=line_durs)
            if subs:
                shot["sub_shots"] = subs

            all_files.append(shot_audio)
            cursor += total

        if not all_files:
            raise RuntimeError("没有任何可用配音")

        full = audio_dir / "full.mp3"
        if any_new or not full.exists():
            full = tts_mod.concat_audio(all_files, full) if any_new else full
        ok_full, _ = integrity.validate_audio(full) if full.exists() else (False, "")
        if not ok_full:
            full = tts_mod.concat_audio(all_files, audio_dir / "full.mp3")

        # 字幕：已有且没新配音就复用
        srt = self.chapter_dir / ("chapter_%03d.srt" % self.chapter)
        if any_new or not srt.exists():
            entries, cur = [], 0.0
            for shot in script.get("shots") or []:
                for sub in shot.get("sub_shots") or []:
                    d = float(sub.get("duration") or 0)
                    entries.append((cur, cur + d, sub.get("display") or ""))
                    cur += d
            srt = tts_mod.build_srt(entries, srt)
            n_subs = len(entries)
        else:
            n_subs = -1

        # 回写剧本（含 _duration / sub_shots）——内容没变就不写，
        # 否则 mtime 变新会让初剪误判"输入已变"而重渲染
        script_path = self.chapter_dir / "script.json"
        new_text = json.dumps(script, ensure_ascii=False, indent=2)
        old_text = ""
        if script_path.exists():
            try:
                old_text = script_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                old_text = ""
        if _normalize(old_text) != _normalize(new_text):
            atomic.atomic_write_text(script_path, new_text)

        print("[编排] 配音 %.1fs，%d 条字幕%s" % (
            cursor, n_subs if n_subs >= 0 else 0,
            "" if any_new else "（全部复用）"))
        if pa is not None:
            pa.finish_stage(self.job_id, "audio", ok=True)
            pa.log(self.job_id, "配音完成，总长 %.1f 秒" % cursor,
                   stage="audio")
        return {"audio": full, "srt": srt}

    # ---------------- BGM ----------------
    def _stage_bgm(self, script):
        pb = self._prog()
        if pb is not None:
            pb.start_stage(self.job_id, 'bgm', total=1, label='音乐')
            pb.ensure_item(self.job_id, 'bgm', 'bgm', label='背景音乐')
            pb.start_item(self.job_id, 'bgm', 'bgm', label='背景音乐')
        bp = self.p("bgm")
        if hasattr(bp, "bind"):
            bp.bind(self.chapter_dir / "manual")
        t0 = time.time()
        try:
            res = bp.build(script.get("shots") or [], self.chapter_dir)
            dt = time.time() - t0
            self._record("bgm", bp.name, res.cost, elapsed=dt)
            self._spend("bgm", shot_idx=None,
                        amount=float(getattr(res.cost, "yuan", 0) or 0))
            # 完成追踪
            if pb is not None:
                pb.finish_item(self.job_id, "bgm", "bgm", ok=True,
                               cost=float(getattr(res.cost, "yuan", 0) or 0),
                               elapsed=dt,
                               detail={"path": str(res.path) if res.path else "",
                                       "provider": bp.name})
                pb.log(self.job_id, "背景音乐完成", stage="bgm", item="bgm")
                pb.finish_stage(self.job_id, "bgm", ok=True)
            return {"bgm": res.path}
        except Exception as exc:  # noqa: BLE001
            if pb is not None:
                pb.finish_item(self.job_id, "bgm", "bgm", ok=False,
                               elapsed=time.time() - t0,
                               error=str(exc)[:200])
                pb.log(self.job_id, "背景音乐失败：%s" % str(exc)[:60],
                       stage="bgm", item="bgm")
                pb.finish_stage(self.job_id, "bgm", ok=False,
                                error=str(exc)[:200])
            self._record("bgm", bp.name, Cost.zero(), status="degraded",
                         msg=str(exc)[:120], elapsed=time.time() - t0)
            print("[编排] BGM 失败，跳过：%s" % exc)
            return {"bgm": None}

    # ---------------- 初剪 ----------------
    def _stage_edit(self, script):
        pe = self._prog()
        if pe is not None:
            pe.start_stage(self.job_id, 'edit', total=1, label='成片')
            pe.ensure_item(self.job_id, 'edit', 'edit', label='剪辑合成')
            pe.start_item(self.job_id, 'edit', 'edit', label='剪辑合成')
            pe.log(self.job_id, '开始剪辑合成', stage='edit', item='edit')
        ep = self.p("edit")
        audio = self.chapter_dir / "audio" / "full.mp3"
        srt = self.chapter_dir / ("chapter_%03d.srt" % self.chapter)
        bgm = self.chapter_dir / "audio" / "bgm_track.mp3"
        if not bgm.exists():
            bgm = None

        out = self.chapter_dir / ("chapter_%03d.mp4" % self.chapter)

        # 复用：成片比所有输入都新，且校验通过 → 不重渲染
        if self.resume and out.exists():
            newer = self._is_newer_than(out, [audio, bgm, srt,
                                              self.chapter_dir / "script.json"])
            if newer:
                ok, _ = integrity.validate_video(out)
                if ok:
                    print("[编排] 复用已有成片（输入未变）")
                    self.result.video = out
                    self.state.set_item("edit", state.ItemStatus.OK,
                                        path=str(out), note="复用")
                    if pe is not None:
                        pe.skip_item(self.job_id, "edit", "edit",
                                     reason="复用已有成片")
                        pe.finish_stage(self.job_id, "edit", ok=True)
                    return {"video": out, "pack": self._existing_pack()}

        t0 = time.time()
        res = ep.build(self.chapter_dir, script.get("shots") or [],
                       audio if audio.exists() else None, bgm,
                       srt if srt.exists() else None)

        # 人工剪辑：只出了素材包，等人工剪完再回来
        if not res.video or not Path(res.video).exists():
            if res.pack_dir is not None:
                self.state.set_item("edit", state.ItemStatus.DEGRADED,
                                    pack=str(res.pack_dir),
                                    note="等待人工剪辑")
                self._record("edit", ep.name, res.cost, status="degraded",
                             msg="已导出素材包，等待人工剪辑",
                             elapsed=time.time() - t0)
                self._todo("edit", None,
                           "已导出素材包，等你剪好放到 manual/final.mp4",
                           {"pack": str(res.pack_dir)})
                self.result.pack = res.pack_dir
                self.result.video = None
                self.result.needs_manual = True
                print("[编排] 素材包已导出：%s" % res.pack_dir)
                print("[编排] 剪好后把成片放到 manual/final.mp4，再跑一次即可")
                return {"video": None, "pack": res.pack_dir}

            if pe is not None:
                pe.finish_item(self.job_id, "edit", "edit", ok=False,
                               elapsed=time.time() - t0,
                               error="剪辑没有产出")
                pe.finish_stage(self.job_id, "edit", ok=False,
                                error="剪辑没有产出")
            raise RuntimeError("剪辑没有产出成片，也没有素材包")

        self._record("edit", ep.name, res.cost, elapsed=time.time() - t0)
        self._spend("edit", shot_idx=None,
                    amount=float(getattr(res.cost, "yuan", 0) or 0))

        # 成片落到标准命名：优先硬链接（同卷零成本），失败才复制
        if Path(res.video) != out:
            try:
                how, p = atomic.link_or_copy(
                    res.video, out, verify=integrity.validate_video)
                if how == "copy":
                    try:
                        Path(res.video).unlink()   # 复制成功后删原件，避免双份
                    except OSError:
                        pass
            except Exception:  # noqa: BLE001
                # 硬链接失败回落普通复制
                atomic.atomic_copy(res.video, out)

        self.result.video = out
        self.result.pack = res.pack_dir
        self.state.set_item("edit", state.ItemStatus.OK, path=str(out))
        print("[编排] 成片 %s %.1fMB" % (
            out.name, out.stat().st_size / 1024 / 1024 if out.exists() else 0))

        # 完成追踪
        if pe is not None:
            pe.finish_item(self.job_id, "edit", "edit", ok=True,
                           cost=float(getattr(res.cost, "yuan", 0) or 0),
                           elapsed=time.time() - t0,
                           detail={"video": str(out),
                                   "size_mb": round(out.stat().st_size / 1024 / 1024, 2)
                                   if out.exists() else 0,
                                   "provider": ep.name})
            pe.log(self.job_id, "成片完成 %s" % out.name,
                   stage="edit", item="edit")
            pe.finish_stage(self.job_id, "edit", ok=True)

        return {"video": out, "pack": res.pack_dir}

    def _existing_pack(self):
        p = self.chapter_dir / "pack"
        return p if p.exists() else None

    # ---------------- 扩展能力：3D / 电台 ----------------
    def render_3d(self, scene, camera, spec=None, out_name="3d"):
        """渲染一个 3D 镜头（可选环节，不影响主流程）。

        Args:
            scene:   SceneAsset
            camera:  CameraPath
            spec:    RenderSpec（None 用默认）
            out_name: 输出子目录名

        Returns:
            Scene3DResult
        """
        from .extensions import RenderSpec
        from ..plugins import base as pbase

        pv = self._pick_ext("scene3d")
        if spec is None:
            spec = RenderSpec()

        out_dir = self.chapter_dir / "3d" / out_name
        out_dir.mkdir(parents=True, exist_ok=True)

        print("[3D] 场景=%s 引擎=%s 分辨率=%dx%d 时长=%.1fs"
              % (scene.name, spec.engine, spec.width, spec.height,
                 camera.duration_sec))

        t0 = time.time()
        try:
            res = pv.render(scene, camera, spec, out_dir)
        except pbase.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise pbase.ProviderError(
                "3D 渲染失败：%r" % exc, provider=getattr(pv, "name", "?"))

        elapsed = time.time() - t0
        frames = res.data.get("frames", 0) if res.data else 0
        print("[3D] 完成 %d 帧 %.1f 秒 → %s"
              % (frames, elapsed, res.frames_dir or res.video))

        self._record("scene3d", getattr(pv, "name", "?"),
                     res.cost or Cost.zero(), elapsed=elapsed)
        self._spend("scene3d", shot_idx=None,
                    amount=float(getattr(res.cost, "yuan", 0) or 0))

        return res

    def render_radio(self, script, out_name="radio"):
        """渲染一期电台（可选环节，不影响主流程）。

        Args:
            script: RadioScript

        Returns:
            RadioResult
        """
        from ..plugins import base as pbase

        pr = self._pick_ext("radio")
        out_dir = self.chapter_dir / "radio" / out_name
        out_dir.mkdir(parents=True, exist_ok=True)

        segs = len(script.segments)
        est = script.total_duration_estimate()
        print("[电台] 《%s》%d 段，预估 %.1f 秒"
              % (script.title or "未命名", segs, est))

        t0 = time.time()
        try:
            res = pr.render(script, out_dir)
        except pbase.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise pbase.ProviderError(
                "电台渲染失败：%r" % exc, provider=getattr(pr, "name", "?"))

        elapsed = time.time() - t0
        if res.audio:
            print("[电台] 完成 %.1f 秒 → %s" % (res.duration, res.audio))
            self.state.set_item("radio", state.ItemStatus.OK,
                                path=str(res.audio))
        else:
            print("[电台] 已导出清单（人工处理）→ %s" % out_dir)
            self.state.set_item("radio", state.ItemStatus.DEGRADED,
                                path=str(out_dir), note="等待人工处理")

        self._record("radio", getattr(pr, "name", "?"),
                     res.cost or Cost.zero(), elapsed=elapsed)
        self._spend("radio", shot_idx=None,
                    amount=float(getattr(res.cost, "yuan", 0) or 0))

        return res

    def _pick_ext(self, kind):
        """取扩展类插件（3d / radio），优先用已指定的，其次自动选。"""
        if kind in self._providers:
            from ..plugins import registry
            cls = registry.get_class(kind, self._providers[kind])
            if cls is None:
                raise RuntimeError("%s/%s 不存在"
                                   % (kind, self._providers[kind]))
            return cls()
        from ..plugins import registry
        registry.load_all()
        return registry.pick(kind)

    def _is_newer_than(self, target, sources):
        """target 是否比所有存在的 sources 都新。"""
        target = Path(target)
        if not target.exists():
            return False
        t = target.stat().st_mtime
        for s in sources:
            if s and Path(s).exists() and Path(s).stat().st_mtime > t:
                return False
        return True

    # ---------------- 报表 ----------------
    def _stage_report(self, script):
        lg = self._log or registry.pick("log")

        # 汇总事件
        try:
            lg.log({"stage": "summary", "status": "ok",
                    "cost_si": self.ledger.known_si,
                    "detail": "已知 %s 元，未知 %d 次" % (
                        fmt_yuan(self.ledger.known_si),
                        self.ledger.unknown_count)})
        except Exception:  # noqa: BLE001
            pass

        out = self.chapter_dir / "report.xlsx"
        rep = None
        try:
            rep = lg.emit(out)
        except Exception as exc:  # noqa: BLE001
            print("[编排] 报表失败：%s" % exc)

        # 成本明细另存 json（给程序读）
        atomic.atomic_write_text(
            self.chapter_dir / "cost.json",
            json.dumps({"summary": self.ledger.summary(),
                        "records": self.ledger.records},
                       ensure_ascii=False, indent=2))

        self.result.report = rep
        return {"report": rep}

    # ---------------- 辅助 ----------------
    def _load_script(self):
        """从磁盘读已有剧本（供非 script 阶段使用）。"""
        p = self.chapter_dir / "script.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("shots"):
                return data
        except Exception:  # noqa: BLE001
            pass
        return None

    def _find_manual(self, sub, idx, exts):
        d = self.chapter_dir / "manual" / sub
        if not d.exists():
            return None
        for ext in exts:
            for cand in (d / ("shot_%02d%s" % (idx, ext)),
                         d / ("%02d%s" % (idx, ext))):
                if cand.exists():
                    return cand
        return None

    def _find_manual_audio(self, shot_idx, line_no):
        """找人工配音文件。

        支持这些命名：
            manual/audio/s01_01.mp3
            manual/audio/s01_01_台词.mp3
            manual/audio/shot_01_01.mp3
        """
        d = self.chapter_dir / "manual" / "audio"
        if not d.exists():
            return None

        stems = [
            "s%02d_%02d" % (shot_idx, line_no),
            "shot_%02d_%02d" % (shot_idx, line_no),
        ]
        exts = (".mp3", ".wav", ".m4a", ".aac")

        # 先精确匹配
        for stem in stems:
            for ext in exts:
                p = d / (stem + ext)
                if p.exists() and p.stat().st_size > 512:
                    return p

        # 再模糊匹配（文件名以 stem 开头，后面可能带台词）
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            for stem in stems:
                if p.stem.startswith(stem) and p.stat().st_size > 512:
                    return p

        return None

    def _flush_todos(self):
        """把失败清单写进 manual_todo/。"""
        if not self._todos:
            return
        d = self.chapter_dir / "manual_todo"
        d.mkdir(parents=True, exist_ok=True)

        atomic.atomic_write_text(
            d / "todos.json",
            json.dumps(self._todos, ensure_ascii=False, indent=2))

        # 按类型分表
        for kind in ("image", "video", "audio", "memory_corrupt", "file_corrupt"):
            items = [x for x in self._todos if x["kind"] == kind]
            if not items:
                continue
            try:
                self._write_todo_xlsx(d / ("%s.xlsx" % kind), items)
            except Exception:  # noqa: BLE001
                pass

        print("[编排] 待人工处理 %d 项 → %s" % (len(self._todos), d))

    @staticmethod
    def _write_todo_xlsx(path, items):
        import openpyxl
        from openpyxl.styles import Font
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "待处理"
        headers = ["镜头", "原因", "建议操作", "创建时间"]
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            ws.cell(row=1, column=c).font = Font(bold=True)
        for it in items:
            ws.append([it.get("shot") or "-", it.get("reason", ""),
                       _suggest(it), it.get("created_at", "")])
        ws.freeze_panes = "A2"
        for col in ws.columns:
            letter = col[0].column_letter
            width = max(10, min(50, max(
                sum(2 if ord(ch) > 127 else 1 for ch in str(c.value or ""))
                for c in col) + 2))
            ws.column_dimensions[letter].width = width
        wb.save(str(path))


def _normalize(text):
    """去掉格式差异与浮点噪声，用于判断 JSON 内容是否真的变了。"""
    try:
        data = json.loads(text or "{}")
    except Exception:  # noqa: BLE001
        return str(text or "")

    def _round(obj):
        if isinstance(obj, dict):
            return {k: _round(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_round(v) for v in obj]
        if isinstance(obj, float):
            return round(obj, 3)      # 时长类浮点只到毫秒，足够
        return obj

    return json.dumps(_round(data), ensure_ascii=False, sort_keys=True)


def _suggest(item):
    kind = item.get("kind")
    if kind == "image":
        return "把新图改名为 shot_%02d.png 放进 manual/images/" % (item.get("shot") or 0)
    if kind == "video":
        return "把视频命名为 shot_%02d.mp4 放进 manual/videos_ok/" % (item.get("shot") or 0)
    if kind == "audio":
        return "把录音放进 manual/audio/"
    if kind == "memory_corrupt":
        return "检查 memory.json，或从 .bak 恢复"
    if kind == "file_corrupt":
        return "检查 _corrupt/ 里的文件"
    return ""
