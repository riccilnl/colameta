# AGENTS.md 生成规则

## 定位

AGENTS.md 是项目级长期契约文件，不是版本任务文件。

它用于告诉本地执行器：

- 这个项目的长期目标是什么
- 哪些规则跨版本长期有效
- 如何处理与 Runner 当前版本任务的优先级关系
- 哪些行为永远禁止
- 默认如何保持改动小、可审查、可验证

版本级任务、allowed_files、acceptance_commands、临时实现方案，不应写进 AGENTS.md。

## 指令优先级

如果指令冲突，优先级如下：

1. 用户当前明确指令
2. 当前 Runner 版本 prompt / allowed_files / acceptance_commands
3. AGENTS.md
4. 通用编码习惯

AGENTS.md 不得覆盖当前 Runner 版本任务。

## 应该写入 AGENTS.md 的内容

只写长期稳定内容，例如：

- 项目目标
- 项目长期不做什么
- 稳定架构原则
- 长期安全边界
- 默认协作规则
- 默认验证原则
- 执行器行为约束

## 不应该写入 AGENTS.md 的内容

不要写入可能随版本变化的内容，例如：

- 本版本要做什么
- 本版本允许修改哪些文件
- 本版本验收命令
- 临时 bug 修复方案
- 临时目录迁移规则
- 一次性的实现细节
- 某个版本专属的禁止/允许项

这些应写入 `.mvp-runner/prompts/**` 或 plan。

## 推荐模板

```markdown
# AGENTS.md

## Purpose

This project is managed by MVP Runner.

Use this file only for stable project-level rules.  
Current version tasks are defined in `.mvp-runner/plan.json` and `.mvp-runner/prompts/**`.

## Priority

If instructions conflict, follow this order:

1. Explicit user instruction
2. Current Runner version prompt, allowed_files, and acceptance_commands
3. This AGENTS.md
4. General coding conventions

## Stable Rules

- Keep changes small and scoped to the current Runner version.
- Do not modify files outside the current version scope unless the user explicitly asks.
- Do not read, print, store, or expose secrets, tokens, API keys, or Bearer values.
- Do not run destructive Git commands.
- Prefer existing project structure over new abstractions.
- Do not introduce new dependencies, services, frameworks, or configuration unless the current version requires it.

## Validation

Use the acceptance commands from the current Runner version.

If validation cannot run, report the exact command and reason.