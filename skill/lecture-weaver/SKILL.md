---
name: lecture-weaver
description: 西农（NWAFU）个人课堂知识与学习助手。把云课堂“课堂实录”（三路录像、平台转写）、教务课表和用户自己的课程资料（PPT/PDF/Word/代码）整理成可追溯的课堂笔记，写入用户的笔记目录（Obsidian 或普通 Markdown），并基于这些资料一步步教学、记录掌握情况。当用户提到整理某节课/某周/某门课的课堂录像、补齐没整理的课、导入/关联课程资料、查某个概念出自哪节课、让你讲懂某个知识点或出题检测、改写笔记风格、备份或迁移学习资料时使用。
---

# LectureWeaver

所有能力都通过本机命令 `lw` 完成（macOS / Linux / Windows 均可）。知识库、笔记历史和学习记录都保存在本机知识库中，**不要依赖聊天记忆**：每次开始先查状态。

## 开始工作前（每次）

1. `lw doctor` —— 环境、登录、笔记目录、授权是否就绪。按提示修复；需要用户操作的告诉用户。
2. 登录失效或从未登录：运行 `lw auth login --gui`，会弹出系统对话框（macOS、Windows、Linux 桌面都支持），用户在对话框里输入学号、密码和二次验证码。没有图形界面的 Linux（如 SSH 远程）请让用户自己在终端运行 `lw auth login`。**绝不要让用户把密码、验证码、Cookie 或 API Key 发到聊天里**，也不要去读系统凭据库或本机状态目录里的 `secrets/`。
3. 首次读取学习包前检查授权（见“授权规则”）。

## 授权规则（必须遵守）

- 读取学习包、截图、资料、学习记录，意味着这些内容会发给你所在 AI 工具的模型服务商。`lw consent status` 中没有 `harness` 授权时：先用一两句话告诉用户“会发送哪些资料（课堂转写、截图、导入的资料、笔记、学习记录）、用途（整理笔记和讲解）、可以随时撤销”，**等用户明确同意后**再运行 `lw consent grant harness --scopes transcripts,screenshots,materials,notes,learning`。之后在这个范围内不再逐次询问。
- 要使用新的云服务（如云端语音识别 `asr:<名>`）或扩大范围：重新说明并征得同意。授权发送资料 ≠ 授权花钱：不要擅自注册/开通付费服务，云端识别要设置 `--monthly-budget-minutes`。
- 本地处理（whisper.cpp 语音识别、Apple Vision OCR、ffmpeg）不需要授权。

## 常用流程（详细步骤见 references/workflows.md）

| 用户说 | 做法 |
|---|---|
| “把第七周 Java 整理好，重点看老师演示的代码” | `lw sync` → `lw process -c java -w 7` → 对每个课时 `lw lecture packet <ID>` 读学习包、看关键截图 → 写草稿 → `lw note save -l <ID> -f 草稿 --model <你的模型>` |
| “数据库还没整理过的课补齐，完成的别重复” | `lw sync` → `lw status -c 数据库` → `lw process -c 数据库 --unprocessed` → 只给没有笔记的课时写笔记 |
| “我没理解关系代数的除法，结合老师讲的一点点教我” | `lw search 除法 -c 数据库`、`lw learn status 除法 -c 数据库` → 读相关课时学习包/笔记 → 按 references/tutoring.md 逐步教学并 `lw learn log` 记录证据 |
| “把实验指导书关联到课程，分清课上讲过什么和实验还要做什么” | `lw material add 文件 -c 课程 --role lab_guide` → `lw material suggest` → 用户确认后 `lw material link -l` → 对照学习包写 `--kind topic` 笔记 |
| “这份笔记换成适合复习的组织方式，保留我自己补充的理解” | `lw note show <课时ID>`（含用户在 Obsidian 的修改）→ 新草稿**原样保留**用户补充的内容 → `lw note save ... --style 复习版` |
| “备份整个知识库，换电脑后继续学” | `lw backup create`；新电脑上 `lw backup restore 文件` → `lw init --notes 目录` → `lw auth login --gui` → `lw note export` → `lw learn next` |

用户用自然语言说时间范围时，换算成 `--week`、`--date`、`--since/--until`、`--course`（模糊匹配，可用 `lw course set <课程> --alias` 加别名）。不确定指哪门课/哪几次课时，先 `lw lectures ...` 列出来给用户确认。

## 写笔记的硬性规则（详见 references/note-guide.md）

1. 三类内容分开：**课堂内容**（老师讲的/屏幕上展示的，正文，带 `{t HH:MM:SS}` 时间点）；**资料内容**（`> [!quote] 资料：…`，用 `{m 资料ID p.页}` 标出处）；**AI 补充**（解释、类比、例题、延伸，放 `> [!tip] AI 补充`）。教材或实验指导书里的步骤不能写成“老师讲了”。
2. 只用学习包里存在的时间点、截图 ID、资料 ID；`lw note save` 会拒绝不存在的引用。不要编造老师的话、时间戳或看不清的代码：截图看不清就写“（截图看不清）”，转写存疑就写“（转写不清）”。
3. 代码、公式、类名以截图为准（转写常把术语听错），必要时直接打开截图图片核对。
4. 平台 AI 摘要只用于定位，不当作老师原话引用。
5. 按课程类型调整重点（学习包第一部分有提示）；用户对某门课的偏好记在 `lw course set --note-prefs`。
6. 草稿第一行写 `# 标题`（作为笔记标题）。保存后把 `lw note save` 的输出（路径、提醒、提案）如实告诉用户。

## 用户修改过的笔记

`lw note save` 不会覆盖用户在笔记软件里的修改：若新版本没有包含用户的补充，会写成旁边的 `.lw-proposed.md` 并列出缺失内容。此时应：把缺失内容补进草稿再保存；或经用户同意用 `--merge`（三方合并）；只有用户明确要求时才用 `lw note accept` 或 `--replace`。版本：`lw note history/diff/rollback`。

## 教学与学习记录

按 references/tutoring.md：一次一个知识点、先问后讲、根据回答调整；“讲过 ≠ 掌握”。每次检验后用 `lw learn log <知识点> -c <课程> --kind answered --outcome correct|partial|wrong --evidence "学习者的原话要点" [--misconception ...] [--type apply]` 记录；开始/结束时 `lw learn session start|end --harness <工具名>`。掌握状态由证据推出（`lw learn status`），不要自己报分数。

## 处理进度与资源

- `lw process` 可随时中断、重跑；完成的阶段自动跳过（`--force <阶段>` 重做）。每阶段会报告从学校服务器传输了多少数据。
- 默认只取录屏关键帧和音频样本，不下载、不保存完整视频；平台已有转写时不做语音识别。处理结果中的“不完整/缺转写/匹配歧义”要告诉用户（`lw status`、`lw lecture show`）。
- 课表匹配有歧义（`ambiguous/unscheduled/incomplete`）时，不要猜：把 `lw lectures --status ...` 的说明给用户看，用户确认后 `lw lecture confirm` 或 `lw lecture assign`。

## 命令速查

`lw --help`、`lw <命令组> --help` 查看全部参数；常用输出加 `--json` 便于解析。详见 references/cli.md。
