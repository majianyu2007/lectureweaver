# 工作流程详解

所有命令可加 `--json`。课程名可模糊匹配（“java”“数据库”），课时 ID 形如 `2026-10-20_1234567_p3-4`（日期_课程号_节次）。

## A. 整理某周/某天/某门课（含“重点看演示的代码”）

1. `lw sync`（只同步元数据，几秒钟）。
2. `lw lectures -c java -w 7` 列出将处理的课时；有 ⚠ 的看说明，必要时问用户。
3. `lw process -c java -w 7`。每个课时依次：details → transcript → visual → packet；默认同时处理 3 个课时。输出里会报告传输量；失败的阶段重跑即可续上（网络中断属于这种情况）。课时多时放到后台运行，同时用 `lw lectures --ready` 找已经可以写笔记的课时。
4. 对每个课时：
   - `lw lecture packet <ID>` 得到学习包路径并阅读（大的学习包可分段读：先“素材情况”和“时间线”，再按需看“截图文字”）。
   - 用户强调代码/板书/某部分时，打开相应截图图片（学习包给出目录）核对。
   - 按 note-guide.md 写草稿到临时文件（如 `/tmp/lw-<ID>.md`）。
   - **核对**（note-guide.md“内容要对”）：定义、定理、公式、代码逐条对照学习包“截图文字”；例题数值用 Python 复算，代码能运行就运行；口头说法与屏幕或标准结论冲突时以屏幕为准、正文写对。大多数课没有资料可对照，这一步不能省。
   - `lw note save -l <ID> -f 草稿 --check` 校验，按“写作提醒”修改，把“核对”清单里的句子逐条确认，再正式保存：
     `lw note save -l <ID> -f 草稿 --model <模型@工具> --style <风格>`
   - 同时登记本课的主要知识点：`lw learn concept "<名称>" -l <ID> --at HH:MM:SS --summary "<一句话>"`（让以后能检索、能教学、能跨课联系）。
5. 汇报：笔记路径、素材缺口（无转写/匹配歧义/录像缺段）、传输量。

## B. 补齐未整理的课

`lw status -c 数据库` → `lw process -c 数据库 --unprocessed`（后台）→ 对 `lw lectures -c 数据库 --ready` 列出的课时执行 A.4。已有笔记的不要重写，除非用户要求。

## B2. 全部课程一次整理（几十个课时）

1. 先估时并告诉用户：`lw lectures --unprocessed` 看数量；有平台转写的约 1 分钟/个，需要本地语音识别的约 5–10 分钟/个。
2. 后台只启动一个处理命令：`lw process --unprocessed`（全部课程；默认 3 个并行，本地语音识别排队）。
3. 写笔记与处理同时进行：循环 `lw lectures --ready` → 对列出的课时执行 A.4。并行写作的子代理只分配 `--ready` 里的课时，不要让它们各自运行 `lw process`。数学、理科课再派一个只做核对的子代理：读学习包和已保存的笔记，只查对错、改错（按 A.4 的核对步骤），不改写法，改完重新保存。
4. 最后 `lw status` 核对：每个课时都有笔记；“不完整/失败”的课时重跑 `lw process -l <ID>`，仍不行就把原因告诉用户。

## B3. 按新写法重写已有笔记

用户觉得笔记不好读、想换写法时：对每个课时 `lw note show <ID> --draft`（含用户修改，输出成草稿标记，改完可直接保存）+ `lw lecture packet <ID>` → 按 note-guide.md 重写 → `lw note save -l <ID> -f 草稿 --style <风格>`。旧版本都在历史里，可 `lw note rollback`。用户在 Obsidian 改过的笔记不会被覆盖（见 SKILL.md）。

## C. 历史学期

学期代码格式为“学年-学期”，如 2025-2026-2（2025–2026 学年第二学期；第 3 学期是夏季小学期）。`lw sync --term 2025-2026-2` 同步该学期的课表和录像（不确定有哪些学期时随便写一个，报错信息会列出云课堂里可选的学期），之后 `lw lectures/process/status` 等都加 `--term 2025-2026-2`。不加 `--term` 时一律指最新同步的学期，旧学期的课时不会被顺带处理。旧学期的课程和笔记与当前学期并存在同一个知识库里，检索（`lw search`）跨学期进行。

## D. 课程资料

- 资料是可选的：大多数课没有资料，笔记照常靠课堂（转写 + 截图）写，不要为此向用户要资料或让用户去找电子课本。
- 导入：`lw material add 实验指导书.pdf -c 数据库 --role lab_guide`（支持 PDF、PPTX、DOCX、Markdown/文本、代码）。扫描版 PDF 和文字层是乱码的 PDF 会自动用本地 OCR，输出里会注明；以前导入、`lw material show` 显示乱码的，用 `lw material refresh <资料ID>` 重新提取（必要时加 `--ocr`）。先用 `lw material show` 的文字定位，公式等 OCR 认不准的地方再单独渲染那一页核对，不要把整份 PDF 逐页渲染成图片来读。旧版 .ppt/.doc 请用户另存为新格式。
- 关联到课时：`lw material suggest <资料ID> -c 数据库` 给出候选和共同词 → 给用户看 → `lw material link <资料ID> -l <课时ID> --role lab_guide`。
- “分清课堂讲了什么、实验还要做什么”：读资料（`lw material show <ID> --units 1-5`）和相关课时学习包/笔记，写一篇 `--kind topic --key "实验三对照" -c 数据库` 笔记，分三栏：课堂讲过（带时间点）/ 资料要求（带页码）/ 尚未讲过需要自学或注意（AI 补充）。

## D2. 用资料完善已有笔记

用户拿来课件/教材，让你“用资料完善笔记”时：

1. 导入并关联：`lw material add` → `lw material suggest` → 确认后 `lw material link -l`。每份资料只关联到真正讲了其中内容的课时。
2. 对每个课时：`lw note show <ID> --draft`（含用户修改，草稿标记）+ 资料相应页（`lw material show <资料ID> --units 页`）。先列出这节课讲过的小节（笔记里有课堂时间点的），资料只对照这些小节。
3. **先改错**：逐条比对定义、定理、条件、公式、例题答案，正文里错的直接改正（老师口误值得保留时按 note-guide.md 加 `[!warning]`）。
4. **再补充**：只在资料原文更准确的地方加 `> [!quote] 资料：…`（`{m 资料ID p.N}`，N 是 PDF 第几页），一篇一般 0–5 处；你写的解释放 `[!tip] AI 补充`。资料里这节课没讲的内容不写进来，`--check` 提示“没有课堂时间点”的小节要删掉。
5. 保存后汇报：每篇改正了哪些错（原来怎么写 → 现在怎么写，依据哪一页），补了什么。改正清单能看出这门课转写和口述常错在哪，用户也可以据此检查没有资料的其他笔记。

## E. 检索出处

`lw search 除法 -c 数据库`：返回课堂转写（带日期和时间点）、截图文字、资料页码、笔记中的命中。回答“出自哪节课”时给出课时、时间点，并提醒可点击笔记中的时间链接回看。

## F. 笔记版本与冲突

- `lw note list -c 课程`、`lw note show <ID>`、`lw note history <ID>`、`lw note diff <ID> --a 1 --b 3`、`lw note rollback <ID> --to 2`。
- 用户在 Obsidian 改过的笔记：`lw note sync` 会把修改记为“你的修改”版本。新 AI 版本不会覆盖用户修改（见 SKILL.md）。
- 笔记目录换了位置：`lw init --notes 新目录` → `lw note export`。

## G. 备份、迁移

- 备份：`lw backup create -o ~/Desktop`（会先记录 Obsidian 中的修改；不含任何密码/Cookie/API Key）。`lw backup verify <文件>` 校验。
- 新电脑（macOS / Linux / Windows 均可，安装方法见项目 README）：安装 `lw` → `lw backup restore <文件>` → `lw init --notes <目录>` → `lw auth login --gui`（无图形界面时 `lw auth login`）→ `lw note export` → `lw skill install` → `lw doctor`。知识库可以在不同系统之间直接互相恢复。
- 知识库自检：`lw check`。

## H. 云端语音识别（可选）

默认本地 whisper。用户想用云端（更快/更准）时：
1. 说明会上传课堂音频、可能收费，得到同意。
2. `lw provider add <名> --base-url https://.../v1 --model <模型> --monthly-budget-minutes 300`
3. 用户自己输入 Key：`lw provider set-key <名> --gui`
4. `lw consent grant asr:<名> --scopes audio`
5. `lw process ... --asr <名>`
