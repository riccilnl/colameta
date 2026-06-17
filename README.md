```
   ______      __                      __     
  / ____/___  / /___ _____ ___  ____ _/ /____ 
 / /   / __ \/ / __ `/ __ `__ \/ __ `/ __/ _ \
/ /___/ /_/ / / /_/ / / / / / / /_/ / /_/  __/
\____/\____/_/\__,_/_/ /_/ /_/\__,_/\__/\___/ 

🥤 enjoy your vibe coding with GPTs! ✨
```

# ColaMeta

[**English**](README.md) | [**中文**](README.zh-CN.md)

ColaMeta is an AI coding workflow harness that connects ChatGPT / GPTs to local executors.

It's not another coding agent. It's a controlled workflow layer between GPTs and your local development environment: GPTs handles judgment, triage, and task design; Runner handles version planning, scope control, preview/apply, validation review, and Git closure; local executors actually read code, edit code, and run tests.

## Installation

```bash
pip3 install colameta
```

If `pip3` is not available, use venv:

```bash
python3 -m venv path/to/venv
source path/to/venv/bin/activate
pip3 install colameta
```

After installation, use the `colameta` command:

```bash
colameta /path/to/your/project --public-base-url https://your-domain.com
colameta serve /path/to/your/project --auth-mode none --open
```

## Quick Start

```bash
colameta /path/to/project source-only   # Read-only mode
colameta /path/to/project managed       # Full mode
colameta serve /path/to/project --open  # Start Web Console
```

Default local addresses:

- Web Console: `http://127.0.0.1:8799`
- MCP HTTP: `http://0.0.0.0:8765/mcp`

## Capabilities

- **GPTs to local executor connection**: Connects ChatGPT / GPTs judgment and prompt design to local repos and executors via MCP / GPTs Actions.
- **Web Console**: Local browser-based dashboard for project status, plans, prompts, Git state, executor status, reports, and next actions.
- **Runner version planning**: Manages version tasks, allowed/forbidden files, acceptance commands, and version progression.
- **Version records & workflow runs**: Saves version state, workflow records, executor reports, Git diffs, and review evidence.
- **Project memory**: Supports memory, todo, and decision records for GPTs long-term memory and decision tracking.
- **Multi-project management**: Register multiple projects via local registry, route by `project_name` in Actions calls.
- **Prompt & plan management**: Generate, save, insert, fix, and advance version prompts and plans.
- **Controlled preview/apply**: Docs, patches, plans, prompts, executor runs, commits, and remote ops all generate preview first, then apply via `preview_id`.
- **Executor dispatch & review**: Trigger local executors, read reports, combine diffs, acceptance commands, and review conclusions.
- **Token usage & cache stats**: Executor reports track input/output/cached tokens and cache hit rate.
- **Controlled Git closure**: Commits, pushes, reverts, and file restore go through Runner toolchain, never direct Git operations.

## Toolchain

- **ChatGPT / GPTs**: User describes requirements, GPTs judges tasks, generates prompts, reviews results.
- **MCP / GPTs Actions**: Controlled tool layer covering state analysis, plan/prompt management, project memory, executor runs, reports, docs, patches, Git commits, and remote ops.
- **Web Console**: Local browser workspace for status display and controlled actions.
- **CLI**: Local command entry for project start, registration, mode switching, plan lint, and debugging.
- **Local executor**: Reads code, edits code, runs tests, and returns results to Runner for review.
- **Runtime directory**: `.colameta/` stores plans, state, prompts, runtime, logs, reports, workflow records, and executor sessions.

## Project Registration

Register multiple projects by name:

```bash
colameta add my-project /path/to/project source-only
colameta add my-project /path/to/project managed
colameta list
colameta remove my-project
```

GPTs Actions should pass `project_name` instead of relying on the current working directory.

## Configuration & Authentication

Use user-level config, not project `.env` files:

- `~/.config/colameta/config.json`
- `~/.config/colameta/auth.json`

Auth modes:

- `none`: local debugging
- `token`: Bearer token for GPTs Actions
- `oauth`: OAuth authorization code + PKCE for MCP

## Web Console

The Web Console is the local workspace for:

- Viewing current version, plan, Git state, and executor status
- Starting executor development or fix runs
- Re-testing, phase review, report reading
- Previewing and applying plan patches
- Advancing versions, preparing commits, viewing remote status

Built with native HTML/CSS/JS — no npm, bundler, or CDN required.

## MCP / GPTs Actions

Key tools:

- `analyze_project_state` — aggregated project, Git, Runner, plan, executor, and report status
- `manage_files` — unified file search, read, create, edit, delete with preview/apply lifecycle
- `manage_git` — status, diff, review, commit preview/apply, push, pull, history, file restore, revert
- `manage_runner_workflow` — high-level workflow entry
- `manage_plan_version` / `manage_prompt_file` — version plan and prompt management
- `manage_executor_workflow` — executor preflight, preview, run, report reading, audit
- `manage_project_docs` — document management

All write operations require preview → apply via `preview_id`. Commits and pushes follow the same controlled flow.

## Executors

Supported providers:

- `codex`
- `opencode`

Executors read code, edit code, run tests, and produce reports. GPTs / MCP handles task design, review, preview, apply, commit decisions, and status closure.

## Runtime Directory

```text
.colameta/
```

Common contents:

- `plan.json` — version plan
- `state.json` — runtime state
- `runner-settings.json` — project executor settings
- `prompts/*.md` — version prompts
- `runtime/` — active prompts, workflow records, executor sessions
- `logs/` — run logs and audit logs
- `reports/` — phase review reports and executor reports
- `plan-patches/` — pending plan patches

## Requirements

- Python 3.10+
- Git

## Safety Boundaries

- No automatic push / merge / rebase / reset / clean
- No exposure of tokens, API keys, or Bearer values
- All write operations must go through preview/apply flow
- Commits and pushes use controlled chains, never bypassing preview

## License

Open source, but **commercial use is prohibited**.
