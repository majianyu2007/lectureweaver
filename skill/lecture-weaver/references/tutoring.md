# 教学指南

用户偏好：循序渐进、一次一个关键点、根据回答判断，已经会的不反复讲，不懂的换一种讲法或练习方式。

## 开始

1. `lw learn session start -c <课程> --focus <主题> --harness <工具名>`
2. 找材料：`lw search <关键词> -c <课程>` 找到老师讲这个概念的课时和时间点；`lw lecture packet <课时ID>` / `lw note show <课时ID>` 读老师当时的讲法；需要时看相关前后课时（学习包“前后课时”一节）和 `lw material show`。
3. 查已有状态：`lw learn status <知识点> -c <课程>`（掌握情况、未解决的误解、最近证据）。已经“掌握”的不要从头讲，先用一道应用题确认。

## 每个知识点的循环

1. **先问**：一个能暴露理解程度的小问题（不要“你懂了吗”）。例：“R÷S 的结果里每个元组需要满足什么条件？”
2. **根据回答**：
   - 答对：简短确认 + 追问一个应用/变式题（`--type apply`）。
   - 部分对/错：指出具体哪里不对，换一种讲法（例子、对比、画表、从老师课上的例子出发 `{课时 时间点}`），再出一题。
   - 说不会：先拆成更小的前置概念（如除法 → 笛卡尔积、投影、差）。
3. **记录证据**（每次作答后）：
   `lw learn log 关系代数除法 -c 数据库 --kind answered --outcome partial --evidence "能说出要包含S的全部元组，但把结果属性写成了R的全部属性" --question "R÷S 的结果属性是什么" --misconception "以为除法结果包含被除关系的全部属性"`
   讲解后可记 `--kind explained`（这只是“讲过”）。用户自述“还是不懂”记 `--kind self_report --outcome unsure --evidence "..."`。
4. 误解被纠正后：`lw learn resolve <误解ID> --evidence "..."`（ID 见 `lw learn status <知识点>`）。

## 原则

- 讲过 ≠ 掌握；掌握状态只由作答证据推出，不自己给分数。用户坚持说会了：可 `lw learn set ... --reason`，但后续的新证据会重新判断。
- 区分来源：老师课上怎么讲的（引用课时与时间点）、教材怎么写的、你自己的补充例子。
- 一次只推进一个点；每 2–3 个点做一次小结；用户累了就结束。
- 结束：`lw learn next -c <课程>` 看下次建议，`lw learn session end --summary "..."`。

## 换电脑/换工具后继续

`lw learn next -c <课程>` 和 `lw learn status -c <课程>` 就能接上，不需要用户重新说明进度。
