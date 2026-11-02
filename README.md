# video2doc

把抖音视频变成文字文档，让 AI 读懂它。

> **这是一份给 agent 用的 skill。** 你只要说一句「装 video2doc」，agent 自己就能部署完。
>
> **它解决的问题**：看长视频容易走神，重要内容要反复拖进度条。转成文字之后就能
> 像读文档一样读、直接搜关键词、回头随时查。
>
> 它**不是**下载器、搬运工具或采集器。不支持也不建议用于批量抓取、内容二次分发、
> 商业服务。使用前请读 [NOTICE.md](NOTICE.md)。
>
> 全程本地运行——音频不上传、不调任何在线 API、不花钱。

转写用 whisper.cpp + Vulkan（支持 NVIDIA / AMD / Intel Arc，无独显走 CPU）。

---

## 一句话安装

对 agent 说：

> **安装 video2doc 这个 skill**

或者直接给它这一行：

```bash
git clone --depth 1 https://github.com/1iuchao/video2doc.git ~/.workbuddy/skills/video2doc
```

装完再说「帮我分析这条抖音 <分享链接>」，agent 会读 `SKILL.md`，自己检查环境、
缺什么装什么。**你不需要懂 Node、Python、whisper 是什么。**

其他 agent 框架同理，把这个目录放进它的 skills 路径即可。

---

## 它能做什么

发一段抖音分享文案给 agent，得到两份 markdown：

- **原文**——视频里说的话，清洗过标点和重复字，保留原意可追溯
- **分析**——讲了什么、核心要点、深度分析（结构、手法、逻辑漏洞、数据反常点）

不限于抖音：B 站、YouTube、小红书等走 yt-dlp 的站点也支持。

## 一条链接一个上下文

每处理一条视频，agent 只读这一条的内容，**不会把前面几条混进来**。既省 token
也避免串味（上一条的内容跑进这一条的分析里，出错还看不出来）。

想对比多条的时候，明确说一句「跟刚才那条比一下」就行，这时 agent 才会回溯。

觉得慢或者觉得贵，最有效的办法就是这条规则：连着发链接但不要让 agent 回顾前面，
或者干脆新开一个对话。

---

## 手动安装（不想用 agent 的话）

```bash
git clone --depth 1 https://github.com/1iuchao/video2doc.git
cd video2doc
python setup.py --check      # 看缺什么
python setup.py --install    # 自动下载引擎和模型
python setup.py --verify     # 真跑一次转写确认真能用
```

---

## 依赖

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | **Windows 10/11** | 目前只支持 Windows，因为抓取依赖 Edge 的 CDP 接口 |
| Python | 3.9+ | 只用标准库，无需 pip install |
| Node.js | 22+ | 抖音抓取器需要，[下载](https://nodejs.org/) |
| Microsoft Edge | 任意新版 | Windows 自带，无需额外安装 |
| 转写引擎 | 自动下载 | 约 178MB，见下方 Release |
| 语音模型 | 自动下载 | small 约 490MB / medium 约 1.5GB |

> **Mac / Linux 用户**：抓取部分依赖 Edge 的 CDP，改造成本较高，暂不支持。

---

## 为什么模型要单独下

GitHub 限制单文件 2GB，仓库里放不下模型。所以引擎和模型放在 [Releases](../../releases) 里：

| 资产 | 大小 | 说明 |
|---|---|---|
| `engine-win64.zip` | ~200MB | vibe-server.exe + ffmpeg.exe + yt-dlp.exe |
| `ggml-small.bin` | 490MB | 默认模型，速度快，精度够用 |
| `ggml-medium.bin` | 1.5GB | 精度更高，CPU 上会慢一些 |

国内直连 GitHub 慢的话，`setup.py` 会自动套 `gh-proxy.com` 加速（实测 2MB/s 左右）。

**如果你手上已经装了 Vibe**（一个开源的语音转写软件），不用下载，直接让它接管：

```bash
python setup.py --from "C:\Program Files\Vibe"
```

---

## 技术实现

```
分享文案
  ↓  正则提取链接 + 跟随短链跳转
作品 id
  ↓  headless Edge + CDP 拦 aweme/detail 接口
play_addr（带签名的播放地址）
  ↓  urllib 带 UA + Referer 直接下
mp4 →  ffmpeg 抽 16kHz 单声道 wav
  ↓  vibe-server.exe (whisper.cpp + Vulkan)
转录稿
  ↓  agent 读、清洗、分析
原文.md + 分析.md
```

### 为什么不用 yt-dlp 抓抖音

试过，不行。抖音的 `aweme/detail` 接口要 `a_bogus` + `msToken` 签名，
yt-dlp 算不出来，稳定报 403。而**浏览器自己会算签名**，所以改成开一个真实浏览器，
让它去请求，我们截胡它的返回数据。

完整踩坑记录在 `SKILL.md` 的「技术路线」一节，一共 6 条死路，写明了每条为什么失败。
如果你打算自己改，**先看那一节**，能省几个小时。

---

## 目录结构

```
video2doc/
├─ SKILL.md              给 agent 的操作规范（核心）
├─ 使用教程.md            给最终用户看的说明
├─ NOTICE.md             使用边界与免责（请读）
├─ README.md             本文件
├─ LICENSE               MIT
├─ setup.py              环境自举：检测 / 下载 / 拷贝 / 验证
├─ pipeline.py           主流程：链接 → 下载 → 抽音 → 转写
├─ gpu.py                显卡探测与设备选择
├─ workspace.py          并发心跳 / 去重 / 命名 / 索引 / 频率护栏
├─ douyin.py             抖音解析与下载
├─ paths.py              路径解析（全部相对，无硬编码）
├─ config.json           配置模板
├─ tools/
│  └─ douyin_detail.js   headless Edge + CDP 抓取器
└─ engine/               引擎和模型（运行时生成，git 忽略）
```

运行时数据落在**当前工作目录**，不在 skill 目录：

```
{你的工作目录}/
├─ 视频分析数据/   转录稿、meta、视频缓存、索引.csv
└─ aaa输出/        最终交付的两份 markdown
```

这样一套 skill 装在全局，可以在任意项目目录下用，数据互不干扰。

---

## 配置

改 `config.json`：

| 键 | 默认 | 说明 |
|---|---|---|
| `gpu_device` | `auto` | `auto` 自动选第一块显卡，没显卡自动走 CPU；也可以填 `0`/`1` 指定，或 `-1` 强制 CPU |
| `language` | `zh` | 转写语言，`auto` 为自动检测 |
| `model` | `models/ggml-small.bin` | 相对 `engine/` 的路径 |
| `data_dir` | `视频分析数据` | 相对当前工作目录 |
| `output_dir` | `aaa输出` | 相对当前工作目录 |
| `keep_video_hours` | `24` | 视频缓存保留时长 |
| `release_base` | 占位 | 改成你自己的 Release 地址 |

## 显卡

**不挑品牌，自动检测，不需要 CUDA。**

底层是 whisper.cpp + ggml，加速走 Vulkan，NVIDIA / AMD / Intel Arc 独显都能用，
大部分核显也能用。启动时自动探测，有就用，没有就退 CPU，不会报错。

```bash
python gpu.py     # 查看检测到的设备和实际会用的那一块
```

多卡环境默认取第一块。显卡转写失败时会自动用 CPU 重试一次，不会让任务直接失败。

---

## 已知限制

- **必须有人触发**。没有常驻进程，agent 不开就不跑。
- **分析质量会波动**。不同模型、同一模型不同次，侧重点可能不同。原文是稳定的，分析不是。
- **抖音随时可能改接口**。签名算法或接口结构变了，`tools/douyin_detail.js` 就需要更新。
- **只支持 Windows**。
- **无 GPU 时慢 8 倍左右**。

---

## 使用边界

完整说明见 **[NOTICE.md](NOTICE.md)**，这里只列最关键的几条。

**适合**
- 把自己会看的视频转成文字，方便阅读、检索、做笔记
- 对公开内容做个人研究、学习、评论

**不适合（也不支持）**
- 批量抓取、定时任务、无人值守循环、多账号并发
- 任何形式的收费服务、代做、二次销售
- 把转录文字或视频发到其他平台（信息网络传播权属于原作者）
- 往项目里塞登录 cookie、接代理池、规避平台风控

**代码里没有技术限制**：不设频率上限、不做批量拦截、不加隐藏门槛。作者的立场
写在 [NOTICE.md](NOTICE.md) 里，但不通过代码强加给使用者——你自己判断，自己负责。

同样，工具也不内置登录态、不提供绕过风控的手段。「无登录、单条处理」是当前实现
的自然形态，不是刻意设的障碍。

**关于技术实现**：程序启动真实 Edge 访问公开页面，读取浏览器自己算好签名的返回值，
没有破解加密，也没有获取非公开数据。但它确实绕过了接口的签名校验环节，
这与平台《用户服务协议》存在冲突，不粉饰。

**关于对作者的影响**：下载不影响播放量统计，但大量使用会间接分流注意力，
影响作者的推荐权重和收益。这是这类工具最实质的伦理问题，使用者应当知情。

**免责**：本项目以 MIT 开源，仅供技术学习与个人效率提升。使用者应自行确保行为
符合所在地法律及平台规则，因使用产生的后果由使用者承担。权利人如认为侵权，
请通过 Issue 联系，作者会移除相关内容或调整实现。

## 许可

MIT。模型部分遵循 [whisper.cpp](https://github.com/ggerganov/whisper.cpp) 上游的 MIT 许可，
Whisper 模型权重由 OpenAI 以 MIT 发布。

自用、二次开发都可以，**请勿用于批量爬取或商业分发**。
