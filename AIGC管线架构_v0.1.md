# AIGC 生产管线 v0.1：架构设计

> 目标：把散落的 98 个脚本重组成可交付、可演示、可复用的系统

---

## 一、现状诊断

### 已有能力（可复用）

| 模块 | 现有文件 | 能力 |
|---|---|---|
| **Workflow 构建** | `SD15完整管线.py` | ComfyUI 工作流生成 |
| **批量出图** | `SD15跑40镜.py` | 40 镜批量 |
| **完整流程** | `出图流程.py` | 312 行，端到端 |
| **后期合成** | `豆包3后期.py` | 去水印+字幕+混音 |
| **配音** | `小样配音.py` | Edge TTS |
| **定妆照** | `生成定妆照.py` | 角色参考图 |
| **剧本解析** | `分析剧本.py` | JSON 分镜 |
| **对比检查** | `拼对比图.py` | 快速 QA |

### 缺失能力（需新建）

| 模块 | 重要性 | 工作量 |
|---|---|---|
| **统一配置** | 🔴 必须 | 1 天 |
| **状态追踪**（剧情连贯） | 🔴 核心差异化 | 2 天 |
| **一致性检查** | 🔴 核心差异化 | 2 天 |
| **素材管理** | 🟡 必须 | 1 天 |
| **云端 API 接入** | 🟡 必须 | 2 天 |
| **文档** | 🔴 必须 | 1 天 |

---

## 二、目标架构

```
manju/
├── config/                     # 统一配置
│   ├── characters.yaml         # 角色定义（外观、参考图、不变特征）
│   ├── scenes.yaml             # 场景定义（固定场景库）
│   ├── styles.yaml             # 画风定义
│   └── pipeline.yaml           # 管线配置（模型、参数、成本）
│
├── data/
│   ├── scripts/                # 分镜脚本（JSON）
│   ├── state/                  # 状态追踪（每个镜头的状态快照）
│   └── assets/                 # 素材（按镜头编号归档）
│       ├── shot_001/
│       │   ├── still_v1.png
│       │   ├── still_v2.png
│       │   ├── video_v1.mp4
│       │   └── meta.json
│       └── ...
│
├── src/manju/
│   ├── core/                   # 核心模块
│   │   ├── config.py           # 配置加载
│   │   ├── state.py            # 状态机（NEW）
│   │   ├── consistency.py      # 一致性检查（NEW）
│   │   └── assets.py           # 素材管理（NEW）
│   │
│   ├── providers/              # 生成后端
│   │   ├── local_sd.py         # 本地 ComfyUI
│   │   ├── doubao.py           # 豆包 API
│   │   ├── jimeng.py           # 即梦 API
│   │   └── base.py             # 抽象接口
│   │
│   ├── pipeline/               # 管线编排
│   │   ├── stills.py           # 静帧阶段
│   │   ├── videos.py           # 视频阶段
│   │   ├── post.py             # 后期阶段
│   │   └── runner.py           # 编排器（NEW）
│   │
│   ├── tools/                  # 工具
│   │   ├── ffmpeg_utils.py     # ffmpeg 封装
│   │   ├── tts.py              # 配音
│   │   └── check.py            # QA 检查
│   │
│   └── cli.py                  # 命令行入口（NEW）
│
├── docs/                       # 文档
│   ├── architecture.md         # 架构说明
│   ├── cost.md                 # 成本核算
│   └── demo.md                 # 演示脚本
│
└── _archive/                   # 历史脚本（不删，归档）
```

---

## 三、核心新增模块设计

### 3.1 状态追踪（`core/state.py`）

**问题**：AI 不知道上一镜发生了什么，导致剧情断。

**方案**：

```python
# 每一镜生成后，记录角色状态
StateSnapshot = {
    "shot_id": "shot_011",
    "characters": {
        "林越": {
            "clothing": "jade green cross-collar robe",   # 服装
            "wounds": ["right cheek cut"],                 # 伤痕
            "dirt": ["snow on shoulders"],                # 污迹
            "emotion": "restrained",
            "pose": "standing, head lowered",
        },
        "月白": {...}
    },
    "scene": "old chinese room, oil lamp",
    "lighting": "warm amber from lamp",
    "time_of_day": "night",
}
```

**传递机制**：
```python
def build_prompt(shot, prev_state):
    """从上一镜的状态生成这一镜的提示词"""
    parts = []
    for char, state in prev_state["characters"].items():
        if char in shot["characters"]:
            parts.append(f"{char}: {state['clothing']}, "
                         f"snow on shoulders")
    # 伤痕、污迹等细节自动带入
    return ", ".join(parts)
```

**一致性规则**：
```yaml
# config/state_rules.yaml
rules:
  - name: wounds_persist
    description: 伤痕必须持续存在直到被"治愈"
    applies: [林越]
    field: wounds
    persist: true

  - name: clothing_change
    description: 换衣服需要剧情节点触发
    requires_event: ["换衣", "clean_up"]
```

---

### 3.2 一致性检查（`core/consistency.py`）

**方案**：用 OpenCV + 人脸检测比较相邻镜头的角色特征。

```python
def check_consistency(shot_a, shot_b):
    """比较两个镜头中的同一角色"""
    results = {}
    # 1. 头发颜色（HSV 直方图）
    results["hair_color"] = compare_hue(shot_a, shot_b)
    # 2. 服装主色
    results["clothing_color"] = compare_histogram(shot_a, shot_b)
    # 3. 人脸相似度
    results["face"] = compare_face(shot_a, shot_b)
    return results
```

**等级判定**：
```yaml
thresholds:
  hair_color: 0.85    # 低于此值报警
  clothing_color: 0.80
  face: 0.75
```

---

### 3.3 素材管理（`core/assets.py`）

**目录约定**：
```
assets/
  shot_001/
    stills/
      still_seed1234.png
      still_seed1234.meta.json
    videos/
      video_v1.mp4
    final.mp4
    meta.json       # 谁选的、谁审的、用的哪个
```

**meta.json**：
```json
{
  "shot_id": "shot_001",
  "generated_at": "2026-11-17T22:00:00",
  "provider": "doubao",
  "cost": {"image": 0.2, "video": 1.5},
  "selected": "stills/still_seed1234.png",
  "approved": true,
  "notes": "门关着，血痕正确"
}
```

---

### 3.4 编排器（`pipeline/runner.py`）

**把流程串成一条命令**：

```bash
# 生成某几镜的静帧
python -m manju.cli stills --episode ep01 --shots 1-5

# 从静帧生成视频
python -m manju.cli videos --episode ep01 --shots 1-5

# 生成后期
python -m manju.cli post --episode ep01 --shots 1-5

# 检查一致性
python -m manju.cli check --episode ep01 --shots 1-5
```

---

## 四、两周实施计划

### 第 1 周：框架重建

| 天 | 任务 | 产出 |
|---|---|---|
| **D1** | 建目录结构 + 归档旧脚本 | `_archive/` |
| **D2** | 统一配置（characters/scenes/styles yaml） | `config/` |
| **D3** | 抽象生成接口（providers/base.py） | 接口定义 |
| **D4** | 迁移本地 SD 到新结构 | `providers/local_sd.py` |
| **D5** | 接入豆包/即梦 API | `providers/doubao.py` |
| **D6** | 状态追踪 + 一致性检查 | `core/state.py` |
| **D7** | 编排器 + CLI | `pipeline/runner.py` |

### 第 2 周：验证 + 文档

| 天 | 任务 | 产出 |
|---|---|---|
| **D8-9** | 用新系统生成《断剑月白》第一集 | 3 分钟短片 |
| **D10** | 成本核算 | `docs/cost.md` |
| **D11** | 写架构文档 | `docs/architecture.md` |
| **D12** | 录屏演示 | `docs/demo.md` |
| **D13-14** | 整理作品集 + 简历 | 求职材料 |

---

## 五、面试话术（管线架构工程师）

**Q：介绍一下你搭的管线**

> 我搭了一套 AI 漫剧生产管线，核心解决三个问题：
> 1. **角色一致性**：用 IP-Adapter + 参考图链，把角色锁定
> 2. **剧情连贯**：设计了状态机，记录每个镜头的角色状态（服装、伤痕、光线），自动传递到下一镜
> 3. **成本控制**：对比了 6 个模型，找到了质量/成本最优组合，一集 3 分钟短片能压到 ¥150

**Q：一集成本多少？**

> 静帧 ¥0.2/张，40 镜 × 4 张挑 1 = ¥32
> 视频 ¥1/秒，12 段 × 5 秒 = ¥60
> 合计约 ¥100，加返工约 ¥150

**Q：怎么保证角色不漂移？**

> 三层保证：
> 1. 定妆照（IP-Adapter 锁定长相）
> 2. 参考图链（每一镜的参考图来自上一镜）
> 3. 自动检查（对比头发颜色、服装色、人脸相似度）

---

## 六、差异化定位

```
别人做：用 AI 工具生成内容
      → 竞争激烈，拼不过大团队

你做：设计让团队高效生产 AI 内容 的系统
      → 稀缺，难替代

核心壁垒：
· 状态追踪（解决剧情连贯）
· 一致性检查（自动 QA）
· 成本模型（最优组合）
· 多模型路由（哪个场景用哪个模型）
```

---

## 七、下一步

1. 确认架构（当前）
2. 建目录、归档旧脚本
3. 写统一配置
4. 迁移核心模块
5. 跑通第一部作品
