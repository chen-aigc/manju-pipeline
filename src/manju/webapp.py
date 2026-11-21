# -*- coding: utf-8 -*-
"""Streamlit 面板：左边输入，中间生成，右边预览。

启动：
    cd D:\\deepseek文件区\\manju
    .venv\\Scripts\\streamlit run src/manju/webapp.py
"""
import json
import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from manju import config, image_gen, pipeline, queue_tasks, video  # noqa: E402

st.set_page_config(page_title="漫剧 AI 工厂", page_icon="🎬", layout="wide")

st.markdown("""
<style>
/* 顶部留白，避免标题被 Streamlit 工具栏遮住 */
.block-container { padding-top: 3.2rem !important; }
header[data-testid="stHeader"] { background: transparent; }
.big-title { font-size: 26px; font-weight: 700; color: #1e3c72;
             margin: 0 0 2px 0; line-height: 1.4; }
.sub { color: #888; font-size: 13px; margin-bottom: 18px; }
.stage-box { background: #f0f5ff; border-radius: 10px; padding: 12px 16px; }
/* 指标数值缩小一点，避免 h264_nvenc 被截断 */
[data-testid="stMetricValue"] { font-size: 18px !important; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="big-title">🎬 漫剧 AI 工厂</div>', unsafe_allow_html=True)
st.markdown('<div class="sub">小说 → 分镜 → 配音 → 画面 → 成片</div>',
            unsafe_allow_html=True)

# ------------------------------------------------------------------ 环境横幅
c1, c2, c3, c4 = st.columns(4)
c1.metric("大模型", config.LLM_MODEL if config.LLM_API_KEY else "未配置")
c2.metric("ComfyUI", "在线" if image_gen.is_alive() else "离线")
c3.metric("编码器", video.best_encoder())
c4.metric("飞书", "已配置" if config.FEISHU_WEBHOOK else "未配置")

if not config.LLM_API_KEY:
    st.warning("还没配置 LLM_API_KEY，可以在 .env 里填，或者勾选下方的「使用内置示例脚本」。")

st.divider()

# ------------------------------------------------------------------ 左侧：输入
left, right = st.columns([5, 4])

with left:
    st.subheader("① 项目与内容")

    existing = []
    if config.PROJECTS_DIR.exists():
        existing = [p.name for p in config.PROJECTS_DIR.iterdir() if p.is_dir()]
    existing.sort()

    mode = st.radio("项目", ["新建项目", "已有项目"], horizontal=True,
                    index=1 if existing else 0)

    if mode == "已有项目" and existing:
        name = st.selectbox("选择项目", existing)
    else:
        name = st.text_input("项目名称", value="我的剧集")

    topic = st.text_area(
        "这一集要改编的内容（小说片段，或一句话主题）",
        height=180,
        placeholder="例：主角穿越成废柴少爷，被退婚后觉醒签到系统……",
    )

    with st.expander("角色设定（可选，影响人物一致性）"):
        st.caption("格式：角色名 = 外貌提示词。外貌写得越具体，生成的人脸越稳定。")
        chars_text = st.text_area(
            "角色", height=110,
            value="林越 = 黑发少年，青色长衫，眼神倔强\n老者 = 白发黑袍，眼神锐利",
            label_visibility="collapsed",
        )

    col_a, col_b = st.columns(2)
    shots = col_a.slider("分镜数量", 4, 15, 8)
    chapter = col_b.number_input("章节号（0=自动递增）", 0, 999, 0)

    use_mock = st.checkbox("使用内置示例脚本（不调大模型，纯测试链路）",
                           value=not bool(config.LLM_API_KEY))
    use_comfy = st.checkbox("调用 ComfyUI 生图（取消勾选则用占位图）",
                            value=image_gen.is_alive())

    st.subheader("② 开始生成")
    st.caption("生成在后台排队进行，页面可以随时刷新，不会卡住。")

    if st.button("🚀 一键生成", type="primary", use_container_width=True):
        if not name.strip():
            st.error("项目名不能为空")
        elif not topic.strip() and not use_mock:
            st.error("请填写要改编的内容")
        else:
            try:
                proj = pipeline.Project(name.strip())
                parsed = []
                for line in (chars_text or "").splitlines():
                    if "=" in line:
                        k, _, v = line.partition("=")
                        parsed.append({"name": k.strip(), "appearance": v.strip()})
                for c in parsed:
                    proj.memory.ensure_character(c["name"], c["appearance"])
                proj.memory.save()

                def job(task_id, *a, **kw):
                    ch = kw.pop("chapter") or (proj.memory.data["plot"].get("chapter", 0) + 1)
                    queue_tasks.stage(task_id, "生成脚本", 10)
                    if kw.pop("mock"):
                        from manju.cli import mock_script
                        mock_script(proj, ch)
                    else:
                        pipeline.step_script(proj, kw.pop("text"), ch, kw.pop("shots"))

                    queue_tasks.stage(task_id, "合成配音", 35)
                    pipeline.step_audio(proj, ch)

                    queue_tasks.stage(task_id, "生成画面", 55)
                    pipeline.step_images(proj, ch, use_comfy=kw.pop("comfy"))

                    queue_tasks.stage(task_id, "渲染成片", 80)
                    return pipeline.step_render(proj, ch)

                tid = queue_tasks.submit(
                    name.strip(), job,
                    text=topic, chapter=int(chapter) or None,
                    shots=int(shots), mock=use_mock, comfy=use_comfy,
                )
                st.session_state["tid"] = tid
                st.success("任务已提交：%s" % tid)
            except Exception as exc:  # noqa: BLE001
                st.error("提交失败：%s" % exc)

# ------------------------------------------------------------------ 右侧：进度 + 预览
with right:
    st.subheader("③ 进度")

    tid = st.session_state.get("tid")
    if tid:
        placeholder = st.empty()
        for _ in range(1):
            t = queue_tasks.get(tid) or {}
            with placeholder.container():
                st.progress(int(t.get("progress") or 0),
                            text="%s · %s" % (t.get("status", "-"), t.get("stage", "-")))
            if t.get("status") in ("queued", "running"):
                auto = st.checkbox("自动刷新", value=True, key="autoref")
                if auto:
                    time.sleep(2)
                    st.rerun()
    else:
        st.info("还没有任务。点左边「一键生成」开始。")

    st.subheader("④ 成片预览")
    vids = []
    if name and (config.PROJECTS_DIR / name).exists():
        vids = sorted((config.PROJECTS_DIR / name).glob("chapter_*/chapter_*.mp4"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if vids:
        pick = st.selectbox("选择成片", vids, format_func=lambda p: p.name)
        if pick.stat().st_size < 100 * 1024 * 1024:
            st.video(str(pick))
        st.caption("路径：%s ｜ %.1f MB" % (pick, pick.stat().st_size / 1024 / 1024))
    else:
        st.caption("暂无成片")

# ------------------------------------------------------------------ 底部：任务历史
st.divider()
st.subheader("任务历史")
tasks = queue_tasks.list_tasks(name if name else None, limit=20)
if tasks:
    rows = [{
        "任务ID": t.get("id"),
        "状态": t.get("status"),
        "阶段": t.get("stage"),
        "进度": "%s%%" % t.get("progress", 0),
        "创建时间": t.get("created_at"),
        "结果": (t.get("result") or "").split("\\")[-1] or (t.get("error") or "")[:60],
    } for t in tasks]
    st.dataframe(rows, use_container_width=True, hide_index=True)
else:
    st.caption("暂无记录")
