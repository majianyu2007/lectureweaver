# 工作流程详解

所有命令可加 `--json`。课程名可模糊匹配（“java”“数据库”），课时 ID 形如 `2026-10-20_2094300_p3-4`。

## A. 整理某周/某天/某门课（含“重点看演示的代码”）

1. `lw sync`（只同步元数据，几秒钟）。
2. `lw lectures -c java -w 7` 列出将处理的课时；有 ⚠ 的看说明，必要时问用户。
3. `lw process -c java -w 7`。每个课时依次：details → transcript → visual → packet。输出里会报告传输量；失败的阶段重跑即可续上。
4. 对每个课时：
   - `lw lecture packet <ID>` 得到学习包路径并阅读（大的学习包可分段读：先“素材情况”和“时间线”，再按需看“截图文字”）。
   - 用户强调代码/板书/某部分时，打开相应截图图片（学习包给出目录）核对。
   - 写草稿到临时文件（如 `/tmp/lw-<ID>.md`），先 `lw note save -l <ID> -f 草稿 --check` 校验，再正式保存：
     `lw note save -l <ID> -f 草稿 --model <模型@工具> --style <风格>`
   - 同时登记本课的主要知识点：`lw learn concept "<名称>" -l <ID> --at HH:MM:SS --summary "<一句话>"`（让以后能检索、能教学、能跨课联系）。
5. 汇报：笔记路径、素材缺口（无转写/匹配歧义/录像缺段）、传输量。

## B. 补齐未整理的课

`lw status -c 数据库` → `lw process -c 数据库 --unprocessed` → 对“已整理素材，尚未生成笔记”的课时执行 A.4。已有笔记的不要重写，除非用户要求。

## C. 历史学期

`lw sync --term 2025-2026-2`，之后用 `--term 2025-2026-2` 筛选。

## D. 课程资料

- 导入：`lw material add 实验指导书.pdf -c 数据库 --role lab_guide`（支持 PDF（含扫描版，本地 OCR）、PPTX、DOCX、Markdown/文本、代码）。旧版 .ppt/.doc 请用户另存为新格式。
- 关联到课时：`lw material suggest <资料ID> -c 数据库` 给出候选和共同词 → 给用户看 → `lw material link <资料ID> -l <课时ID> --role lab_guide`。
- “分清课堂讲了什么、实验还要做什么”：读资料（`lw material show <ID> --units 1-5`）和相关课时学习包/笔记，写一篇 `--kind topic --key "实验三对照" -c 数据库` 笔记，分三栏：课堂讲过（带时间点）/ 资料要求（带页码）/ 尚未讲过需要自学或注意（AI 补充）。

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
