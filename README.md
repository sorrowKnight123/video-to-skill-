# bilibili-persona

把一门 B 站网课变成**两样东西**：一份能查的课程知识库，和一个可选的「讲师人格」。
两个产物分开，因为需求本来就分两种——多数人只想学知识，需要「老师」的人自己开。

```
你给一个 BV 号
   ↓  列分P → 你确认读哪些 → 取字幕（没有就本地转写）→ 读课件 → 每讲结构化
skills/<课程>/       知识库：大纲、讲义、术语表、课件笔记     ← 默认启用
personas/<讲师>/     人格：他怎么讲、怎么判断、怎么押考点     ← 默认不启用，要用自己装
```

实测：在一门 24 分P 的中文网课上，4 小时 08 分音频本地转写用了 **18 分钟**（13.6 倍实时）。

## 快速开始

```bash
# 1. 依赖（yt-dlp 必需；faster-whisper 只在没字幕时需要）
uv pip install yt-dlp faster-whisper pypdf

# 2. 看看有哪些分P
python scripts/bili_fetch.py list BV1xxxxxxxxx

# 3. 下音频 + 转写（国内先设镜像，见下方「常见问题」）
python scripts/bili_fetch.py audio BV1xxxxxxxxx -I 1-5 -o downloads
python scripts/transcribe.py downloads/ -o transcripts

# 自动挖术语纠错表（有课件更好；先看证据表再落盘）
python scripts/glossary.py transcripts/ --reference <知识库>/references/课件笔记     -o 术语表.auto.json
python scripts/transcribe.py --apply-glossary 术语表.auto.json transcripts/ --dry-run
```

然后在 ZCode 里说：「帮我把这门课做成知识库 `https://www.bilibili.com/video/BV1xxxxxxxxx`」，
或者用命令 `/learn-course BV1xxxxxxxxx 1-5`。

## 安装成插件（可选）

ZCode → 设置 → 插件管理 → **发现** → `+` → 添加插件市场，粘贴：

```
https://github.com/sorrowKnight123/video-to-skill-
```

装好后 skill 默认启用。不想用市场的话，把 `skills/`、`scripts/`、`personas/_methods/`
拷进你的项目也一样用。

## 产物长什么样

```
skills/<课程名>/                     # 知识库（默认启用）
├── SKILL.md                        # 触发词 + 大纲索引 + 怎么检索
└── references/
    ├── 大纲.md                      # 全部讲次 + 每讲核心 + 考点清单
    ├── 术语表.md                    # 术语 + 读法 + ASR 勘误对照
    ├── 讲义/01-xxx.md ...           # 每讲一份：概念/公式/推导/例题/易混与坑/考点
    └── 课件笔记/                    # 课件（PDF/PPTX/DOCX）解析结果，带页码锚点

personas/<讲师名>/                    # 人格（默认不启用）
├── SKILL.md                        # 语言风格 + 判断与教学法 + 回答规则
└── references/                     # 原话证据（每条风格特征都能回溯）

transcripts/  downloads/             # 原始字幕与音频（已 gitignore，别提交）
```

讲义的六栏是刻意的：**先结构化，再写作**。口语里的填充语在最前面就被挡住，
所以知识库和人格都不会被「这个这个」「啊」这类废话污染。

## 主要能力

| 能力 | 说明 |
|---|---|
| **三级降级取文字** | 原生字幕（免费秒出）→ 云 ASR（任何 OpenAI 兼容端点）→ 本地 faster-whisper |
| **本地转写** | GPU 优先、CPU 自动兜底；`--check` 自检；断点续跑；不需要 torch |
| **术语纠错** | ASR 对专业课术语错得很系统（实测一个词能有五六种错法），用 JSON 词表确定性纠正 |
| **读课件** | PDF/PPTX/DOCX/MD/TXT 本地解析；扫描件可选走云 OCR；补上「不读视频画面」丢的公式 |
| **知识库与人格分离** | 人格生成但默认不启用；还支持叠加教学法（严格推导 / 直觉类比 / 考点导向） |
| **可溯源** | 讲义条目带 `mm:ss`，有人格的带原话证据；缺证据的地方明确标注而不是编 |

## 命令参考

| 命令 | 作用 |
|---|---|
| `bili_fetch.py list <BV>` | 列分P（序号/时长/cid/标题） |
| `bili_fetch.py audio <BV> -I 4-24 -o downloads` | 下音频（串行 + 解码校验） |
| `bili_fetch.py verify <BV> -o downloads` | 校验本地音频是否完整 |
| `bili_fetch.py subs <BV> -I 1-3 -o transcripts` | 把原生字幕存成和转写同格式的 `.md/.txt` |
| `bili_fetch.py resources <BV> -o materials` | 从简介/置顶/热评发现配套资料链接；`--fetch` 经确认后只下直链文件，网盘只给链接+提取码 |
| `transcribe.py downloads/ -o transcripts` | 转写（`--engine auto\|cloud\|local`） |
| `transcribe.py --check` | 自检设备与模型 |
| `transcribe.py --apply-glossary 词表.json transcripts/ --dry-run` | 术语纠错（先看命中再落盘） |
| `materials.py <课件目录> -o <知识库>/references/课件笔记` | 解析课件 |
| `mock_asr.py` | 本地假 ASR 服务，无密钥验证云转写链路 |

`bili_fetch.py` 还有个 `--delay`（默认 1 秒，被限流就调大）和 `--workers`（默认 1，
**别调大**，见下方常见问题）。

## 常见问题

### yt-dlp 报 HTTP 412，但接口明明是通的

B 站有两种完全不同的 412：

| 成因 | 表现 | 解法 |
|---|---|---|
| **yt-dlp 走的网页端点被风控** | `yt-dlp -f ba` 每个分P 都 412，而同一时刻 `api.bilibili.com` 的公开接口返回 code 0 | 用 `bili_fetch.py`（直连公开 API） |
| **Python 的 TLS 指纹被 WAF 拒绝** | 同一 URL、同样请求头，`urllib`/`requests` 412，**`curl` 200** | `bili_fetch.py` 会自动改用 curl 重试 |

第二种**跟登录无关，cookie 解决不了**。如果连 curl 也开始 412，那才是 IP 限流：等几分钟，
把 `--delay` 调大。

### 下载下来的音频是坏的，但文件大小看着正常

实测踩过：4 个并发下载时，21 个文件里 12 个的 AAC 帧损坏，**字节数却接近正确**；
而 `ffprobe` 也报完整时长——因为分片 DASH 的 init 段会声明完整时长，媒体数据缺了它也不知道。

所以本项目：**默认串行下载**（`--workers 1`，别调大），下载后**用 ffmpeg 解码校验**
（`ffmpeg -v error -i f -f null -`，0 条错误才算过），失败就重下而不是按字节续传
（分片容器拼接会产生「大小对、内容坏」的文件）。随时可以用 `bili_fetch.py verify` 自查。

### 第一次转写卡在下载模型

HuggingFace 在国内连不上，且它的 Xet 存储后端**没有镜像**，所以两件事都要做：

```bash
export HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1
```

```powershell
$env:HF_ENDPOINT='https://hf-mirror.com'; $env:HF_HUB_DISABLE_XET='1'
```

权重 1.6G，建议 `HF_HOME` 指到大盘。脚本在下载失败时会直接把这两行打给你。

### GPU 用不起来

Windows 上只有一件事要保证：**`cublas64_12.dll` 能被找到**。

- CTranslate2 4.5+ 的官方 Windows 轮子**默认不再链接 cuDNN**，网上「必须装 cuDNN 9」的说法
  对当前版本不成立。
- 报 `Library cublas64_12.dll is not found or cannot be loaded` 时，把 CUDA 的 `bin`
  （或 `nvidia-cublas-cu12` 的 `bin`）加进 **PATH**——CT2 用裸 `LoadLibraryA` 加载 cuBLAS，
  `os.add_dll_directory` 对它无效。
- 8G 显存用 `int8_float16`，别用 `float16`。
- 没 GPU 会自动退回 CPU（换 `small` 模型），慢一个数量级。

### 云转写报 404 或返回 HTML

说明 `ASR_BASE_URL` 指向的是**对话模型**网关，它没有 `/audio/transcriptions`。
换一个真正做语音转写的 OpenAI 兼容服务（如硅基流动 `https://api.siliconflow.cn/v1`，
模型 `FunAudioLLM/SenseVoiceSmall`），或者用 `--engine local` 在本机转。

想在没有密钥的情况下验证云链路，跑 `python scripts/mock_asr.py`，再按它输出的提示调用。

### 需要登录态的内容取不到

用 `--cookie "SESSDATA=..."`。yt-dlp 侧还有三种 cookie 坑：浏览器在运行会锁住 cookie 库
（完全退出浏览器再试）、Chrome 127+ 的应用绑定加密解不开（换 edge 或导出 `cookies.txt`）、
firefox 那个 profile 从没用过。

**cookie 属于账号凭据，不要写进仓库、不要贴进对话。**

### 找到的网盘资料为什么不能自动下载

网盘下载要过登录、验证码和风控，自动化不可靠，而且行为和爬虫难以区分。
所以 `bili_fetch.py resources` 只负责**发现**：把链接和提取码列成清单
（`materials/资料清单.md`）；你确认后加 `--fetch` 也只下载**直链文件**
（pdf/zip/压缩包这类不用登录的），网盘的请自己打开。

发现范围默认是当前视频的简介、置顶评论和热评；`--season` 可以把合集里其他分P 的
简介和置顶也扫一遍（很多 UP 主把资料包挂在合集第一集的置顶里）。分享链接随时可能
失效，脚本只负责"找到了"——打不开就是失效了，这不是脚本的错。

`materials/` 整个目录已被 gitignore：资料清单里有你的网盘链接和提取码，别提交。

## 依赖

| 依赖 | 什么时候需要 | 安装 |
|---|---|---|
| `yt-dlp` | 可选（`bili_fetch.py` 是主路径，yt-dlp 是备选） | `uv pip install yt-dlp` |
| `faster-whisper` | 本地转写（**不需要 torch**） | `uv pip install faster-whisper` |
| `pypdf` | 读 PDF 课件 | `uv pip install pypdf` |
| `ffmpeg` | 云 ASR 切分长音频、解码校验 | 系统包管理器 / gyan.dev 版 |
| `MINERU_API_TOKEN` | 扫描件课件走云 OCR 时 | mineru.net 免费申请 |

PPTX / DOCX / MD / TXT 课件**不需要任何额外依赖**（用标准库解压取文本）。

skill 只报告缺什么、给出安装命令，**不会替你静默安装**；也不会静默上传你的文件。

## 核心思路：不拼模型，拼可审计的词表

ASR 对中文专业术语的错**不是随机的，是同音替换**——谐振子被听成斜阵子/邪真子/协真子，
傅里叶变换被听成复利液变换，正确写法常常一次都没出现。这意味着它**可以被机械修正**。

实测（同一门课、同一段音频）：

| 方案 | 术语命中 | 体积 | 速度 |
|---|---|---|---|
| whisper-turbo 原始 | 71% | 1.6GB | 20× 实时 |
| **whisper-turbo + 自动词表** | **100%** | **+0GB** | **20× 实时** |
| Qwen3-ASR-1.7B | 94% | +6.5GB | 3.4× 实时 |

`glossary.py` 用两个信号挖候选：**同音**（拼音相同、字不同）和**差一字**；
配对对象是「转录里罕见的词」×「课件里的词 or 转录里高频的词」。三道闸压噪声
（错法不得在课件出现过、两侧必须是极大 n-gram、候选不含虚词），
可选再让对话模型逐条裁决。全程可审计：每条都带证据表。

## 设计取舍（明确没做的事）

- **不读视频画面**（抽帧/OCR/视觉模型）：明显更慢更贵，用**课件**补这个缺口。
- **不重造轮子**：分块/VAD/纠错/字幕渲染生态里已有成熟实现，我们只做「知识库 + 讲师人格」。
- **不做** MCP server、GUI、配置文件（用 CLI 参数）、向量库 RAG、思维导图。
- **不做**学习者状态跟踪（掌握度、复习队列、错误本）——那是带教系统的领域。
- **只做 B 站**：换平台等真有第二个需求再说。

### 为什么默认不用 Qwen3-ASR

Qwen3-ASR-1.7B 中文术语确实更准（实测同一段课程音频：术语命中 94% vs Whisper 的 71%），
但代价是**慢 5.9 倍**（3.4× 实时 vs 20×）、体积 6.5GB、时间戳要额外挂对齐模型再自己拼句，
而且 `hf_hub_download` 在国内镜像下直接不可用、`qwen-asr` 自 2026-01 停更。
术语准确率我们用词表纠错就能拿到（同一段音频里 Whisper 错的那 4 个术语，词表全修掉了），
成本近乎零。**所以它不做默认引擎**；需要极限术语准确率时再考虑做成可选引擎。

## 目录结构

```
marketplace.json                       插件市场清单
.zcode-plugin/plugin.json              插件清单
skills/bilibili-persona/SKILL.md       工作流（阶段 0–7）
commands/learn-course.md               /learn-course <BV号>
scripts/bili_fetch.py                  列分P / 下音频 / 存原生字幕
scripts/transcribe.py                  转写（云/本地）+ 术语纠错
scripts/glossary.py                    自动挖掘术语纠错词表（拼音信号，无需 API）
scripts/materials.py                   课件解析（本地优先，扫描件可走云 OCR）
scripts/mock_asr.py                    假 ASR 服务，验证云链路
personas/_methods/                     三个教学法叠加层模板
```

## 许可与免责

**GPL-3.0**（见 `LICENSE`）。复用的第三方代码归属见 `NOTICE`——衍生作品必须同样以 GPL 开源。

只做**个人学习用途**的本地文字化：不重新分发视频、音频、字幕、课件，不绕过付费墙或 DRM，
不批量爬取，需要登录态的课程请使用你自己已购买的账号。

- `downloads/`、`transcripts/`、`.cache/`、`_refs/` 已在 `.gitignore` 排除，**不要提交或上传**。
- 用本工具生成的**知识库与人格可以自由分发**；原始字幕、音视频、课件不可以。
- 请遵守 B 站用户协议与你所在地区的法律。使用本工具产生的一切后果由使用者自负。
