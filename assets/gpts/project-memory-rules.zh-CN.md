每个项目通过 manageProjectMemory 管理自己的记忆：
- memory：长期事实。
- todo：未来事项。
- decision：已确认决策。

GPTs 在涉及架构、版本规划、提示词设计、审查闭环、README/AGENTS 更新、提交前一致性检查时，必须读取项目记忆。

memory 不记录临时任务。
todo 不记录已确认架构决策。
decision 不记录未经确认的建议。

修改 README、AGENTS、memory、todo、decision、plan/prompt 任一项后，提交前检查它们之间是否冲突。
