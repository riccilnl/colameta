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

# What is ColaMeta?

ColaMeta is a project dedicated to making vibe coding accessible to more non-professional users.

It builds an orchestration layer from ChatGPT / GPTs to local executors such as Codex and OpenCode, so you can spend more energy on ideas, requirements, and decisions instead of the details of code implementation.

In simple terms:

**You are the client who owns the requirements.**
You describe what you want, add constraints, and make key product decisions.

**ColaMeta is an AI delivery team running on your own device.**
It connects GPTs, local executors, version plans, prompts, audit packages, validation, project memory, Git closure, and the Web Console into one workflow.

**GPTs is the team lead.**
It works like a product manager, architect, and reviewer: it understands your requirements, decides what should and should not be done, splits work into versions, writes strict execution prompts, assigns development, reviews results, and decides whether to fix, commit, push, or move to the next version.

**Local executors are implementation engineers.**
Codex, OpenCode, and similar executors follow GPTs instructions to read code, edit code, run tests, and produce execution reports.

**The Web Console is your window into the AI team.**
You can see version progress, prompts, executor status, execution reports, audit packages, project memory, decision records, and next actions.

ColaMeta is not about forcing you to manage every detail of AI coding. It lets you delegate requirements to an AI team made of GPTs + local executors, much like working with an outsourced engineering team.

---

## How does ColaMeta deliver a requirement?

```text
Human:
"I want to implement feature XXX."

  ↓

GPTs:
"Understood. This feature should be split into 2 versions:
v1 will implement the core capability, and v2 will add interaction details and edge-case handling."

  ↓

ColaMeta:
Creates 2 pending versions,
and stores each version's goal, execution prompt, allowed files, forbidden files, and acceptance commands.

  ↓

GPTs:
"This version touches many files and needs a stronger model.
I will choose a suitable local executor and model for the implementation."

  ↓

Local executor:
Reads code, edits code, runs tests, and outputs an execution report according to GPTs strict prompt.

  ↓

ColaMeta:
Records the version result, execution report, audit package, validation status, and changed files.

  ↓

GPTs:
"I cannot simply trust the executor's own claim that it is done.
I need to review the diff, report, validation result, and audit package."

  ↓

GPTs:
"Something is not quite right here. I will arrange a fix.
After the fix, validation passes. This can be committed and pushed."

  ↓

ColaMeta:
Completes controlled Git commit and push,
and records the current HEAD.

  ↓

GPTs:
"Continue with the next version."

  ↓

GPTs:
"Both versions are complete.
The XXX feature you requested has been implemented.
This changed files A, B, and C.
The current commit HEAD is abc1234."
```

This is ColaMeta's core workflow:
**the human gives the requirement, GPTs leads the delivery, local executors implement it, and ColaMeta manages the process, evidence, and closure.**

---

## Why not just ask a model to write code directly?

Typical vibe coding often looks like this:

```text
The human tells a model what to do in natural language
The model edits code directly
The human reviews the diff, runs tests, and decides whether it can be committed
```

ColaMeta works differently:

```text
The human gives a requirement
  ↓
GPTs understands the requirement and boundaries
  ↓
GPTs writes a strict execution prompt
  ↓
The prompt specifies allowed files / forbidden files / acceptance commands
  ↓
The local executor develops according to the prompt
  ↓
GPTs reads the report, diff, validation result, and audit package
  ↓
GPTs decides whether the work is closed, needs more fixes, or can be committed and pushed
  ↓
The human only reviews the final result and the decisions that require human judgment
```

You are not handing your code to a black-box model.
You are delegating the requirement to an AI delivery team: GPTs leads the work, local executors implement it, and ColaMeta controls the workflow.

---

## Core roles

### Human: client

You are responsible for:

- Describing what you want
- Explaining what you do not want
- Adding business constraints
- Making key product decisions
- Accepting or rejecting the final result

You do not need to be the default:

- Diff reviewer
- Test engineer
- Git operator
- Architecture reviewer
- Executor dispatcher

Those coding workflow responsibilities are handled by GPTs + ColaMeta + local executors.

### GPTs: team lead

GPTs is responsible for:

- Understanding requirements
- Splitting work into versions
- Writing execution prompts
- Deciding which files can and cannot be changed
- Selecting or accepting the local executor and model
- Driving executor development
- Reviewing executor reports
- Checking diffs, validation results, and audit packages
- Deciding whether to fix, commit, push, or move to the next version

ColaMeta works especially well with GPTs because GPTs supports custom instructions, allowing it to consistently act as the team lead, product manager, and reviewer.

### Local executors: implementation engineers

Codex, OpenCode, and similar executors are responsible for:

- Reading code
- Editing code
- Running tests
- Producing reports

Executors do not decide project direction directly.
They execute controlled tasks generated by GPTs.

### ColaMeta: workflow and delivery system

ColaMeta is responsible for:

- Project registration
- Project routing
- source-only / managed modes
- Version plans
- Prompt saving and insertion
- preview / apply
- Executor dispatch
- Reports and audit packages
- Project memory
- Git commit and push
- Web observation window

---

## Web Console: the client's window into the AI team

The ColaMeta Web Console is not a normal developer dashboard.

It is the client's window into the internal work of the AI delivery team.

![ColaMeta Web Console overview](assets/screenshots/web-console-overview.png)

Through the Web Console, you can see:

- Whether an executor has started
- Whether it is currently running
- Which stage it is in
- Which version the project is currently on
- How version development is progressing
- What prompt GPTs generated
- What the executor report says
- What the audit package and validation status show
- What the current Git state is
- What project memory GPTs has recorded
- Which user decisions have been recorded
- Whether GPTs understood your decisions correctly
- What next actions are available

You do not need to personally act as the engineering lead.
But you can still observe, inspect, and correct the process when needed.

---

## Version plans: delivery closure from initial prompt to final report

In the Web Console's version plan, each version can link to its execution prompt and execution report.

This means ColaMeta does not merely show that a task is done. It preserves the full delivery trail:

```text
What GPTs asked the executor to do at the beginning of this version
Which files were allowed to change
Which files were forbidden
What the acceptance criteria were
What the executor actually did
What the validation and audit results were
Why this version was considered complete or incomplete
```

Humans can review each version like a client reviewing an outsourced delivery record, tracing the work from requirement to result.

---

## Delegate multiple projects to ColaMeta at the same time

ColaMeta is not limited to one current directory.

You can register multiple local projects with ColaMeta:

```bash
colameta add my-app /path/to/my-app managed
colameta add my-site /path/to/my-site managed
colameta add open-source-lib /path/to/open-source-lib source-only
```

Then tell ChatGPT / GPTs in the conversation:

```text
This time you are responsible for the my-app project.
I want you to fix the redirect issue on the login page.
```

After that, all GPTs / ColaMeta project-level operations are explicitly routed to `my-app`.

It does not guess the project from the current working directory.
It will not mix projects because you previously opened another repo.
It will not apply project A's prompt, memory, report, or Git operation to project B.

---

## Run multiple GPTs conversations for multiple projects

With multi-project support, you can open multiple GPTs conversations and let each one follow a different project.

For example:

- One conversation handles `my-app`
- One conversation handles `my-site`
- One conversation handles `open-source-lib`

Each conversation only needs to make its current `project_name` clear.
Operations in that conversation are routed to the corresponding project.

This means you can move several projects forward at the same time without mixing them up.

You can manage multiple AI project teams like managing multiple outsourced teams:

```text
Each project has its own lead conversation
Each project has its own executor tasks
Each project has its own version plan
Each project has its own project memory
Each project has its own execution reports and audit packages
Each project has its own Git closure
```

This can significantly increase the speed of parallel development while keeping project boundaries clear.

---

## Each project has its own memory

A common problem with normal ChatGPT conversations is that a new conversation may forget the project background, past decisions, or ideas and bugs you mentioned but have not implemented yet.

In managed mode, ColaMeta stores project memory inside that project's own `.colameta/` directory.

Common records include:

- `memory`: long-term project facts, such as architecture boundaries and project conventions
- `decision`: decisions you have already confirmed, such as product tradeoffs, architecture direction, and long-term rules
- `todolist`: ideas, bugs, and follow-up tasks you mentioned but have not implemented yet

So you can start a new GPTs conversation and say:

```text
Continue working on the my-app project.
Read the project memory and todo list first, then tell me what should be done next.
```

GPTs can use ColaMeta to recover that project's memory, decisions, and todo list, then continue the work.

This means ColaMeta is not only a bridge to local code.
It also gives each project its own long-term context.

---

## Two project modes

### source-only

source-only is the lightweight connection mode.

It is suitable when you already have a source project and want ChatGPT / GPTs to connect to local executors, read code, analyze problems, write prompts, and drive local tasks.

source-only looks like this:

```text
ChatGPT / GPTs
  ↓
ColaMeta
  ↓
Local project + local executor
```

It is useful for lightweight collaboration on existing projects without forcing full version management.

### managed

managed is the full delivery mode.

On top of source-only, it adds:

- Runner version plans
- Current version state
- allowed files / forbidden files
- acceptance commands
- Prompt file management
- Executor reports
- Audit packages
- Project memory
- Workflow records
- Git commit
- Git push
- Version progression

managed looks like this:

```text
Human client
  ↓
GPTs team lead
  ↓
ColaMeta managed workflow
  ↓
Local executor engineer
  ↓
Reports / audit / validation / Git closure
```

managed turns ChatGPT / GPTs into the lead of a local development delivery team.

---

## Clean context, visible cost

Local executors also have their own conversation context.

If every task starts a fresh session, it wastes a large number of tokens and cannot reuse existing context or cache.
If every task blindly resumes the same session, unrelated tasks may be mixed together, causing wrong assumptions or context pollution.

ColaMeta makes executor session start / resume a controlled decision.

GPTs can decide whether a task should:

- Resume an existing executor session to improve cache hit rate and context continuity
- Start a new executor session to avoid context pollution and accidental task mixing

The decision can consider the current project, branch, executor provider, session identity, task semantics, risk, and cache-hit goals.

Each executor task records independent token usage, including input tokens, output tokens, cached tokens, cache writes, and cache hit rate.

This lets the human client see in the Web Console or execution report:

- How many tokens this task actually cost
- How much cache was hit
- Whether reasonable session reuse reduced cost
- Why a particular task needed a new session

---

## GPTs configuration references

ColaMeta is designed to work especially well with GPTs.

You can configure GPTs as the lead of the AI delivery team, responsible for understanding requirements, writing execution prompts, dispatching local executors, reviewing results, advancing versions, and closing Git operations.

Reference documents:

- [GPTs custom instructions reference](assets/gpts/custom-instructions.zh-CN.md)
- [Execution prompt writing reference](assets/gpts/prompt-writing.zh-CN.md)
- [AGENTS.md generation rules](assets/gpts/agents-generation-rules.zh-CN.md)
- [Project memory rules](assets/gpts/project-memory-rules.zh-CN.md)

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

After installation, you can use the `colameta` command directly.

---

## Quick start

After installation, you can use the `colameta` command directly.

### Register a managed project

managed mode creates a minimal `.colameta/` structure for the project and enables full version plans, project memory, executor reports, audit packages, and Git closure.

```bash
colameta add /path/to/project managed
```

### Register a source-only project

source-only mode is suitable when you only want GPTs / MCP to connect to local source code and executors, without enabling full Runner version management yet.

```bash
colameta add /path/to/project source-only
```

### Start ColaMeta

```bash
colameta start
```

This starts both the Web Console and the MCP HTTP Server.

Default addresses:

- Web Console: http://0.0.0.0:8799
- MCP HTTP: http://0.0.0.0:8765/mcp

Restart or stop the service:

```bash
colameta restart
colameta stop
```

### List registered projects

```bash
colameta list
```

### Remove a project registration

```bash
colameta remove my-project
```

## Requirements

- Python 3.10+
- Git
- A local executor environment, such as Codex or OpenCode

---

## License

This project is open source, but **commercial use is prohibited**.
