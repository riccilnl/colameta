# ColaMeta

## 安装

```bash
pip install colameta
```

安装后直接使用 `colameta` 命令：
```bash
colameta /path/to/your/project --public-base-url https://your-domain.com
colameta serve /path/to/your/project --auth-mode none --open
```

## 项目定位

ColaMeta 是连接 ChatGPT / GPTs 和本地执行器的 AI coding workflow harness。

它不是另一个 coding agent，而是 GPTs 到本地开发环境之间的受控工作流层：GPTs 负责判断、分流和任务设计；Runner 负责版本计划、范围控制、preview / apply、验证审查和 Git 闭环；本地执行器负责真正读代码、改代码、跑测试。

ColaMeta 的目标是让本地 AI 开发更受控，并把执行器 token 尽量花在实际 coding 上，而不是浪费在反复探索需求、猜修改边界、理解流程和整理交付状态上。

典型工作流：

1. 用户在 ChatGPT / GPTs 中描述需求。
2. GPTs 判断任务类型，收敛目标、范围和验收方式。
3. Runner 将任务纳入版本计划，生成或保存版本 prompt，并限制允许修改的文件和命令。
4. 本地执行器按任务读取代码、修改代码、运行验证并输出报告。
5. Runner 汇总执行器报告、Git diff、验收结果和审查结论。
6. 通过后再进入受控 commit / push 链路。

因此，ColaMeta 的核心价值不是单纯“让 AI 写代码”，而是把 AI 写代码纳入一个可计划、可控制、可验证、可审查、可提交的本地工程流程。

当前主线是：Web Console + MCP / GPTs Actions + CLI。TUI 已退役。

## 核心能力与工具链

ColaMeta 面向的是完整的 AI 本地开发闭环，而不是单一工具调用。

### 产品能力

- **GPTs 到本地执行器的连接**：通过 MCP / GPTs Actions，把 ChatGPT / GPTs 的判断、分流和提示词设计接到本地仓库与本地执行器。
- **ChatGPT 应用接入**：支持通过 OpenAPI / Actions 把 ColaMeta 工具接入 GPTs；也支持 MCP tools/call、本地 CLI 和可信内部链路。
- **Web 管理台**：提供本地 Web Console，用浏览器查看项目状态、当前版本、计划列表、prompt、Git 状态、执行器状态、报告和下一步动作。
- **Runner 版本计划**：用 Runner plan 管理版本任务、当前版本、下一版本、allowed files、forbidden files、acceptance commands 和版本推进状态。
- **版本记录与 workflow run 记录**：保存版本状态、workflow 记录、执行器运行报告、Git diff、验收结果和审查证据，方便回看每次版本为什么通过或失败。
- **项目记忆系统**：支持 memory、todo 和 decision 三类长期记录，用于保存 GPTs 长期记忆、后续事项和已确认决策。
- **多项目管理**：通过本地 project registry 登记多个项目，并在 Actions 调用中用 project_name 路由到目标项目。
- **prompt 与 plan 管理**：支持生成、保存、插入、修复和推进版本 prompt / plan，把需求变成可执行、可审查、可复盘的任务单。
- **受控 preview / apply**：文档、patch、plan、prompt、执行器运行、提交和远程操作都先生成 preview，再用 preview_id 执行。
- **执行器调度与审查**：触发本地执行器开发或修复，读取执行器报告，结合 diff、验收命令和审查结论决定是否通过。
- **token 用量与缓存命中统计**：执行器报告可记录 input tokens、output tokens、cached input tokens、total tokens 和 cache hit rate，用来观察 token 是否花在有效 coding 上。
- **受控 Git 闭环**：提交、push、回退和文件恢复走 Runner 工具链，不让执行器直接做破坏性 Git 操作。

### 工具链

- **ChatGPT / GPTs**：用户描述需求、GPTs 判断任务、生成开发提示词、审查执行结果。
- **MCP / GPTs Actions**：给 ChatGPT / GPTs 使用的受控工具层，覆盖状态分析、plan 管理、prompt 管理、项目记忆、workflow run 查询、执行器运行、报告读取、文档修改、patch、Git commit 和远程操作。
- **Web Console**：本地浏览器管理台，展示项目状态、当前版本、版本列表、prompt、执行器状态、报告、Git 状态和受控操作入口。
- **CLI**：本地命令入口，用于项目启动、登记、模式切换、plan lint 和调试。
- **本地执行器**：负责真正读代码、改代码、跑测试，并把结果交还 Runner 审查。
- **项目运行目录**：`.colameta/` 保存 plan、state、prompts、runtime、logs、reports、workflow 记录和执行器会话。

## 当前架构

- `runner/`：Runner 核心流程、MCP 工具、Web Console 服务、执行器工作流、Git 受控链路
- `adapters/`：Codex、OpenCode、Pi、Git、Shell 等外部适配层
- `schemas/`：plan、state、command、result、audit 等结构定义
- `scripts/`：CLI 分发与辅助入口
- `bin/colameta`：主命令入口
- `extension/`：浏览器扩展相关文件
- `tests/`：单元测试和边界测试
- `docs/`：设计说明、使用说明和历史审计文档

当前项目内 Runner 元数据入口统一为 `.colameta/`。

## 快速启动

推荐入口：

```bash
colameta /path/to/project --public-base-url https://your-domain.example
```

常用模式：

```bash
colameta /path/to/project source-only
colameta /path/to/project managed
colameta serve /path/to/project --open
```

默认本地地址：

- Web Console: `http://127.0.0.1:8799`
- MCP HTTP: `http://0.0.0.0:8765/mcp`
- OpenAPI: `GET /openapi.json`
- Actions API: `POST /api/{tool_name}`
- MCP JSON-RPC: `POST /mcp`

`source-only` 适合未纳管项目，只开放只读 MCP 能力。`managed` 会启动 Web + MCP，并要求项目具备 Runner 管理目录，或通过受控 onboarding 流程创建最小结构。

## 项目登记

ColaMeta 支持通过本地 registry 按 `project_name` 路由多个已登记项目。

```bash
colameta add my-project /path/to/project source-only
colameta add my-project /path/to/project managed
colameta list
colameta remove my-project
```

GPTs Actions 调用时优先传 `project_name`，避免依赖当前工作目录。

## 配置与认证

推荐使用用户级配置。不要通过业务项目 `.env`、进程环境变量或 `.env.example` 配置 Runner 认证。

用户级配置路径：

- `~/.config/colameta/config.json`
- `~/.config/colameta/auth.json`

首次启动缺少用户级配置时，ColaMeta 会创建 `~/.config/colameta/`，通过交互流程帮助设置 GPTs Actions 使用的 Bearer token，并继续引导添加第一个项目。

认证模式：

- `none`：本地调试
- `token`：GPTs Actions 使用的 Bearer token
- `oauth`：MCP 使用的 OAuth authorization code + PKCE

GPTs Actions 的 Bearer token 与 MCP OAuth 不是同一认证入口。

## Web Console

Web Console 是本地主工作台，只做交互层，不维护独立业务状态机。

它负责展示状态并触发受控动作：

- 查看当前版本、计划、Git 状态和执行器状态
- 启动执行器开发或修复
- 重新测试、阶段审查、报告读取
- 预览并应用 plan patch
- 推进版本、准备提交、查看远程状态

Web Console 由 `runner/web_console.py` 提供原生 HTML / CSS / JS，不依赖 npm、bundler 或 CDN。

## MCP / GPTs Actions

MCP 和 Actions 是 ColaMeta 的受控操作层。

常用入口：

- `analyze_project_state`：聚合读取项目、Git、Runner、plan、执行器和报告状态
- `manage_files`（统一项目文件搜索、读取与受控生命周期工具；action=search/read/create/edit/delete）：搜索和读取白名单项目文件；受控创建/编辑/删除（phase=preview/apply/status）委托 MCPProjectPatchManager
- `manage_git`（统一 Git 域公共工具；action=status/diff/review_context/commit_readiness/commit_message/commit_preview/commit_apply/push_*/pull_*/history_*/restore_file_*/revert_*）：审查工作区和受控 Git 操作
- `manage_runner_workflow`：高层 workflow 入口
- `manage_plan_version` / `manage_prompt_file`：版本计划和提示词管理
- `manage_executor_workflow`：执行器 preflight、preview、run、报告读取和审计包
- `manage_project_docs` / `manage_files action=create|edit|delete`：受控文档和小范围文件生命周期（`manage_files create|edit|delete` 替代旧 `manage_project_patch`）

写入类动作必须经过 preview，再用 preview_id apply。提交和 push 也必须走对应 preview / apply 链路。

## 执行器

当前主支持的执行器 provider：

- `codex`
- `opencode`

`pi` 代码路径已经存在，走 Pi RPC 适配链路，但产品口径上仍按“在路上 / 实验链路”处理，不作为当前主支持执行器宣传。

执行器负责大范围读代码、改代码、跑测试和输出报告。GPTs / MCP 负责任务设计、审查、preview、apply、提交决策和状态闭环。

执行器报告通过 Runner 管理目录保存，并可由 `list_executor_run_reports`、`get_executor_run_report` 和 `inspect_executor_activity` 读取。报告中可包含 token 用量和缓存命中信息，用于观察执行器 token 是否主要花在实际 coding 上。

## 能力边界

ColaMeta 不提供任意 shell 或任意 Git 操作。

长期边界：

- 不自动 push
- 不自动 merge
- 不自动 rebase
- 不自动 reset / clean
- 不暴露 token、API key 或 Bearer 值
- 不绕过 preview_id 执行写入、提交或远程操作

## 运行目录

运行目录：

```text
.colameta/
```

常见内容：

- `plan.json`：版本计划
- `state.json`：运行状态
- `runner-settings.json`：项目执行器设置
- `prompts/*.md`：版本提示词
- `runtime/`：当前提示词、workflow 记录和执行器会话
- `logs/`：运行日志和审计日志
- `reports/`：阶段审查报告和执行器报告
- `plan-patches/`：pending plan patch

## 环境要求

- Python 3.10+
- Git
- 可选执行器 CLI：`codex`、`opencode`、`pi`

基础准备：

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
```

## 验证

常用验证入口：

```bash
python -m pytest
colameta lint-plan /path/to/project
```

如只审查当前 Runner 版本，应优先使用该版本 plan 中的 acceptance commands。

## 文档索引

- 使用说明：`docs/RUNNER_USAGE.md`
- MCP 说明：`docs/MCP_PLANNING_BRIDGE.md`
- Plan 与提示词：`docs/RUNNER_PLAN_AND_PROMPTS.md`
- Runner 接口说明：`docs/MVP_BUILD_RUNNER_INTERFACES.md`
- 架构审计：`docs/architecture-audit.md`
- 执行器架构审计：`docs/executor-architecture-audit.md`
- 工具链地图：`docs/runner_toolchain_map.md`

## 当前仓库状态说明

本仓库是 ColaMeta / MVP Runner 接口项目本身，不是被管理的业务样例项目。

截至当前审计状态：

- Git 工作区存在 `.gitignore` 修改
- `.ruff_cache/` 缓存目录已从工作区移除
- 当前 Runner plan 存在，最近状态为已通过
- README 只描述当前主线，不再保留逐版本历史说明
