# LectureWeaver · 个人课堂知识与学习助手

[![CI](https://github.com/majianyu2007/lectureweaver/actions/workflows/ci.yml/badge.svg)](https://github.com/majianyu2007/lectureweaver/actions/workflows/ci.yml)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-blue)
![License](https://img.shields.io/badge/license-MIT-green)

把西农（西北农林科技大学）云课堂的**课堂实录**（三路录像、平台转写）、**教务课表**和**你自己的课程资料**整理成可追溯、可持续积累的学习资料，并让 AI（Claude Code、Codex 等）基于这些资料一步步教你、记住你学到哪里。

- 形态：本机命令行 `lw` + Skill `lecture-weaver`（`skill/lecture-weaver/`）。支持 macOS、Linux、Windows。
- 资料与学习状态保存在独立的知识库目录（默认 `~/LectureWeaver`），不依赖任何一次聊天或某个模型的记忆；可备份，可在不同电脑、不同系统之间迁移。
- 笔记自动写入你指定的笔记目录（Obsidian Vault 或任意 Markdown 文件夹），截图作为附件一起写出；你在 Obsidian 里改过的内容不会被 AI 静默覆盖。

## 安装

共同需要：Python 3.11+（推荐用 [uv](https://github.com/astral-sh/uv) 安装和管理）、ffmpeg、git。可选：[whisper.cpp](https://github.com/ggml-org/whisper.cpp)（平台没有转写时在本机识别语音）、本地 OCR。

| | macOS | Linux（以 Debian/Ubuntu 为例） | Windows |
|---|---|---|---|
| ffmpeg | `brew install ffmpeg` | `sudo apt install ffmpeg` | `winget install Gyan.FFmpeg` |
| whisper.cpp（可选） | `brew install whisper-cpp` | 按 whisper.cpp 说明用 cmake 编译 | 下载 whisper.cpp 发布页的 Windows 压缩包并解压 |
| 本地 OCR | 自带 Apple Vision（需 `xcode-select --install`） | 安装时加 `[ocr]`（RapidOCR）或装 tesseract + `chi_sim` | 安装时加 `[ocr]`（RapidOCR） |
| 登录对话框 | 系统对话框 | zenity / kdialog（桌面环境）；无桌面时在终端输入 | 系统对话框 |
| 密码存放 | 钥匙串 | Secret Service（GNOME Keyring/KWallet）；没有时为仅本人可读的文件 | Windows 凭据管理器 |

```bash
git clone https://github.com/majianyu2007/lectureweaver.git
cd lectureweaver
uv tool install -e .              # macOS
uv tool install -e ".[ocr]"       # Linux / Windows：附带本地 OCR

# 推荐的中文识别模型（约 550MB）和语音检测模型，放在 ~/.cache/whisper/（Windows 为 %USERPROFILE%\.cache\whisper\）
mkdir -p ~/.cache/whisper && cd ~/.cache/whisper
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
curl -LO https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin

lw init --notes "<你的 Obsidian Vault>/课程笔记"
# whisper-cli / ffmpeg 不在 PATH 里时：lw init --whisper-bin <路径> --ffmpeg <路径>
lw auth login --gui     # 弹出系统对话框输入学号、密码、二次验证码（无桌面时用 lw auth login 在终端输入）
lw skill install        # 安装 Skill 到 ~/.claude/skills 和 ~/.codex/skills（Windows 无链接权限时复制）
lw doctor               # 检查各项是否就绪
```

本机状态（会话 Cookie、临时文件、本机路径设置）在 macOS `~/Library/Application Support/LectureWeaver`、Linux `~/.local/state/lectureweaver`、Windows `%LOCALAPPDATA%\LectureWeaver`。密码和会话都不进入知识库、笔记、日志或备份。

## 日常使用

在 Claude Code / Codex 里直接说，例如：

- “把第七周 Java 的课堂整理好，重点看老师演示的代码，存到我的笔记目录。”
- “把数据库这门课还没整理过的课堂补齐，已经完成的不要重复处理。”
- “我还是没理解关系代数的除法。结合老师这次的讲解和之前的课程，一点点教我。”
- “把这份实验指导书关联到对应课程，帮我分清课堂讲过什么，以及实验还要求做什么。”
- “这份笔记换一种更适合复习的组织方式，但保留我自己补充的理解。”
- “备份整个知识库，换台电脑之后继续上次的学习。”

AI 第一次读取你的课堂资料前会说明发送范围并征求同意（`lw consent`）。也可以自己用命令：

```bash
lw sync                         # 同步课表与课堂实录元数据（几秒，不下载视频）
lw lectures -c java -w 7        # 看看有哪些课时、匹配是否有歧义
lw process -c java -w 7         # 转写 + 截图 + OCR + 学习包；可中断、重跑自动续上
lw status                       # 哪些完成、哪些不完整、哪些还没写笔记
lw search 除法 -c 数据库         # 某个概念/例题出自哪节课的哪个时间点
lw learn status -c 数据库        # 知识点掌握情况（带依据）
lw backup create -o ~/Desktop
```

完整命令见 `lw --help` 和 [`skill/lecture-weaver/references/cli.md`](skill/lecture-weaver/references/cli.md)。

## 它是怎么处理一节课的

| 阶段 | 做什么 | 网络/存储 |
|---|---|---|
| sync | 云课堂录像列表 + 教务课表；按教学班 ID 关联课程，按 `courseTableDetailId` 把各节录像合成课时，用校历算周次、对照课表 | 只有元数据 |
| details | 每节录像的文件信息、平台课件识别、平台 AI 摘要（标注为平台生成） | 几 KB |
| transcript | 优先用平台转写；没有时只下载**音频样本**（按 MP4 样本表取字节范围）本地 whisper 识别 | 有平台转写：0；否则每节约 21MB（整个文件约 730MB） |
| visual | 读 MP4 索引，只下载**录屏关键帧**：每 8 秒采样、按“画面”分组、保留每个画面的最终状态和长时间编辑的中间状态、去重；录屏长时间不变而老师还在讲时补拍教师机位（板书）；学生机位不使用；本地 Apple Vision OCR | 每节约 20MB（录屏文件约 380MB）；保存 WebP 截图每节约 4MB |
| packet | 生成学习包：素材情况、前后课时、关联资料、转写与截图时间线、压缩后的 OCR | 本地 |

实测（2026-10-20 Java 两节课）：三路录像合计约 3GB，实际从学校服务器传输 42MB，保存 9.9MB；不保存任何视频。无平台转写的课（2026-10-14 概率论）再加每节约 21MB 音频样本，本地识别 92 分钟音频约 6 分钟，音频识别后删除。

哪一路录像录到了老师的声音因教室和日期而异（实测 10-13 N8420 三路都几乎静音，10-20 三路都有声音），所以识别前会对三路各抽 3×15 秒测语音量，选真正有声音的一路；都静音时如实标记“无法生成转写”，不会让模型凭空编出文字。

## 笔记

AI 按 `skill/lecture-weaver/references/note-guide.md` 写草稿，`lw note save` 负责：

- 校验每个时间点都落在本课时录像内、每张截图和资料都真实存在（编造的引用会被拒绝）；
- 时间点渲染为可点击链接，在云课堂播放器中跳到该秒（已实测）；截图复制到笔记目录的 `attachments/`；自动追加“资料说明”；
- 正文 = 课堂内容；`> [!quote] 资料` = 教材/指导书；`> [!tip] AI 补充` = AI 的解释和例题；
- 每次保存都是一个版本；你在 Obsidian 的修改会记为“你的修改”。新版本缺少你补充的内容时，不覆盖原文件，而是写出 `*.lw-proposed.md` 并列出缺失内容；可 `--merge` 三方合并，`lw note history / diff / rollback` 查看和回滚。

## 学习状态

`lw learn` 记录的是证据（问了什么、你怎么答的、暴露了什么误解），掌握程度由证据推出：未接触 → 讲过（未检验）→ 不稳 → 基本会 → 掌握（不同日期两次答对且含应用题、无未解决误解）。“AI 讲过”只算“讲过”。可以手动修正，但之后的新证据优先。

## 备份与迁移

`lw backup create` 生成一个 `.tar.gz`：SQLite 快照 + 转写/截图/资料/全部笔记版本 + 校验清单（不含密码、Cookie、API Key、本机路径、临时媒体）。新电脑上：

```bash
uv tool install -e <项目目录>
lw backup restore LectureWeaver-backup-xxxx.tar.gz   # 先完整校验，原知识库会改名保留
lw init --notes "<新的笔记目录>"
lw auth login --gui
lw note export                                      # 把笔记和附件写到新笔记目录
lw learn next                                       # 从上次的学习进度继续
```

已处理的课时不会被当成新任务（处理状态在知识库里）。

## 隐私与授权

- 本地处理（语音识别、OCR、媒体）不需要授权。
- AI 工具读取学习包 = 资料发送给该工具的模型服务商：首次使用时说明并征得同意（`lw consent grant harness ...`），之后在范围内复用，随时 `lw consent revoke harness`。
- 云端语音识别（可选，OpenAI 兼容接口）需要单独授权和 API Key，并可设置每 30 天的分钟数上限；不会自动开通付费服务。
- 教师机位画面可能拍到同学背影，只在录屏长时间不变时少量截取；学生机位从不使用。

## 已知限制

- 依赖学校平台当前接口（见 [`docs/platform.md`](docs/platform.md)）；平台改版需要更新。
- 课堂转写（平台或本地）有同音错字，笔记中存疑处会标注；本地识别时间戳精度约 ±1 秒。
- 截图选择是启发式的：快速翻页（停留不到约 2 秒）的画面可能漏掉；可用 `lw process --force visual` 配合更密的采样重做。
- 老旧的 `.ppt/.doc` 需另存为新格式后导入。
- 如果当天录像没有声音（麦克风未开），只能依据截图整理，笔记会注明。
- 音频只在录屏文件里时（与视频逐帧交错），本地识别需要整段读取该文件（约 390MB，只保留音频）。
- 只支持本人账号能看到的课程；不做后台自动扫描（需要时手动 `lw sync`）。

## 开发

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # 离线测试（含用 ffmpeg 生成的合成录像）
python scripts/gen_cli_doc.py
```

- 测试在 macOS、Linux、Windows 上由 GitHub Actions 运行（`.github/workflows/ci.yml`）。

代码结构：`platform/`（统一认证、云课堂、教务）、`sync.py`（课时与课表匹配）、`mp4index.py`/`visual.py`/`audio.py`/`asr.py`/`ocr.py`（媒体）、`pipeline.py`/`packet.py`（处理与学习包）、`notes.py`（渲染、版本、冲突）、`materials.py`、`search.py`、`learning.py`、`backup.py`、`consent.py`。

## 免责声明

本项目是个人学习工具，与西北农林科技大学及相关平台厂商无关联、未获背书。它只使用你本人账号在官方网页中本就能看到的内容，请求频率与正常浏览相当；请遵守学校的相关规定，不要用于获取他人数据或传播课堂录像。课堂录像与教学内容的权利归学校和授课教师所有。

## 许可

[MIT](LICENSE)
