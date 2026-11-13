---
name: bilibili-persona
description: Turn a bilibili/B站 course video or BV id into a course knowledge base plus an optional teacher persona skill. Use when the user gives a BV id / bilibili link for a course, lecture series or 网课 and wants to learn from it, summarize it into notes, build a 知识库, or 蒸馏 the teacher into a persona skill. Also handles course materials (课件/讲义/PDF/PPT) sitting in the workspace.
when_to_use: BV号, bilibili.com/video, 网课, 课程, 分P, 系列讲座, 把这个老师做成skill, 模仿这个老师讲课, 课件, 讲义, 学习资料
license: GPL-3.0
---

# B 站网课 → 知识库 + 讲师人格

把一门 B 站课程变成**两样分开的东西**，因为需求本来就分两种：

| 产物 | 给谁用 | 默认 |
|---|---|---|
| **知识库** `skills/<课程名>/` | 只想学这门课知识的人 | 生成并**默认启用** |
| **讲师人格** `personas/<讲师名>/` | 要按这个老师的口吻讲/答/复习的人 | 生成但**默认不启用** |

分开的原因：多数人只需要知识；需要"老师"的人自己开。`personas/` 不是 skills 目录，
宿主不会自动加载它——用户想用就拷进 `skills/`（SKILL.md 末尾给命令）。

**角色分工**：yt-dlp 负责下载，`scripts/transcribe.py` 负责把音频变文字，
`scripts/materials.py` 负责读课件，**蒸馏和写作是 LLM（你）的活**，没有别的运行时。

## 阶段 0 · 环境自检（只报告，不擅自安装）

```bash
yt-dlp --version                     # 必需
python -c "import faster_whisper"    # 只有走到本地转写才需要
python -c "import pypdf"             # 只有读 PDF 课件才需要
ffmpeg -version                      # 云 ASR 切分长音频时需要
```

缺什么就**给命令让用户自己装**，不要静默安装。**信任边界**贯穿全程：不静默装依赖、
不静默上传文件、不静默改用户机器上的任何东西。

## 阶段 1 · 列分P 并强制确认范围

`yt-dlp --flat-playlist` 对 B 站只给序号、**拿不到标题**（实测），而且它的元数据来自**视频网页**，
那个端点会被 B 站风控（见下）。用我们的守卫脚本一次拿全：

```bash
python "<插件根>/scripts/bili_fetch.py" list <BV号或链接>
#   p03  01:28  27236959711  03讲 一维问题：薛定谔方程
```

它走 `x/web-interface/view` 公开接口。需要自己解析时，字段对应关系是
`data.title / data.owner.name / data.videos / data.pages[] -> { page, part, duration, cid }`。

用表格把**全部分P**呈现给用户，然后**停下来问：全部读，还是只读指定的几个？** 不要替他决定。
同时把代价讲清楚：

- 转写本地免费，但**蒸馏要烧 token，成本随时长线性增长**。
- 换算：1 小时中文讲课 ≈ 1.2–1.5 万字 ≈ 1 万 token 量级。**超过 5 小时就要分层做**。
- 建议先跑 2–3 个分P 看质量，再决定全量。

### ⚠️ HTTP 412 是常态，先知道怎么应对

B 站会对**视频网页**端点返回 `412 Precondition Failed`，实测同一时刻**公开 JSON 接口完全正常**。
所以：

| 情况 | 用什么 |
|---|---|
| 拿分P/元数据/音频/字幕 | `scripts/bili_fetch.py`（走公开接口，公开视频不需要 cookie） |
| 它报 412 | 说明 IP 被限流了，**等几分钟**，并把 `--delay` 调大（默认 1 秒） |
| 需要登录态的内容 | `--cookie "SESSDATA=..."`，**绝不写进仓库或贴进对话** |
| yt-dlp 报 412 | 同一个原因。别急着上 cookie，先试 `bili_fetch.py` |

## 阶段 2 · 拿文字（三级降级）

### 2a. 先查有没有原生字幕（免费、秒级，优先）

```bash
python "<插件根>/scripts/bili_fetch.py" subs <BV号> -I 1-3 -o transcripts
```

它查 `x/player/v2` 的 `data.subtitle.subtitles[]` 并直接把字幕写成和转写**同格式**的
`.md`/`.txt`（带时间戳），所以下游完全不用区分来源。

- `subtitles[]` 是**空数组** → 这个视频根本没有字幕，**别再折腾 cookie**。实测很多课程连
  AI 字幕都没有，这跟登录无关。此时直接进 2b。
- 注意：匿名响应里**没有** `need_login_subtitle` 字段，别拿它当判据。
- 需要登录态时（`subtitles[]` 非空但取不到、或取到空轨道），才去解决 cookie。用 yt-dlp 时有三种坑：
  1. `Could not copy Chrome cookie database` → 浏览器在运行锁住了库，**完全退出浏览器**再试
  2. `Failed to decrypt with DPAPI` → Chrome 127+ 的应用绑定加密，换 edge 或导出 `cookies.txt`
  3. firefox 找不到 cookies 库 → 那个 profile 没用过，换浏览器
- **绝不把 cookie / cookies.txt 写进仓库或贴进对话。**

### 2b. 云 ASR（无 GPU 用户的主力）

```bash
export ASR_API_KEY=...                                  # 必需
export ASR_BASE_URL=https://api.siliconflow.cn/v1        # 任何 OpenAI 兼容端点
export ASR_MODEL=FunAudioLLM/SenseVoiceSmall
python "<插件根>/scripts/transcribe.py" downloads/ -o transcripts --engine cloud
```

**上传前必须告诉用户**：这一步会把音频传到该服务商；不想上传就用 `--engine local`。
（硅基流动有免费额度；任何 OpenAI 兼容端点都能插，包括你自己搭的服务。）
脚本会自动用 ffmpeg 切分长音频（多数服务限制 ~25MB，90 分钟课程约 50MB）并合并结果。

### 2c. 本地 ASR（默认，隐私最好）

```bash
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \
  python "<插件根>/scripts/transcribe.py" downloads/ -o transcripts
```

- GPU 默认 `large-v3-turbo`，CPU 自动退回 `small`；`--check` 可先自检。
- **实测速度：13.6 倍实时**（RTX 4060 Laptop，int8_float16），即 1 小时课约 4.5 分钟。
  CPU 慢一个数量级——**先报预估耗时再决定跑不跑**。
- 国内首次必踩：HuggingFace 连不上。镜像 + 关 Xet 两件事都要做（脚本会在报错里提示）。
  模型 1.6G，建议 `HF_HOME` 指到大盘。
- 产出 `transcripts/<名>.md`（带时间戳，用来引用）和 `.txt`（纯文本，用来读）。
  已完成的自动跳过，中断重跑即续上。**开跑前先确认目录非空也可以秒退**（不需要加载模型）。

### 2d. 术语纠错（核心创新，也是本插件相对「换大模型」的胜负手）

专业课 ASR 的错误**不是随机的，是同音替换**：斜阵子/邪真子/协真子 都是「谐振子」，
复利液变换 是「傅里叶变换」。正确写法常常一次都没出现，而错法各有两三处。
这决定了它可以被机械修正——实测同一门课，词表覆盖到位后术语命中从 71% 到 100%，
超过了 4.7GB 的 Qwen3-ASR-1.7B（94%），而成本是零字节、零耗时。

三步：

1. **自动挖候选**（`scripts/glossary.py`，无需 API）：

   ```bash
   python "<插件根>/scripts/glossary.py" transcripts/        -o 术语表.auto.json --reference <知识库>/references/课件笔记
   ```

   两个信号：**同音**（拼音相同、字不同，`pypinyin`）和**差一字**（非同类错误，仅 4 字以上）。
   配对对象是「转录里出现 ≤2 次的罕见词」×「课件里的词 or 转录里高频的词」。
   三道闸压噪声：错法不得在课件里出现过（出现=真词）、两侧必须是极大 n-gram
   （扩一字频率暴涨=跨词窗口）、候选不得含虚词（的了是在…）。

2. **裁决**（可选，`--llm`）：确定性挖掘只知道频率和读音，不知道「常温是真词、
   搅动量必须是角动量」。让对话模型逐条裁决 keep/reject/fix（OpenAI 兼容端点，
   环境变量 `GLOSSARY_API_KEY`/`GLOSSARY_BASE_URL`/`GLOSSARY_MODEL`）。
   **用了 `--llm` 的话，词表里只会有裁决过的条目**，未裁决的候选直接丢弃
   （想多审就调大 `--llm-top`）。

3. **人工审 + dry-run**：脚本同时写 `术语表.auto.md` 证据表（哪条、依据什么、各出现几次）。
   删掉不同意的条目，然后：

   ```bash
   python "<插件根>/scripts/transcribe.py" --apply-glossary 术语表.auto.json transcripts/ --dry-run
   python "<插件根>/scripts/transcribe.py" --apply-glossary 术语表.auto.json transcripts/
   ```

   dry-run 报每条命中数；命中超 200 会告警（合法高频词如 ψ 会到几十次，阈值设得高）；
   单字键默认拒绝（中文里单字替换必然误伤）。

**没有课件也能跑**（只靠高频同形聚类），但召回会低——课件是正确写法的最强来源。

## 阶段 3 · 读课件（补上"不读视频画面"的缺口）

课程通常配课件/讲义，那里面有板书、公式和 PPT 原文——**这正是我们不抽帧所丢掉的**。
先自动发现，再解析：

```bash
python "<插件根>/scripts/materials.py"                    # 自动找 课件/讲义/materials/ 等目录
python "<插件根>/scripts/materials.py" 课件/ -o skills/<课程>/references/课件笔记
```

- MD/TXT/PPTX/DOCX/文本型 PDF **全部本地解析，不联网**（opus 只多需要一个 `pypdf`）。
- 产出带锚点的 `第N页` / `第N张` 笔记 + `_manifest.json`（含讲次号，供阶段 7 质检）。
- **扫描件**本地读不了。检测到就**先问用户**要不要用云 OCR：

```bash
export MINERU_API_TOKEN=...        # mineru.net 免费申请
python "<插件根>/scripts/materials.py" 扫描件.pdf --cloud-ocr
```

  这一步会把文件上传到 mineru.net，**必须用户明确同意**，不要自己加 `--cloud-ocr`。
- 课件是**额外参考**，不是必需；没有就跳过，并在最终报告里说明「无课件，公式细节仅来自口述」。

## 阶段 4 · 上游结构化 → 知识库（先结构，再写作）

**这一步决定了"减废话"能不能成立。** 先让 LLM 把每讲压成结构化条目，口语垃圾在最前面就被挡住，
知识库拿到的是骨架，人格层只从骨架 + 精选原话生成。**不要先生成啰嗦笔记再去删。**

每个分P 一份 `references/讲义/NN-<讲名>.md`，固定用这几栏（按需增删，不要硬凑）：

```markdown
## 概念
- <术语>：<定义>（`mm:ss`）
## 公式
- <公式>：<适用条件 / 单位 / 符号含义>（`mm:ss`）
## 推导
- <步骤骨架，不是逐句转录>
## 例题
- <题目 + 他怎么解的>（`mm:ss`）
## 易混与坑
- <常见错误 / 他反复纠正的点>（`mm:ss`）
## 考点
- <他明确说"必考/要记"的内容>（`mm:ss`）
```

同时产出：

- `references/大纲.md`：全部讲次 + 每讲核心（未读的讲次要明确标「未蒸馏」）
- `references/术语表.md`：术语 + 正确写法 + 勘误对照（阶段 2d 的词表落成人类可读版）
- `references/课件笔记/`：阶段 3 的产出，或指向它的链接
- `SKILL.md`（知识库的）：触发词 + 大纲索引 + **怎么检索**（哪个问题去哪个文件找）

**知识库的三条纪律**：条目化不逐句转录；每条尽量带时间点或课页锚点；
没有依据的内容标出来，别和讲过的内容混在一起。

## 阶段 5 · 蒸馏人格（可选产物，默认不启用）

人格**分两层写**，缺一层都会退化——只有风格层就是个模仿秀，只有判断层就是个教科书。

### 第 1 层：判断与教学法（他"怎么教"）

从阶段 4 的条目里提炼，而不是从原始口语里捞：

- **先看什么**：他遇到问题先抓哪个量／哪种图景
- **怎么框定**：他习惯把新问题归到哪个已讲过的框架（他常做经典↔量子对比）
- **怎么判断**：他给出"什么情况用哪个方法"的规则；以及不适用条件
- **怎么纠错**：他纠正学生的方式（点名反问？先讲错的再讲对的？重述规则？）
- **什么时候说不行**：他明确划的边界、他拒绝的提法（如「不许再讲轨道」）
- **考点观**：他怎么判定什么必考

### 第 2 层：语言风格（他"怎么说"）

只保留**有辨识度的**：高频口头禅的**原句**、自称与称呼方式、提问与互动的习惯、
类比取材的来源（他爱用哪类生活/历史例子）、幽默与敲打的方式。

### 减废话约束（硬要求）

口语里大量填充语没有信息量，**不要往人格里搬**：

- 丢掉：`这个这个`、`啊`、无意义重复、纯语气词、被截断的半句
- 保留：体现**他怎么想/怎么教/怎么与"标准讲法"不同**的口头禅与动作
- 判据：删掉这条，读者对这个老师的认识会不会变少？不会就删。

### 溯源与置信（借 lineage-skill 的机制）

人格里每一条都要能引回原话，标好来源强度：

| 标记 | 含义 |
|---|---|
| `direct_source` | 原话里直接有（附时间点/课页） |
| `source_grounded_synthesis` | 由原话归纳，未超出原文范围 |
| `mentor_inference` | 你的推断，他本人没说 |
| `external_general_knowledge` | 来自课程之外的通识 |
| `unsupported` | 没有依据——**不要写进人格** |

规则：`mentor_inference` 必须显式标注；不是 `direct_source` 的不要标成高置信；
同一处有矛盾的说法就并列保留，不要编一个统一结论。

### 产出

```
personas/<讲师名>/
├── SKILL.md            # 两层人格 + 回答规则 + 边界声明
└── references/
    ├── 风格证据.md       # 每条风格特征 → 原话 + 时间点（供用户核查）
    └── 大纲索引.md       # 指向知识库的对应内容
```

`SKILL.md` 的 `description` 里要塞进触发词（老师名、课程名、学科，前 250 字符最关键）。
**回答规则**要写明：用他的口吻和术语答；引用出处到「第几讲 + 时间点」；
**超出已蒸馏范围的问题明说「这部分我还没读到」，不许编**；区分原话（引号）与你的归纳。

**讲师名**取标题里的讲师名或 UP 主名，两者都不像人名时**问用户**。目录名用小写 kebab-case。

### 可选：叠加大师人格（用户决定）

`personas/` 下可以放多个（自己蒸的讲师、别人的大师人格），用户加载哪个用哪个。
也可以在生成时**叠加一个方法论约束层**：把 `personas/_methods/` 里选一个（或用户自己写的）
附加到人格里，得到「讲师 A + 教学法 B」。默认不叠加；**不打包任何第三方人格内容**。

## 阶段 6 · 可选产出（用户明确要求才做）

- **章节速览**：`references/讲义-速览.md`，每讲 3–5 条最该记住的，一页纸复习用。

思维导图之类**刻意不做**——市面工具很多、各有擅长，用户需要自己接一个就行，
本项目不为此背依赖。

## 阶段 7 · 交付自检（一个报告文件，不是一堆脚本）

产出 `交付报告.md`，逐项回答（维度借 lineage-skill 的蒸馏审计）：

- **来源覆盖**：每个确认要读的分P 是否都有转录 + 讲义？课件是否都有对应文件？
- **课件未匹配**：`_manifest.json` 里有讲次号、但知识库里没人引用 → 列出来
- **证据缺口**：哪些结论只有 `mentor_inference`、没有原话支撑
- **人格保真**：`风格证据.md` 里每条样式特征是否都能被时间点验证
- **空/过短产物**：有没有生成了但基本没内容的文件
- **越界声明**：未蒸馏的讲次是否在知识库和人格里都标了「未读到」
- **隐私**：有没有把音频/字幕/课件原件写进仓库（应为否；`.gitignore` 已排除）

**最后必须停下来把结论告诉用户**：覆盖了什么、缺什么、哪里的置信度低、token 花了多少。

## 不要做的事

- 不读视频画面（抽帧/OCR/视觉模型）：v1 明确不做，用课件补。用户主动要求再单独讨论。
- 不重造分块/VAD/纠错/字幕渲染：生态里已有成熟实现（WhisperX、VideoCaptioner 等），
  我们只做「知识库 + 人格」这件没人做的事。
- 不做 MCP server、不做 GUI、不做配置文件（用 CLI 参数）、不做向量库 RAG。
- 不做学习者状态跟踪（掌握度、复习队列、错误本）——那是带教系统的领域，不是本项目。
- 不绕付费墙：付费课程需要用户自己已购买并登录，不碰 DRM、不批量爬取。
- 不重新分发原视频/音频/字幕/课件文件。
- 不静默安装依赖、不静默上传文件、不静默改用户浏览器或系统配置。

## 启用讲师人格（给用户的命令）

```bash
cp -r personas/<讲师名> skills/          # 拷进 skills 目录后，宿主的 skill 列表里就有了
```

## 大课程（> 5 小时）怎么做

不要跳过分P 结构化去硬塞全文——也塞不进去。固定用 map-reduce：每讲条目 → 合并。
用户只想先看效果，就只跑前 2–3 讲，知识库先出骨架，`references/` 后续按需补齐。
