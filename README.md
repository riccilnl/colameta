```
   ______      __                      __     
  / ____/___  / /___ _____ ___  ____ _/ /____ 
 / /   / __ \/ / __ `/ __ `__ \/ __ `/ __/ _ \
/ /___/ /_/ / / /_/ / / / / / / /_/ / /_/  __/
\____/\____/_/\__,_/_/ /_/ /_/\__,_/\__/\___/ 

🥤 enjoy your vibe coding with GPTs! ✨
```

# ColaMeta

ColaMeta 是连接 ChatGPT / GPTs 和本地执行器的 AI coding workflow harness。

它不是另一个 coding agent，而是 GPTs 到本地开发环境之间的受控工作流层：GPTs 负责判断、分流和任务设计；Runner 负责版本计划、范围控制、preview / apply、验证审查和 Git 闭环；本地执行器负责真正读代码、改代码、跑测试。

## 安装

```bash
pip3 install colameta
```

安装后直接使用 `colameta` 命令：

```bash
colameta /path/to/your/project --public-base-url https://your-domain.com
colameta serve /path/to/your/project --auth-mode none --open
```

如果系统没有 `pip3` 命令，用 venv 隔离安装：

```bash
python3 -m venv path/to/venv
source path/to/venv/bin/activate
pip3 install colameta
```

## 快速开始

```bash
colameta /path/to/project source-only   # 只读模式
colameta /path/to/project managed       # 完整模式
colameta serve /path/to/project --open  # 启动 Web 控制台
```

默认本地地址：

- Web Console: `http://127.0.0.1:8799`
- MCP HTTP: `http://0.0.0.0:8765/mcp`

## 环境要求

- Python 3.10+
- Git

## 能力边界

- 不自动 push / merge / rebase / reset / clean
- 不暴露 token、API key 或 Bearer 值
- 所有写入操作必须经过 preview / apply 流程
- 提交和推送走受控链路，不绕过 preview

## 许可证

本项目开放源代码，但**禁止商业使用**。
