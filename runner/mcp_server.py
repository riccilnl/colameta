import json
import copy
import os
import re
import sys
import time
import hashlib
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from runner.http_server_utils import ReusableThreadingHTTPServer
from runner.mcp_oauth import MCPOAuthProvider, default_server_oauth_store_file
from runner.planning_bridge import PlanningBridge, PlanningBridgeError
from runner.source_review_bridge import SourceReviewBridge, SourceReviewError
from runner.executor_inventory import load_executor_inventory
from runner.executor_run_reports import ExecutorRunReportStore
from runner.executor_session import ExecutorSessionStore
from runner.project_identity import build_project_identity
from runner.project_registry import ProjectRegistry
from runner.execution_standards import get_execution_standards
from runner.plan_standards_linter import PlanStandardsLinter
from runner.mcp_git_commit import MCPGitCommitManager
from runner.mcp_git_remote import MCPGitRemoteManager
from runner.mcp_runner_plan import MCPRunnerPlanManager
from runner.mcp_decisions import MCPDecisionRecordsManager
from runner.mcp_project_memory import MCPProjectMemoryManager
from runner.mcp_todolist import MCPTodoListManager
from runner.mcp_project_patch import MCPProjectPatchManager
from runner.mcp_git_history import MCPGitHistoryManager
from runner.mcp_plan_workflow import MCPPlanWorkflowManager
from runner.mcp_project_docs import MCPProjectDocsManager
from runner.mcp_workflow_router import MCPWorkflowRouter
from runner.core_orchestrator import WorkflowOrchestrator
from runner.core_workflow_registry import SUPPORTED_CORE_WORKFLOWS, normalize_workflow_name, is_supported_core_workflow
from runner.mcp_executor_workflow import MCPExecutorWorkflowManager
from runner.mcp_executor_config import MCPExecutorConfigManager
from runner.mcp_validation_run import MCPValidationRunManager
from runner.executor_read import handle_inspect_executor_activity
from runner.workflow_engine import should_record_tool, record_tool_call
from runner.workflow_records import WorkflowRecordStore
from runner.runner_paths import (
    is_project_runner_path,
    resolve_project_runner_dir,
    resolve_project_runner_path,
    resolve_project_runner_plan_path,
    resolve_project_runner_rel_dir,
)


MCP_EXPOSURE_PROFILE_ENV = "MCP_EXPOSURE_PROFILE"
MCP_EXPOSURE_PROFILE_NORMAL = "normal"
MCP_EXPOSURE_PROFILE_MAINTAINER = "maintainer"
MCP_EXPOSURE_PROFILE_LEGACY = "legacy"
ACTIONS_API_PREFIX = "/api/"
ACTIONS_TARGET_RESPONSE_CHARS = 60000
ACTIONS_HARD_RESPONSE_CHARS = 75000
ACTIONS_HARD_REQUEST_CHARS = 90000
MCP_TARGET_TOOL_RESULT_CHARS = 60000
MCP_HARD_TOOL_RESULT_CHARS = 75000

NORMAL_EXPOSED_TOOLS = (
    "list_registered_projects",
    "analyze_project_state",
    "run_mcp_workflow",
    "manage_executor_config",
    "manage_executor_workflow",
    "manage_validation_run",
    "manage_git",
    "manage_project_docs",
    "manage_prompt_file",
    "manage_workflow_run",
    "get_runner_execution_standards",
    "get_plan_standards_report",
    "manage_files",
    "manage_runner_plan",
    "manage_project_memory",
    "manage_plan_version",
    "list_executor_run_reports",
    "get_executor_run_report",
    "inspect_executor_activity",
)

MAINTAINER_EXTRA_TOOLS = (
    "get_project_identity",
    "get_runner_workbench_context",
)

LEGACY_EXTRA_TOOLS = (
    "get_runner_status",
    "get_plan_overview",
    "get_next_version_plan",
    "get_version_result",
    "get_project_doc_section",
    "get_plan_patch_status",
    "get_executor_session_status",
    "get_executor_continuation_preview",
    "get_executor_continuation_decision",
    "get_executor_resume_invocation_preview",
    "get_executor_inventory",
    "get_git_log",
    "get_repo_overview",
    "preview_insert_version",
    "preview_update_version",
    "manage_plan_workflow",
)

_PROFILE_ORDERS: dict[str, tuple[str, ...]] = {
    MCP_EXPOSURE_PROFILE_NORMAL: NORMAL_EXPOSED_TOOLS,
    MCP_EXPOSURE_PROFILE_MAINTAINER: NORMAL_EXPOSED_TOOLS + MAINTAINER_EXTRA_TOOLS,
    MCP_EXPOSURE_PROFILE_LEGACY: NORMAL_EXPOSED_TOOLS + MAINTAINER_EXTRA_TOOLS + LEGACY_EXTRA_TOOLS,
}


_SUPPORTED_MCP_WORKFLOWS = SUPPORTED_CORE_WORKFLOWS
_normalize_run_mcp_workflow_name = normalize_workflow_name


def _find_next_actions(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Extract next_actions list from a tool result dict, handling both flat and data-wrapped structures."""
    next_actions = result.get("next_actions")
    if isinstance(next_actions, list):
        return next_actions
    data = result.get("data")
    if isinstance(data, dict):
        next_actions = data.get("next_actions")
        if isinstance(next_actions, list):
            return next_actions
    return None


PROJECT_NAME_REQUIRED_TOOLS = {
    "get_plan_standards_report",
    "get_review_context",
    "manage_project_memory",
    "manage_git",
    "manage_git_commit",
    "manage_git_remote",
    "todo_read",
    "todo_add",
    "todo_update",
    "todo_delete",
    "decision_read",
    "decision_add",
    "decision_update",
    "decision_delete",
    "manage_plan_version",
    "manage_git_history",
    "manage_project_docs",
    "manage_prompt_file",
    "manage_files",
    "get_git_status",
    "get_git_diff",
    "list_executor_run_reports",
    "get_executor_run_report",
    "inspect_executor_activity",
    "analyze_project_state",
    "run_mcp_workflow",
    "manage_executor_config",
    "manage_executor_workflow",
    "manage_validation_run",
    "manage_workflow_run",
    "list_workflow_runs",
    "get_workflow_run",
}


def _parse_prompt_front_matter(content: str) -> tuple[dict[str, Any], str | None]:
    if not content:
        return {}, None
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, content
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, None
    raw = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    fm: dict[str, Any] = {}
    stack: list[tuple[str, Any, int]] = []
    for line in raw.split("\n"):
        stripped_line = line.lstrip()
        indent = len(line) - len(stripped_line)
        if not stripped_line or stripped_line.startswith("#"):
            continue
        list_match = re.match(r"^-\s+(.+)$", stripped_line)
        kv_match = re.match(r"^(\w[\w-]*):\s*(.*)$", stripped_line)
        if list_match:
            val = list_match.group(1).strip()
            while stack and stack[-1][2] >= indent:
                stack.pop()
            if stack:
                parent_key, parent_dict, _ = stack[-1]
                if not isinstance(parent_dict.get(parent_key), list):
                    parent_dict[parent_key] = []
                parent_dict[parent_key].append(val)
        elif kv_match:
            key = kv_match.group(1)
            raw_val = kv_match.group(2).strip()
            val: Any = raw_val
            val_lower = raw_val.lower()
            if val_lower in ("true", "yes"):
                val = True
            elif val_lower in ("false", "no"):
                val = False
            while stack and stack[-1][2] >= indent:
                stack.pop()
            target: dict[str, Any] = fm
            if stack:
                parent_key, parent_dict, _ = stack[-1]
                parent_val = parent_dict.get(parent_key)
                if isinstance(parent_val, dict):
                    target = parent_val
                else:
                    parent_dict[parent_key] = {}
                    target = parent_dict[parent_key]
            if raw_val == "":
                target[key] = {}
                stack.append((key, target, indent))
            else:
                target[key] = val
                stack.append((key, target, indent))
    return fm, body


@dataclass
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


@dataclass
class MCPToolInputError(Exception):
    error_code: str
    message: str
    details: dict[str, Any] | None = None


class MCPPlanningBridgeServer:
    def __init__(self, project_path: str, *, service_mode: bool = False):
        self.project_root = os.path.abspath(os.path.expanduser(project_path))
        self.service_mode = service_mode
        self.project_registry = ProjectRegistry()
        self.mcp_exposure_profile = self._get_exposure_profile()
        self.bridge = PlanningBridge()
        self.source_review = SourceReviewBridge()
        if self.service_mode:
            self.project_identity = {"service": "colameta-mcp", "routing": "registry"}
            self.project_hint = "ColaMeta Service"
        else:
            self.project_identity = build_project_identity(self.project_root)
            self.project_hint = self.project_identity.get("mcp_display_hint", f"Project:{os.path.basename(self.project_root)}")
        common_output_schema = self._build_common_output_schema()
        self.tools = {
            "list_registered_projects": self._tool_list_registered_projects,
            "get_runner_status": self._tool_get_runner_status,
            "get_version_result": self._tool_get_version_result,
            "get_next_version_plan": self._tool_get_next_version_plan,
            "get_plan_overview": self._tool_get_plan_overview,
            "get_review_context": self._tool_get_review_context,
            "get_runner_workbench_context": self._tool_get_runner_workbench_context,
            "get_project_doc_section": self._tool_get_project_doc_section,
            "preview_insert_version": self._tool_preview_insert_version,
            "preview_update_version": self._tool_preview_update_version,
            "get_plan_patch_status": self._tool_get_plan_patch_status,
            "get_repo_overview": self._tool_get_repo_overview,
            "get_git_status": self._tool_get_git_status,
            "get_git_log": self._tool_get_git_log,
            "manage_files": self._tool_manage_files,
            "get_source_file": self._tool_get_source_file,
            "search_source": self._tool_search_source,
            "get_git_diff": self._tool_get_git_diff,
            "get_executor_inventory": self._tool_get_executor_inventory,
            "get_project_identity": self._tool_get_project_identity,
            "get_runner_execution_standards": self._tool_get_runner_execution_standards,
            "get_plan_standards_report": self._tool_get_plan_standards_report,
            "get_executor_session_status": self._tool_get_executor_session_status,
            "get_executor_continuation_preview": self._tool_get_executor_continuation_preview,
            "get_executor_continuation_decision": self._tool_get_executor_continuation_decision,
            "get_executor_resume_invocation_preview": self._tool_get_executor_resume_invocation_preview,
            "manage_git": self._tool_manage_git,
            "manage_git_commit": self._tool_manage_git_commit,
            "manage_git_remote": self._tool_manage_git_remote,
            "manage_runner_plan": self._tool_manage_runner_plan,
            "manage_runner_record": self._tool_manage_runner_record,
            "manage_project_memory": self._tool_manage_project_memory,
            "manage_workflow_run": self._tool_manage_workflow_run,
            "todo_read": self._tool_todo_read,
            "todo_add": self._tool_todo_add,
            "todo_update": self._tool_todo_update,
            "todo_delete": self._tool_todo_delete,
            "decision_read": self._tool_decision_read,
            "decision_add": self._tool_decision_add,
            "decision_update": self._tool_decision_update,
            "decision_delete": self._tool_decision_delete,
            "list_executor_run_reports": self._tool_list_executor_run_reports,
            "get_executor_run_report": self._tool_get_executor_run_report,
            "analyze_project_state": self._tool_analyze_project_state,
            "manage_plan_version": self._tool_manage_plan_version,
            "manage_project_patch": self._tool_manage_project_patch,
            "manage_git_history": self._tool_manage_git_history,
            "manage_plan_workflow": self._tool_manage_plan_workflow,
            "manage_project_docs": self._tool_manage_project_docs,
            "manage_prompt_file": self._tool_manage_prompt_file,
            "run_mcp_workflow": self._tool_run_mcp_workflow,
            "manage_executor_config": self._tool_manage_executor_config,
            "inspect_executor_activity": self._tool_inspect_executor_activity,
            "manage_executor_workflow": self._tool_manage_executor_workflow,
            "manage_validation_run": self._tool_manage_validation_run,
            "list_workflow_runs": self._tool_list_workflow_runs,
            "get_workflow_run": self._tool_get_workflow_run,
        }
        self.tool_defs = [
            MCPToolDef(
                name="list_registered_projects",
                description=f"[{self.project_hint}] 列出本地 registry 中已登记项目。只接受本地 allowlist 项目，不解析任意 project_root。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_project_identity",
                description=f"[{self.project_hint}] 读取当前 MCP 绑定项目的身份标识，可用于在多项目 MCP 间确认上下文。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目身份。",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_plan_standards_report",
                description="Read a structured lint report for the current Runner plan before generating or updating plan patches. If blocking_issue_count > 0, do not call preview_insert_version or preview_update_version except to fix those plan issues.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 plan 标准报告。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_runner_execution_standards",
                description="Read Runner execution standards before generating initial plans, plan.json, plan patches, prompts, fix prompts, diff reviews, or low-cost executor instructions. Includes bootstrap_plan, strict plan_format, and acceptance_commands rules.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "Optional section name (bootstrap_plan, plan_format, version_prompt, fix_prompt, plan_patch, diff_review, execution_branch, commit_review, low_cost_executor, executor_selection_strategy). Defaults to all.",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_runner_status",
                description=f"[{self.project_hint}] 读取 Runner 当前状态",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目状态。",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_executor_session_status",
                description="Read the current project-scoped executor session manifest. This is read-only and does not resume, reset, or modify executor sessions.",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_executor_continuation_preview",
                description="Read a read-only continuation preview for the current project executor session. This does not resume, reset, modify files, or call any executor.",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_executor_continuation_decision",
                description="Read a read-only continuation decision for the requested executor provider. This does not resume, reset, modify files, or call any executor.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": ["pi", "codex", "opencode"],
                            "description": "Executor provider to evaluate continuation decision.",
                        }
                    },
                    "required": ["provider"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_executor_resume_invocation_preview",
                description="Read a read-only provider-specific resume invocation preview for the requested executor provider. This does not resume, reset, modify files, or call any executor.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": ["pi", "codex", "opencode"],
                            "description": "Executor provider to inspect invocation preview.",
                        }
                    },
                    "required": ["provider"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_review_context",
                description=f"[{self.project_hint}] Read a bundled review context for validating recent changes before telling the user whether a version can be committed. This is read-only and never stages, resets, cleans, or commits.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "max_diff_chars": {
                            "type": "integer",
                            "description": "Maximum characters for git diff. Defaults to 60000 and is capped at 120000.",
                        },
                        "include_log": {
                            "type": "boolean",
                            "description": "Whether to include recent git log. Defaults to true.",
                        },
                        "log_limit": {
                            "type": "integer",
                            "description": "Recent commit count when include_log is true. Defaults to 5 and is capped at 20.",
                        },
                        "include_repo_overview": {
                            "type": "boolean",
                            "description": "Whether to include repo overview/file tree. Defaults to false.",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "Maximum file entries for repo overview when included. Defaults to 200 and is capped at 500.",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 review context。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_runner_workbench_context",
                description=f"[{self.project_hint}] Read a bundled workbench context for quickly understanding Runner status, plan state, executor continuation, and git status. Partial failures are returned per section.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "include_runner_state": {
                            "type": "boolean",
                            "description": "Whether to include runner status, current version result, next plan, and plan overview. Defaults to true.",
                        },
                        "include_executor": {
                            "type": "boolean",
                            "description": "Whether to include executor session and continuation preview. Defaults to true.",
                        },
                        "include_git_status": {
                            "type": "boolean",
                            "description": "Whether to include git status. Defaults to true.",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["pi", "codex", "opencode"],
                            "description": "Optional provider for continuation decision and resume invocation preview.",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_git",
                description=(
                    f"[{self.project_hint}] 统一 Git 工具。"
                    "通过 action 路由到受控 Git 子操作。"
                    "支持 project_name 路由到已登记 managed 项目。"
                    "此工具不会执行任意 Git 命令，不会绕过 preview 审批。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "status",
                                "diff",
                                "review_context",
                                "commit_readiness",
                                "commit_message",
                                "commit_preview",
                                "commit_apply",
                                "push_status",
                                "push_preview",
                                "push_apply",
                                "pull_status",
                                "pull_preview",
                                "pull_apply",
                                "history_log",
                                "history_show",
                                "diff_commits",
                                "restore_file_preview",
                                "restore_file_apply",
                                "revert_preview",
                                "revert_apply",
                            ],
                            "description": "Git domain action. Routes to existing Git capability.",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由目标项目。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "apply 类 action 必填。来自对应 preview 的 preview_id。",
                        },
                        "message": {
                            "type": "string",
                            "description": "commit_preview/commit_apply 的提交信息。",
                        },
                        "commit": {
                            "type": "string",
                            "description": "history_show/restore_file_preview/revert_preview 的 commit ref。",
                        },
                        "base": {
                            "type": "string",
                            "description": "diff_commits 的基础 commit。",
                        },
                        "head": {
                            "type": "string",
                            "description": "diff_commits 的目标 commit。",
                        },
                        "file": {
                            "type": "string",
                            "description": "restore_file_preview/diff_commits 的文件路径。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "history_log 返回 commit 数量。默认 12，最大 50。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "diff/history_show/diff_commits/revert_preview 的 diff 字符限制。默认 40000，最大 80000。",
                        },
                        "include_diff_summary": {
                            "type": "boolean",
                            "description": "commit_readiness/commit_message 是否包含 diff 摘要。默认 true。",
                        },
                        "max_diff_chars": {
                            "type": "integer",
                            "description": "commit_readiness/commit_message 的 diff 字符限制。默认 40000，最大 80000。",
                        },
                        "style": {
                            "type": "string",
                            "enum": ["conventional", "runner_version", "concise"],
                            "description": "commit_message 可选。commit message 风格倾向。默认 runner_version。",
                        },
                        "scope_hint": {
                            "type": "string",
                            "description": "commit_message 可选。版本号或 scope 提示。",
                        },
                        "include_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。commit_readiness/commit_message 指定的文件子集。",
                        },
                        "exclude_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。commit_readiness/commit_message 排除的文件。",
                        },
                        "include_patch": {
                            "type": "boolean",
                            "description": "history_show 是否包含 patch。默认 true。",
                        },
                        "include_log": {
                            "type": "boolean",
                            "description": "review_context 是否包含 git log。默认 true。",
                        },
                        "log_limit": {
                            "type": "integer",
                            "description": "review_context 的 log 数量。默认 5，最大 20。",
                        },
                        "include_repo_overview": {
                            "type": "boolean",
                            "description": "review_context 是否包含 repo overview。默认 false。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "review_context 的 repo overview 最大文件数。默认 200，最大 500。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选。preview 类动作的理由说明。",
                        },
                        "scan_limit": {
                            "type": "integer",
                            "description": "reconcile_git_history_preview 可选。扫描最近 N 个 commit，默认 20，最大 100。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_git_commit",
                description=f"[{self.project_hint}] Manage a controlled git commit flow with readiness, suggest_commit_message, commit_workflow_preview, preview, and commit actions. 支持按已登记 managed project_name 路由目标项目。This tool never runs arbitrary shell, never exposes arbitrary git commands, and never stages all files at once.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["readiness", "suggest_commit_message", "commit_workflow_preview", "preview", "commit"],
                            "description": "Commit workflow action.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Commit message for preview, commit_workflow_preview, or commit. Required for preview; optional for commit if matching preview message is stored.",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "Preview id returned by action=preview or commit_workflow_preview. Required for action=commit.",
                        },
                        "include_diff_summary": {
                            "type": "boolean",
                            "description": "Whether readiness/preview should include a bounded diff summary. Defaults to true.",
                        },
                        "max_diff_chars": {
                            "type": "integer",
                            "description": "Maximum diff characters to include in readiness/preview. Defaults to 40000 and is capped at 80000.",
                        },
                        "style": {
                            "type": "string",
                            "enum": ["conventional", "runner_version", "concise"],
                            "description": "suggest_commit_message 可选。commit message 风格倾向。默认 runner_version。",
                        },
                        "scope_hint": {
                            "type": "string",
                            "description": "suggest_commit_message 可选。版本号或 scope 提示，例如 v1.73。",
                        },
                        "include_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选，仅用于 readiness/suggest_commit_message/commit_workflow_preview/preview。指定要提交的文件子集。",
                        },
                        "exclude_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选，仅用于 readiness/suggest_commit_message/commit_workflow_preview/preview。用于从选择结果中排除文件。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由 readiness、suggest_commit_message、commit_workflow_preview、preview、commit。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_git_remote",
                description=f"[{self.project_hint}] 受控 Git remote 工具。支持 push、fetch preview/apply 与 fast-forward pull preview/apply。project_name 当前支持已登记 managed 项目的 push_status、push_preview、push_apply。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "push_status",
                                "push_preview",
                                "push_apply",
                                "fetch_preview",
                                "fetch_apply",
                                "pull_status",
                                "pull_preview",
                                "pull_apply",
                            ],
                            "description": "Git remote action.",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "apply 类 action 必填。来自对应 preview 的 preview_id。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选。预览原因说明。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由 push_status、push_preview、push_apply。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_runner_plan",
                description=f"[{self.project_hint}] Manage controlled Runner plan onboarding for the bound source project with inspect, preview, and apply actions. bootstrap_preview project_name is the new plan name, not a registry routing key. This never writes arbitrary files and does not use paste-plan UI.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["inspect", "bootstrap_preview", "import_preview", "apply"],
                            "description": "Runner plan management action.",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "bootstrap_preview 必填。新建 plan.json 的 project_name；仅用于命名当前绑定 source 项目，不按 registry 路由。",
                        },
                        "plan_json": {
                            "type": "string",
                            "description": "Full plan JSON string for import_preview. Intended for MCP/ChatGPT structured import, not Web paste UI.",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "Preview id returned by bootstrap_preview or import_preview. Required for apply.",
                        },
                        "allow_overwrite": {
                            "type": "boolean",
                            "description": "Whether apply can overwrite an existing .colameta/plan.json. Defaults to false.",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_project_memory",
                description=f"[{self.project_hint}] 统一项目记忆工具。支持 record_type=memory|todo|decision 与 action=read|add|update|delete。memory 记录 GPTs 长期记忆，todo 记录后续事项，decision 记录已确认决策。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "record_type": {
                            "type": "string",
                            "enum": ["memory", "todo", "decision"],
                            "description": "记忆类型。memory=GPTs 长期记忆；todo=后续事项；decision=已确认决策。",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["read", "add", "update", "delete"],
                            "description": "记忆操作。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由到目标项目记忆。",
                        },
                        "id": {
                            "type": "string",
                            "description": "todo/decision update/delete 必填；memory 不使用。",
                        },
                        "include_done": {
                            "type": "boolean",
                            "default": False,
                            "description": "仅 todo read 有意义。是否包含 done 条目。",
                        },
                        "content": {
                            "type": "string",
                            "description": "todo add/update 的内容；memory add/update 的完整 Markdown 内容。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "仅 memory read 有意义。返回内容字符上限，默认 30000，最大 120000。",
                        },
                        "status": {
                            "type": "string",
                            "description": "todo 或 decision 的状态。具体允许值由底层记录类型校验。",
                        },
                        "title": {
                            "type": "string",
                            "description": "decision add/update 的标题。",
                        },
                        "decision": {
                            "type": "string",
                            "description": "decision add/update 的决策内容。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "decision add/update 的原因。",
                        },
                        "related_versions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "decision add/update 的相关版本列表。",
                        },
                    },
                    "required": ["record_type", "action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_runner_record",
                description=f"[{self.project_hint}] 统一项目记录工具。支持 record_type=todo|decision 与 action=read|add|update|delete，内部复用现有 todo/decision 实现与校验。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "record_type": {
                            "type": "string",
                            "enum": ["todo", "decision"],
                            "description": "记录类型。",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["read", "add", "update", "delete"],
                            "description": "记录操作。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由到目标项目记录。",
                        },
                        "id": {
                            "type": "string",
                            "description": "update/delete 必填；todo/decision 记录 id。",
                        },
                        "include_done": {
                            "type": "boolean",
                            "default": False,
                            "description": "仅 todo read 有意义。是否包含 done 条目。",
                        },
                        "content": {
                            "type": "string",
                            "description": "todo add/update 的内容。",
                        },
                        "status": {
                            "type": "string",
                            "description": "todo 或 decision 的状态。具体允许值由底层记录类型校验。",
                        },
                        "title": {
                            "type": "string",
                            "description": "decision add/update 的标题。",
                        },
                        "decision": {
                            "type": "string",
                            "description": "decision add/update 的决策内容。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "decision add/update 的原因。",
                        },
                        "related_versions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "decision add/update 的相关版本列表。",
                        },
                    },
                    "required": ["record_type", "action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_workflow_run",
                description=f"[{self.project_hint}] 统一 workflow run 查询工具。支持 action=list|get，内部复用现有 workflow record 列表与详情读取实现。支持 project_name 路由到已登记 managed 项目。scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "get"],
                            "description": "查询操作。",
                        },
                        "workflow_id": {
                            "type": "string",
                            "description": "action=get 必填；workflow_id。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "action=list 可选。最大返回条数。默认 20，最大 100。",
                        },
                        "workflow_name": {
                            "type": "string",
                            "description": "action=list 可选。按 workflow_name 筛选。",
                        },
                        "status": {
                            "type": "string",
                            "description": "action=list 可选。按 status 筛选（running/succeeded/failed/partial/unsupported）。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 workflow records。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="todo_read",
                description=f"[{self.project_hint}] 读取 .colameta/todolist.json，可选只看 planned 项或包含 done 项。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "include_done": {
                            "type": "boolean",
                            "default": False,
                            "description": "是否包含 done 条目。默认只返回 planned 条目。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 todolist。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="todo_add",
                description=f"[{self.project_hint}] 追加一条需求备忘录，可选指定 status。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "需求压缩描述。",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["planned", "done"],
                            "description": "条目状态。默认 planned。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由写入目标项目 todolist。",
                        },
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="todo_update",
                description=f"[{self.project_hint}] 按 id 更新一条需求备忘录内容或状态，保留原 id 和 created_at。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "todo id。",
                        },
                        "content": {
                            "type": "string",
                            "description": "更新后的需求压缩描述。",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["planned", "done"],
                            "description": "更新后的条目状态。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由更新目标项目 todolist。",
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="todo_delete",
                description=f"[{self.project_hint}] 按 id 删除一条需求备忘录。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "todo id。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由删除目标项目 todolist。",
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="decision_read",
                description=f"[{self.project_hint}] 读取 .colameta/decisions.json，返回已记录的产品或架构决策。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 decisions。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="decision_add",
                description=f"[{self.project_hint}] 追加一条已接受的产品或架构决策记录。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "决策标题。",
                        },
                        "decision": {
                            "type": "string",
                            "description": "决策内容。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "决策原因。",
                        },
                        "related_versions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "相关版本列表。",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["active", "superseded", "rejected"],
                            "description": "决策状态。默认 active。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由写入目标项目 decisions。",
                        },
                    },
                    "required": ["title", "decision", "reason"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="decision_update",
                description=f"[{self.project_hint}] 按 id 更新决策记录内容、原因、相关版本或状态。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "decision id。",
                        },
                        "title": {
                            "type": "string",
                            "description": "更新后的决策标题。",
                        },
                        "decision": {
                            "type": "string",
                            "description": "更新后的决策内容。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "更新后的决策原因。",
                        },
                        "related_versions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "更新后的相关版本列表。",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["active", "superseded", "rejected"],
                            "description": "更新后的决策状态。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由更新目标项目 decisions。",
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="decision_delete",
                description=f"[{self.project_hint}] 按 id 删除一条决策记录。支持 project_name 路由到已登记 managed 项目。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "decision id。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由删除目标项目 decisions。",
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_plan_version",
                description=f"[{self.project_hint}] 结构化 Runner plan 版本管理工具。支持 inspect、insert/update/repair preview、insert_from_prompt_file_preview、apply_preview_status、apply_preview、reload_plan、continue_next_version。reload_plan/continue_next_version 会同步 state.json。project_name 支持已登记 managed 项目的 preview、status、apply_preview、reload_plan、continue_next_version 路由。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["inspect", "insert_preview", "update_preview", "repair_preview", "apply_preview_status", "insert_from_prompt_file_preview", "apply_preview", "reload_plan", "continue_next_version"],
                            "description": "Plan version 管理操作。reload_plan 会重载 plan 并同步 state.json；continue_next_version 会在当前版本通过后推进到下一版本；apply_preview 受控应用 plan patch。",
                        },
                        "patch_id": {
                            "type": "string",
                            "description": "apply_preview_status 或 apply_preview 操作需要的 patch_id。",
                        },
                        "insert_after": {
                            "type": "string",
                            "description": "insert_preview 操作需要。在此版本后插入新版本。",
                        },
                        "version": {
                            "type": "string",
                            "description": "insert_preview（新版本号）或 update_preview（目标版本号）或 repair_preview（可选版本过滤）。",
                        },
                        "name": {
                            "type": "string",
                            "description": "insert_preview 必填。版本显示名称。",
                        },
                        "description": {
                            "type": "string",
                            "description": "insert_preview 必填。版本描述。",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "insert_preview 必填。版本 prompt 内容。update_preview 可选更新 prompt。",
                        },
                        "allowed_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "insert_preview 必填。版本允许修改的文件模式列表。不能为空。",
                        },
                        "acceptance_commands": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "object",
                                     "properties": {
                                         "command": {"type": "string"},
                                         "timeout_seconds": {"type": "integer"},
                                         "continue_on_failure": {"type": "boolean"},
                                     },
                                     "required": ["command"],
                                     "additionalProperties": False,
                                    },
                                ],
                            },
                            "description": "insert_preview 必填。版本验收命令列表。可以是 string 或 object（command/timeout_seconds/continue_on_failure）。不允许空列表。",
                        },
                        "manual_acceptance": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。手动验收检查项列表。",
                        },
                        "out_of_scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。此版本不包含的范围说明列表。",
                        },
                        "context_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。版本上下文文件模式列表。",
                        },
                        "forbidden_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。版本禁止修改的文件模式列表。",
                        },
                        "allow_no_changes": {
                            "type": "boolean",
                            "description": "可选。read-only/audit 版本设置为 true 后，可在验收通过且无 allowed_files diff 时通过。默认 false 仍阻断无变更。",
                        },
                        "execution": {
                            "type": "object",
                            "description": "可选。版本执行器配置。provider 必须是 pi/codex/opencode。",
                            "properties": {
                                "provider": {
                                    "type": "string",
                                    "enum": ["pi", "codex", "opencode"],
                                    "description": "执行器 provider。",
                                },
                            },
                            "additionalProperties": True,
                        },
                        "prompt_file": {
                            "type": "string",
                            "description": "insert_preview 可选。覆盖默认 prompt 文件名。insert_from_prompt_file_preview 必填。prompt 文件相对路径，仅文件名，例如 v1.84.54.md。",
                        },
                        "repair_kinds": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["acceptance_command_shape", "invalid_provider", "missing_optional_safety_fields", "prompt_file_safety"],
                            },
                            "description": "repair_preview 可选。指定需要修复的种类；不传时自动检测所有可修复项。",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "repair_preview 可选。是否只做检查不生成 patch。默认 true。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由所有支持动作：insert_preview、update_preview、repair_preview、apply_preview_status、insert_from_prompt_file_preview、apply_preview、reload_plan、continue_next_version。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_project_patch",
                description=f"[{self.project_hint}] 通用小范围非文档文件的受控 patch 工具（源码、脚本、配置、测试数据）。README.md、AGENTS.md、docs/*.md 请优先使用 manage_project_docs。只有用户明确给出 exact old_text/new_text 或非文档通用 patch 时，才用本工具。scope：status=mcp:read，preview=mcp:preview，apply=mcp:commit。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["preview", "apply", "status"],
                            "description": "Patch 操作。preview 预览改动（不写文件），apply 应用 preview（写文件），status 查询 preview 状态。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "apply 或 status 操作需要的 preview_id。",
                        },
                        "file": {
                            "type": "string",
                            "description": "精确替换模式的相对文件路径。",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "精确替换模式的旧文本。必须在文件中唯一。",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "精确替换模式的新文本。可以为空字符串。",
                        },
                        "patch_text": {
                            "type": "string",
                            "description": "unified diff 模式的 patch 文本。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选。patch 理由说明。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "可选。最大文件数。默认 5，最大 5。",
                        },
                        "max_diff_chars": {
                            "type": "integer",
                            "description": "可选。最大 diff 字符数。默认 20000，最大 20000。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由所有操作。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_git_history",
                description=f"[{self.project_hint}] 受控 Git 历史管理工具。支持 log（查看历史）、show（查看 commit 详情）、diff_commits（对比 commit）、reconcile_git_history_preview（扫描 direct version 候选）、restore_file_preview（恢复文件预览）、restore_file_apply（恢复文件）、revert_preview（撤销预览）、revert_apply（受控撤销应用，必须使用 revert_preview 返回的 preview_id，不自动 commit，冲突时不自动解决）。不提供 reset/clean/push/merge/rebase。scope：log/show/diff_commits=mcp:read，reconcile_git_history_preview/restore_file_preview/revert_preview=mcp:preview，restore_file_apply/revert_apply=mcp:commit。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["log", "show", "diff_commits", "reconcile_git_history_preview", "restore_file_preview", "restore_file_apply", "revert_preview", "revert_apply"],
                            "description": "Git history 操作。",
                        },
                        "commit": {
                            "type": "string",
                            "description": "show、restore_file_preview、revert_preview 使用的 commit ref。",
                        },
                        "base": {
                            "type": "string",
                            "description": "diff_commits 的基础 commit。",
                        },
                        "head": {
                            "type": "string",
                            "description": "diff_commits 的目标 commit。",
                        },
                        "file": {
                            "type": "string",
                            "description": "restore_file_preview 必填的相对文件路径；diff_commits 可选过滤文件。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "restore_file_apply/revert_apply 使用的 preview_id。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "log 返回 commit 数量。默认 12，最大 50。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "show/diff_commits/revert_preview 的 diff 字符限制。默认 40000，最大 80000。",
                        },
                        "include_patch": {
                            "type": "boolean",
                            "description": "show 是否包含 patch。默认 true。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选。preview 类动作的理由说明。",
                        },
                        "scan_limit": {
                            "type": "integer",
                            "description": "reconcile_git_history_preview 可选。扫描最近 N 个 commit，默认 20，最大 100。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由所有操作。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_plan_workflow",
                description=f"[{self.project_hint}] [已弃用/legacy] 受控 Plan Workflow 自动化工具。此工具仅用于兼容旧流程，新流程请使用 manage_runner_plan（source-only 纳管）或 manage_plan_version（版本管理）。支持 source_onboarding_preview（从源码项目自动生成 onboarding 预览）、plan_repair_preview（lint 修复预览）、plan_extend_preview（扩展新版本预览）。project_name 当前仅支持已登记 managed 项目的 plan_repair_preview、plan_extend_preview。scope=mcp:preview。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["source_onboarding_preview", "plan_repair_preview", "plan_extend_preview"],
                            "description": "Plan workflow action。",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "source_onboarding_preview 和 plan_repair_preview 支持 dry_run=true 只做分析不生成 patch。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "source_onboarding_preview 可选。仓库文件树最大文件数。默认 300，最大 500。",
                        },
                        "version": {
                            "type": "string",
                            "description": "plan_repair_preview 可选版本过滤；plan_extend_preview 新版本号。",
                        },
                        "repair_kinds": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["acceptance_command_shape", "invalid_provider", "missing_optional_safety_fields", "prompt_file_safety"],
                            },
                            "description": "plan_repair_preview 可选。指定修复种类。",
                        },
                        "insert_after": {
                            "type": "string",
                            "description": "plan_extend_preview 可选。在此版本后插入。",
                        },
                        "name": {
                            "type": "string",
                            "description": "plan_extend_preview 可选。版本名称。",
                        },
                        "description": {
                            "type": "string",
                            "description": "plan_extend_preview 可选。版本描述。",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "plan_extend_preview 可选。版本 prompt。不传则自动生成。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "source_onboarding_preview 单项目模式下可选，覆盖项目名称。不传则自动推断。project_name 路由模式下仅支持已登记 managed 项目的 plan_repair_preview、plan_extend_preview。",
                        },
                        "goal": {
                            "type": "string",
                            "description": "source_onboarding_preview 可选。覆盖项目目标。不传则自动推断。",
                        },
                        "first_version": {
                            "type": "string",
                            "description": "source_onboarding_preview 可选。首版本号。默认 v1.0。",
                        },
                        "first_version_name": {
                            "type": "string",
                            "description": "source_onboarding_preview 可选。首版本显示名称。默认 Adopt existing project into Runner。",
                        },
                        "target_version": {
                            "type": "string",
                            "description": "manage_plan_workflow 可选。目标版本号，用于 plan_repair_preview。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "manage_plan_workflow 可选。操作理由说明，进入 workflow record。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_project_docs",
                description=f"[{self.project_hint}] 文档语义层工具。创建或修改 README.md、AGENTS.md、docs/*.md 时优先使用。支持 index、search、read_section、update_section_preview、append_section_preview（支持创建新文件）、sync_docs_preview、apply。底层复用 manage_project_patch。scope：index/search/read_section=mcp:read，preview 类=mcp:preview，apply=mcp:commit。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["index", "search", "read_section", "update_section_preview", "append_section_preview", "sync_docs_preview", "apply"],
                            "description": "Docs management action。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由文档索引、读取、搜索、预览和 apply。",
                        },
                        "file": {
                            "type": "string",
                            "description": "read_section/update_section_preview/append_section_preview 使用的文件路径。只允许 README.md、AGENTS.md、docs/*.md。",
                        },
                        "heading": {
                            "type": "string",
                            "description": "read_section/update_section_preview 使用的 Markdown heading。",
                        },
                        "query": {
                            "type": "string",
                            "description": "search 使用的搜索关键词。",
                        },
                        "new_content": {
                            "type": "string",
                            "description": "update_section_preview 使用的 section body 新内容（不含 heading 行）。",
                        },
                        "section_heading": {
                            "type": "string",
                            "description": "append_section_preview 使用的新 section heading。",
                        },
                        "section_content": {
                            "type": "string",
                            "description": "append_section_preview 使用的新 section 内容。",
                        },
                        "after_heading": {
                            "type": "string",
                            "description": "append_section_preview 可选。指定在此 heading section 后追加。",
                        },
                        "stale_terms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "sync_docs_preview 可选。自定义过时术语列表。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "apply 使用的 preview_id。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "read/index/search 输出字符限制。默认 12000，最大 30000。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "index/search/sync_docs_preview 最大文件数。默认 50，最大 100。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "可选。操作理由，进入 workflow record 和底层 patch reason。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_prompt_file",
                description=(
                    f"[{self.project_hint}] 受控提示词文件保存工具。"
                    "支持 preview（预览）、apply（应用 preview 写入文件）、status（查询 preview 状态）、discard（废弃 preview artifact）。"
                    "文件写入 .colameta/prompts/{version}.md。"
                    "不运行执行器、不提交 Git、不修改 Runner plan。"
                    "project_name 支持已登记 managed 项目的 preview、apply、status、discard。"
                    "scope：status=mcp:read，preview=mcp:preview，discard=mcp:preview，apply=mcp:commit。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["preview", "apply", "status", "discard"],
                            "description": "Prompt file management action. discard 废弃 preview artifact，不写文件。",
                        },
                        "version": {
                            "type": "string",
                            "description": "preview 必填。版本号，用于生成文件名 .colameta/prompts/{version}.md。",
                        },
                        "content": {
                            "type": "string",
                            "description": "preview 必填。提示词正文。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "apply/status/discard 必填。来自 preview 的 preview_id。",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "preview 可选。是否允许覆盖已有文件。默认 false。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "preview 可选。操作理由。",
                        },
                        "max_preview_chars": {
                            "type": "integer",
                            "description": "preview 可选。content_preview 截断字符数。默认 200，最小 1，最大 5000。",
                        },
                        "allowed_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "preview 可选。自动写入 prompt front matter 的 allowed_files。",
                        },
                        "acceptance_commands": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "command": {"type": "string"},
                                            "timeout_seconds": {"type": "integer"},
                                            "continue_on_failure": {"type": "boolean"},
                                        },
                                        "required": ["command"],
                                        "additionalProperties": False,
                                    },
                                ],
                            },
                            "description": "preview 可选。自动写入 prompt front matter 的 acceptance_commands。",
                        },
                        "allow_no_changes": {
                            "type": "boolean",
                            "description": "preview 可选。自动写入 prompt front matter；read-only/audit 版本可在验收通过且无 allowed_files diff 时通过。",
                        },
                        "execution": {
                            "type": "object",
                            "properties": {
                                "provider": {
                                    "type": "string",
                                    "enum": ["pi", "codex", "opencode"],
                                    "description": "执行器 provider。",
                                },
                            },
                            "additionalProperties": False,
                            "description": "preview 可选。自动写入 prompt front matter 的 execution 配置。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由 prompt preview/apply/status/discard。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_version_result",
                description="读取指定版本或当前版本结果",
                input_schema={
                    "type": "object",
                    "properties": {
                        "version": {
                            "type": "string",
                            "description": "Version to inspect. Omit this field to inspect the current version.",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_next_version_plan",
                description="读取下一版本计划",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_plan_overview",
                description=f"[{self.project_hint}] 读取计划概览",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_project_doc_section",
                description="读取项目白名单文档中指定 heading 的段落内容。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Relative project document path, for example docs/Prompt.md.",
                        },
                        "heading": {
                            "type": "string",
                            "description": "Markdown heading or version label to extract, for example v1.1.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum characters to return. Defaults to 12000. Maximum 30000.",
                        },
                    },
                    "required": ["file", "heading"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="preview_insert_version",
                description="Preview insertion of a new version into the Runner plan. The spec_json string must be a JSON object with fields: insert_after, version, name, description, prompt, allowed_files, acceptance_commands, and optional manual_acceptance, out_of_scope, context_files. This only creates a pending patch and does not modify plan.json.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "spec_json": {
                            "type": "string",
                            "description": "JSON string for the version insertion spec. It must include insert_after, version, name, description, prompt, allowed_files, and acceptance_commands.",
                        }
                    },
                    "required": ["spec_json"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="preview_update_version",
                description="Preview update of an existing Runner version. The spec_json string must be a JSON object with version and at least one update field such as prompt, description, allowed_files, acceptance_commands, manual_acceptance, out_of_scope, context_files, or execution. This only creates a pending patch and does not modify plan.json.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "spec_json": {
                            "type": "string",
                            "description": "JSON string for the version update spec. It must include version and at least one update field such as prompt, description, allowed_files, acceptance_commands, manual_acceptance, out_of_scope, context_files, or execution.",
                        }
                    },
                    "required": ["spec_json"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_plan_patch_status",
                description="查询 patch 状态",
                input_schema={
                    "type": "object",
                    "properties": {"patch_id": {"type": "string"}},
                    "required": ["patch_id"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_repo_overview",
                description=f"[{self.project_hint}] 读取受控仓库概览，包括 git 状态、最近提交和安全过滤后的文件树。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目仓库概览。",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Maximum file tree depth. Defaults to 3.",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "Maximum number of file tree entries. Defaults to 300.",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_git_status",
                description=f"[{self.project_hint}] 读取 git status --short。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目 git 状态。",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_git_log",
                description="读取当前 MCP 绑定项目的最近提交记录，支持按 project_name 路由。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum commits to return. Defaults to 12 and is capped at 50.",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目提交记录。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_source_file",
                description="读取当前 MCP 绑定项目白名单源码文件的全文或指定行范围。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目源码文件。",
                        },
                        "file": {
                            "type": "string",
                            "description": "Relative source file path, for example runner/web_console.py.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum characters to return. Defaults to 30000 and is capped at 100000.",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "Optional 1-based start line.",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Optional 1-based end line.",
                        },
                    },
                    "required": ["file"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="search_source",
                description="在当前 MCP 绑定项目的白名单源码文件中搜索关键词。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由搜索目标项目源码。",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query, 1 to 120 characters.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum results to return. Defaults to 30 and is capped at 100.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_files",
                description=f"[{self.project_hint}] 统一项目文件搜索、读取与受控编辑工具。action=search 按关键词搜索白名单项目文件；action=read 读取指定文件内容；action=create/edit/delete 受控文件生命周期操作（委托 MCPProjectPatchManager），均需 phase=preview|apply|status。scope：search/read/status=mcp:read，preview=mcp:preview，apply=mcp:commit。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "read", "create", "edit", "delete"],
                            "description": "文件操作。search=搜索，read=读取，create=创建，edit=编辑，delete=删除。create/edit/delete 需要 phase=preview|apply|status。",
                        },
                        "phase": {
                            "type": "string",
                            "enum": ["preview", "apply", "status"],
                            "description": "action=create/edit/delete 必填。preview 预览改动（不写文件），apply 应用 preview（写文件），status 查询 preview 状态。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由到目标项目。",
                        },
                        "query": {
                            "type": "string",
                            "description": "action=search 必填。搜索关键词，1 到 120 字符。",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "action=search 可选。最大返回条数。默认 30，最大 100。",
                        },
                        "file": {
                            "type": "string",
                            "description": "action=read 或 action=create/edit/delete 必填。相对文件路径，例如 runner/web_console.py。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "action=read 可选。最大返回字符数。默认 30000，最大 100000。",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "action=read 可选。1-based 起始行号。",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "action=read 可选。1-based 结束行号。",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "action=edit phase=preview 精确替换模式的旧文本。必须在文件中唯一。",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "action=create/edit phase=preview 精确替换模式的新文本。create 时写入完整文件内容，edit 时替换 old_text。可以为空字符串。",
                        },
                        "patch_text": {
                            "type": "string",
                            "description": "action=edit phase=preview unified diff 模式的 patch 文本。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "action=create/edit/delete phase=apply 或 phase=status 需要。来自 preview 操作返回的 preview_id。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "action=create/edit/delete 可选。改动理由说明。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "action=edit phase=preview 可选。最大文件数。默认 5，最大 5。",
                        },
                        "max_diff_chars": {
                            "type": "integer",
                            "description": "action=create/edit/delete phase=preview 可选。最大 diff 字符数。默认 20000，最大 20000。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_git_diff",
                description=f"[{self.project_hint}] 读取 git diff，用于审查工作区改动。只返回白名单源码文件的 diff，过滤虚拟环境、本地运行态和敏感文件。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["diff", "summary", "file", "files", "page"],
                            "description": "可选。diff=默认聚合，summary=只返回 diff map，file=单文件，files=指定文件集合，page=单文件分页。",
                        },
                        "file": {
                            "type": "string",
                            "description": "可选。file/page 模式读取单个白名单源码文件 diff。",
                        },
                        "include_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。files 模式读取指定文件集合。",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "可选。file/page 模式分页偏移量，默认 0。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "最大字符数。默认 60000，最大 120000。",
                        },
                        "cached": {
                            "type": "boolean",
                            "description": "是否使用 --cached 查看暂存区 diff。默认 false。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目 diff。多项目环境建议显式指定。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_executor_inventory",
                description=f"[{self.project_hint}] 读取本地已保存的执行器 inventory，不触发探测，不执行任何命令。需要先通过 CLI probe-models 探测。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="list_executor_run_reports",
                description=f"[{self.project_hint}] 列出执行器完成报告。每次执行器执行完成后会自动保存结构化报告。支持按已登记 managed project_name 路由读取目标项目报告。只读，scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目报告列表。",
                        },
                        "version": {
                            "type": "string",
                            "description": "可选版本过滤。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最大返回数。默认 10，最大 50。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_executor_run_report",
                description=f"[{self.project_hint}] 读取执行器完成报告的详细内容。支持按已登记 managed project_name 路由读取目标项目报告。只读，scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目报告详情。",
                        },
                        "version": {
                            "type": "string",
                            "description": "可选版本。简化 latest=true 时可不传。",
                        },
                        "report_id": {
                            "type": "string",
                            "description": "可选报告 ID，由 list_executor_run_reports 返回。",
                        },
                        "latest": {
                            "type": "boolean",
                            "description": "是否返回最新报告。默认 true。",
                        },
                        "include_markdown": {
                            "type": "boolean",
                            "description": "是否包含 markdown 内容。默认 true。",
                        },
                        "max_markdown_chars": {
                            "type": "integer",
                            "description": "最大 markdown 字符数。默认 30000，最大 60000。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="inspect_executor_activity",
                description=f"[{self.project_hint}] 只读执行器状态/报告查询工具。支持 action：run_status（按 run_id 或 preview_id 查询运行状态）、latest_run_status（返回最近一次运行状态，没有记录时返回 found=false）、list_reports（列出执行器报告，支持 version 过滤和 limit）、get_report（读取指定 report 详情）、get_audit_summary（返回审计包只读摘要，不触发 recheck）。支持按已登记 managed project_name 路由读取目标项目。所有 action 都是只读不操作，scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["run_status", "latest_run_status", "list_reports", "get_report", "get_audit_summary"],
                            "description": "只读查询 action。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目执行器状态或报告。",
                        },
                        "run_id": {
                            "type": "string",
                            "description": "run_status 可选。执行器运行 ID。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "run_status 可选。preview ID。",
                        },
                        "version": {
                            "type": "string",
                            "description": "list_reports/get_report/get_audit_summary 可选。版本过滤。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "list_reports 可选。最大返回数。默认 10，最大 50。",
                        },
                        "report_id": {
                            "type": "string",
                            "description": "get_report 可选。指定 report_id。",
                        },
                        "latest": {
                            "type": "boolean",
                            "description": "get_report 可选。是否返回最新报告。默认 true。",
                        },
                        "include_markdown": {
                            "type": "boolean",
                            "description": "get_report 可选。是否包含 markdown 内容。默认 true。",
                        },
                        "max_report_chars": {
                            "type": "integer",
                            "description": "get_report 可选。最大字符数。默认 30000，最大 60000。",
                        },
                        "section": {
                            "type": "string",
                            "enum": ["summary", "lineage", "scope", "report_excerpt"],
                            "description": "get_audit_summary 可选。审计包 section。默认 summary。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="analyze_project_state",
                description=f"[{self.project_hint}] 只读项目状态分析工具。一次性返回项目身份、模式、Git、Runner、计划、执行器和报告的聚合状态，以及推荐下一步操作和阻断/警告。适合 ChatGPT 开始工作时先调用此工具全面了解项目状态，而不是手动串多个底层工具。scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 project_name 路由读取目标项目分析结果。",
                        },
                        "include_repo_overview": {
                            "type": "boolean",
                            "description": "是否包含仓库概览文件树。默认 false。",
                        },
                        "include_reports": {
                            "type": "boolean",
                            "description": "是否包含执行器运行报告列表。默认 true。",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["pi", "codex", "opencode"],
                            "description": "可选执行器 provider，用于评估 continuation 决策。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "仓库概览文件树最大文件数。默认 200，最大 500。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="run_mcp_workflow",
                description=(
                    f"[{self.project_hint}] Bounded Workflow Runner 统一入口。"
                    "减少工具选择压力，将常用流程收敛为一个高层入口。"
                    "auto_preview（v1.75）：自动分析 goal 并选择 bounded workflow，串联多个 read/preview 步骤，"
                    "在 apply/commit/executor-run 边界停止。推荐 ChatGPT 首选入口。"
                    "prompt_to_plan（v1.84.58）：串联 prompt 文件保存、plan insert preview、plan patch apply，"
                    "停在 executor preflight/run_once_preview 边界。"
                    "支持 workflow：auto_preview、project_status、source_onboarding、plan_update、"
                    "small_project_patch、docs_update、git_commit、git_restore_file、git_revert、git_undo_version、agent_dispatch、prompt_to_plan。"
                    "不执行 executor，不自动 commit，写入类默认停 preview。"
                    "commit 只确认已有受控预览(preview_id)，不执行任意 shell，不 git add .，不绕过 preview。"
                    "没有匹配的 stored preview_id 不能创建 commit。"
                    "git_revert 不自动 commit。"
                    "scope 按 workflow/phase 动态映射。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "enum": [
                                "auto_preview", "project_status", "source_onboarding",
                                "plan_update", "small_project_patch", "docs_update",
                                "git_commit", "git_restore_file", "git_revert", "git_undo_version",
                                "agent_dispatch", "prompt_to_plan",
                            ],
                            "description": "要执行的工作流。auto_preview 是 v1.75 首选高层入口，自动分析 goal 并选择 bounded workflow。prompt_to_plan 是 v1.84.58 prompt 文件到 plan apply 链路入口。",
                        },
                        "phase": {
                            "type": "string",
                            "enum": ["inspect", "preview", "apply", "plan_preview", "plan_apply", "apply_all", "run_preview", "run", "commit", "status"],
                            "description": "工作流阶段。inspect/read/status 只读；preview/run_preview/plan_preview 只生成预览；apply/commit/run/plan_apply/apply_all 只确认受控预览ID，不执行任意 git 命令。prompt_to_plan 推荐主流程：preview → apply_all → run_preview → run。旧 phase apply/plan_preview/plan_apply 仍保留兼容。apply_all 一键完成 prompt 保存 + plan 登记。run_preview 生成执行器运行预览，不运行执行器。run 使用 run_preview 返回的 preview_id 执行一次执行器。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "apply/commit/run 阶段必填。prompt_to_plan apply_all 使用 prompt preview_id（来自 prompt_to_plan preview）；prompt_to_plan run 使用 executor run_once_preview 返回的 preview_id。没有匹配的 stored preview 不执行任何写入或提交。不能用 preview_id 绕过安全检查。",
                        },
                        "patch_id": {
                            "type": "string",
                            "description": "agent_dispatch apply 可选，prompt_to_plan plan_apply 使用 patch_id。apply_all 内部生成并使用 patch_id，但用户不传 patch_id。",
                        },
                        "commit": {
                            "type": "string",
                            "description": "撤销目标 commit ref。git_undo_version preview 阶段必填，其他 workflow 可选。",
                        },
                        "file": {
                            "type": "string",
                            "description": "要恢复的文件路径。git_undo_version 可选，恢复单文件时使用。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "操作理由，进入 workflow record。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "输出字符限制。",
                        },
                        "max_files": {
                            "type": "integer",
                            "description": "最大文件数。",
                        },
                        "include_diff_summary": {
                            "type": "boolean",
                            "description": "是否包含 diff 摘要。",
                        },
                        "max_diff_chars": {
                            "type": "integer",
                            "description": "最大 diff 字符数。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。project_status inspect、plan_update、prompt_to_plan、small_project_patch 支持按已登记 managed project_name 路由。source-onboarding 仍将该字段用作 onboarding 项目名称。",
                        },
                        "goal": {
                            "type": "string",
                            "description": "source_onboarding 项目目标。",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["pi", "codex", "opencode"],
                            "description": "auto_preview 可选。执行器 provider，用于 executor preflight 和 continuation 决策。",
                        },
                        "first_version": {
                            "type": "string",
                            "description": "source_onboarding 首版本号。",
                        },
                        "first_version_name": {
                            "type": "string",
                            "description": "source_onboarding 首版本显示名称。",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "source_onboarding 是否 dry_run。",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["repair", "extend"],
                            "description": "plan_update 模式。",
                        },
                        "version": {
                            "type": "string",
                            "description": "plan_update 版本号。",
                        },
                        "target_version": {
                            "type": "string",
                            "description": "plan_update 目标版本号（repair）。",
                        },
                        "insert_after": {
                            "type": "string",
                            "description": "plan_update extend 插入位置。",
                        },
                        "name": {
                            "type": "string",
                            "description": "plan_update extend 版本名称。",
                        },
                        "description": {
                            "type": "string",
                            "description": "plan_update extend 版本描述。",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "plan_update extend 版本 prompt。",
                        },
                        "user_request": {
                            "type": "string",
                            "description": "agent_dispatch preview 或 plan_update extend preview 的用户需求文本。",
                        },
                        "allowed_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent_dispatch preview 或 plan_update extend preview 的显式 allowed_files。",
                        },
                        "forbidden_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent_dispatch preview 或 plan_update extend preview 的显式 forbidden_files。",
                        },
                        "acceptance_commands": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "command": {"type": "string"},
                                            "timeout_seconds": {"type": "integer"},
                                            "continue_on_failure": {"type": "boolean"},
                                        },
                                        "required": ["command"],
                                        "additionalProperties": True,
                                    },
                                ],
                            },
                            "description": "agent_dispatch preview 或 plan_update extend preview 的显式 acceptance_commands。",
                        },
                        "manual_acceptance": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent_dispatch preview 或 plan_update extend preview 的显式 manual_acceptance。",
                        },
                        "out_of_scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent_dispatch preview 或 plan_update extend preview 的显式 out_of_scope。",
                        },
                        "context_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent_dispatch preview 或 plan_update extend preview 的显式 context_files。",
                        },
                        "repair_kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "plan_update repair 指定修复类型。",
                        },
                        "file": {
                            "type": "string",
                            "description": "small_project_patch / git_restore_file 文件路径。",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "small_project_patch 旧文本。",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "small_project_patch 新文本。",
                        },
                        "patch_text": {
                            "type": "string",
                            "description": "small_project_patch unified diff 文本。",
                        },
                        "docs_action": {
                            "type": "string",
                            "enum": ["index", "search", "read_section", "update_section_preview", "append_section_preview", "sync_docs_preview", "apply"],
                            "description": "docs_update 动作。",
                        },
                        "heading": {
                            "type": "string",
                            "description": "docs_update 文档 heading。",
                        },
                        "query": {
                            "type": "string",
                            "description": "docs_update 搜索关键词。",
                        },
                        "section_heading": {
                            "type": "string",
                            "description": "docs_update 新 section heading。",
                        },
                        "new_content": {
                            "type": "string",
                            "description": "docs_update 更新后的 section 内容。",
                        },
                        "section_content": {
                            "type": "string",
                            "description": "docs_update 新 section 内容。",
                        },
                        "after_heading": {
                            "type": "string",
                            "description": "docs_update 指定追加位置。",
                        },
                        "stale_terms": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "docs_update 过时术语列表。",
                        },
                        "message": {
                            "type": "string",
                            "description": "git_commit commit message。",
                        },
                        "style": {
                            "type": "string",
                            "enum": ["conventional", "runner_version", "concise"],
                            "description": "git_commit commit message 风格。",
                        },
                        "scope_hint": {
                            "type": "string",
                            "description": "git_commit 版本号或 scope 提示。",
                        },
                        "commit": {
                            "type": "string",
                            "description": "git_restore_file / git_revert commit ref。",
                        },
                        "content": {
                            "type": "string",
                            "description": "prompt_to_plan preview 必填。prompt 文本内容。",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "prompt_to_plan preview 可选。是否覆盖已存在的 prompt 文件。默认 false。",
                        },
                        "prompt_file": {
                            "type": "string",
                            "description": "prompt_to_plan plan_preview 必填。prompt 文件名，例如 v1.84.58.md。只接受文件名，不接受路径。",
                        },
                    },
                    "required": ["workflow"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_executor_config",
                description=(
                    f"[{self.project_hint}] 受控执行器配置管理工具。支持 action："
                    "inspect_inventory（只读，返回安全的 inventory 摘要，不暴露 token/api_key/Bearer/secret）；"
                    "probe_models_preview（生成 preview_id，不探测执行器）；"
                    "probe_models_apply（基于 preview_id 执行受控探测，执行 probe_executor_inventory，"
                    "验证 project_root/expiry/provider 一致性）；"
                    "set_default_profile_preview / set_default_profile_apply（受控设置项目本地 executor profile）。"
                    "provider 可选，必须是 codex、opencode 或 pi；model/reasoning_effort 仅用于 profile 设置。"
                    "不执行任意 shell 命令，不写 token，不安装模型，不修改登录态。"
                    "scope：inspect_inventory=mcp:read，probe_models_preview/set_default_profile_preview=mcp:preview，"
                    "probe_models_apply/set_default_profile_apply=mcp:commit。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "inspect_inventory",
                                "probe_models_preview",
                                "probe_models_apply",
                                "set_default_profile_preview",
                                "set_default_profile_apply",
                            ],
                            "description": "执行器配置管理 action。",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["codex", "opencode", "pi"],
                            "description": "可选。执行器 provider 过滤或 profile provider。不传时返回所有 provider。",
                        },
                        "model": {
                            "type": "string",
                            "description": "set_default_profile_preview 可选。项目本地 executor profile 的模型名，例如 opencode/deepseek-v4-flash-free。",
                        },
                        "reasoning_effort": {
                            "type": "string",
                            "description": "set_default_profile_preview 可选。项目本地 executor profile 的 reasoning effort。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "probe_models_apply 或 set_default_profile_apply 必填。来自对应 preview 的 preview_id。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由项目本地 executor profile 和受控 preview/apply。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_executor_workflow",
                description=(
                    f"[{self.project_hint}] 受控执行器工作流工具。支持以下 action："
                    "preflight（只读预检，检查项目与执行器就绪状态）；"
                    "run_once_preview（生成 preview_id，不执行执行器）；"
                    "run_once（异步执行，需要来自 run_once_preview 的 preview_id。快速返回 started/running 状态，后台执行。完成后通过 status run_id 或 preview_id 轮询获取结果。不循环，不自动修复，不自动提交）；"
                    "run_bounded_preview（只做预检并生成 bounded loop preview，不执行执行器）；"
                    "run_bounded（基于 run_bounded_preview 的 preview_id 执行 bounded loop，受 max_iterations 限制）；"
                    "get_audit_package（读取执行审计包的轻量摘要与lineage）；"
                    "refresh_audit_package（按 version 生成新的版本审计包 refresh 快照）；"
                    "recheck_report_preview（只读重审旧 report 的 scope 结论，生成状态刷新 preview）；"
                    "recheck_report_apply（基于 recheck_report_preview 的 preview_id 刷新目标 version 的 state 状态）；"
                    "manual_fix_prompt_preview（为当前 blocked/failure 版本生成手动修复提示词准备 preview）；"
                    "manual_fix_prompt_apply（基于 manual_fix_prompt_preview 的 preview_id 写入 current-fix-prompt.md 并把当前版本置为 FIX_PROMPT_READY）；"
                    "manual_validation_preview（基于已通过的 manage_validation_run 记录生成手动验收通过 state 刷新 preview）；"
                    "manual_validation_apply（基于 manual_validation_preview 的 preview_id 登记手动/等价验收通过，不改 executor report）；"
                    "scope_mismatch_preview（只读输出授权范围与实际 changed_files 的通用差异诊断，生成 resolution preview，不改 state/report/audit/Git）；"
                    "scope_mismatch_apply（基于 scope_mismatch_preview 的 preview_id 执行受控 resolution 状态落盘，不改 report/Git）；"
                    "reconcile_orphaned_claims_preview（只读扫描 RUNNING claim 并生成失联 claim reconcile preview，不改 runtime）；"
                    "reconcile_orphaned_claims_apply（基于 reconcile_orphaned_claims_preview 的 preview_id 受控终结仍失联的 RUNNING claim，不删除 claim，不杀进程）；"
                    "status（查看当前执行器会话状态）。"
                    "此工具遵循单项预览/应用审批模式。"
                    "project_root 可选，缺省使用 MCP 绑定项目，仅用于显式覆盖。"
                    "run_bounded 默认 max_iterations=1，最大 3；max_iterations>1 需要 trusted_mode=true。"
                    "不支持无限循环。allow_fix=false 时不执行 fix；allow_fix=true 只允许已有 FIX_PROMPT_READY。"
                    "allow_commit 不会执行 commit，只能停在 commit preview/next_action 边界。"
                    "run_once/run_bounded 不执行任意 git reset/clean/stash/merge/rebase/push，不创建或切换分支。"
                    "status 使用非阻塞轮询契约：next_poll_after_seconds=3，max_poll_attempts=3，最多轮询 3 次。支持 preview_id/run_id 查询。"
                    "project_name 支持已登记 managed 项目的所有 action。"
                    "scope：preflight/status/get_audit_package=mcp:read，run_once_preview/run_bounded_preview/recheck_report_preview/manual_fix_prompt_preview/manual_validation_preview/scope_mismatch_preview/reconcile_orphaned_claims_preview=mcp:preview，run_once/run_bounded/refresh_audit_package/recheck_report_apply/manual_fix_prompt_apply/manual_validation_apply/scope_mismatch_apply/reconcile_orphaned_claims_apply=mcp:commit。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["preflight", "run_once_preview", "run_once", "run_bounded_preview", "run_bounded", "get_audit_package", "refresh_audit_package", "recheck_report_preview", "recheck_report_apply", "manual_fix_prompt_preview", "manual_fix_prompt_apply", "manual_validation_preview", "manual_validation_apply", "scope_mismatch_preview", "scope_mismatch_apply", "reconcile_orphaned_claims_preview", "reconcile_orphaned_claims_apply", "status"], "description": "执行器工作流操作。"},
                        "project_name": {"type": "string", "description": "可选。按已登记 managed project_name 路由 preflight、run_once_preview、run_once、status。"},
                        "project_root": {"type": "string", "description": "可选。项目根目录路径；不传时使用 MCP 绑定项目。"},
                        "provider": {"type": "string", "enum": ["pi", "codex", "opencode"], "description": "执行器 provider。默认 codex。"},
                        "model": {"type": "string", "description": "run_once_preview/run_once 可选。显式指定本次执行器模型；run_once 必须与对应 preview 中记录的 model 一致。"},
                        "execution_mode": {"type": "string", "enum": ["run", "fix"], "description": "执行模式。run 为正常执行，fix 仅当当前状态为 FIX_PROMPT_READY 时可用。默认 run。"},
                        "preview_id": {"type": "string", "description": "run_once/run_bounded/recheck_report_apply/manual_fix_prompt_apply/manual_validation_apply/scope_mismatch_apply/reconcile_orphaned_claims_apply 必填；status 可选。来自对应 preview 的 preview_id。"},
                        "manual_fix_prompt": {"type": "string", "description": "manual_fix_prompt_preview 必填。用户提供的手动修复提示词内容。"},
                        "validation_run_id": {"type": "string", "description": "manual_validation_preview 必填。来自 manage_validation_run run/status 的 validation run ID。"},
                        "resolution": {"type": "string", "enum": ["refresh_in_scope_state", "record_direct_manual_review", "abort_version"], "description": "scope_mismatch_apply 必填。resolution 选项。"},
                        "run_id": {"type": "string", "description": "status 可选。执行器运行 ID。"},
                        "poll_attempt": {"type": "integer", "description": "status 可选。轮询次数。默认 1，最大 3。"},
                        "max_diff_chars": {"type": "integer", "default": 40000, "minimum": 1, "maximum": 80000, "description": "run_once 可选。diff 输出字符限制。默认 40000，最大 80000。"},
                        "include_diff_summary": {"type": "boolean", "default": True, "description": "run_once 可选。是否返回 diff_summary。默认 true。"},
                        "include_report_markdown": {"type": "boolean", "default": False, "description": "run_once 可选。是否返回报告 markdown。默认 false。"},
                        "max_report_chars": {"type": "integer", "default": 30000, "minimum": 1, "maximum": 60000, "description": "run_once 可选。报告 markdown 最大字符数。默认 30000，最大 60000。"},
                        "executor_session_mode": {"type": "string", "enum": ["auto", "resume_existing", "start_new"], "default": "auto", "description": "run_once 可选。执行器会话模式：auto（默认）使用自动续接决策；resume_existing 要求续接现有会话；start_new 启动新会话。默认 auto。"},
                        "reason": {"type": "string", "description": "可选。执行理由说明。"},
                        "max_iterations": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3, "description": "run_bounded 可选。循环轮数，默认 1，最小 1，最大 3。"},
                        "trusted_mode": {"type": "boolean", "default": False, "description": "run_bounded 可选。仅 trusted_mode=true 时允许 max_iterations>1。默认 false。"},
                        "stop_on_acceptance_failure": {"type": "boolean", "default": True, "description": "run_bounded 可选。是否在验收失败时停止。默认 true。"},
                        "stop_on_scope_violation": {"type": "boolean", "default": True, "description": "run_bounded 可选。是否在 scope violation 时停止。默认 true。"},
                        "stop_on_diff_too_large": {"type": "boolean", "default": True, "description": "run_bounded 可选。是否在 diff 超阈值时停止。默认 true。"},
                        "max_total_diff_chars": {"type": "integer", "default": 80000, "minimum": 1, "maximum": 200000, "description": "run_bounded 可选。总 diff 字符阈值，默认 80000，最大 200000。"},
                        "allow_fix": {"type": "boolean", "default": False, "description": "run_bounded 可选。默认 false；仅已有 FIX_PROMPT_READY 时允许 fix 轮。"},
                        "allow_commit": {"type": "boolean", "default": False, "description": "run_bounded 可选。默认 false；即使 true 也不会自动 commit。"},
                        "latest": {"type": "boolean", "default": True, "description": "get_audit_package 可选。默认 true。"},
                        "report_id": {"type": "string", "description": "get_audit_package/recheck_report_preview/scope_mismatch_preview 可选。指定 report_id。"},
                        "version": {"type": "string", "description": "get_audit_package/recheck_report_preview/manual_fix_prompt_preview/manual_validation_preview/scope_mismatch_preview/refresh_audit_package 可选。指定 version。"},
                        "section": {"type": "string", "enum": ["summary", "lineage", "validation", "scope", "report_excerpt"], "description": "get_audit_package 可选。默认 summary。"},
                        "include_markdown": {"type": "boolean", "default": False, "description": "get_audit_package 可选。section=report_excerpt 时是否返回 markdown 片段。"},
                        "max_chars": {"type": "integer", "default": 20000, "minimum": 1, "maximum": 60000, "description": "get_audit_package 可选。返回字符上限。"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="manage_validation_run",
                description=(
                    f"[{self.project_hint}] 通用受控验证运行工具。"
                    "GPTs 只提供 scope/target_files；Runner 本地选择验证策略。"
                    "inspect/status 只读；preview 生成固定 argv，不运行命令；run 只执行 preview 固化命令，shell=False，输出脱敏截断。"
                    "scope：inspect/status=mcp:read，preview=mcp:preview，run=mcp:commit。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["inspect", "preview", "run", "status"],
                            "description": "验证动作。inspect/status 只读；preview 生成固定验证命令；run 使用 preview_id 执行一次。",
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["changed_files", "target_files", "current_version", "full"],
                            "description": "验证范围。默认 changed_files；target_files 使用 target_files；current_version/full 优先运行当前版本 acceptance_commands。",
                        },
                        "target_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选目标文件列表。只接受项目内相对路径。",
                        },
                        "preview_id": {
                            "type": "string",
                            "description": "run 必填。来自 preview 的 preview_id。",
                        },
                        "run_id": {
                            "type": "string",
                            "description": "status 必填。验证运行 ID。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由所有操作。",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="list_workflow_runs",
                description=f"[{self.project_hint}] 列出 workflow run records。每次受控 MCP 操作（analyze_project_state、manage_plan_version insert/update/repair preview、manage_project_patch preview/apply、manage_git_history restore/preview/revert、manage_git_commit preview/commit、run_mcp_workflow、manage_executor_workflow）会自动生成 workflow record。返回摘要列表，不包含完整 steps。scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "最大返回条数。默认 20，最大 100。",
                        },
                        "workflow_name": {
                            "type": "string",
                            "description": "按 workflow_name 筛选。",
                        },
                        "status": {
                            "type": "string",
                            "description": "按 status 筛选（running/succeeded/failed/partial/unsupported）。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 workflow records。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
            MCPToolDef(
                name="get_workflow_run",
                description=f"[{self.project_hint}] 查看单个 workflow run record 详情。返回完整 workflow record，包含 steps 数组。scope=mcp:read。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workflow_id": {
                            "type": "string",
                            "description": "workflow_id。",
                        },
                        "project_name": {
                            "type": "string",
                            "description": "可选。按已登记 managed project_name 路由读取目标项目 workflow record。",
                        },
                    },
                    "required": ["workflow_id"],
                    "additionalProperties": False,
                },
                output_schema=common_output_schema,
            ),
        ]

    def validate_project(self, mode: str | None = None) -> None:
        if not os.path.isdir(self.project_root):
            raise PlanningBridgeError(f"项目目录不存在：{self.project_root}")
        if mode == "source-only":
            return
        runner_dir = resolve_project_runner_dir(self.project_root)
        plan_file = os.path.join(runner_dir, "plan.json")
        state_file = os.path.join(runner_dir, "state.json")
        if mode == "managed":
            if not os.path.exists(plan_file):
                raise PlanningBridgeError(
                    "当前项目尚未纳入 Runner 管理；后续版本会支持 managed 自动最小纳管。当前可先使用 source-only 模式启动 MCP，或通过 manage_runner_plan 完成纳管。"
                )
            return
        if os.path.exists(plan_file) and os.path.exists(state_file):
            return
        git_dir = os.path.join(self.project_root, ".git")
        if os.path.isdir(git_dir):
            return
        raise PlanningBridgeError(f"缺少计划文件或 Git 仓库：{plan_file}")

    def serve_stdio(self) -> int:
        self._log(f"MCP Planning Bridge server started, project={self.project_root}")
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            response = self._handle_line_stdio(line)
            if response is None:
                continue
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        self._log("MCP Planning Bridge server stopped")
        return 0

    def serve_http(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        auth_token: str | None = None,
        auth_mode: str | None = None,
        public_base_url: str | None = None,
        oauth_token_ttl_seconds: int = 3600,
        debug_actions: bool = False,
    ) -> int:
        server = self
        _debug_counter = 0
        resolved_auth_mode = auth_mode or ("token" if auth_token else "none")
        if resolved_auth_mode not in {"none", "token", "oauth"}:
            raise PlanningBridgeError(f"auth_mode 无效：{resolved_auth_mode}")
        if resolved_auth_mode == "token" and not auth_token:
            raise PlanningBridgeError("token auth mode requires auth_token.")
        normalized_public_base_url = public_base_url.rstrip("/") if public_base_url else None
        oauth_provider: MCPOAuthProvider | None = None
        if resolved_auth_mode == "oauth":
            if not normalized_public_base_url:
                raise PlanningBridgeError("oauth auth mode requires public_base_url.")
            oauth_provider = MCPOAuthProvider(
                self.project_root,
                normalized_public_base_url,
                token_ttl_seconds=oauth_token_ttl_seconds,
            )

        def _debug_log(handler: BaseHTTPRequestHandler, status_code: int, response_payload: dict[str, Any] | None = None) -> None:
            if not debug_actions:
                return
            nonlocal _debug_counter
            _debug_counter += 1
            start = getattr(handler, "_debug_start", 0.0)
            duration_ms = int((time.time() - start) * 1000) if start else 0
            request_id = getattr(handler, "_debug_request_id", f"d{_debug_counter}")
            method = getattr(handler, "_debug_method", "?")
            path = getattr(handler, "_debug_path", "?")
            tool_name = getattr(handler, "_debug_tool_name", "")
            body_keys = getattr(handler, "_debug_body_keys", None)
            body_parse_error = getattr(handler, "_debug_body_parse_error", False)
            auth_header = handler.headers.get("Authorization", "")
            has_auth = bool(auth_header)
            if auth_header.startswith("Bearer "):
                auth_scheme = "Bearer"
                auth_len = len(auth_header) - 7
            elif auth_header.startswith("Basic "):
                auth_scheme = "Basic"
                auth_len = len(auth_header) - 6
            elif has_auth:
                auth_scheme = "Other"
                auth_len = 0
            else:
                auth_scheme = "Missing"
                auth_len = 0
            content_type = handler.headers.get("Content-Type", "") or "-"
            ua = handler.headers.get("User-Agent", "") or "-"
            ua_summary = ua[:60]
            if body_keys is None:
                body_keys_list: list[str] = []
            else:
                body_keys_list = body_keys
            body_keys_str = ",".join(body_keys_list) if body_keys_list else "-"
            response_ok: Any = None
            response_error_code: Any = None
            if response_payload:
                if "result" in response_payload:
                    r = response_payload.get("result", {})
                    if isinstance(r, dict):
                        response_ok = r.get("ok")
                        response_error_code = r.get("error_code")
                elif "error" in response_payload:
                    response_ok = False
                    err = response_payload.get("error", {})
                    if isinstance(err, dict):
                        response_error_code = err.get("data", {}).get("error_code", err.get("code"))
                else:
                    response_ok = response_payload.get("ok")
                    response_error_code = response_payload.get("error_code")
            parts = [
                "[actions-debug]",
                f"request_id={request_id}",
                f"method={method}",
                f"path={path}",
            ]
            if tool_name:
                parts.append(f"tool_name={tool_name}")
            parts.extend([
                f"status_code={status_code}",
                f"duration_ms={duration_ms}",
                f"auth_mode={resolved_auth_mode}",
                f"has_authorization={'true' if has_auth else 'false'}",
                f"authorization_scheme={auth_scheme}",
                f"authorization_length={auth_len}",
                f"content_type={content_type}",
                f"user_agent_summary={ua_summary}",
                f"body_keys={body_keys_str}",
            ])
            if body_parse_error:
                parts.append("body_parse_error=true")
            parts.append(f"response_ok={response_ok}" if response_ok is not None else "response_ok=-")
            parts.append(f"response_error_code={response_error_code}" if response_error_code is not None else "response_error_code=-")
            sys.stderr.write(" ".join(parts) + "\n")
            sys.stderr.flush()

        class MCPHTTPRequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                server._log(f"{self.address_string()} - {format % args}")

            def _send_json(
                self,
                status_code: int,
                payload: dict[str, Any],
                headers: dict[str, str] | None = None,
            ) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
                _debug_log(self, status_code, payload)

            def _send_html(self, status_code: int, body_text: str) -> None:
                body = body_text.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_redirect(self, location: str) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send_auth_error(self) -> None:
                headers: dict[str, str] = {}
                if resolved_auth_mode == "oauth" and normalized_public_base_url:
                    headers["WWW-Authenticate"] = (
                        'Bearer resource_metadata="'
                        f'{normalized_public_base_url}/.well-known/oauth-protected-resource"'
                    )
                self._send_json(
                    401,
                    {
                        "ok": False,
                        "error_code": "UNAUTHORIZED",
                        "message": "Invalid or missing bearer token",
                    },
                    headers=headers,
                )

            def _auth_context(self) -> dict[str, Any] | None:
                if resolved_auth_mode == "none":
                    return {"mode": "none"}
                authorization = self.headers.get("Authorization", "")
                if resolved_auth_mode == "token":
                    if not authorization.startswith("Bearer "):
                        return None
                    token = authorization[len("Bearer ") :]
                    return {"mode": "token"} if token == auth_token else None
                if resolved_auth_mode == "oauth" and oauth_provider is not None:
                    if not authorization.startswith("Bearer "):
                        return None
                    token = authorization[len("Bearer ") :]
                    token_payload = oauth_provider.validate_token(token)
                    if token_payload is None:
                        return None
                    return {"mode": "oauth", "token": token_payload, "oauth_provider": oauth_provider}
                return None

            def _read_body(self) -> bytes:
                length_value = self.headers.get("Content-Length", "0")
                try:
                    content_length = int(length_value)
                except ValueError:
                    content_length = 0
                if content_length <= 0:
                    return b""
                return self.rfile.read(content_length)

            def _read_json_body(self) -> dict[str, Any] | None:
                raw = self._read_body()
                if not raw:
                    return None
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    return None
                return payload if isinstance(payload, dict) else None

            def _read_params_body(self) -> dict[str, Any]:
                raw = self._read_body()
                if not raw:
                    return {}
                content_type = self.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except Exception:
                        return {}
                    return payload if isinstance(payload, dict) else {}
                try:
                    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                except Exception:
                    return {}
                return {key: values[-1] for key, values in parsed.items() if values}

            def _send_oauth_page_result(self, result: dict[str, Any]) -> None:
                kind = result.get("kind")
                if kind == "redirect":
                    self._send_redirect(str(result.get("location") or "/"))
                    return
                if kind == "html":
                    self._send_html(int(result.get("status") or 200), str(result.get("body") or ""))
                    return
                self._send_json(500, {"ok": False, "error_code": "OAUTH_RESPONSE_INVALID"})

            def do_GET(self) -> None:
                parsed_url = urlparse(self.path)
                path = parsed_url.path
                if debug_actions:
                    self._debug_start = time.time()
                    self._debug_request_id = os.urandom(4).hex()
                    self._debug_method = "GET"
                    self._debug_path = path
                if path == "/healthz":
                    try:
                        payload = {
                            "ok": True,
                            "service": "colameta-mcp",
                            "auth_mode": resolved_auth_mode,
                        }
                        if server.service_mode:
                            payload["routing"] = "registry"
                        else:
                            status = server.bridge.get_runner_status(server.project_root)
                            payload["project"] = server.project_root
                            payload["current_version"] = status.get("current_version")
                        self._send_json(200, payload)
                        return
                    except Exception:
                        self._send_json(
                            500,
                            {
                                "ok": False,
                                "error_code": "HEALTH_CHECK_FAILED",
                                "message": "health 检查失败。",
                            },
                        )
                        return
                if path == "/openapi.json":
                    payload = server._build_actions_openapi_schema(
                        public_base_url=normalized_public_base_url,
                        host=host,
                        port=port,
                    )
                    self._send_json(200, payload)
                    return
                if path == "/mcp":
                    payload = {
                        "ok": True,
                        "message": "MCP endpoint ready. Use POST /mcp with JSON-RPC 2.0.",
                        "auth_mode": resolved_auth_mode,
                    }
                    if resolved_auth_mode == "oauth" and normalized_public_base_url:
                        payload["protected_resource_metadata"] = (
                            f"{normalized_public_base_url}/.well-known/oauth-protected-resource"
                        )
                    self._send_json(200, payload)
                    return
                if path == "/.well-known/oauth-protected-resource":
                    if oauth_provider is None:
                        self._send_json(404, {"ok": False, "error_code": "NOT_FOUND", "message": "OAuth 未启用。"})
                        return
                    self._send_json(200, oauth_provider.protected_resource_metadata())
                    return
                if path == "/.well-known/oauth-authorization-server":
                    if oauth_provider is None:
                        self._send_json(404, {"ok": False, "error_code": "NOT_FOUND", "message": "OAuth 未启用。"})
                        return
                    self._send_json(200, oauth_provider.authorization_server_metadata())
                    return
                if path == "/authorize":
                    if oauth_provider is None:
                        self._send_json(404, {"ok": False, "error_code": "NOT_FOUND", "message": "OAuth 未启用。"})
                        return
                    self._send_oauth_page_result(
                        oauth_provider.authorize(parse_qs(parsed_url.query, keep_blank_values=True))
                    )
                    return
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error_code": "NOT_FOUND",
                        "message": "请求路径不存在。",
                    },
                )

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if debug_actions:
                    self._debug_start = time.time()
                    self._debug_request_id = os.urandom(4).hex()
                    self._debug_method = "POST"
                    self._debug_path = path
                if path == "/register":
                    if oauth_provider is None:
                        self._send_json(404, {"ok": False, "error_code": "NOT_FOUND", "message": "OAuth 未启用。"})
                        return
                    payload = self._read_json_body()
                    if payload is None:
                        self._send_json(400, {"error": "invalid_request", "error_description": "JSON body is required."})
                        return
                    status_code, response = oauth_provider.register_client(payload)
                    self._send_json(status_code, response)
                    return
                if path == "/token":
                    if oauth_provider is None:
                        self._send_json(404, {"ok": False, "error_code": "NOT_FOUND", "message": "OAuth 未启用。"})
                        return
                    status_code, response = oauth_provider.exchange_token(self._read_params_body())
                    self._send_json(status_code, response)
                    return
                if path == "/revoke":
                    if oauth_provider is None:
                        self._send_json(404, {"ok": False, "error_code": "NOT_FOUND", "message": "OAuth 未启用。"})
                        return
                    status_code, response = oauth_provider.revoke_token(self._read_params_body())
                    self._send_json(status_code, response)
                    return
                tool_name = server._actions_tool_name_from_path(path)
                if tool_name is not None:
                    auth_context = self._auth_context()
                    if auth_context is None:
                        self._send_auth_error()
                        return
                    visible_tool_names = set(server._visible_tool_names())
                    if tool_name not in visible_tool_names:
                        self._send_json(
                            404,
                            {
                                "ok": False,
                                "error_code": "TOOL_NOT_FOUND",
                                "message": f"未知 tool：{tool_name}",
                            },
                        )
                        return
                    if debug_actions:
                        self._debug_tool_name = tool_name
                    raw = self._read_body()
                    if server._is_actions_request_too_large(raw):
                        self._send_json(400, server._actions_request_too_large_payload(tool_name))
                        return
                    if not raw:
                        arguments: Any = {}
                    else:
                        try:
                            arguments = json.loads(raw.decode("utf-8"))
                        except Exception:
                            if debug_actions:
                                self._debug_body_keys = []
                                self._debug_body_parse_error = True
                            self._send_json(
                                400,
                                {
                                    "ok": False,
                                    "error_code": "INVALID_JSON",
                                    "message": "请求不是合法 JSON。",
                                },
                            )
                            return
                    if not isinstance(arguments, dict):
                        if debug_actions:
                            self._debug_body_keys = []
                            self._debug_body_parse_error = True
                        self._send_json(
                            400,
                            {
                                "ok": False,
                                "error_code": "INVALID_PARAMS",
                                "message": "tool 参数必须是 JSON 对象。",
                            },
                        )
                        return
                    if debug_actions and isinstance(arguments, dict):
                        self._debug_body_keys = list(arguments.keys())
                    tool_result = server._call_tool(tool_name, arguments, auth_context=auth_context)
                    response_payload = server._package_actions_rest_response(tool_name, arguments, tool_result)
                    self._send_json(200, response_payload)
                    return
                if path != "/mcp":
                    self._send_json(
                        404,
                        {
                            "ok": False,
                            "error_code": "NOT_FOUND",
                            "message": "请求路径不存在。",
                        },
                    )
                    return
                auth_context = self._auth_context()
                if auth_context is None:
                    self._send_auth_error()
                    return
                request = self._read_json_body()
                if debug_actions:
                    if request is not None:
                        self._debug_body_keys = list(request.keys())
                        method_name = request.get("method", "")
                        if method_name in ("tools/call", "call_tool"):
                            rpc_params = request.get("params", {})
                            if isinstance(rpc_params, dict):
                                self._debug_tool_name = f"{method_name}/{rpc_params.get('name', '')}"
                            else:
                                self._debug_tool_name = method_name
                        else:
                            self._debug_tool_name = method_name
                    else:
                        self._debug_body_keys = []
                        self._debug_body_parse_error = True
                if request is None:
                    self._send_json(
                        400,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32700,
                                "message": "请求不是合法 JSON。",
                                "data": {"error_code": "invalid_json"},
                            },
                        },
                    )
                    return
                response = server._handle_jsonrpc_request(request, auth_context=auth_context)
                self._send_json(200, response)

        httpd = ReusableThreadingHTTPServer((host, port), MCPHTTPRequestHandler)
        self._httpd = httpd
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            self._log("MCP HTTP server interrupted")
        finally:
            httpd.shutdown()
            httpd.server_close()
            self._log("MCP HTTP server stopped")
        return 0

    def _handle_line_stdio(self, line: str) -> dict[str, Any] | None:
        try:
            request = json.loads(line)
        except Exception:
            return self._protocol_error(None, -32700, "invalid_json", "请求不是合法 JSON。")
        if not isinstance(request, dict):
            return self._protocol_error(None, -32600, "invalid_request", "请求必须是 JSON 对象。")
        return self._handle_jsonrpc_request(request)

    def _handle_jsonrpc_request(
        self,
        request: dict[str, Any],
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if method is None:
            return self._protocol_error(req_id, -32600, "invalid_request", "请求缺少 method。")

        try:
            if method == "initialize":
                return self._result(
                    req_id,
                    {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "colameta-mcp", "version": "1.0.0"},
                        "capabilities": {"tools": {"listChanged": False}},
                    },
                )
            if method == "notifications/initialized":
                return self._result(req_id, {"ok": True})
            if method in ("ping", "health"):
                return self._result(req_id, {"ok": True, "tool": method, "data": {"status": "ok"}})
            if method in ("list_tools", "tools/list"):
                return self._result(req_id, {"tools": self._tool_defs_payload()})
            if method in ("call_tool", "tools/call"):
                if not isinstance(params, dict):
                    return self._result(req_id, self._tool_error("call_tool", "INVALID_PARAMS", "params 必须是对象。"))
                name = params.get("name")
                arguments = params.get("arguments", {})
                tool_result = self._call_tool(name, arguments, auth_context=auth_context)
                if method == "tools/call":
                    return self._result(req_id, self._as_mcp_call_result(tool_result, arguments))
                return self._result(req_id, tool_result)
            if method == "apply_plan_patch":
                return self._result(
                    req_id,
                    self._tool_error(
                        "apply_plan_patch",
                        "TOOL_NOT_EXPOSED",
                        "apply_plan_patch is intentionally not exposed over MCP. Runner applies pending patches locally via Web Console or CLI.",
                    ),
                )
            if method in self.tools:
                return self._result(req_id, self._call_tool(method, params, auth_context=auth_context))
            return self._protocol_error(req_id, -32601, "method_not_found", f"未知方法：{method}")
        except Exception as e:
            return self._result(
                req_id,
                self._tool_error("internal", "INTERNAL_ERROR", "服务器内部错误。", {"message": str(e)}),
            )

    def _tool_defs_payload(self) -> list[dict[str, Any]]:
        exposed_tool_defs = self._filter_tools_by_exposure_profile(self.tool_defs)
        payload: list[dict[str, Any]] = []
        for tool in exposed_tool_defs:
            payload.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "input_schema": tool.input_schema,
                    "outputSchema": tool.output_schema,
                }
            )
        return payload

    def _snake_to_camel(self, name: str) -> str:
        parts = [part for part in name.strip().split("_") if part]
        if not parts:
            return ""
        head = parts[0].lower()
        tail = "".join(part[:1].upper() + part[1:] for part in parts[1:])
        return f"{head}{tail}"

    def _actions_operation_id(self, name: str) -> str:
        if name == "run_mcp_workflow":
            return "manageRunnerWorkflow"
        return self._snake_to_camel(name)

    def _actions_operation_summary(self, name: str) -> str:
        if name == "run_mcp_workflow":
            return "管理 Runner 工作流"
        return f"调用 {name}"

    def _truncate_description(self, text: Any, max_len: int = 280) -> str:
        if not isinstance(text, str):
            return ""
        trimmed = " ".join(text.split())
        if len(trimmed) <= max_len:
            return trimmed
        if max_len <= 3:
            return trimmed[:max_len]
        return f"{trimmed[: max_len - 3].rstrip()}..."

    def _actions_path_for_tool(self, name: str) -> str:
        return f"{ACTIONS_API_PREFIX}{name}"

    def _actions_tool_name_from_path(self, path: str) -> str | None:
        if not isinstance(path, str) or not path.startswith(ACTIONS_API_PREFIX):
            return None
        tool_name = path[len(ACTIONS_API_PREFIX):].strip("/")
        if not tool_name:
            return None
        return tool_name

    def _normalize_openapi_schema(self, schema: Any) -> Any:
        if isinstance(schema, dict):
            normalized: dict[str, Any] = {}
            for key, value in schema.items():
                if key == "properties":
                    if isinstance(value, dict):
                        normalized_properties: dict[str, Any] = {}
                        for prop_name, prop_schema in value.items():
                            normalized_properties[prop_name] = self._normalize_openapi_property_schema(prop_schema)
                        normalized[key] = normalized_properties
                    else:
                        normalized[key] = {}
                    continue
                if key == "description":
                    normalized[key] = self._truncate_description(value)
                else:
                    normalized[key] = self._normalize_openapi_schema(value)
            return normalized
        if isinstance(schema, list):
            return [self._normalize_openapi_schema(item) for item in schema]
        return schema

    def _normalize_openapi_property_schema(self, prop_schema: Any) -> dict[str, Any]:
        if isinstance(prop_schema, dict):
            normalized = self._normalize_openapi_schema(prop_schema)
            return normalized if isinstance(normalized, dict) else {"type": "string"}
        if isinstance(prop_schema, str):
            return {
                "type": "string",
                "description": self._truncate_description(prop_schema),
            }
        if isinstance(prop_schema, bool):
            if prop_schema:
                return {}
            return {"not": {}}
        if prop_schema is None:
            return {"type": "string", "description": ""}
        return {
            "type": "string",
            "description": self._truncate_description(str(prop_schema)),
        }

    def _actions_readonly_tools(self) -> set[str]:
        return {
            "get_project_identity",
            "get_plan_standards_report",
            "get_runner_execution_standards",
            "get_runner_status",
            "get_executor_session_status",
            "get_executor_continuation_preview",
            "get_executor_continuation_decision",
            "get_executor_resume_invocation_preview",
            "get_review_context",
            "get_runner_workbench_context",
            "get_project_doc_section",
            "get_repo_overview",
            "get_git_status",
            "get_git_log",
            "get_source_file",
            "search_source",
            "get_git_diff",
            "get_executor_inventory",
            "list_executor_run_reports",
            "get_executor_run_report",
            "inspect_executor_activity",
            "analyze_project_state",
            "manage_workflow_run",
            "list_workflow_runs",
            "get_workflow_run",
            "todo_read",
            "decision_read",
        }

    def _is_actions_consequential_tool(self, tool_name: str) -> bool:
        return tool_name not in self._actions_readonly_tools()

    def _actions_manage_executor_allowed_actions(self) -> tuple[str, ...]:
        return (
            "preflight",
            "run_once_preview",
            "run_once",
            "get_audit_package",
            "refresh_audit_package",
            "recheck_report_preview",
            "recheck_report_apply",
            "manual_fix_prompt_preview",
            "manual_fix_prompt_apply",
            "manual_validation_preview",
            "manual_validation_apply",
            "scope_mismatch_preview",
            "scope_mismatch_apply",
            "reconcile_orphaned_claims_preview",
            "reconcile_orphaned_claims_apply",
            "status",
        )

    def _actions_openapi_tool_description(self, tool_name: str, description: str) -> str:
        if tool_name != "manage_executor_workflow":
            return self._truncate_description(description)
        return self._truncate_description(
            (
                "受控执行器工作流。GPT Actions 推荐链路：run_once_preview -> run_once -> status -> "
                "get_executor_run_report。支持旧报告重审链路：recheck_report_preview -> recheck_report_apply。"
                "支持手动修复提示词准备链路：manual_fix_prompt_preview -> manual_fix_prompt_apply。"
                "支持手动验收登记链路：manual_validation_preview -> manual_validation_apply。"
                "支持通用范围诊断链路：scope_mismatch_preview -> scope_mismatch_apply。"
                "支持失联 claim 受控协调链路：reconcile_orphaned_claims_preview -> reconcile_orphaned_claims_apply。"
            )
        )

    def _actions_openapi_request_schema(self, tool_name: str, request_schema: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request_schema, dict):
            return request_schema
        schema = copy.deepcopy(request_schema)
        props = schema.get("properties")
        if not isinstance(props, dict):
            return schema
        if tool_name in PROJECT_NAME_REQUIRED_TOOLS:
            project_schema = props.setdefault("project_name", {
                "type": "string",
                "description": "必填。服务模式下项目级工具必须显式提供已登记 project_name。",
            })
            if isinstance(project_schema, dict):
                project_schema["description"] = "必填。服务模式下项目级工具必须显式提供已登记 project_name。"
            required = schema.setdefault("required", [])
            if isinstance(required, list) and "project_name" not in required:
                required.append("project_name")
        if tool_name != "manage_executor_workflow":
            return schema
        action_schema = props.get("action")
        if isinstance(action_schema, dict):
            current_enum = action_schema.get("enum")
            allowed = list(self._actions_manage_executor_allowed_actions())
            if isinstance(current_enum, list):
                filtered = [item for item in current_enum if item in allowed]
                action_schema["enum"] = filtered or allowed
            else:
                action_schema["enum"] = allowed
            action_schema["description"] = (
                "执行器工作流操作。GPT Actions 暴露：preflight、run_once_preview、run_once、"
                "get_audit_package、refresh_audit_package、recheck_report_preview、recheck_report_apply、"
                "manual_fix_prompt_preview、manual_fix_prompt_apply、"
                "manual_validation_preview、manual_validation_apply、scope_mismatch_preview、scope_mismatch_apply、"
                "reconcile_orphaned_claims_preview、reconcile_orphaned_claims_apply、status。"
            )
        preview_schema = props.get("preview_id")
        if isinstance(preview_schema, dict):
            preview_schema["description"] = (
                "run_once/recheck_report_apply/manual_fix_prompt_apply/manual_validation_apply/scope_mismatch_apply/reconcile_orphaned_claims_apply 必填；status 可选。"
                "来自 run_once_preview、recheck_report_preview、manual_fix_prompt_preview、manual_validation_preview、scope_mismatch_preview 或 reconcile_orphaned_claims_preview 的 preview_id。"
            )
        for bounded_only_param in (
            "max_iterations",
            "trusted_mode",
            "stop_on_acceptance_failure",
            "stop_on_scope_violation",
            "stop_on_diff_too_large",
            "max_total_diff_chars",
            "allow_fix",
            "allow_commit",
        ):
            props.pop(bounded_only_param, None)
        return schema

    def _is_actions_bounded_next_action(self, item: dict[str, Any]) -> bool:
        candidates: list[str] = []
        direct = item.get("action")
        if isinstance(direct, str) and direct.strip():
            candidates.append(direct.strip().lower())
        for key in ("params", "arguments"):
            container = item.get(key)
            if isinstance(container, dict):
                action_val = container.get("action")
                if isinstance(action_val, str) and action_val.strip():
                    candidates.append(action_val.strip().lower())
        for candidate in candidates:
            if "run_bounded" in candidate:
                return True
        return False

    def _actions_run_once_preview_next_action(self, original: dict[str, Any]) -> dict[str, Any]:
        provider = "codex"
        for key in ("params", "arguments"):
            container = original.get(key)
            if isinstance(container, dict):
                provider_val = container.get("provider")
                if isinstance(provider_val, str) and provider_val.strip():
                    provider = provider_val.strip()
                    break
        return {
            "action": "manage_executor_workflow.run_once_preview",
            "label": "生成执行器运行预览",
            "reason": "GPT Actions 使用 run_once_preview -> run_once -> status -> get_executor_run_report 链路。",
            "tool": "manage_executor_workflow",
            "params": {"action": "run_once_preview", "provider": provider, "execution_mode": "run"},
            "risk_level": "preview",
            "requires_confirmation": True,
        }

    def _actions_sanitize_next_actions(self, items: list[Any]) -> list[Any]:
        sanitized: list[Any] = []
        seen_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                sanitized.append(item)
                continue
            if self._is_actions_bounded_next_action(item):
                replacement = self._actions_run_once_preview_next_action(item)
                key = json.dumps(replacement, ensure_ascii=False, sort_keys=True)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                sanitized.append(replacement)
                continue
            sanitized.append(item)
        return sanitized

    def _actions_sanitize_tool_result(self, tool_result: Any) -> Any:
        if isinstance(tool_result, dict):
            result: dict[str, Any] = {}
            for key, value in tool_result.items():
                if key == "next_actions" and isinstance(value, list):
                    result[key] = self._actions_sanitize_next_actions(value)
                else:
                    result[key] = self._actions_sanitize_tool_result(value)
            return result
        if isinstance(tool_result, list):
            return [self._actions_sanitize_tool_result(item) for item in tool_result]
        return tool_result

    def _build_actions_openapi_schema(
        self,
        public_base_url: str | None,
        host: str,
        port: int,
    ) -> dict[str, Any]:
        server_url = public_base_url.rstrip("/") if isinstance(public_base_url, str) and public_base_url.strip() else f"http://{host}:{port}"
        visible_tool_defs = self._filter_tools_by_exposure_profile(self.tool_defs)
        common_output_schema = self._build_common_output_schema()
        normalized_output_schema = self._normalize_openapi_schema(common_output_schema)
        paths: dict[str, Any] = {}
        for tool in visible_tool_defs:
            path = self._actions_path_for_tool(tool.name)
            summary = self._actions_operation_summary(tool.name)
            description = self._actions_openapi_tool_description(tool.name, tool.description)
            request_schema = self._normalize_openapi_schema(tool.input_schema)
            request_schema = self._actions_openapi_request_schema(tool.name, request_schema)
            paths[path] = {
                "post": {
                    "operationId": self._actions_operation_id(tool.name),
                    "summary": self._truncate_description(summary, max_len=120),
                    "description": description,
                    "x-openai-isConsequential": self._is_actions_consequential_tool(tool.name),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": request_schema,
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": normalized_output_schema,
                                }
                            },
                        }
                    },
                    "security": [{"BearerAuth": []}],
                }
            }

        schema = {
            "openapi": "3.1.0",
            "info": {
                "title": "MVP Runner Actions API",
                "version": "1.0.0",
                "description": self._truncate_description(
                    "REST adapter for MVP Runner project status, source review, git review, docs, plan, executor and commit workflows."
                ),
            },
            "servers": [{"url": server_url}],
            "security": [{"BearerAuth": []}],
            "paths": paths,
            "components": {
                "securitySchemes": {
                    "BearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                    }
                },
                "schemas": {
                    "ToolResult": normalized_output_schema,
                },
            },
        }
        return self._normalize_openapi_schema(schema)

    def _get_exposure_profile(self) -> str:
        raw = os.getenv(MCP_EXPOSURE_PROFILE_ENV, MCP_EXPOSURE_PROFILE_NORMAL)
        if isinstance(raw, str):
            normalized = raw.strip().lower()
        else:
            normalized = MCP_EXPOSURE_PROFILE_NORMAL
        if normalized in _PROFILE_ORDERS:
            return normalized
        return MCP_EXPOSURE_PROFILE_NORMAL

    def _get_exposed_tool_names(self, profile: str | None = None) -> set[str]:
        profile_name = profile or self.mcp_exposure_profile
        tool_order = _PROFILE_ORDERS.get(profile_name, _PROFILE_ORDERS[MCP_EXPOSURE_PROFILE_NORMAL])
        return set(tool_order)

    def _filter_tools_by_exposure_profile(self, tools: list[MCPToolDef]) -> list[MCPToolDef]:
        allowed = self._get_exposed_tool_names(self.mcp_exposure_profile)
        return [tool for tool in tools if tool.name in allowed]

    def _visible_tool_names(self) -> list[str]:
        return [tool.name for tool in self._filter_tools_by_exposure_profile(self.tool_defs)]

    def _mcp_default_next_reads(self, tool_name: str) -> list[dict[str, Any]]:
        return self._actions_default_next_reads(tool_name)

    def _mcp_recommended_next_reads(
        self,
        tool_name: str,
        params: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return self._actions_recommended_next_reads(tool_name, params, tool_result)

    def _shape_mcp_call_result(
        self,
        tool_result: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_params = params if isinstance(params, dict) else {}
        is_error = not bool(tool_result.get("ok"))
        tool_name = str(tool_result.get("tool") or "unknown_tool")
        if self._json_char_count(tool_result) <= MCP_TARGET_TOOL_RESULT_CHARS:
            if is_error:
                err_msg = str(tool_result.get("message") or "unknown error")
                text_payload = f"{tool_name} failed: {err_msg}"
            else:
                text_payload = f"{tool_name} completed."
            return {
                "content": [{"type": "text", "text": text_payload}],
                "structuredContent": tool_result,
                "isError": is_error,
            }
        try:
            data = tool_result.get("data")
            data_keys: list[str] = []
            if isinstance(data, dict):
                data_keys = [str(k) for k in list(data.keys())[:40]]
            omitted_fields = [f"data.{k}" for k in data_keys] if data_keys else ["data"]
            manifest_sc: dict[str, Any] = {
                "ok": bool(tool_result.get("ok")),
                "tool": tool_name,
                "packaged": True,
                "package_mode": "manifest",
                "message": "结果内容较大，已返回摘要与续读建议。",
                "summary": {
                    "result_char_estimate": self._json_char_count(tool_result),
                    "target_tool_result_chars": MCP_TARGET_TOOL_RESULT_CHARS,
                    "hard_tool_result_chars": MCP_HARD_TOOL_RESULT_CHARS,
                    "data_key_count": len(data.keys()) if isinstance(data, dict) else 0,
                    "data_keys": data_keys,
                    "original_error_code": tool_result.get("error_code"),
                },
                "omitted_fields": omitted_fields,
                "recommended_next_reads": self._mcp_recommended_next_reads(tool_name, safe_params, tool_result),
            }
            if not manifest_sc["ok"] and isinstance(tool_result.get("error_code"), str):
                manifest_sc["error_code"] = tool_result.get("error_code")
            manifest_text = json.dumps(manifest_sc, ensure_ascii=False)
            packaged_result = {
                "content": [{"type": "text", "text": manifest_text}],
                "structuredContent": manifest_sc,
                "isError": is_error,
            }
            if self._json_char_count(packaged_result) <= MCP_HARD_TOOL_RESULT_CHARS:
                return packaged_result

            reduced_sc = {
                "ok": bool(tool_result.get("ok")),
                "tool": tool_name,
                "packaged": True,
                "package_mode": "manifest",
                "message": "结果内容较大，已返回最小续读提示。",
                "summary": {
                    "result_char_estimate": self._json_char_count(tool_result),
                    "target_tool_result_chars": MCP_TARGET_TOOL_RESULT_CHARS,
                    "hard_tool_result_chars": MCP_HARD_TOOL_RESULT_CHARS,
                },
                "omitted_fields": ["data"],
                "recommended_next_reads": self._mcp_recommended_next_reads(tool_name, safe_params, tool_result)[:2],
            }
            if not reduced_sc["ok"] and isinstance(tool_result.get("error_code"), str):
                reduced_sc["error_code"] = tool_result.get("error_code")
            reduced_text = json.dumps(reduced_sc, ensure_ascii=False)
            reduced_result = {
                "content": [{"type": "text", "text": reduced_text}],
                "structuredContent": reduced_sc,
                "isError": is_error,
            }
            if self._json_char_count(reduced_result) <= MCP_HARD_TOOL_RESULT_CHARS:
                return reduced_result
        except Exception:
            pass

        fallback_sc = {
            "ok": False,
            "tool": tool_name,
            "packaged": True,
            "error_code": "MCP_RESULT_SHAPING_FAILED",
            "message": "工具结果过大且摘要失败，请按续读建议分步读取。",
            "recommended_next_reads": self._mcp_default_next_reads(tool_name),
        }
        fallback_text = json.dumps(fallback_sc, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": fallback_text}],
            "structuredContent": fallback_sc,
            "isError": True,
        }

    def _as_mcp_call_result(
        self,
        tool_result: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._shape_mcp_call_result(tool_result, params)

    def _build_common_output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ok": {
                    "type": "boolean",
                    "description": "Whether the tool call succeeded.",
                },
                "tool": {
                    "type": "string",
                    "description": "Tool name.",
                },
                "data": {
                    "type": "object",
                    "description": "Structured payload returned by the tool.",
                    "additionalProperties": True,
                },
                "error_code": {
                    "type": "string",
                    "description": "Machine-readable error code when ok is false.",
                },
                "message": {
                    "type": "string",
                    "description": "Human-readable message.",
                },
                "details": {
                    "type": "object",
                    "description": "Additional structured error details.",
                    "additionalProperties": True,
                },
                "packaged": {
                    "type": "boolean",
                    "description": "Whether a large response was replaced by a compact manifest.",
                },
                "package_mode": {
                    "type": "string",
                    "description": "Large-response packaging mode, for example manifest.",
                },
                "summary": {
                    "type": "object",
                    "description": "Summary for a packaged large response.",
                    "additionalProperties": True,
                },
                "omitted_fields": {
                    "type": "array",
                    "description": "Fields omitted from a packaged large response.",
                    "items": {"type": "string"},
                },
                "recommended_next_reads": {
                    "type": "array",
                    "description": "Suggested smaller follow-up reads when a large response is packaged.",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["ok", "tool"],
            "additionalProperties": False,
        }

    def _json_char_count(self, payload: Any) -> int:
        try:
            return len(json.dumps(payload, ensure_ascii=False))
        except Exception:
            return 10**9

    def _is_actions_request_too_large(self, raw: bytes) -> bool:
        if not raw:
            return False
        try:
            body_text = raw.decode("utf-8")
        except Exception:
            return len(raw) > ACTIONS_HARD_REQUEST_CHARS
        return len(body_text) > ACTIONS_HARD_REQUEST_CHARS

    def _actions_request_too_large_payload(self, tool_name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": tool_name,
            "error_code": "ACTION_REQUEST_TOO_LARGE",
            "message": "Actions 请求体过大，请拆分请求后重试。",
            "recommended_next_reads": [
                {
                    "tool": "manage_files",
                    "arguments": {"action": "edit", "phase": "preview"},
                    "reason": "将大 patch 拆成多个 preview 分批提交。",
                },
                {
                    "tool": "manage_executor_workflow",
                    "arguments": {"action": "preflight"},
                    "reason": "复杂改动优先使用受控执行器工作流。",
                },
            ],
        }

    def _actions_default_next_reads(self, tool_name: str) -> list[dict[str, Any]]:
        return [
            {
                "tool": "analyze_project_state",
                "arguments": {"include_repo_overview": False, "include_reports": False},
                "reason": "先读取项目摘要，再按需调用细粒度工具。",
            },
            {
                "tool": tool_name,
                "arguments": {},
                "reason": "缩小参数范围后重试当前工具。",
            },
        ]

    def _actions_recommended_next_reads(
        self,
        tool_name: str,
        params: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        normalized_tool = str(tool_name or "").strip()
        if normalized_tool == "get_review_context":
            suggestions: list[dict[str, Any]] = [
                {
                    "tool": "manage_git",
                    "arguments": {"action": "diff", "mode": "summary"},
                    "reason": "先读取 diff 摘要再进入文件级审阅。",
                },
                {
                    "tool": "manage_git",
                    "arguments": {"action": "review_context", "include_repo_overview": False, "max_diff_chars": 20000},
                    "reason": "降低 diff 大小后读取上下文。",
                },
            ]
            data = tool_result.get("data")
            if isinstance(data, dict):
                changed_files = data.get("changed_files")
                if isinstance(changed_files, list):
                    first_file = next((x for x in changed_files if isinstance(x, str) and x.strip()), None)
                    if first_file:
                        suggestions.append(
                            {
                                "tool": "manage_git",
                                "arguments": {"action": "diff", "mode": "page", "file": first_file, "offset": 0, "max_chars": 30000},
                                "reason": "按文件分页续读 diff。",
                            }
                        )
            return suggestions
        if normalized_tool == "get_git_diff":
            suggestions = [
                {
                    "tool": "manage_git",
                    "arguments": {"action": "diff", "mode": "summary"},
                    "reason": "先读取变更文件摘要。",
                },
                {
                    "tool": "manage_git",
                    "arguments": {"action": "diff", "mode": "page", "offset": 0, "max_chars": 30000},
                    "reason": "分页读取单文件 diff。",
                },
            ]
            include_files = params.get("include_files")
            if isinstance(include_files, list):
                normalized_files = [x for x in include_files if isinstance(x, str) and x.strip()][:3]
                if normalized_files:
                    suggestions.append(
                        {
                            "tool": "manage_git",
                            "arguments": {"action": "diff", "mode": "files", "include_files": normalized_files, "max_chars": 30000},
                            "reason": "按文件子集续读 diff。",
                        }
                    )
            return suggestions
        if normalized_tool == "get_source_file":
            target_file = params.get("file") if isinstance(params.get("file"), str) else ""
            suggestions = []
            if target_file:
                suggestions.append(
                    {
                        "tool": "get_source_file",
                        "arguments": {"file": target_file, "start_line": 1, "end_line": 200, "max_chars": 20000},
                        "reason": "按行范围读取源码。",
                    }
                )
            suggestions.append(
                {
                    "tool": "search_source",
                    "arguments": {"query": "TODO", "max_results": 30},
                    "reason": "先定位关键片段再读取局部源码。",
                }
            )
            return suggestions
        if normalized_tool == "manage_files":
            action_name = params.get("action")
            if isinstance(action_name, str) and action_name.strip().lower() == "edit":
                return [
                    {
                        "tool": "manage_files",
                        "arguments": {"action": "edit", "phase": "preview", "max_diff_chars": 12000, "max_files": 3},
                        "reason": "拆小 patch 预览，分批确认。",
                    },
                    {
                        "tool": "manage_executor_workflow",
                        "arguments": {"action": "preflight"},
                        "reason": "复杂改动可转为执行器受控流程。",
                    },
                ]
            target_file = params.get("file") if isinstance(params.get("file"), str) else ""
            suggestions = []
            if target_file:
                suggestions.append(
                    {
                        "tool": "manage_files",
                        "arguments": {"action": "read", "file": target_file, "start_line": 1, "end_line": 200, "max_chars": 20000},
                        "reason": "按行范围读取源码。",
                    }
                )
            suggestions.append(
                {
                    "tool": "manage_files",
                    "arguments": {"action": "search", "query": "TODO", "max_results": 30},
                    "reason": "先定位关键片段再读取局部源码。",
                }
            )
            return suggestions
        if normalized_tool == "manage_git_commit":
            action = params.get("action")
            action_name = action.strip().lower() if isinstance(action, str) else ""
            if action_name in {"readiness", "preview", "commit_workflow_preview", "suggest_commit_message"}:
                return [
                    {
                        "tool": "manage_git",
                        "arguments": {"action": "commit_readiness", "include_diff_summary": False, "max_diff_chars": 20000},
                        "reason": "关闭大 diff 摘要并缩小字符上限。",
                    },
                    {
                        "tool": "manage_git",
                        "arguments": {"action": "diff", "mode": "summary"},
                        "reason": "使用 diff 摘要替代内嵌大 diff。",
                    },
                    {
                        "tool": "manage_git",
                        "arguments": {"action": "diff", "mode": "page", "offset": 0, "max_chars": 30000},
                        "reason": "按文件分页读取具体差异。",
                    },
                ]
        if normalized_tool == "manage_project_docs":
            action = params.get("action")
            action_name = action.strip().lower() if isinstance(action, str) else ""
            if action_name in {"read_section", "search", "index"}:
                return [
                    {
                        "tool": "manage_project_docs",
                        "arguments": {"action": action_name or "search", "max_chars": 8000, "max_files": 20},
                        "reason": "缩小文档读取范围与字符数。",
                    }
                ]
        if normalized_tool == "manage_git_history":
            action = params.get("action")
            action_name = action.strip().lower() if isinstance(action, str) else ""
            if action_name in {"show", "diff_commits", "revert_preview"}:
                action_map = {"show": "history_show"}
                mg_action = action_map.get(action_name, action_name)
                read_args: dict[str, Any] = {"action": mg_action, "max_chars": 20000}
                if action_name == "show":
                    read_args["include_patch"] = False
                for key in ("commit", "base", "head", "file"):
                    val = params.get(key)
                    if isinstance(val, str) and val.strip():
                        read_args[key] = val
                return [
                    {
                        "tool": "manage_git",
                        "arguments": read_args,
                        "reason": "使用较小 max_chars 或禁用 patch 续读。",
                    }
                ]
        if normalized_tool == "get_executor_run_report":
            args: dict[str, Any] = {"latest": True, "include_markdown": False}
            for key in ("version", "report_id"):
                val = params.get(key)
                if isinstance(val, str) and val.strip():
                    args[key] = val.strip()
            return [
                {
                    "tool": "get_executor_run_report",
                    "arguments": args,
                    "reason": "先读取结构化报告，按需再取 markdown。",
                },
                {
                    "tool": "get_executor_run_report",
                    "arguments": {**args, "include_markdown": True, "max_markdown_chars": 12000},
                    "reason": "缩小 markdown 字符数分步读取。",
                },
            ]
        if normalized_tool in {"manage_workflow_run", "list_workflow_runs"}:
            action_name = params.get("action")
            action_name = action_name.strip().lower() if isinstance(action_name, str) else "list"
            if action_name == "get":
                workflow_id = params.get("workflow_id")
                if isinstance(workflow_id, str) and workflow_id.strip():
                    return [
                        {
                            "tool": "manage_workflow_run",
                            "arguments": {"action": "get", "workflow_id": workflow_id.strip()},
                            "reason": "按单个 workflow_id 续读。",
                        }
                    ]
            return [
                {
                    "tool": "manage_workflow_run",
                    "arguments": {"action": "list", "limit": 20},
                    "reason": "缩小 workflow run 列表返回规模。",
                }
            ]
        if normalized_tool == "get_workflow_run":
            workflow_id = params.get("workflow_id")
            if isinstance(workflow_id, str) and workflow_id.strip():
                return [
                    {
                        "tool": "manage_workflow_run",
                        "arguments": {"action": "get", "workflow_id": workflow_id.strip()},
                        "reason": "按单个 workflow_id 续读。",
                    }
                ]
        return self._actions_default_next_reads(normalized_tool or "unknown_tool")

    def _package_actions_rest_response(
        self,
        tool_name: str,
        params: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            sanitized_tool_result = self._actions_sanitize_tool_result(tool_result)
            response_chars = self._json_char_count(sanitized_tool_result)
            if response_chars <= ACTIONS_TARGET_RESPONSE_CHARS:
                return sanitized_tool_result
            ok_value = bool(sanitized_tool_result.get("ok"))
            data = sanitized_tool_result.get("data")
            data_keys: list[str] = []
            if isinstance(data, dict):
                data_keys = [str(k) for k in list(data.keys())[:40]]
            omitted_fields = [f"data.{k}" for k in data_keys] if data_keys else ["data"]
            summary: dict[str, Any] = {
                "response_char_estimate": response_chars,
                "target_response_chars": ACTIONS_TARGET_RESPONSE_CHARS,
                "hard_response_chars": ACTIONS_HARD_RESPONSE_CHARS,
                "data_key_count": len(data.keys()) if isinstance(data, dict) else 0,
                "data_keys": data_keys,
                "original_error_code": sanitized_tool_result.get("error_code"),
            }
            manifest: dict[str, Any] = {
                "ok": ok_value,
                "tool": tool_name,
                "packaged": True,
                "package_mode": "manifest",
                "message": "响应内容较大，已返回摘要与续读建议。",
                "summary": summary,
                "omitted_fields": omitted_fields,
                "recommended_next_reads": self._actions_recommended_next_reads(tool_name, params, sanitized_tool_result),
            }
            if not ok_value and isinstance(sanitized_tool_result.get("error_code"), str):
                manifest["error_code"] = sanitized_tool_result.get("error_code")
            if self._json_char_count(manifest) <= ACTIONS_HARD_RESPONSE_CHARS:
                return manifest
            reduced_manifest: dict[str, Any] = {
                "ok": ok_value,
                "tool": tool_name,
                "packaged": True,
                "package_mode": "manifest",
                "message": "响应内容较大，已返回最小续读提示。",
                "summary": {
                    "response_char_estimate": response_chars,
                    "target_response_chars": ACTIONS_TARGET_RESPONSE_CHARS,
                    "hard_response_chars": ACTIONS_HARD_RESPONSE_CHARS,
                },
                "omitted_fields": ["data"],
                "recommended_next_reads": self._actions_recommended_next_reads(tool_name, params, sanitized_tool_result)[:2],
            }
            if not ok_value and isinstance(sanitized_tool_result.get("error_code"), str):
                reduced_manifest["error_code"] = sanitized_tool_result.get("error_code")
            if self._json_char_count(reduced_manifest) <= ACTIONS_HARD_RESPONSE_CHARS:
                return reduced_manifest
            return {
                "ok": False,
                "tool": tool_name,
                "packaged": True,
                "error_code": "ACTION_RESPONSE_TOO_LARGE",
                "message": "响应体超过安全上限，请使用推荐的续读工具。",
                "recommended_next_reads": self._actions_default_next_reads(tool_name),
            }
        except Exception:
            return {
                "ok": False,
                "tool": tool_name,
                "error_code": "ACTION_RESPONSE_PACKAGING_FAILED",
                "message": "Actions 响应包装失败，请缩小请求范围后重试。",
                "recommended_next_reads": self._actions_default_next_reads(tool_name),
            }

    def _call_tool(
        self,
        name: Any,
        params: Any,
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            return self._tool_error("unknown", "INVALID_TOOL", "tool 名称无效。")
        if name == "apply_plan_patch":
            return self._tool_error(
                "apply_plan_patch",
                "TOOL_NOT_EXPOSED",
                "apply_plan_patch is intentionally not exposed over MCP. Runner applies pending patches locally via Web Console or CLI.",
            )
        tool = self.tools.get(name)
        if tool is None:
            return self._tool_error(name, "TOOL_NOT_FOUND", f"未知 tool：{name}")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._tool_error(name, "INVALID_PARAMS", "tool 参数必须是 JSON 对象。")
        if (self.service_mode or auth_context is not None) and name in PROJECT_NAME_REQUIRED_TOOLS:
            project_name = params.get("project_name")
            if not isinstance(project_name, str) or not project_name.strip():
                return self._tool_error(
                    name,
                    "PROJECT_NAME_REQUIRED",
                    "服务模式下项目级工具必须显式提供 project_name，不能使用默认项目。",
                    {"tool": name},
                )
        scope_error = self._oauth_scope_error(name, params, auth_context)
        if scope_error is not None:
            return scope_error
        relay_scope_error = self._cloud_relay_scope_error(name, params, auth_context)
        if relay_scope_error is not None:
            return relay_scope_error
        try:
            data = tool(params)
            return {"ok": True, "tool": name, "data": data}
        except MCPToolInputError as e:
            return self._tool_error(name, e.error_code, e.message, e.details)
        except PlanningBridgeError as e:
            return self._tool_error(name, "BRIDGE_ERROR", str(e))
        except SourceReviewError as e:
            return self._tool_error(name, "SOURCE_REVIEW_ERROR", str(e))
        except Exception as e:
            return self._tool_error(name, "TOOL_EXEC_ERROR", "工具执行失败。", {"message": str(e)})

    def call_tool_for_agent(
        self,
        name: str,
        arguments: dict[str, Any],
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._call_tool(name, arguments, auth_context=auth_context)

    def get_required_scope_for_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return self._required_scope_for_tool(name, arguments)

    def _required_scope_for_tool(self, name: str, params: dict[str, Any]) -> str:
        required_scope = "mcp:read"
        if name in {"preview_insert_version", "preview_update_version"}:
            required_scope = "mcp:preview"
        elif name == "manage_git":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"status", "diff", "review_context", "commit_readiness", "commit_message",
                          "push_status", "pull_status", "history_log", "history_show", "diff_commits"}:
                required_scope = "mcp:read"
            elif action in {"commit_preview", "push_preview", "pull_preview",
                            "restore_file_preview", "revert_preview"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_git_commit":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"readiness", "suggest_commit_message"}:
                required_scope = "mcp:read"
            elif action in {"preview", "commit_workflow_preview"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_git_remote":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"push_status", "pull_status"}:
                required_scope = "mcp:read"
            elif action in {"push_preview", "fetch_preview", "pull_preview"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_runner_plan":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action == "inspect":
                required_scope = "mcp:read"
            elif action in {"bootstrap_preview", "import_preview"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:plan"
        elif name in {"manage_runner_record", "manage_project_memory"}:
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action == "read":
                required_scope = "mcp:read"
            else:
                required_scope = "mcp:preview"
        elif name == "manage_workflow_run":
            required_scope = "mcp:read"
        elif name == "todo_read":
            required_scope = "mcp:read"
        elif name in {"todo_add", "todo_update", "todo_delete"}:
            required_scope = "mcp:preview"
        elif name == "decision_read":
            required_scope = "mcp:read"
        elif name in {"decision_add", "decision_update", "decision_delete"}:
            required_scope = "mcp:preview"
        elif name == "manage_plan_version":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"inspect", "apply_preview_status"}:
                required_scope = "mcp:read"
            elif action in {"insert_preview", "update_preview", "repair_preview", "insert_from_prompt_file_preview"}:
                required_scope = "mcp:preview"
            elif action in {"apply_preview", "reload_plan", "continue_next_version"}:
                required_scope = "mcp:commit"
            else:
                required_scope = "mcp:preview"
        elif name == "manage_project_patch":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action == "status":
                required_scope = "mcp:read"
            elif action in {"preview", "preview_delete"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_git_history":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"log", "show", "diff_commits"}:
                required_scope = "mcp:read"
            elif action in {"restore_file_preview", "revert_preview"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_plan_workflow":
            required_scope = "mcp:preview"
        elif name == "manage_project_docs":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"index", "search", "read_section"}:
                required_scope = "mcp:read"
            elif action in {"update_section_preview", "append_section_preview", "sync_docs_preview"}:
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "run_mcp_workflow":
            workflow = params.get("workflow")
            phase = params.get("phase", "")
            docs_action = params.get("docs_action", "")
            if isinstance(workflow, str):
                workflow = workflow.strip().lower()
            if isinstance(phase, str):
                phase = phase.strip().lower()
            else:
                phase = ""
            if workflow == "auto_preview":
                required_scope = "mcp:preview"
            elif workflow == "project_status":
                required_scope = "mcp:read"
            elif workflow == "source_onboarding":
                required_scope = "mcp:preview"
            elif workflow == "plan_update":
                required_scope = "mcp:preview"
            elif workflow == "small_project_patch":
                if phase == "status":
                    required_scope = "mcp:read"
                elif phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            elif workflow == "docs_update":
                if docs_action in ("index", "search", "read_section") or phase == "inspect":
                    required_scope = "mcp:read"
                elif docs_action in ("update_section_preview", "append_section_preview", "sync_docs_preview") or phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            elif workflow == "git_commit":
                if phase in ("inspect", "status"):
                    required_scope = "mcp:read"
                elif phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            elif workflow == "git_restore_file":
                if phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            elif workflow == "git_revert":
                if phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            elif workflow == "git_undo_version":
                if phase == "inspect":
                    required_scope = "mcp:read"
                elif phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            elif workflow == "agent_dispatch":
                if phase in ("inspect", "status"):
                    required_scope = "mcp:read"
                elif phase in ("preview", "run_preview"):
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_prompt_file":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action == "status":
                required_scope = "mcp:read"
            elif action in ("preview", "discard"):
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_executor_config":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action == "inspect_inventory":
                required_scope = "mcp:read"
            elif action == "probe_models_preview":
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_executor_workflow":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in ("preflight", "status", "get_audit_package"):
                required_scope = "mcp:read"
            elif action in ("run_once_preview", "run_bounded_preview", "recheck_report_preview", "manual_validation_preview", "scope_mismatch_preview", "reconcile_orphaned_claims_preview"):
                required_scope = "mcp:preview"
            elif action in ("refresh_audit_package", "recheck_report_apply", "manual_validation_apply", "scope_mismatch_apply", "reconcile_orphaned_claims_apply"):
                required_scope = "mcp:commit"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_validation_run":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in ("inspect", "status"):
                required_scope = "mcp:read"
            elif action == "preview":
                required_scope = "mcp:preview"
            else:
                required_scope = "mcp:commit"
        elif name == "manage_files":
            action = params.get("action")
            if isinstance(action, str):
                action = action.strip().lower()
            else:
                action = None
            if action in {"create", "edit", "delete"}:
                phase = params.get("phase")
                if isinstance(phase, str):
                    phase = phase.strip().lower()
                else:
                    phase = None
                if phase == "status":
                    required_scope = "mcp:read"
                elif phase == "preview":
                    required_scope = "mcp:preview"
                else:
                    required_scope = "mcp:commit"
            else:
                required_scope = "mcp:read"
        elif name == "inspect_executor_activity":
            required_scope = "mcp:read"
        elif name in {"list_workflow_runs", "get_workflow_run"}:
            required_scope = "mcp:read"
        return required_scope

    def _oauth_scope_error(
        self,
        name: str,
        params: dict[str, Any],
        auth_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(auth_context, dict) or auth_context.get("mode") != "oauth":
            return None
        oauth_provider = auth_context.get("oauth_provider")
        token_payload = auth_context.get("token")
        if not isinstance(oauth_provider, MCPOAuthProvider) or not isinstance(token_payload, dict):
            return self._tool_error(name, "UNAUTHORIZED", "OAuth token is invalid.")
        required_scope = self._required_scope_for_tool(name, params)
        if oauth_provider.validate_scope(token_payload, required_scope):
            return None
        return self._tool_error(
            name,
            "INSUFFICIENT_SCOPE",
            "OAuth token scope is insufficient for this tool.",
        )

    def _cloud_relay_scope_error(
        self,
        name: str,
        params: dict[str, Any],
        auth_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(auth_context, dict) or auth_context.get("mode") != "cloud-relay":
            return None
        granted_scopes = auth_context.get("scopes", [])
        if not isinstance(granted_scopes, list):
            return self._tool_error(name, "UNAUTHORIZED", "cloud-relay scopes 无效。")
        required_scope = self._required_scope_for_tool(name, params)
        if required_scope in granted_scopes:
            return None
        return self._tool_error(
            name,
            "INSUFFICIENT_SCOPE",
            f"cloud-relay scope 不足，需要 {required_scope}，当前 scopes: {granted_scopes}",
        )

    def _project_identity(self) -> dict[str, Any]:
        return build_project_identity(self.project_root)

    def _project_identity_for_root(self, project_root: str) -> dict[str, Any]:
        return build_project_identity(project_root)

    def _resolve_registered_project_by_name(self, project_name: Any) -> dict[str, Any]:
        if not isinstance(project_name, str) or not project_name.strip():
            raise MCPToolInputError("INVALID_PROJECT_NAME", "project_name 必须是非空字符串。")
        result = self.project_registry.resolve_project_name(project_name.strip())
        if not result.get("ok"):
            raise MCPToolInputError(
                str(result.get("error_code") or "PROJECT_NOT_REGISTERED"),
                str(result.get("message") or "project_name 未登记。"),
                {"project_name": project_name.strip()},
            )
        project = result.get("project")
        if not isinstance(project, dict):
            raise MCPToolInputError("PROJECT_NOT_REGISTERED", "project_name 未登记。", {"project_name": project_name.strip()})
        return project

    def _resolve_managed_project_by_name(self, project_name: Any) -> dict[str, Any]:
        if not isinstance(project_name, str) or not project_name.strip():
            raise MCPToolInputError("INVALID_PROJECT_NAME", "project_name 必须是非空字符串。")
        result = self.project_registry.resolve_managed_project_name(project_name.strip())
        if not result.get("ok"):
            raise MCPToolInputError(
                str(result.get("error_code") or "PROJECT_MODE_UNSUPPORTED"),
                str(result.get("message") or "当前操作需要 managed 项目。"),
                {"project_name": project_name.strip()},
            )
        project = result.get("project")
        if not isinstance(project, dict):
            raise MCPToolInputError("PROJECT_NOT_REGISTERED", "project_name 未登记。", {"project_name": project_name.strip()})
        return project

    def _resolve_read_only_project_context(self, params: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        project_name = params.get("project_name")
        if project_name is None:
            if self.service_mode:
                raise MCPToolInputError(
                    "PROJECT_NAME_REQUIRED",
                    "项目级调用必须显式提供 project_name；服务不会替 GPTs 选择项目。",
                )
            return self.project_root, None
        project = self._resolve_registered_project_by_name(project_name)
        return str(project.get("project_root") or self.project_root), project

    def _resolve_managed_project_context(self, params: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        project_name = params.get("project_name")
        if project_name is None:
            if self.service_mode:
                raise MCPToolInputError(
                    "PROJECT_NAME_REQUIRED",
                    "项目级调用必须显式提供 project_name；服务不会替 GPTs 选择项目。",
                )
            return self.project_root, None
        project = self._resolve_managed_project_by_name(project_name)
        return str(project.get("project_root") or self.project_root), project

    def _strip_project_name_param(self, params: dict[str, Any]) -> dict[str, Any]:
        clean = dict(params)
        clean.pop("project_name", None)
        return clean

    def _route_project_name_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        require_managed: bool,
    ) -> dict[str, Any]:
        project_root_override = params.get("project_root")
        if isinstance(project_root_override, str) and project_root_override.strip():
            raise MCPToolInputError(
                "PROJECT_ROOT_OVERRIDE_NOT_ALLOWED",
                "project_name 路由不接受 project_root 覆盖。",
            )
        if require_managed:
            project_root, _ = self._resolve_managed_project_context(params)
        else:
            project_root, _ = self._resolve_read_only_project_context(params)
        routed_server = self.__class__(project_root)
        routed_tool = routed_server.tools.get(tool_name)
        if not callable(routed_tool):
            raise MCPToolInputError("TOOL_NOT_FOUND", f"未知 tool：{tool_name}")
        routed_params = self._strip_project_name_param(params)
        routed_params.pop("project_root", None)
        original_project_name = params.get("project_name")
        result = routed_tool(routed_params)
        if isinstance(result, dict) and isinstance(original_project_name, str) and original_project_name.strip():
            next_actions = _find_next_actions(result)
            if next_actions is not None:
                for action in next_actions:
                    if isinstance(action, dict):
                        action_params = action.get("params")
                        if isinstance(action_params, dict) and "project_name" not in action_params:
                            action_params["project_name"] = original_project_name.strip()
        return result

    def _list_registered_projects_payload(self) -> dict[str, Any]:
        listed = self.project_registry.list_projects()
        projects = listed.get("projects")
        if not isinstance(projects, list):
            return listed
        enriched: list[dict[str, Any]] = []
        for item in projects:
            if not isinstance(item, dict):
                continue
            project = dict(item)
            root = str(project.get("project_root") or "")
            project["available"] = os.path.isdir(root)
            if root and os.path.isdir(root):
                project["runner_managed"] = self.project_registry.is_runner_managed_project(root)
            else:
                project["runner_managed"] = False
            enriched.append(project)
        listed["projects"] = enriched
        return listed

    def _with_project_identity(self, result: dict[str, Any], project_root: str | None = None, *, hint_project_name: bool = False) -> dict[str, Any]:
        if isinstance(result, dict) and result.get("ok"):
            result["project_identity"] = self._project_identity_for_root(project_root or self.project_root)
        return result

    def _tool_list_registered_projects(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._list_registered_projects_payload()

    def _tool_get_project_identity(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, project_record = self._resolve_read_only_project_context(params)
        visible_names = self._visible_tool_names()
        return {
            "ok": True,
            "project_identity": self._project_identity_for_root(project_root),
            "mcp_exposure_profile": self.mcp_exposure_profile,
            "visible_tool_count": len(visible_names),
            "visible_tool_names": visible_names,
            "project": project_record,
        }

    def _tool_get_plan_standards_report(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("get_plan_standards_report", params, require_managed=True)
        return PlanStandardsLinter().lint_project(self.project_root)

    def _tool_get_runner_execution_standards(self, params: dict[str, Any]) -> dict[str, Any]:
        section = params.get("section")
        if section is not None and not isinstance(section, str):
            raise MCPToolInputError("INVALID_SECTION", "section 必须是字符串。")
        return get_execution_standards(section=section)

    def _tool_get_runner_status(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, project_record = self._resolve_read_only_project_context(params)
        if isinstance(project_record, dict) and project_record.get("project_mode") == "source-only":
            raise MCPToolInputError(
                "PROJECT_MODE_UNSUPPORTED",
                "source-only 项目请使用 analyze_project_state 或 run_mcp_workflow workflow=project_status phase=inspect。",
                {"project_name": project_record.get("project_name")},
            )
        return self._with_project_identity(self.bridge.get_runner_status(project_root), project_root)

    def _tool_get_executor_session_status(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._with_project_identity(ExecutorSessionStore(self.project_root).get_status())

    def _tool_get_executor_continuation_preview(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._with_project_identity(ExecutorSessionStore(self.project_root).get_continuation_preview())

    def _tool_get_executor_continuation_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        provider = params.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() not in {"pi", "codex", "opencode"}:
            raise MCPToolInputError("INVALID_PROVIDER", "provider 必须是 pi、codex 或 opencode。")
        return self._with_project_identity(
            ExecutorSessionStore(self.project_root).get_continuation_decision(
                requested_provider=provider.strip().lower()
            )
        )

    def _tool_get_executor_resume_invocation_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        provider = params.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() not in {"pi", "codex", "opencode"}:
            raise MCPToolInputError("INVALID_PROVIDER", "provider 必须是 pi、codex 或 opencode。")
        return self._with_project_identity(
            ExecutorSessionStore(self.project_root).get_resume_invocation_preview(
                requested_provider=provider.strip().lower()
            )
        )

    def _tool_get_review_context(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("get_review_context", params, require_managed=True)
        max_diff_chars = self._bounded_int_param(params.get("max_diff_chars"), default=60000, minimum=1, maximum=120000)
        include_log = self._bool_param(params.get("include_log"), default=True)
        log_limit = self._bounded_int_param(params.get("log_limit"), default=5, minimum=1, maximum=20)
        include_repo_overview = self._bool_param(params.get("include_repo_overview"), default=False)
        max_files = self._bounded_int_param(params.get("max_files"), default=200, minimum=1, maximum=500)

        partial_errors: list[dict[str, str]] = []
        review_hints: list[str] = []
        data: dict[str, Any] = {
            "project_identity": self._project_identity(),
            "git_status": None,
            "git_diff": None,
            "git_log": None,
            "repo_overview": None,
            "changed_files": [],
            "untracked_files": [],
            "is_dirty": None,
            "has_untracked_runtime": False,
            "runtime_untracked_files": [],
            "review_hints": review_hints,
            "partial_errors": partial_errors,
        }

        git_status_item = self._collect_context_item("git_status", self._tool_get_git_status, {}, partial_errors)
        data["git_status"] = git_status_item["result"]

        git_diff_item = self._collect_context_item(
            "git_diff",
            self._tool_get_git_diff,
            {"max_chars": max_diff_chars},
            partial_errors,
        )
        data["git_diff"] = git_diff_item["result"]

        git_log_result: dict[str, Any] | None = None
        if include_log:
            git_log_item = self._collect_context_item(
                "git_log",
                self._tool_get_git_log,
                {"limit": log_limit},
                partial_errors,
            )
            git_log_result = git_log_item["result"]
        data["git_log"] = git_log_result

        repo_overview_result: dict[str, Any] | None = None
        if include_repo_overview:
            repo_overview_item = self._collect_context_item(
                "repo_overview",
                self._tool_get_repo_overview,
                {"max_files": max_files, "max_depth": 3},
                partial_errors,
            )
            repo_overview_result = repo_overview_item["result"]
        data["repo_overview"] = repo_overview_result

        changed_files: list[str] = []
        untracked_files: list[str] = []
        status_payload = data["git_status"]
        if isinstance(status_payload, dict) and status_payload.get("ok"):
            changed_files = [str(item) for item in status_payload.get("changed_files", []) if isinstance(item, str)]
            untracked_files = [str(item) for item in status_payload.get("untracked_files", []) if isinstance(item, str)]
        data["changed_files"] = changed_files
        data["untracked_files"] = untracked_files
        data["is_dirty"] = bool(changed_files or untracked_files)

        runtime_untracked = [item for item in untracked_files if is_project_runner_path(item)]
        data["runtime_untracked_files"] = runtime_untracked
        data["has_untracked_runtime"] = bool(runtime_untracked)

        if data["is_dirty"] is False:
            review_hints.append("working_tree_clean")
        if runtime_untracked and len(runtime_untracked) == len(untracked_files):
            review_hints.append("only_local_runner_runtime_untracked")
        if changed_files:
            review_hints.append("review_git_diff_before_commit")
        non_runtime_untracked = [item for item in untracked_files if item not in runtime_untracked]
        if non_runtime_untracked:
            review_hints.append("untracked_non_runtime_files_require_attention")
        diff_payload = data["git_diff"]
        data["diff_truncated"] = False
        data["diff_summary_available"] = False
        data["recommended_next_action"] = None
        if isinstance(diff_payload, dict) and diff_payload.get("ok") and diff_payload.get("truncated"):
            data["diff_truncated"] = True
            data["diff_summary_available"] = True
            data["recommended_next_action"] = 'manage_git diff(mode="summary")'
            review_hints.append("diff_truncated_review_specific_files")

        return data

    def _tool_get_runner_workbench_context(self, params: dict[str, Any]) -> dict[str, Any]:
        include_runner_state = self._bool_param(params.get("include_runner_state"), default=True)
        include_executor = self._bool_param(params.get("include_executor"), default=True)
        include_git_status = self._bool_param(params.get("include_git_status"), default=True)

        provider_raw = params.get("provider")
        provider: str | None = None
        if provider_raw is not None:
            if not isinstance(provider_raw, str) or provider_raw.strip().lower() not in {"pi", "codex", "opencode"}:
                raise MCPToolInputError("INVALID_PROVIDER", "provider 必须是 pi、codex 或 opencode。")
            provider = provider_raw.strip().lower()

        partial_errors: list[dict[str, str]] = []
        context: dict[str, Any] = {
            "project_identity": self._project_identity(),
            "runner_status": None,
            "current_version_result": None,
            "next_version_plan": None,
            "plan_overview": None,
            "executor_session_status": None,
            "executor_continuation_preview": None,
            "executor_continuation_decision": None,
            "executor_resume_invocation_preview": None,
            "git_status": None,
            "summary": {},
            "partial_errors": partial_errors,
        }

        item_states: dict[str, bool] = {}

        if include_runner_state:
            runner_status_item = self._collect_context_item(
                "runner_status",
                self._tool_get_runner_status,
                {},
                partial_errors,
            )
            context["runner_status"] = runner_status_item["result"]
            item_states["runner_status"] = runner_status_item["ok"]

            version_result_item = self._collect_context_item(
                "current_version_result",
                self._tool_get_version_result,
                {},
                partial_errors,
            )
            context["current_version_result"] = version_result_item["result"]
            item_states["current_version_result"] = version_result_item["ok"]

            next_plan_item = self._collect_context_item(
                "next_version_plan",
                self._tool_get_next_version_plan,
                {},
                partial_errors,
            )
            context["next_version_plan"] = next_plan_item["result"]
            item_states["next_version_plan"] = next_plan_item["ok"]

            plan_overview_item = self._collect_context_item(
                "plan_overview",
                self._tool_get_plan_overview,
                {},
                partial_errors,
            )
            context["plan_overview"] = plan_overview_item["result"]
            item_states["plan_overview"] = plan_overview_item["ok"]

        if include_executor:
            session_item = self._collect_context_item(
                "executor_session_status",
                self._tool_get_executor_session_status,
                {},
                partial_errors,
            )
            context["executor_session_status"] = session_item["result"]
            item_states["executor_session_status"] = session_item["ok"]

            continuation_item = self._collect_context_item(
                "executor_continuation_preview",
                self._tool_get_executor_continuation_preview,
                {},
                partial_errors,
            )
            context["executor_continuation_preview"] = continuation_item["result"]
            item_states["executor_continuation_preview"] = continuation_item["ok"]

            if provider is not None:
                decision_item = self._collect_context_item(
                    "executor_continuation_decision",
                    self._tool_get_executor_continuation_decision,
                    {"provider": provider},
                    partial_errors,
                )
                context["executor_continuation_decision"] = decision_item["result"]
                item_states["executor_continuation_decision"] = decision_item["ok"]

                invocation_item = self._collect_context_item(
                    "executor_resume_invocation_preview",
                    self._tool_get_executor_resume_invocation_preview,
                    {"provider": provider},
                    partial_errors,
                )
                context["executor_resume_invocation_preview"] = invocation_item["result"]
                item_states["executor_resume_invocation_preview"] = invocation_item["ok"]

        if include_git_status:
            git_status_item = self._collect_context_item(
                "git_status",
                self._tool_get_git_status,
                {},
                partial_errors,
            )
            context["git_status"] = git_status_item["result"]
            item_states["git_status"] = git_status_item["ok"]

        working_tree_clean: bool | None = None
        if isinstance(context["git_status"], dict) and context["git_status"].get("ok"):
            changed_files = context["git_status"].get("changed_files", [])
            untracked_files = context["git_status"].get("untracked_files", [])
            if isinstance(changed_files, list) and isinstance(untracked_files, list):
                working_tree_clean = len(changed_files) == 0 and len(untracked_files) == 0

        has_executor_session = False
        session_payload = context.get("executor_session_status")
        if isinstance(session_payload, dict):
            has_executor_session = bool(session_payload.get("active")) or isinstance(session_payload.get("record"), dict)

        recommended_next_reads: list[str] = []
        if working_tree_clean is False:
            recommended_next_reads.append("manage_git review_context")
        if include_runner_state and not item_states.get("runner_status", False):
            recommended_next_reads.extend(["get_repo_overview", "get_source_file"])
        if include_runner_state and item_states.get("plan_overview", False) and not item_states.get("next_version_plan", False):
            recommended_next_reads.append("get_next_version_plan")

        plan_path = resolve_project_runner_path(self.project_root, "plan.json")
        state_path = resolve_project_runner_path(self.project_root, "state.json")
        has_plan_file = os.path.isfile(plan_path)
        has_state_file = os.path.isfile(state_path)
        mode = self._build_state_mode(has_plan_file, has_state_file)

        blockers: list[str] = []
        warnings: list[str] = []
        if mode == "plan_without_state":
            blockers.append("state_missing")
        elif mode == "state_without_plan":
            blockers.append("plan_missing")
        elif mode == "invalid_or_partial":
            blockers.append("runner_state_invalid")
        if working_tree_clean is False:
            warnings.append("working_tree_dirty")

        recommended_workflows: list[str] = []
        if mode == "source_only":
            recommended_workflows.extend([
                "analyze_project_state",
                "manage_runner_plan.inspect",
                "manage_runner_plan.bootstrap_preview",
            ])
        else:
            recommended_workflows.append("analyze_project_state")
        if working_tree_clean is False:
            recommended_workflows.extend([
                "manage_git review_context",
                "manage_git commit_readiness",
            ])

        context["summary"] = {
            "has_runner_state": bool(item_states.get("runner_status", False)),
            "has_plan": bool(item_states.get("plan_overview", False)),
            "has_executor_session": has_executor_session,
            "working_tree_clean": working_tree_clean,
            "recommended_next_reads": recommended_next_reads,
            "mode": mode,
            "blockers": blockers,
            "warnings": warnings,
            "recommended_workflows": recommended_workflows,
        }
        return context

    def _tool_analyze_project_state(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, project_record = self._resolve_read_only_project_context(params)
        routed_params = self._strip_project_name_param(params)
        include_repo_overview = self._bool_param(params.get("include_repo_overview"), default=False)
        include_reports = self._bool_param(params.get("include_reports"), default=True)
        max_files = self._bounded_int_param(params.get("max_files"), default=200, minimum=1, maximum=500)

        provider_raw = params.get("provider")
        provider: str | None = None
        if provider_raw is not None:
            if not isinstance(provider_raw, str) or provider_raw.strip().lower() not in {"pi", "codex", "opencode"}:
                raise MCPToolInputError("INVALID_PROVIDER", "provider 必须是 pi、codex 或 opencode。")
            provider = provider_raw.strip().lower()

        orchestrator = WorkflowOrchestrator(
            project_root=project_root,
            source_review=self.source_review,
            planning_bridge=self.bridge,
        )
        fact_snapshot = orchestrator.build_fact_snapshot(provider=provider, include_reports=include_reports)

        core_output = orchestrator._build_analyze_core_output(fact_snapshot)

        repo_overview = None
        partial_errors = list(fact_snapshot.partial_errors)
        if include_repo_overview:
            repo_item = self._collect_context_item(
                "repo_overview", self._tool_get_repo_overview,
                {"max_files": max_files, "max_depth": 3, **({"project_name": project_record.get("project_name")} if isinstance(project_record, dict) else {})}, partial_errors,
            )
            repo_overview = repo_item["result"]

        legacy = {
            "ok": True,
            "project_identity": fact_snapshot.project_identity,
            "mcp_exposure_profile": self.mcp_exposure_profile,
            "visible_tool_count": len(self._visible_tool_names()),
            "visible_tool_names": self._visible_tool_names(),
            "mode": fact_snapshot.mode,
            "risk_level": core_output.risk_level,
            "git": core_output.result.get("git") if isinstance(core_output.result, dict) else {},
            "runner": core_output.result.get("runner") if isinstance(core_output.result, dict) else {},
            "plan": core_output.result.get("plan") if isinstance(core_output.result, dict) else {},
            "executor": core_output.result.get("executor") if isinstance(core_output.result, dict) else {},
            "reports": core_output.result.get("reports") if isinstance(core_output.result, dict) else {},
            "summary": fact_snapshot.summary,
            "recommended_next_actions": self._normalize_recommended_actions_for_visible_tools(
                self._with_maintainer_review_recommendation(list(core_output.next_actions))
            ),
            "repo_overview": repo_overview,
            "blockers": list(core_output.blockers),
            "warnings": list(core_output.warnings),
            "unreconciled_direct_version_count": fact_snapshot.unreconciled_direct_version_count,
            "unreconciled_direct_versions": fact_snapshot.unreconciled_direct_versions,
            "partial_errors": partial_errors,
        }

        if project_record is None:
            self._record_workflow_if_needed("analyze_project_state", "analyze", routed_params, legacy)
        return legacy

    def _tool_inspect_executor_activity(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action", "")
        if not isinstance(action_raw, str) or not action_raw.strip():
            return {
                "ok": False,
                "error_code": "ACTION_REQUIRED",
                "message": "action 不能为空。支持：run_status、latest_run_status、list_reports、get_report、get_audit_summary。",
            }
        action = action_raw.strip().lower()
        if action not in ("run_status", "latest_run_status", "list_reports", "get_report", "get_audit_summary"):
            return {
                "ok": False,
                "error_code": "UNKNOWN_ACTION",
                "message": "不支持的 action。支持：run_status、latest_run_status、list_reports、get_report、get_audit_summary。",
            }
        if params.get("project_name") is not None:
            return self._route_project_name_tool("inspect_executor_activity", params, require_managed=True)
        return handle_inspect_executor_activity(self.project_root, action, params)

    def _build_state_mode(self, has_plan: bool, has_state: bool) -> str:
        if not has_plan and not has_state:
            return "source_only"
        if has_plan and has_state:
            return "runner_managed"
        if has_plan and not has_state:
            return "plan_without_state"
        if not has_plan and has_state:
            return "state_without_plan"
        return "invalid_or_partial"


    def _with_maintainer_review_recommendation(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.mcp_exposure_profile != MCP_EXPOSURE_PROFILE_MAINTAINER:
            return actions
        if any(isinstance(item, dict) and item.get("tool") == "manage_git" and item.get("params", {}).get("action") == "review_context" for item in actions):
            return actions
        return [
            *actions,
            {
                "action": "review_context",
                "label": "读取审查上下文",
                "reason": "maintainer profile 保留 manage_git review_context 审查入口。",
                "tool": "manage_git",
                "params": {"action": "review_context"},
                "risk_level": "none",
                "requires_confirmation": False,
            },
        ]

    def _normalize_recommended_actions_for_visible_tools(
        self,
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        visible_names = set(self._visible_tool_names())
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()

        for action in actions:
            if not isinstance(action, dict):
                continue
            candidate = dict(action)
            tool = str(candidate.get("tool") or "")
            if tool not in visible_names:
                candidate = self._replace_hidden_recommended_action(candidate, visible_names)
            if not isinstance(candidate, dict):
                continue
            candidate_tool = str(candidate.get("tool") or "")
            if candidate_tool not in visible_names:
                if "analyze_project_state" not in visible_names:
                    continue
                candidate = self._fallback_analyze_action()
                candidate_tool = "analyze_project_state"
            key = self._recommended_action_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(candidate)

        if not normalized and "analyze_project_state" in visible_names:
            normalized.append(self._fallback_analyze_action())
        return normalized

    def _replace_hidden_recommended_action(
        self,
        action: dict[str, Any],
        visible_names: set[str],
    ) -> dict[str, Any]:
        tool = str(action.get("tool") or "")
        if tool == "manage_runner_plan":
            if "run_mcp_workflow" in visible_names:
                return {
                    "action": "source_onboarding",
                    "label": "生成纳管预览",
                    "reason": "当前 profile 仅展示高层入口，使用 run_mcp_workflow source_onboarding preview。",
                    "tool": "run_mcp_workflow",
                    "params": {"workflow": "source_onboarding", "phase": "preview"},
                    "risk_level": "info",
                    "requires_confirmation": True,
                }
            return self._fallback_analyze_action()

        if tool in {"get_review_context", "get_git_status", "get_git_diff"}:
            if "manage_git" in visible_names:
                return {
                    "action": "status",
                    "label": "检查 Git 状态",
                    "reason": "当前 profile 仅展示高层入口，使用 manage_git status。",
                    "tool": "manage_git",
                    "params": {"action": "status"},
                    "risk_level": "info",
                    "requires_confirmation": False,
                }
            if "manage_git_commit" in visible_names:
                return {
                    "action": "commit_readiness",
                    "label": "检查提交准备状态",
                    "reason": "当前 profile 仅展示高层入口，使用 manage_git_commit readiness。",
                    "tool": "manage_git_commit",
                    "params": {"action": "readiness"},
                    "risk_level": "info",
                    "requires_confirmation": False,
                }
            if "run_mcp_workflow" in visible_names:
                return {
                    "action": "git_commit_inspect",
                    "label": "审查并提交改动",
                    "reason": "当前 profile 仅展示高层入口，使用 run_mcp_workflow git_commit inspect。",
                    "tool": "run_mcp_workflow",
                    "params": {"workflow": "git_commit", "phase": "inspect"},
                    "risk_level": "info",
                    "requires_confirmation": False,
                }
            return self._fallback_analyze_action()

        if tool in {"list_executor_run_reports", "get_executor_run_report", "get_executor_session_status"}:
            if "manage_executor_workflow" in visible_names:
                return {
                    "action": "executor_status",
                    "label": "查看执行器会话状态",
                    "reason": "当前 profile 仅展示高层入口，使用 manage_executor_workflow status。",
                    "tool": "manage_executor_workflow",
                    "params": {"action": "status"},
                    "risk_level": "info",
                    "requires_confirmation": False,
                }
            return self._fallback_analyze_action()

        if tool == "none":
            return self._fallback_analyze_action()

        return self._fallback_analyze_action()

    def _recommended_action_key(self, action: dict[str, Any]) -> str:
        tool = str(action.get("tool") or "")
        action_name = str(action.get("action") or "")
        params = action.get("params", {})
        try:
            params_key = json.dumps(params, ensure_ascii=False, sort_keys=True)
        except Exception:
            params_key = str(params)
        return f"{tool}|{action_name}|{params_key}"

    def _fallback_analyze_action(self) -> dict[str, Any]:
        return {
            "action": "refresh_project_state",
            "label": "刷新项目状态",
            "reason": "使用 analyze_project_state 获取当前可见范围内的下一步建议。",
            "tool": "analyze_project_state",
            "params": {},
            "risk_level": "none",
            "requires_confirmation": False,
        }

    def _append_context_error(self, name: str, message: str, partial_errors: list[dict[str, str]]) -> None:
        partial_errors.append({
            "name": name,
            "error_code": "CONTEXT_ERROR",
            "message": str(message),
        })

    def _tool_manage_plan_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"source_onboarding_preview", "plan_repair_preview", "plan_extend_preview"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 source_onboarding_preview、plan_repair_preview 或 plan_extend_preview。")

        if params.get("project_name") is not None:
            if action not in {"plan_repair_preview", "plan_extend_preview"}:
                raise MCPToolInputError(
                    "PROJECT_NAME_ROUTING_NOT_SUPPORTED",
                    "project_name 路由当前仅支持 manage_plan_workflow 的 managed preview：plan_repair_preview、plan_extend_preview。",
                )
            return self._route_project_name_tool("manage_plan_workflow", params, require_managed=True)

        manager = MCPPlanWorkflowManager(self.project_root, self.source_review)
        result = manager.handle(action, params)
        self._record_workflow_if_needed("manage_plan_workflow", action, params, result)
        if isinstance(result, dict):
            result["_legacy_warning"] = "manage_plan_workflow 已弃用。新流程请使用 manage_runner_plan（source-only 纳管）或 manage_plan_version（版本管理）。"
            result.setdefault("warnings", []).append("manage_plan_workflow 已弃用，请使用 manage_runner_plan 或 manage_plan_version。")
        return result

    def _tool_manage_project_docs(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_project_docs", params, require_managed=True)
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"index", "search", "read_section", "update_section_preview", "append_section_preview", "sync_docs_preview", "apply"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 index、search、read_section、update_section_preview、append_section_preview、sync_docs_preview 或 apply。")

        manager = MCPProjectDocsManager(self.project_root, self.source_review)
        result = manager.handle(action, params)
        self._record_workflow_if_needed("manage_project_docs", action, params, result)
        return result

    def _tool_manage_prompt_file(self, params: dict[str, Any]) -> dict[str, Any]:
        from runner.mcp_prompt_file import MCPPromptFileManager
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"preview", "apply", "status", "discard"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 preview、apply、status 或 discard。")

        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_prompt_file", params, require_managed=True)

        manager = MCPPromptFileManager(self.project_root)
        result = manager.handle(action, params)
        self._record_workflow_if_needed("manage_prompt_file", action, params, result)
        return result

    def _tool_manage_git(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        all_actions = {
            "status", "diff", "review_context",
            "commit_readiness", "commit_message", "commit_preview", "commit_apply",
            "push_status", "push_preview", "push_apply",
            "pull_status", "pull_preview", "pull_apply",
            "history_log", "history_show", "diff_commits",
            "restore_file_preview", "restore_file_apply",
            "revert_preview", "revert_apply",
        }
        if action not in all_actions:
            return {
                "ok": False,
                "error_code": "UNSUPPORTED_ACTION",
                "message": f"manage_git action '{action}' 暂无安全的路由目标，不自行创建新行为。",
                "action": action,
            }

        def _with_common_fields(result: dict[str, Any], delegated_tool: str) -> dict[str, Any]:
            if isinstance(result, dict):
                result["delegated_tool"] = delegated_tool
                result["action"] = action
            return result

        record_and_return = lambda result, tool: (self._record_workflow_if_needed("manage_git", action, params, result), _with_common_fields(result, tool))[1]

        # --- status: delegates to get_git_status ---
        if action == "status":
            status_params = {}
            if params.get("project_name") is not None:
                status_params["project_name"] = params["project_name"]
                return self._route_project_name_tool("manage_git", params, require_managed=True)
            result = self._tool_get_git_status(status_params)
            return record_and_return(result, "get_git_status")

        # --- diff: delegates to get_git_diff ---
        if action == "diff":
            diff_params: dict[str, Any] = {}
            for key in ("mode", "file", "include_files", "offset", "max_chars", "cached", "project_name"):
                if key in params:
                    diff_params[key] = params[key]
            if diff_params.get("project_name") is not None:
                return self._route_project_name_tool("manage_git", params, require_managed=True)
            result = self._tool_get_git_diff(diff_params)
            return record_and_return(result, "get_git_diff")

        # --- review_context: delegates to get_review_context ---
        if action == "review_context":
            ctx_params: dict[str, Any] = {}
            for key in ("max_diff_chars", "include_log", "log_limit", "include_repo_overview", "max_files", "project_name"):
                if key in params:
                    ctx_params[key] = params[key]
            if ctx_params.get("project_name") is not None:
                return self._route_project_name_tool("manage_git", params, require_managed=True)
            result = self._tool_get_review_context(ctx_params)
            return record_and_return(result, "get_review_context")

        # --- commit_readiness -> manage_git_commit readiness ---
        if action == "commit_readiness":
            delegate_params: dict[str, Any] = {"action": "readiness"}
            for key in ("include_diff_summary", "max_diff_chars", "include_files", "exclude_files", "project_name"):
                if key in params:
                    delegate_params[key] = params[key]
            result = self._delegate_manage_git_commit(delegate_params, record=False)
            return record_and_return(result, "manage_git_commit")

        # --- commit_message -> manage_git_commit suggest_commit_message ---
        if action == "commit_message":
            delegate_params = {"action": "suggest_commit_message"}
            for key in ("include_diff_summary", "max_diff_chars", "style", "scope_hint", "include_files", "exclude_files", "project_name"):
                if key in params:
                    delegate_params[key] = params[key]
            result = self._delegate_manage_git_commit(delegate_params, record=False)
            return record_and_return(result, "manage_git_commit")

        # --- commit_preview -> manage_git_commit preview ---
        if action == "commit_preview":
            message = params.get("message")
            if not isinstance(message, str) or not message.strip():
                raise MCPToolInputError("INVALID_MESSAGE", "commit_preview 需要非空 message。")
            delegate_params = {"action": "preview", "message": message.strip()}
            for key in ("include_diff_summary", "max_diff_chars", "include_files", "exclude_files", "project_name"):
                if key in params:
                    delegate_params[key] = params[key]
            result = self._delegate_manage_git_commit(delegate_params, record=False)
            return record_and_return(result, "manage_git_commit")

        # --- commit_apply -> manage_git_commit commit ---
        if action == "commit_apply":
            preview_id = params.get("preview_id")
            if not isinstance(preview_id, str) or not preview_id.strip():
                raise MCPToolInputError("INVALID_PREVIEW_ID", "commit_apply 需要非空 preview_id。")
            delegate_params = {"action": "commit", "preview_id": preview_id.strip()}
            msg = params.get("message")
            if isinstance(msg, str) and msg.strip():
                delegate_params["message"] = msg.strip()
            if params.get("project_name") is not None:
                delegate_params["project_name"] = params["project_name"]
            result = self._delegate_manage_git_commit(delegate_params, record=False)
            return record_and_return(result, "manage_git_commit")

        # --- push/pull actions -> manage_git_remote ---
        if action in ("push_status", "push_preview", "push_apply"):
            result = self._delegate_manage_git_remote(action, params, record=False)
            return record_and_return(result, "manage_git_remote")
        if action in ("pull_status", "pull_preview", "pull_apply"):
            result = self._delegate_manage_git_remote(action, params, record=False)
            return record_and_return(result, "manage_git_remote")

        # --- history actions -> manage_git_history ---
        if action in ("history_log", "history_show", "diff_commits",
                      "restore_file_preview", "restore_file_apply",
                      "revert_preview", "revert_apply"):
            mapped = {
                "history_log": "log",
                "history_show": "show",
                "diff_commits": "diff_commits",
                "restore_file_preview": "restore_file_preview",
                "restore_file_apply": "restore_file_apply",
                "revert_preview": "revert_preview",
                "revert_apply": "revert_apply",
            }
            history_action = mapped[action]
            history_params: dict[str, Any] = {"action": history_action}
            for key in ("commit", "base", "head", "file", "preview_id", "limit", "max_chars", "include_patch", "reason", "scan_limit", "project_name"):
                if key in params:
                    history_params[key] = params[key]
            if history_params.get("project_name") is not None:
                return self._route_project_name_tool("manage_git", params, require_managed=True)
            manager = MCPGitHistoryManager(self.project_root, self.source_review)
            result = manager.handle(history_action, history_params)
            return record_and_return(result, "manage_git_history")

        return {
            "ok": False,
            "error_code": "UNSUPPORTED_ACTION",
            "message": f"manage_git action '{action}' 暂无安全的路由目标，不自行创建新行为。",
            "action": action,
        }

    def _delegate_manage_git_commit(self, delegate_params: dict[str, Any], *, record: bool = True) -> dict[str, Any]:
        project_name = delegate_params.get("project_name")
        if project_name is not None:
            return self._route_project_name_tool("manage_git_commit", delegate_params, require_managed=True)
        action = delegate_params.get("action", "")
        manager = MCPGitCommitManager(self.project_root)

        if action == "readiness":
            result = manager.readiness(
                include_diff_summary=delegate_params.get("include_diff_summary", True),
                max_diff_chars=delegate_params.get("max_diff_chars", 40000),
                include_files=delegate_params.get("include_files"),
                exclude_files=delegate_params.get("exclude_files"),
            )
        elif action == "suggest_commit_message":
            result = manager.suggest_commit_message(
                include_diff_summary=delegate_params.get("include_diff_summary", True),
                max_diff_chars=delegate_params.get("max_diff_chars", 40000),
                style=delegate_params.get("style", "runner_version"),
                scope_hint=delegate_params.get("scope_hint"),
                include_files=delegate_params.get("include_files"),
                exclude_files=delegate_params.get("exclude_files"),
            )
        elif action == "preview":
            message = delegate_params.get("message", "")
            result = manager.preview(
                message=message.strip(),
                include_diff_summary=delegate_params.get("include_diff_summary", True),
                max_diff_chars=delegate_params.get("max_diff_chars", 40000),
                include_files=delegate_params.get("include_files"),
                exclude_files=delegate_params.get("exclude_files"),
            )
        elif action == "commit":
            result = manager.commit(
                preview_id=delegate_params.get("preview_id", "").strip(),
                message=delegate_params.get("message"),
            )
        else:
            return {"ok": False, "error_code": "INVALID_ACTION", "message": f"未知 manage_git_commit action：{action}"}

        if record:
            self._record_workflow_if_needed("manage_git_commit", action, delegate_params, result)
        return result

    def _delegate_manage_git_remote(self, action: str, params: dict[str, Any], *, record: bool = True) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_git_remote", params, require_managed=True)
        manager = MCPGitRemoteManager(self.project_root)
        if action == "push_status":
            result = manager.push_status()
        elif action == "push_preview":
            reason = params.get("reason")
            reason_str = reason.strip() if isinstance(reason, str) else None
            result = manager.push_preview(reason=reason_str)
        elif action == "push_apply":
            preview_id = params.get("preview_id")
            if not isinstance(preview_id, str) or not preview_id.strip():
                return {"ok": False, "error_code": "INVALID_PREVIEW_ID", "message": "push_apply 需要非空 preview_id。"}
            result = manager.push_apply(preview_id.strip())
        elif action == "pull_status":
            result = manager.pull_status()
        elif action == "pull_preview":
            reason = params.get("reason")
            reason_str = reason.strip() if isinstance(reason, str) else None
            result = manager.pull_preview(reason=reason_str)
        elif action == "pull_apply":
            preview_id = params.get("preview_id")
            if not isinstance(preview_id, str) or not preview_id.strip():
                return {"ok": False, "error_code": "INVALID_PREVIEW_ID", "message": "pull_apply 需要非空 preview_id。"}
            result = manager.pull_apply(preview_id.strip())
        else:
            return {"ok": False, "error_code": "INVALID_ACTION", "message": f"未知 manage_git_remote action：{action}"}
        if record:
            self._record_workflow_if_needed("manage_git_remote", action, params, result)
        return result

    def _tool_manage_git_commit(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"readiness", "suggest_commit_message", "commit_workflow_preview", "preview", "commit"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 readiness、suggest_commit_message、commit_workflow_preview、preview 或 commit。")

        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_git_commit", params, require_managed=True)

        include_diff_summary = self._bool_param(params.get("include_diff_summary"), default=True)
        max_diff_chars = self._bounded_int_param(
            params.get("max_diff_chars"),
            default=40000,
            minimum=1,
            maximum=80000,
        )
        style = params.get("style")
        if not isinstance(style, str) or style not in {"conventional", "runner_version", "concise"}:
            style = "runner_version"
        scope_hint = params.get("scope_hint")
        if not isinstance(scope_hint, str) or not scope_hint.strip():
            scope_hint = None
        include_files = params.get("include_files")
        exclude_files = params.get("exclude_files")

        manager = MCPGitCommitManager(self.project_root)

        if action == "readiness":
            return manager.readiness(
                include_diff_summary=include_diff_summary,
                max_diff_chars=max_diff_chars,
                include_files=include_files,
                exclude_files=exclude_files,
            )

        if action == "suggest_commit_message":
            result = manager.suggest_commit_message(
                include_diff_summary=include_diff_summary,
                max_diff_chars=max_diff_chars,
                style=style,
                scope_hint=scope_hint,
                include_files=include_files,
                exclude_files=exclude_files,
            )
            self._record_workflow_if_needed("manage_git_commit", action, params, result)
            return result

        if action == "commit_workflow_preview":
            message = params.get("message")
            if message is not None:
                if not isinstance(message, str) or not message.strip():
                    raise MCPToolInputError("INVALID_MESSAGE", "message 必须是非空字符串。")
                if len(message.strip()) > 200:
                    raise MCPToolInputError("INVALID_MESSAGE", "message 长度不能超过 200。")
            result = manager.commit_workflow_preview(
                message=message.strip() if isinstance(message, str) else None,
                include_diff_summary=include_diff_summary,
                max_diff_chars=max_diff_chars,
                style=style,
                scope_hint=scope_hint,
                include_files=include_files,
                exclude_files=exclude_files,
            )
            self._record_workflow_if_needed("manage_git_commit", action, params, result)
            return result

        if action == "preview":
            message = params.get("message")
            if not isinstance(message, str) or not message.strip():
                raise MCPToolInputError("INVALID_MESSAGE", "preview 操作需要非空 message。")
            normalized_message = message.strip()
            if len(normalized_message) > 200:
                raise MCPToolInputError("INVALID_MESSAGE", "message 长度不能超过 200。")
            result = manager.preview(
                message=normalized_message,
                include_diff_summary=include_diff_summary,
                max_diff_chars=max_diff_chars,
                include_files=include_files,
                exclude_files=exclude_files,
            )
            self._record_workflow_if_needed("manage_git_commit", action, params, result)
            return result

        if include_files is not None or exclude_files is not None:
            raise MCPToolInputError(
                "INVALID_FILE_SELECTION",
                "commit 操作不接受 include_files 或 exclude_files，请使用 preview 中保存的文件集合。",
            )
        preview_id = params.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            raise MCPToolInputError("INVALID_PREVIEW_ID", "commit 操作需要 preview_id。")
        message = params.get("message")
        if message is not None:
            if not isinstance(message, str) or not message.strip():
                raise MCPToolInputError("INVALID_MESSAGE", "message 必须是非空字符串。")
            normalized_message = message.strip()
            if len(normalized_message) > 200:
                raise MCPToolInputError("INVALID_MESSAGE", "message 长度不能超过 200。")
        else:
            normalized_message = None
        result = manager.commit(preview_id=preview_id.strip(), message=normalized_message)
        self._record_workflow_if_needed("manage_git_commit", action, params, result)
        return result

    def _tool_manage_git_remote(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        allowed_actions = {
            "push_status",
            "push_preview",
            "push_apply",
            "fetch_preview",
            "fetch_apply",
            "pull_status",
            "pull_preview",
            "pull_apply",
        }
        if action not in allowed_actions:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 manage_git_remote 支持的受控 action。")

        if params.get("project_name") is not None:
            if action not in {"push_status", "push_preview", "push_apply"}:
                raise MCPToolInputError(
                    "PROJECT_NAME_ROUTING_NOT_SUPPORTED",
                    "project_name 路由当前仅支持 manage_git_remote 的 push_status、push_preview、push_apply。",
                )
            return self._route_project_name_tool("manage_git_remote", params, require_managed=True)

        manager = MCPGitRemoteManager(self.project_root)
        if action == "push_status":
            result = manager.push_status()
            self._record_workflow_if_needed("manage_git_remote", action, params, result)
            return result
        if action == "push_preview":
            reason = params.get("reason")
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise MCPToolInputError("INVALID_REASON", "reason 必须是非空字符串。")
            result = manager.push_preview(reason=reason.strip() if isinstance(reason, str) else None)
            self._record_workflow_if_needed("manage_git_remote", action, params, result)
            return result
        if action == "fetch_preview":
            reason = params.get("reason")
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise MCPToolInputError("INVALID_REASON", "reason 必须是非空字符串。")
            result = manager.fetch_preview(reason=reason.strip() if isinstance(reason, str) else None)
            self._record_workflow_if_needed("manage_git_remote", action, params, result)
            return result
        if action == "pull_status":
            result = manager.pull_status()
            self._record_workflow_if_needed("manage_git_remote", action, params, result)
            return result
        if action == "pull_preview":
            reason = params.get("reason")
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise MCPToolInputError("INVALID_REASON", "reason 必须是非空字符串。")
            result = manager.pull_preview(reason=reason.strip() if isinstance(reason, str) else None)
            self._record_workflow_if_needed("manage_git_remote", action, params, result)
            return result
        preview_id = params.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            raise MCPToolInputError("INVALID_PREVIEW_ID", f"{action} 需要 preview_id。")
        if action == "push_apply":
            result = manager.push_apply(preview_id.strip())
        elif action == "fetch_apply":
            result = manager.fetch_apply(preview_id.strip())
        else:
            result = manager.pull_apply(preview_id.strip())
        self._record_workflow_if_needed("manage_git_remote", action, params, result)
        return result

    def _tool_manage_runner_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"inspect", "bootstrap_preview", "import_preview", "apply"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 inspect、bootstrap_preview、import_preview 或 apply。")

        manager = MCPRunnerPlanManager(self.project_root)

        if action == "inspect":
            return manager.inspect()

        if action == "bootstrap_preview":
            project_name = params.get("project_name")
            if not isinstance(project_name, str) or not project_name.strip():
                raise MCPToolInputError("INVALID_PROJECT_NAME", "bootstrap_preview 需要非空 project_name。")
            return manager.bootstrap_preview(
                project_name=project_name.strip(),
            )

        if action == "import_preview":
            plan_json = params.get("plan_json")
            if not isinstance(plan_json, str) or not plan_json.strip():
                raise MCPToolInputError("INVALID_PLAN_JSON", "import_preview 需要非空 plan_json 字符串。")
            return manager.import_preview(plan_json=plan_json)

        preview_id = params.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            raise MCPToolInputError("INVALID_PREVIEW_ID", "apply 操作需要 preview_id。")
        allow_overwrite = self._bool_param(params.get("allow_overwrite"), default=False)
        result = manager.apply(preview_id=preview_id.strip(), allow_overwrite=allow_overwrite)
        if isinstance(result, dict) and result.get("ok"):
            version_count = int(result.get("plan_summary", {}).get("version_count", 0))
            next_actions = [
                {
                    "tool": "run_mcp_workflow",
                    "action": "project_status.inspect",
                    "params": {"workflow": "project_status", "phase": "inspect"},
                    "reason": "先读取纳管后的统一项目状态与当前版本。",
                    "requires_confirmation": False,
                },
            ]
            if version_count <= 0:
                next_actions.append({
                    "tool": "manage_prompt_file",
                    "action": "manage_prompt_file.preview",
                    "params": {"action": "preview"},
                    "reason": "纳管完成（空版本）。先保存开发 prompt 文件，再通过 manage_plan_version insert_from_prompt_file_preview 插入第一个开发版本。",
                    "requires_confirmation": True,
                })
                next_actions.append({
                    "tool": "manage_plan_version",
                    "action": "insert_from_prompt_file_preview",
                    "params": {"action": "insert_from_prompt_file_preview"},
                    "reason": "从 prompt 文件插入第一个开发版本预览。",
                    "requires_confirmation": True,
                })
            else:
                next_actions.append({
                    "tool": "manage_executor_workflow",
                    "action": "run_once_preview",
                    "params": {"action": "run_once_preview", "provider": "codex", "execution_mode": "run"},
                    "reason": "生成当前版本的执行器运行预览。",
                    "requires_confirmation": True,
                })
                next_actions.append({
                    "tool": "manage_executor_workflow",
                    "action": "run_once",
                    "params": {
                        "action": "run_once",
                        "provider": "codex",
                        "execution_mode": "run",
                        "preview_id": "<from_run_once_preview.preview_id>",
                    },
                    "reason": "用 run_once_preview 返回的 preview_id 启动异步执行。",
                    "requires_confirmation": True,
                })
                next_actions.append({
                    "tool": "manage_executor_workflow",
                    "action": "status",
                    "params": {
                        "action": "status",
                        "preview_id": "<from_run_once_preview.preview_id>",
                    },
                    "reason": "run_once 返回 started/running 后，用 status 轮询终态。",
                    "requires_confirmation": False,
                })
                next_actions.append({
                    "tool": "get_executor_run_report",
                    "action": "latest_report",
                    "params": {"latest": True, "include_markdown": False},
                    "reason": "status 到 completed 后读取最新执行报告。",
                    "requires_confirmation": False,
                })
            result["next_actions"] = next_actions
            if version_count <= 0:
                result["next_action_hint"] = "纳管完成（空版本）。先保存 prompt 文件，再通过 manage_plan_version insert_from_prompt_file_preview 插入第一个开发版本。"
            else:
                result["next_action_hint"] = "按 run_once_preview -> run_once -> status -> get_executor_run_report 链路继续。"
        return result

    def _tool_todo_read(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("todo_read", params, require_managed=True)
        manager = MCPTodoListManager(self.project_root)
        include_done = self._bool_param(params.get("include_done"), default=False)
        result = manager.read(include_done=include_done)
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("todo_read", "todo_read", params, result)
        return result

    def _tool_manage_runner_record(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._tool_manage_project_memory_impl("manage_runner_record", params)

    def _tool_manage_project_memory(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._tool_manage_project_memory_impl("manage_project_memory", params)

    def _tool_manage_project_memory_impl(self, workflow_tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        raw_record_type = params.get("record_type")
        raw_action = params.get("action")
        if not isinstance(raw_record_type, str) or not raw_record_type.strip():
            raise MCPToolInputError("INVALID_RECORD_TYPE", "record_type 必须是 memory、todo 或 decision。")
        if not isinstance(raw_action, str) or not raw_action.strip():
            raise MCPToolInputError("INVALID_RECORD_ACTION", "action 必须是 read、add、update 或 delete。")
        if params.get("project_name") is not None:
            return self._route_project_name_tool(workflow_tool_name, params, require_managed=True)
        record_type = raw_record_type.strip().lower()
        action = raw_action.strip().lower()
        tool_name = self._runner_record_tool_name(record_type, action)
        delegate_params = self._runner_record_delegate_params(record_type, action, params)
        delegate_params["__skip_workflow_record"] = True
        if tool_name.startswith("memory_"):
            result = self._tool_manage_runner_record_memory_delegate(action, delegate_params)
        elif tool_name.startswith("todo_"):
            result = self._tool_manage_runner_record_todo_delegate(tool_name, delegate_params)
        else:
            result = self._tool_manage_runner_record_decision_delegate(tool_name, delegate_params)
        self._record_workflow_if_needed(workflow_tool_name, action, params, result)
        return result

    def _runner_record_tool_name(self, record_type: str, action: str) -> str:
        if record_type not in {"memory", "todo", "decision"}:
            raise MCPToolInputError("INVALID_RECORD_TYPE", "record_type 只能是 memory、todo 或 decision。")
        if action not in {"read", "add", "update", "delete"}:
            raise MCPToolInputError("INVALID_RECORD_ACTION", "action 只能是 read、add、update 或 delete。")
        return f"{record_type}_{action}"

    def _runner_record_delegate_params(self, record_type: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        allowed_keys_by_type = {
            "memory": {"project_name", "content", "max_chars"},
            "todo": {"project_name", "include_done", "id", "content", "status"},
            "decision": {"project_name", "id", "status", "title", "decision", "reason", "related_versions"},
        }
        delegate: dict[str, Any] = {}
        for key in allowed_keys_by_type[record_type]:
            if key in params:
                delegate[key] = params.get(key)
        if record_type in {"todo", "decision"} and action in {"update", "delete"}:
            if "id" not in delegate:
                raise MCPToolInputError("INVALID_ID", "update/delete 操作需要 id。")
        return delegate

    def _tool_manage_runner_record_memory_delegate(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        manager = MCPProjectMemoryManager(self.project_root)
        if action == "read":
            return manager.read(max_chars=params.get("max_chars"))
        if action == "add":
            return manager.add(params.get("content"))
        if action == "update":
            return manager.update(params.get("content"))
        return manager.delete()

    def _tool_manage_runner_record_todo_delegate(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "todo_read":
            return self._tool_todo_read(params)
        if tool_name == "todo_add":
            return self._tool_todo_add(params)
        if tool_name == "todo_update":
            return self._tool_todo_update(params)
        return self._tool_todo_delete(params)

    def _tool_manage_runner_record_decision_delegate(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "decision_read":
            return self._tool_decision_read(params)
        if tool_name == "decision_add":
            return self._tool_decision_add(params)
        if tool_name == "decision_update":
            return self._tool_decision_update(params)
        return self._tool_decision_delete(params)

    def _tool_manage_workflow_run(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_action = params.get("action")
        if not isinstance(raw_action, str) or not raw_action.strip():
            raise MCPToolInputError("INVALID_WORKFLOW_ACTION", "action 必须是 list 或 get。")
        action = raw_action.strip().lower()
        if action == "list":
            return self._tool_list_workflow_runs(self._workflow_run_delegate_params(action, params))
        if action == "get":
            return self._tool_get_workflow_run(self._workflow_run_delegate_params(action, params))
        raise MCPToolInputError("INVALID_WORKFLOW_ACTION", "action 只能是 list 或 get。")

    def _workflow_run_delegate_params(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        allowed_keys_by_action = {
            "list": {"project_name", "limit", "workflow_name", "status"},
            "get": {"project_name", "workflow_id"},
        }
        delegate: dict[str, Any] = {}
        for key in allowed_keys_by_action[action]:
            if key in params:
                delegate[key] = params.get(key)
        if action == "get" and "workflow_id" not in delegate:
            raise MCPToolInputError("INVALID_WORKFLOW_ID", "action=get 需要 workflow_id。")
        return delegate

    def _tool_todo_add(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("todo_add", params, require_managed=True)
        manager = MCPTodoListManager(self.project_root)
        result = manager.add(params.get("content"), params.get("status"))
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("todo_add", "todo_add", params, result)
        return result

    def _tool_todo_update(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("todo_update", params, require_managed=True)
        manager = MCPTodoListManager(self.project_root)
        result = manager.update(
            params.get("id"),
            params.get("content") if "content" in params else None,
            params.get("status") if "status" in params else None,
        )
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("todo_update", "update", params, result)
        return result

    def _tool_todo_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("todo_delete", params, require_managed=True)
        manager = MCPTodoListManager(self.project_root)
        result = manager.delete(params.get("id"))
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("todo_delete", "todo_delete", params, result)
        return result

    def _tool_decision_read(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("decision_read", params, require_managed=True)
        manager = MCPDecisionRecordsManager(self.project_root)
        result = manager.read()
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("decision_read", "decision_read", params, result)
        return result

    def _tool_decision_add(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("decision_add", params, require_managed=True)
        manager = MCPDecisionRecordsManager(self.project_root)
        result = manager.add(
            params.get("title"),
            params.get("decision"),
            params.get("reason"),
            params.get("related_versions"),
            params.get("status"),
        )
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("decision_add", "decision_add", params, result)
        return result

    def _tool_decision_update(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("decision_update", params, require_managed=True)
        manager = MCPDecisionRecordsManager(self.project_root)
        changes: dict[str, Any] = {}
        for key in ("title", "decision", "reason", "related_versions", "status"):
            if key in params:
                changes[key] = params.get(key)
        result = manager.update(params.get("id"), **changes)
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("decision_update", "decision_update", params, result)
        return result

    def _tool_decision_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("decision_delete", params, require_managed=True)
        manager = MCPDecisionRecordsManager(self.project_root)
        result = manager.delete(params.get("id"))
        if not self._bool_param(params.get("__skip_workflow_record"), default=False):
            self._record_workflow_if_needed("decision_delete", "decision_delete", params, result)
        return result

    def _tool_list_executor_run_reports(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("list_executor_run_reports", params, require_managed=True)
        version_raw = params.get("version")
        version: str | None = None
        if version_raw is not None:
            if not isinstance(version_raw, str) or not version_raw.strip():
                raise MCPToolInputError("INVALID_VERSION", "version 必须是字符串。")
            version = version_raw.strip()
            from runner.executor_run_reports import _validate_version
            try:
                _validate_version(version)
            except ValueError as exc:
                raise MCPToolInputError("INVALID_VERSION", str(exc))

        limit = self._bounded_int_param(params.get("limit"), default=10, minimum=1, maximum=50)
        store = ExecutorRunReportStore(self.project_root)
        reports = store.list_reports(version=version, limit=limit)
        result = {"reports": reports}
        if not reports:
            result["message"] = "No executor run reports found."
        return result

    def _tool_get_executor_run_report(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("get_executor_run_report", params, require_managed=True)
        version_raw = params.get("version")
        report_id_raw = params.get("report_id")
        latest = self._bool_param(params.get("latest"), default=True)
        include_markdown = self._bool_param(params.get("include_markdown"), default=True)
        max_md = self._bounded_int_param(params.get("max_markdown_chars"), default=30000, minimum=1, maximum=60000)

        version: str | None = None
        if version_raw is not None:
            if not isinstance(version_raw, str) or not version_raw.strip():
                raise MCPToolInputError("INVALID_VERSION", "version 必须是字符串。")
            version = version_raw.strip()
            from runner.executor_run_reports import _validate_version
            try:
                _validate_version(version)
            except ValueError as exc:
                raise MCPToolInputError("INVALID_VERSION", str(exc))

        report_id: str | None = None
        if report_id_raw is not None:
            if not isinstance(report_id_raw, str) or not report_id_raw.strip():
                raise MCPToolInputError("INVALID_REPORT_ID", "report_id 必须是字符串。")
            report_id = report_id_raw.strip()
            from runner.executor_run_reports import _validate_report_id
            try:
                _validate_report_id(report_id)
            except ValueError as exc:
                raise MCPToolInputError("INVALID_REPORT_ID", str(exc))

        store = ExecutorRunReportStore(self.project_root)
        result = store.get_report(
            version=version,
            report_id=report_id,
            latest=latest,
            include_markdown=include_markdown,
            max_markdown_chars=max_md,
        )
        if not result.get("ok"):
            return result
        return {"report": result.get("report", {}), "report_markdown": result.get("report_markdown"), "truncated": result.get("truncated", False)}

    def _collect_context_item(
        self,
        name: str,
        fn: Any,
        params: dict[str, Any],
        partial_errors: list[dict[str, str]],
    ) -> dict[str, Any]:
        try:
            result = fn(params)
            return {"ok": True, "result": result}
        except MCPToolInputError as exc:
            error = self._context_error(name, exc.error_code, exc.message)
        except PlanningBridgeError as exc:
            error = self._context_error(name, "BRIDGE_ERROR", str(exc))
        except SourceReviewError as exc:
            error = self._context_error(name, "SOURCE_REVIEW_ERROR", str(exc))
        except Exception as exc:
            error = self._context_error(name, "ITEM_EXEC_ERROR", str(exc))
        partial_errors.append({
            "name": name,
            "error_code": str(error.get("error_code") or "ITEM_EXEC_ERROR"),
            "message": str(error.get("message") or "context item failed"),
        })
        return {"ok": False, "result": error}

    def _context_error(self, name: str, error_code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "name": name,
            "error_code": error_code,
            "message": message,
        }

    def _bool_param(self, value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        return default

    def _bounded_int_param(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except Exception:
            return default
        return max(minimum, min(parsed, maximum))

    def _tool_get_version_result(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("version")
        if version is not None and not isinstance(version, str):
            raise MCPToolInputError("INVALID_VERSION", "version 必须是字符串。")
        if isinstance(version, str) and not version.strip():
            version = None
        return self.bridge.get_version_result(self.project_root, version=version)

    def _tool_get_next_version_plan(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.bridge.get_next_version_plan(self.project_root)

    def _tool_get_plan_overview(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._with_project_identity(self.bridge.get_plan_overview(self.project_root))

    def _tool_get_project_doc_section(self, params: dict[str, Any]) -> dict[str, Any]:
        result = self.bridge.get_project_doc_section(self.project_root, params)
        if result.get("ok"):
            return result
        raise MCPToolInputError(
            str(result.get("error_code") or "DOC_SECTION_ERROR"),
            str(result.get("message") or "读取文档段落失败。"),
            {"available_headings": result.get("available_headings", [])},
        )

    def _tool_manage_plan_version(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"inspect", "insert_preview", "update_preview", "repair_preview", "apply_preview_status", "insert_from_prompt_file_preview", "apply_preview", "reload_plan", "continue_next_version"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 inspect、insert_preview、update_preview、repair_preview、apply_preview_status、insert_from_prompt_file_preview、apply_preview、reload_plan 或 continue_next_version。")

        if params.get("project_name") is not None:
            if action not in {"insert_preview", "update_preview", "repair_preview", "apply_preview_status", "insert_from_prompt_file_preview", "apply_preview", "reload_plan", "continue_next_version"}:
                raise MCPToolInputError(
                    "PROJECT_NAME_ROUTING_NOT_SUPPORTED",
                    "project_name 路由仅支持 manage_plan_version 的已登记 managed 项目动作：insert_preview、update_preview、repair_preview、apply_preview_status、insert_from_prompt_file_preview、apply_preview、reload_plan、continue_next_version。",
                )
            return self._route_project_name_tool("manage_plan_version", params, require_managed=True)

        plan_path = resolve_project_runner_plan_path(self.project_root)
        has_plan = os.path.isfile(plan_path)

        if action == "inspect":
            if not has_plan:
                return {
                    "ok": True, "action": "inspect",
                    "has_plan": False, "mode": "source_only",
                    "can_insert_preview": False, "can_update_preview": False,
                    "recommended_tool": "manage_runner_plan",
                    "recommended_action": "inspect",
                    "message": "当前项目是 source-only，尚未纳入 Runner 管理。请使用 manage_runner_plan 完成纳管。",
                }
            return self._plan_version_inspect_managed()

        if action == "apply_preview_status":
            patch_id = params.get("patch_id")
            if not isinstance(patch_id, str) or not patch_id.strip():
                raise MCPToolInputError("INVALID_PATCH_ID", "apply_preview_status 需要非空 patch_id。")
            try:
                return self.bridge.get_plan_patch_status(self.project_root, patch_id.strip())
            except PlanningBridgeError as exc:
                return {"ok": False, "action": "apply_preview_status", "error_code": "PATCH_NOT_FOUND", "message": str(exc)}

        if action == "reload_plan":
            result = self._handle_reload_plan()
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        if action == "continue_next_version":
            result = self._handle_continue_next_version()
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        if not has_plan:
            return {
                "ok": False, "error_code": "PLAN_MISSING", "action": action,
                "message": "当前项目缺少 .colameta/plan.json，无法执行 insert/update/repair preview。请先使用 manage_runner_plan 完成纳管。",
            }

        if action == "insert_preview":
            spec = self._build_insert_version_spec(params)
            result = self.bridge.preview_insert_version(self.project_root, spec)
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        if action == "update_preview":
            spec = self._build_update_version_spec(params)
            result = self.bridge.preview_update_version(self.project_root, spec)
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        if action == "repair_preview":
            result = self._plan_version_repair_preview(params)
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        if action == "insert_from_prompt_file_preview":
            result = self._handle_insert_from_prompt_file_preview(params)
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        if action == "apply_preview":
            result = self._handle_apply_preview(params)
            self._record_workflow_if_needed("manage_plan_version", action, params, result)
            return result

        return {"ok": False, "error_code": "UNEXPECTED", "action": action, "message": "未知操作。"}

    def _handle_reload_plan(self) -> dict[str, Any]:
        from runner.plan_reload_workflow import PlanReloadService

        result = PlanReloadService(self.project_root).reload_plan()
        if not isinstance(result, dict):
            return {
                "ok": False,
                "action": "reload_plan",
                "error_code": "RELOAD_PLAN_INVALID_RESULT",
                "message": "reload_plan 返回结构无效。",
            }
        result["action"] = "reload_plan"
        if result.get("ok") and result.get("current_version"):
            result["next_actions"] = [
                {
                    "tool": "manage_executor_workflow",
                    "action": "preflight",
                    "params": {"action": "preflight", "provider": "codex"},
                    "reason": "state 已同步到当前版本，下一步检查执行器 preflight。",
                    "requires_confirmation": False,
                }
            ]
        return result

    def _handle_continue_next_version(self) -> dict[str, Any]:
        from runner.continue_version_workflow import ContinueNextVersionService

        result = ContinueNextVersionService(self.project_root).continue_next_version()
        if not isinstance(result, dict):
            return {
                "ok": False,
                "action": "continue_next_version",
                "error_code": "CONTINUE_NEXT_VERSION_INVALID_RESULT",
                "message": "continue_next_version 返回结构无效。",
            }
        result["action"] = "continue_next_version"
        if result.get("ok") and result.get("runner_status") != "COMPLETED":
            result["next_actions"] = [
                {
                    "tool": "manage_executor_workflow",
                    "action": "preflight",
                    "params": {"action": "preflight", "provider": "codex"},
                    "reason": "已进入下一版本，下一步检查执行器 preflight。",
                    "requires_confirmation": False,
                }
            ]
        return result

    def _plan_version_inspect_managed(self) -> dict[str, Any]:
        plan_path = resolve_project_runner_path(self.project_root, "plan.json")
        state_path = resolve_project_runner_path(self.project_root, "state.json")
        result: dict[str, Any] = {
            "ok": True, "action": "inspect",
            "has_plan": True, "mode": "runner_managed",
            "has_state": os.path.isfile(state_path),
            "can_insert_preview": True, "can_update_preview": True,
        }
        try:
            from runner.mcp_runner_plan import MCPRunnerPlanManager
            inspect_result = MCPRunnerPlanManager(self.project_root).inspect()
            if isinstance(inspect_result, dict):
                result["plan_summary"] = inspect_result.get("plan_summary")
                result["lint_summary"] = (
                    inspect_result.get("plan_summary", {}).get("lint_status")
                    if isinstance(inspect_result.get("plan_summary"), dict) else None
                )
                result["blockers"] = list(inspect_result.get("blockers", []))
                result["warnings"] = list(inspect_result.get("warnings", []))
        except Exception:
            pass
        return result

    def _build_insert_version_spec(self, params: dict[str, Any]) -> dict[str, Any]:
        insert_after = params.get("insert_after")
        if not isinstance(insert_after, str) or not insert_after.strip():
            if self._plan_versions_empty():
                insert_after = "__first__"
            else:
                raise MCPToolInputError("INVALID_INSERT_AFTER", "insert_preview 需要非空 insert_after。")

        version = params.get("version")
        if not isinstance(version, str) or not version.strip():
            raise MCPToolInputError("INVALID_VERSION", "insert_preview 需要非空 version。")

        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MCPToolInputError("INVALID_NAME", "insert_preview 需要非空 name。")

        description = params.get("description")
        if not isinstance(description, str) or not description.strip():
            raise MCPToolInputError("INVALID_DESCRIPTION", "insert_preview 需要非空 description。")

        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise MCPToolInputError("INVALID_PROMPT", "insert_preview 需要非空 prompt。")

        allowed_files = self._normalize_string_list(params.get("allowed_files"), "allowed_files")
        if not allowed_files:
            raise MCPToolInputError("INVALID_ALLOWED_FILES", "insert_preview 需要非空 allowed_files 列表。")

        acceptance_commands_val = params.get("acceptance_commands")
        if not isinstance(acceptance_commands_val, list) or not acceptance_commands_val:
            raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", "insert_preview 需要非空 acceptance_commands 列表。")
        acceptance_commands = self._normalize_acceptance_commands_param(acceptance_commands_val)

        spec: dict[str, Any] = {
            "insert_after": insert_after.strip(),
            "version": version.strip(),
            "name": name.strip(),
            "description": description.strip(),
            "prompt": prompt,
            "allowed_files": allowed_files,
            "acceptance_commands": acceptance_commands,
        }

        manual_acceptance = self._normalize_optional_string_list(params.get("manual_acceptance"), "manual_acceptance")
        if manual_acceptance is not None:
            spec["manual_acceptance"] = manual_acceptance

        out_of_scope = self._normalize_optional_string_list(params.get("out_of_scope"), "out_of_scope")
        if out_of_scope is not None:
            spec["out_of_scope"] = out_of_scope

        context_files = self._normalize_optional_string_list(params.get("context_files"), "context_files")
        if context_files is not None:
            spec["context_files"] = context_files

        forbidden_files = self._normalize_optional_string_list(params.get("forbidden_files"), "forbidden_files")
        if forbidden_files is not None:
            spec["forbidden_files"] = forbidden_files

        prompt_file = params.get("prompt_file")
        if isinstance(prompt_file, str) and prompt_file.strip():
            spec["prompt_file"] = prompt_file.strip()

        execution = params.get("execution")
        if execution is not None:
            spec["execution"] = self._extract_execution_profile(execution)

        if "allow_no_changes" in params and params.get("allow_no_changes") is not None:
            allow_no_changes = params.get("allow_no_changes")
            if not isinstance(allow_no_changes, bool):
                raise MCPToolInputError("INVALID_ALLOW_NO_CHANGES", "allow_no_changes 必须是布尔值。")
            spec["allow_no_changes"] = allow_no_changes

        return spec

    def _plan_versions_empty(self) -> bool:
        plan_path = resolve_project_runner_plan_path(self.project_root)
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception:
            return False
        versions = plan.get("versions", []) if isinstance(plan, dict) else []
        return isinstance(versions, list) and len(versions) == 0

    def _build_update_version_spec(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("version")
        if not isinstance(version, str) or not version.strip():
            raise MCPToolInputError("INVALID_VERSION", "update_preview 需要非空 version。")

        spec: dict[str, Any] = {"version": version.strip()}
        update_fields = ["name", "description", "prompt"]
        has_update = False
        for field in update_fields:
            val = params.get(field)
            if val is not None:
                if not isinstance(val, str) or not val.strip():
                    raise MCPToolInputError(f"INVALID_{field.upper()}", f"{field} 必须是非空字符串。")
                spec[field] = val.strip()
                has_update = True

        allowed_raw = params.get("allowed_files")
        if allowed_raw is not None:
            allowed = self._normalize_string_list(allowed_raw, "allowed_files")
            if not allowed:
                raise MCPToolInputError("INVALID_ALLOWED_FILES", "allowed_files 不能为空。")
            spec["allowed_files"] = allowed
            has_update = True

        acceptance_raw = params.get("acceptance_commands")
        if acceptance_raw is not None:
            if not isinstance(acceptance_raw, list) or not acceptance_raw:
                raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", "acceptance_commands 不能为空。")
            spec["acceptance_commands"] = self._normalize_acceptance_commands_param(acceptance_raw)
            has_update = True

        for field in ("manual_acceptance", "out_of_scope", "context_files", "forbidden_files"):
            val = params.get(field)
            if val is not None:
                items = self._normalize_string_list(val, field)
                if items is not None:
                    spec[field] = items
                    has_update = True

        execution = params.get("execution")
        if execution is not None:
            spec["execution"] = self._extract_execution_profile(execution)
            has_update = True

        if "allow_no_changes" in params and params.get("allow_no_changes") is not None:
            allow_no_changes = params.get("allow_no_changes")
            if not isinstance(allow_no_changes, bool):
                raise MCPToolInputError("INVALID_ALLOW_NO_CHANGES", "allow_no_changes 必须是布尔值。")
            spec["allow_no_changes"] = allow_no_changes
            has_update = True

        if not has_update:
            raise MCPToolInputError("NO_UPDATE_FIELDS", "update_preview 至少需要一个可更新字段。")

        return spec

    def _normalize_acceptance_commands_param(self, commands: list[Any]) -> list[Any]:
        if not isinstance(commands, list) or not commands:
            raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", "acceptance_commands 必须是非空列表。")
        result: list[Any] = []
        for idx, item in enumerate(commands):
            if isinstance(item, str):
                if not item.strip():
                    raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", f"acceptance_commands[{idx}] 字符串命令不能为空。")
                result.append(item.strip())
            elif isinstance(item, dict):
                cmd_val = item.get("command")
                if not isinstance(cmd_val, str) or not cmd_val.strip():
                    raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", f"acceptance_commands[{idx}] 缺少非空 command。")
                command = cmd_val.strip()
                if "\n" in command or "\r" in command:
                    raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", f"acceptance_commands[{idx}] 不允许多行命令。")
                entry: dict[str, Any] = {"command": command}
                ts_raw = item.get("timeout_seconds")
                if ts_raw is not None:
                    if isinstance(ts_raw, bool) or not isinstance(ts_raw, int) or ts_raw <= 0:
                        raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", f"acceptance_commands[{idx}] timeout_seconds 必须是正整数。")
                    entry["timeout_seconds"] = ts_raw
                cf_raw = item.get("continue_on_failure")
                if cf_raw is not None:
                    if not isinstance(cf_raw, bool):
                        raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", f"acceptance_commands[{idx}] continue_on_failure 必须是布尔值。")
                    entry["continue_on_failure"] = cf_raw
                result.append(entry)
            else:
                raise MCPToolInputError("INVALID_ACCEPTANCE_COMMANDS", f"acceptance_commands[{idx}] 必须是字符串或对象。")
        return result

    def _extract_execution_profile(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise MCPToolInputError("INVALID_EXECUTION", "execution 必须是 JSON 对象。")
        allowed = {"provider", "model", "model_name", "pi_model", "codex_model", "opencode_model", "lane", "capability_level", "notes"}
        unknown = set(value.keys()) - allowed
        if unknown:
            raise MCPToolInputError("INVALID_EXECUTION", f"execution 包含不支持字段：{'、'.join(sorted(unknown))}")
        normalized: dict[str, Any] = {}
        for key in allowed:
            if key not in value:
                continue
            raw = value[key]
            if key == "provider":
                if not isinstance(raw, str) or not raw.strip():
                    raise MCPToolInputError("INVALID_EXECUTION", "execution.provider 必须是非空字符串。")
                provider_val = raw.strip().lower()
                if provider_val not in {"pi", "codex", "opencode"}:
                    raise MCPToolInputError("INVALID_EXECUTION", "execution.provider 必须是 pi、codex 或 opencode。")
                normalized[key] = provider_val
            else:
                if not isinstance(raw, str) or not raw.strip():
                    raise MCPToolInputError("INVALID_EXECUTION", f"execution.{key} 必须是非空字符串。")
                normalized[key] = raw.strip()
        return normalized

    def _normalize_optional_string_list(self, value: Any, field_name: str) -> list[str] | None:
        if value is None:
            return None
        return self._normalize_string_list(value, field_name)

    def _normalize_string_list(self, value: Any, field_name: str) -> list[str]:
        if not isinstance(value, list):
            raise MCPToolInputError(f"INVALID_{field_name.upper()}", f"{field_name} 必须是字符串列表。")
        result: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise MCPToolInputError(f"INVALID_{field_name.upper()}", f"{field_name}[{idx}] 必须是非空字符串。")
            result.append(item.strip())
        return result

    def _plan_version_repair_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        from runner.plan_standards_linter import PlanStandardsLinter
        lint_result = PlanStandardsLinter().lint_project(self.project_root)
        if not isinstance(lint_result, dict) or not lint_result.get("ok"):
            return {
                "ok": True, "action": "repair_preview",
                "can_preview": False,
                "message": "无法读取 plan lint 状态。请先检查 plan.json。",
                "suggested_next_action": "fix_plan_manually",
            }

        target_version = params.get("version")
        if isinstance(target_version, str):
            target_version = target_version.strip()
        else:
            target_version = None

        repair_kinds_raw = params.get("repair_kinds")
        allowed_kinds = {"acceptance_command_shape", "invalid_provider", "missing_optional_safety_fields", "prompt_file_safety"}
        repair_kinds: set[str] | None = None
        if isinstance(repair_kinds_raw, list) and repair_kinds_raw:
            kinds = set()
            for item in repair_kinds_raw:
                if isinstance(item, str) and item.strip() in allowed_kinds:
                    kinds.add(item.strip())
            if kinds:
                repair_kinds = kinds

        issues = lint_result.get("issues", [])
        repair_candidates: list[dict[str, Any]] = []
        blockers: list[str] = []
        warnings: list[str] = []

        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if target_version:
                ver = issue.get("version")
                if ver is not None and str(ver) != target_version:
                    continue

            error_code = issue.get("error_code", "")
            field = issue.get("field", "")
            blocking = bool(issue.get("blocking", False))
            suggestion = issue.get("suggestion", "")

            if repair_kinds and error_code not in self._repair_issue_codes(repair_kinds):
                continue

            repair: dict[str, Any] = {"issue": error_code, "field": field, "blocking": blocking, "message": issue.get("message", "")}

            if error_code == "LEGACY_STRING_ACCEPTANCE_COMMAND" and (not repair_kinds or "acceptance_command_shape" in (repair_kinds or set())):
                repair["repair_action"] = "normalize_to_object"
                repair["repair_suggestion"] = "将字符串命令转为 {\"command\": \"...\", \"timeout_seconds\": 600, \"continue_on_failure\": false}"
                repair_candidates.append(repair)

            elif error_code == "MISSING_TIMEOUT_SECONDS" and (not repair_kinds or "acceptance_command_shape" in (repair_kinds or set())):
                repair["repair_action"] = "add_default_timeout"
                repair["repair_suggestion"] = "添加 timeout_seconds: 600"
                repair_candidates.append(repair)

            elif error_code == "MISSING_CONTINUE_ON_FAILURE" and (not repair_kinds or "acceptance_command_shape" in (repair_kinds or set())):
                repair["repair_action"] = "add_default_continue_on_failure"
                repair["repair_suggestion"] = "添加 continue_on_failure: false"
                repair_candidates.append(repair)

            elif error_code == "INVALID_EXECUTION_PROVIDER" and (not repair_kinds or "invalid_provider" in (repair_kinds or set())):
                repair["repair_action"] = "blocker_user_must_choose"
                repair["repair_suggestion"] = "需要用户从 pi、codex、opencode 中选择合法 provider。"
                repair_candidates.append(repair)

            elif error_code == "INVALID_MODEL_EXECUTION_PROVIDER" and (not repair_kinds or "invalid_provider" in (repair_kinds or set())):
                repair["repair_action"] = "blocker_user_must_choose"
                repair["repair_suggestion"] = "需要用户从 pi、codex、opencode 中选择合法 provider。"
                repair_candidates.append(repair)

            elif error_code in ("MISSING_OUT_OF_SCOPE", "MISSING_VERSION_DESCRIPTION") and (not repair_kinds or "missing_optional_safety_fields" in (repair_kinds or set())):
                repair["repair_action"] = "optional_recommendation"
                repair_candidates.append(repair)

            elif error_code == "PROMPT_FILE_PATH_UNSAFE" and (not repair_kinds or "prompt_file_safety" in (repair_kinds or set())):
                repair["repair_action"] = "blocker_manual_fix_required"
                repair_candidates.append(repair)
                if blocking:
                    blockers.append(f"prompt_file 路径不安全：{issue.get('message', '')}")

            if blocking and repair.get("repair_action") not in ("blocker_user_must_choose", "blocker_manual_fix_required"):
                blockers.append(f"{error_code}: {issue.get('message', '')}")

        can_preview = True
        has_blocker_repairs = any(
            r.get("repair_action") in ("blocker_user_must_choose", "blocker_manual_fix_required")
            for r in repair_candidates
        )
        has_actionable = any(
            r.get("repair_action") in ("normalize_to_object", "add_default_timeout", "add_default_continue_on_failure", "optional_recommendation")
            for r in repair_candidates
        )

        if not repair_candidates:
            can_preview = False
            return {
                "ok": True, "action": "repair_preview",
                "can_preview": False,
                "repair_candidates": [],
                "blockers": blockers,
                "warnings": warnings,
                "message": "未检测到可自动修复的问题。",
                "suggested_next_action": "no_repair_needed",
            }

        suggested_next_action = "review_repair_candidates"
        if has_blocker_repairs and not has_actionable:
            can_preview = False
            suggested_next_action = "manual_fix_required"

        return {
            "ok": True, "action": "repair_preview",
            "can_preview": can_preview,
            "repair_candidates": repair_candidates,
            "blockers": blockers,
            "warnings": warnings,
            "message": "" if can_preview else "存在需要人工修复的阻断问题。",
            "suggested_next_action": suggested_next_action,
        }

    def _repair_issue_codes(self, kinds: set[str]) -> set[str]:
        mapping: dict[str, set[str]] = {
            "acceptance_command_shape": {"LEGACY_STRING_ACCEPTANCE_COMMAND", "MISSING_TIMEOUT_SECONDS", "MISSING_CONTINUE_ON_FAILURE"},
            "invalid_provider": {"INVALID_EXECUTION_PROVIDER", "INVALID_MODEL_EXECUTION_PROVIDER"},
            "missing_optional_safety_fields": {"MISSING_OUT_OF_SCOPE", "MISSING_VERSION_DESCRIPTION"},
            "prompt_file_safety": {"PROMPT_FILE_PATH_UNSAFE"},
        }
        result: set[str] = set()
        for kind in kinds:
            codes = mapping.get(kind)
            if codes:
                result.update(codes)
        return result

    _VERSION_FILENAME_RE = re.compile(r"^[vV]\d[\d.]*\.md$")

    def _validate_prompt_file_safe(self, prompt_file: str) -> None:
        if not isinstance(prompt_file, str) or not prompt_file.strip():
            raise MCPToolInputError("PROMPT_FILE_REQUIRED", "prompt_file 不能为空。")
        if os.path.isabs(prompt_file):
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 不能是绝对路径。")
        if ".." in prompt_file.split("/"):
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 不能包含 ..。")
        if "\\" in prompt_file:
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 不能包含反斜杠。")
        if "/" in prompt_file:
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 不能包含多级路径，仅允许文件名。")
        if not prompt_file.endswith(".md"):
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 必须以 .md 结尾。")
        if not self._VERSION_FILENAME_RE.match(prompt_file):
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 必须是版本文件名，例如 v1.84.54.md。")

    def _version_from_prompt_filename(self, prompt_file: str) -> str:
        v = prompt_file[:-3]
        if not v:
            raise MCPToolInputError("INVALID_PROMPT_FILE", "prompt_file 版本号不能为空。")
        return v

    def _parse_version_tuple(self, version: str) -> tuple[int, ...] | None:
        parts = version.lstrip("vV").replace("-", ".").split(".")
        nums: list[int] = []
        for p in parts:
            try:
                nums.append(int(p))
            except ValueError:
                return None
        return tuple(nums)

    def _auto_derive_insert_after(self, version: str) -> str:
        plan_path = resolve_project_runner_plan_path(self.project_root)
        if not os.path.isfile(plan_path):
            return ""
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception:
            return ""
        versions = plan.get("versions", [])
        if not versions:
            return "__first__"
        new_parsed = self._parse_version_tuple(version)
        if not new_parsed:
            return ""
        candidates: list[tuple[tuple[int, ...], str]] = []
        for v in versions:
            v_ver = v.get("version", "")
            v_parsed = self._parse_version_tuple(v_ver)
            if v_parsed and v_parsed < new_parsed:
                candidates.append((v_parsed, v_ver))
        if not candidates:
            return ""
        candidates.sort(key=lambda x: x[0])
        return candidates[-1][1]

    def _version_exists_in_plan(self, version: str) -> bool:
        plan_path = resolve_project_runner_plan_path(self.project_root)
        if not os.path.isfile(plan_path):
            return False
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except Exception:
            return False
        for v in plan.get("versions", []):
            if v.get("version") == version:
                return True
        return False

    def _handle_insert_from_prompt_file_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt_file = params.get("prompt_file")
        if not isinstance(prompt_file, str) or not prompt_file.strip():
            return {"ok": False, "error_code": "PROMPT_FILE_REQUIRED", "action": "insert_from_prompt_file_preview",
                    "message": "insert_from_prompt_file_preview 需要非空 prompt_file。"}
        prompt_file = prompt_file.strip()
        try:
            self._validate_prompt_file_safe(prompt_file)
        except MCPToolInputError as e:
            return {"ok": False, "error_code": e.error_code, "action": "insert_from_prompt_file_preview", "message": e.message}

        version = self._version_from_prompt_filename(prompt_file)
        version_param = params.get("version")
        if version_param is not None:
            if not isinstance(version_param, str) or not version_param.strip():
                return {"ok": False, "error_code": "INVALID_VERSION", "action": "insert_from_prompt_file_preview",
                        "message": "version 必须是非空字符串。"}
            if version_param.strip() != version:
                return {"ok": False, "error_code": "INVALID_VERSION", "action": "insert_from_prompt_file_preview",
                        "message": f"version 必须与 prompt_file 匹配：{version}"}

        prompts_dir = resolve_project_runner_path(self.project_root, "prompts")
        file_path = os.path.join(prompts_dir, prompt_file)
        real_prompts = os.path.realpath(prompts_dir)
        real_file = os.path.realpath(file_path)
        if not real_file.startswith(real_prompts + os.sep):
            return {"ok": False, "error_code": "PROMPT_FILE_UNSAFE", "action": "insert_from_prompt_file_preview",
                    "message": "prompt 文件路径不安全。"}
        if not os.path.isfile(real_file):
            return {"ok": False, "error_code": "PROMPT_FILE_NOT_FOUND", "action": "insert_from_prompt_file_preview",
                    "message": f"prompt 文件不存在：{resolve_project_runner_rel_dir(self.project_root)}/prompts/{prompt_file}"}

        try:
            with open(real_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return {"ok": False, "error_code": "PROMPT_FILE_READ_ERROR", "action": "insert_from_prompt_file_preview",
                    "message": f"读取 prompt 文件失败：{resolve_project_runner_rel_dir(self.project_root)}/prompts/{prompt_file}"}

        if not content.strip():
            return {"ok": False, "error_code": "CONTENT_EMPTY", "action": "insert_from_prompt_file_preview",
                    "message": "prompt 文件内容为空。"}

        front_matter, body = _parse_prompt_front_matter(content)
        if body is None:
            return {"ok": False, "error_code": "FRONT_MATTER_INVALID", "action": "insert_from_prompt_file_preview",
                    "message": "prompt 文件 front matter 缺少结束分隔符 ---。"}

        if not body.strip():
            return {"ok": False, "error_code": "CONTENT_EMPTY", "action": "insert_from_prompt_file_preview",
                    "message": "prompt 正文为空。"}

        if self._version_exists_in_plan(version):
            return {"ok": False, "error_code": "VERSION_EXISTS", "action": "insert_from_prompt_file_preview",
                    "message": f"版本 {version} 已存在于 plan 中。"}

        merged_params: dict[str, Any] = {
            "version": version,
            "prompt": body,
            "prompt_file": prompt_file,
        }

        insert_after = params.get("insert_after")
        if insert_after is None:
            insert_after = self._auto_derive_insert_after(version)
            if not insert_after:
                return {"ok": False, "error_code": "INSERT_AFTER_NOT_FOUND", "action": "insert_from_prompt_file_preview",
                        "message": f"无法推导 insert_after：未找到小于 {version} 的版本。"}
        merged_params["insert_after"] = insert_after

        name_value = params.get("name")
        if not isinstance(name_value, str) or not name_value.strip():
            return {"ok": False, "error_code": "NAME_MISSING", "action": "insert_from_prompt_file_preview",
                    "message": "insert_from_prompt_file_preview 需要 GPTs 显式提供非空 name；不要从 prompt 文件或默认 Version vX 推导。"}
        merged_params["name"] = name_value.strip()

        description_value = params.get("description")
        if not isinstance(description_value, str) or not description_value.strip():
            return {"ok": False, "error_code": "DESCRIPTION_MISSING", "action": "insert_from_prompt_file_preview",
                    "message": "insert_from_prompt_file_preview 需要 GPTs 显式提供非空 description；不要从 prompt 文件或默认描述推导。"}
        merged_params["description"] = description_value.strip()

        allowed_files = params.get("allowed_files", front_matter.get("allowed_files"))
        if allowed_files is None:
            return {"ok": False, "error_code": "ALLOWED_FILES_MISSING", "action": "insert_from_prompt_file_preview",
                    "message": "insert_from_prompt_file_preview 需要 allowed_files 参数，或 prompt 文件 front matter 提供 allowed_files。"}
        merged_params["allowed_files"] = allowed_files

        acceptance_commands = params.get("acceptance_commands", front_matter.get("acceptance_commands"))
        if acceptance_commands is None:
            return {"ok": False, "error_code": "ACCEPTANCE_COMMANDS_MISSING", "action": "insert_from_prompt_file_preview",
                    "message": "insert_from_prompt_file_preview 需要 acceptance_commands 参数，或 prompt 文件 front matter 提供 acceptance_commands。"}
        merged_params["acceptance_commands"] = acceptance_commands

        for field in ("manual_acceptance", "out_of_scope", "context_files", "forbidden_files", "allow_no_changes"):
            if field in params:
                merged_params[field] = params.get(field)
            elif field in front_matter:
                merged_params[field] = front_matter.get(field)

        if "execution" in params:
            merged_params["execution"] = params.get("execution")
        elif "execution" in front_matter:
            execution = front_matter.get("execution")
            if execution is not None:
                if isinstance(execution, dict):
                    provider = execution.get("provider")
                    if provider is not None:
                        if not isinstance(provider, str) or not provider.strip():
                            return {"ok": False, "error_code": "INVALID_PROVIDER", "action": "insert_from_prompt_file_preview",
                                    "message": "执行器 provider 必须是非空字符串。"}
                        provider_str = provider.strip()
                        if provider_str not in ("pi", "codex", "opencode"):
                            return {"ok": False, "error_code": "INVALID_PROVIDER", "action": "insert_from_prompt_file_preview",
                                    "message": f"执行器 provider 必须是 pi、codex 或 opencode，收到：{provider_str}"}
                merged_params["execution"] = execution

        try:
            spec = self._build_insert_version_spec(merged_params)
        except MCPToolInputError as e:
            return {"ok": False, "error_code": e.error_code, "action": "insert_from_prompt_file_preview", "message": e.message}

        try:
            result = self.bridge.preview_insert_version(self.project_root, spec)
        except PlanningBridgeError as e:
            return {"ok": False, "error_code": "BRIDGE_ERROR", "action": "insert_from_prompt_file_preview",
                    "message": str(e)}

        if isinstance(result, dict) and result.get("ok"):
            result["source"] = "insert_from_prompt_file_preview"
            result["prompt_file"] = prompt_file
            result["version_from_filename"] = version
            if "recommended_next_action" not in result:
                result["recommended_next_action"] = {
                    "tool": "manage_plan_version",
                    "action": "apply_preview",
                    "params": {"action": "apply_preview", "patch_id": result.get("patch_id", "")},
                    "reason": "应用 plan patch，将新版本写入 plan.json 和 prompt 文件。",
                    "requires_confirmation": True,
                }
        return result

    def _handle_apply_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        patch_id = params.get("patch_id")
        if not isinstance(patch_id, str) or not patch_id.strip():
            return {"ok": False, "action": "apply_preview", "error_code": "PATCH_ID_REQUIRED",
                    "message": "apply_preview 需要非空 patch_id。", "patch_id": ""}
        patch_id = patch_id.strip()

        try:
            result = self.bridge.apply_plan_patch(self.project_root, patch_id)
        except PlanningBridgeError as e:
            return {"ok": False, "action": "apply_preview", "error_code": "PATCH_NOT_FOUND",
                    "message": str(e), "patch_id": patch_id}

        if isinstance(result, dict) and result.get("ok"):
            result["action"] = "apply_preview"
            inserted = result.get("inserted_version")
            updated = result.get("updated_version")
            operation = result.get("operation", "")
            executor_provider = None
            if inserted or updated:
                plan_path = resolve_project_runner_plan_path(self.project_root)
                if os.path.isfile(plan_path):
                    try:
                        with open(plan_path, "r", encoding="utf-8") as f:
                            plan = json.load(f)
                        target_version = inserted or updated
                        for v in plan.get("versions", []):
                            if v.get("version") == target_version:
                                exec_cfg = v.get("execution", {})
                                if isinstance(exec_cfg, dict) and exec_cfg.get("provider"):
                                    executor_provider = exec_cfg["provider"]
                                break
                    except Exception:
                        pass
            if not executor_provider:
                executor_provider = "codex"
            result["next_actions"] = [
                {
                    "tool": "manage_executor_workflow",
                    "action": "preflight",
                    "params": {"action": "preflight", "provider": executor_provider},
                    "reason": f"检查 {executor_provider} 执行器可用性。",
                    "requires_confirmation": False,
                },
                {
                    "tool": "manage_plan_version",
                    "action": "inspect",
                    "params": {"action": "inspect"},
                    "reason": "查看应用 patch 后的 plan 状态。",
                    "requires_confirmation": False,
                },
            ]
        else:
            result["action"] = "apply_preview"
            if "patch_id" not in result:
                result["patch_id"] = patch_id
        return result

    def _tool_manage_project_patch(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"preview", "apply", "status", "preview_delete"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 preview、apply、status 或 preview_delete。")
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_project_patch", params, require_managed=True)
        manager = MCPProjectPatchManager(self.project_root, self.source_review)
        if action == "preview":
            result = manager.preview(params)
            self._record_workflow_if_needed("manage_project_patch", action, params, result)
            return result
        if action == "preview_delete":
            result = manager.preview_delete(params)
            self._record_workflow_if_needed("manage_project_patch", "preview", params, result)
            return result
        if action == "apply":
            result = manager.apply(params)
            self._record_workflow_if_needed("manage_project_patch", action, params, result)
            return result
        return manager.status(params)

    def _tool_manage_git_history(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"log", "show", "diff_commits", "reconcile_git_history_preview", "restore_file_preview", "restore_file_apply", "revert_preview", "revert_apply"}:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 log、show、diff_commits、reconcile_git_history_preview、restore_file_preview、restore_file_apply、revert_preview 或 revert_apply。")
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_git_history", params, require_managed=True)
        manager = MCPGitHistoryManager(self.project_root, self.source_review)
        result = manager.handle(action, params)
        self._record_workflow_if_needed("manage_git_history", action, params, result)
        return result

    def _tool_preview_insert_version(self, params: dict[str, Any]) -> dict[str, Any]:
        spec = self._parse_spec_json_or_legacy(params)
        return self.bridge.preview_insert_version(self.project_root, spec)

    def _tool_preview_update_version(self, params: dict[str, Any]) -> dict[str, Any]:
        spec = self._parse_spec_json_or_legacy(params)
        return self.bridge.preview_update_version(self.project_root, spec)

    def _tool_get_plan_patch_status(self, params: dict[str, Any]) -> dict[str, Any]:
        patch_id = params.get("patch_id")
        if not isinstance(patch_id, str) or not patch_id.strip():
            raise PlanningBridgeError("patch_id 参数不能为空。")
        return self.bridge.get_plan_patch_status(self.project_root, patch_id.strip())

    def _tool_get_repo_overview(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, _ = self._resolve_read_only_project_context(params)
        result = self.source_review.get_repo_overview(project_root, self._strip_project_name_param(params))
        return self._with_project_identity(result, project_root)

    def _tool_get_git_status(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, _ = self._resolve_read_only_project_context(params)
        hint = params.get("project_name") is None
        return self._with_project_identity(self.source_review.get_git_status(project_root), project_root, hint_project_name=hint)

    def _tool_get_git_log(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, project_record = self._resolve_read_only_project_context(params)
        result = self.source_review.get_git_log(project_root, self._strip_project_name_param(params))
        if isinstance(project_record, dict) and result.get("ok"):
            result["project_name"] = project_record.get("project_name")
        return result

    def _tool_manage_files(self, params: dict[str, Any]) -> dict[str, Any]:
        action = params.get("action", "")
        if not isinstance(action, str) or not action.strip():
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 search、read、create、edit 或 delete。")
        action = action.strip().lower()
        if action == "search":
            search_params = dict(params)
            search_params.pop("action", None)
            result = self._tool_search_source(search_params)
            if isinstance(result, dict) and result.get("ok"):
                result["action"] = "search"
                result["delegated_tool"] = "search_source"
            return result
        elif action == "read":
            read_params = dict(params)
            read_params.pop("action", None)
            result = self._tool_get_source_file(read_params)
            if isinstance(result, dict) and result.get("ok"):
                result["action"] = "read"
                result["delegated_tool"] = "get_source_file"
            return result
        elif action in {"create", "edit", "delete"}:
            phase = params.get("phase")
            if not isinstance(phase, str) or not phase.strip():
                raise MCPToolInputError("INVALID_PHASE", f"{action} 操作需要 phase（preview、apply 或 status）。")
            phase = phase.strip().lower()
            if phase not in {"preview", "apply", "status"}:
                raise MCPToolInputError("INVALID_PHASE", "phase 必须是 preview、apply 或 status。")
            lifecycle_params = dict(params)
            lifecycle_params.pop("phase", None)
            if action == "create" and phase == "preview":
                if "patch_text" in lifecycle_params:
                    raise MCPToolInputError("INVALID_INPUT", "create preview 不支持 patch_text；请使用 file + new_text 创建新文件。")
                old_text = lifecycle_params.get("old_text", "")
                if old_text != "":
                    raise MCPToolInputError("INVALID_OLD_TEXT", "create preview 必须使用 old_text=\"\" 或省略 old_text；编辑已有文件请使用 action=edit。")
                lifecycle_params["action"] = "preview"
                lifecycle_params["old_text"] = ""
                lifecycle_params["allow_create"] = True
            elif action == "delete" and phase == "preview":
                lifecycle_params["action"] = "preview_delete"
                for key in ("old_text", "new_text", "patch_text", "max_files"):
                    lifecycle_params.pop(key, None)
            else:
                lifecycle_params["action"] = phase
                if action == "edit" and phase == "preview":
                    lifecycle_params["allow_create"] = False
                    lifecycle_params["require_existing_file"] = True
            result = self._tool_manage_project_patch(lifecycle_params)
            if isinstance(result, dict):
                result["action"] = action
                result["phase"] = phase
                result["delegated_tool"] = "manage_project_patch"
            return result
        else:
            raise MCPToolInputError("INVALID_ACTION", "action 必须是 search、read、create、edit 或 delete。")

    def _tool_get_source_file(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, _ = self._resolve_read_only_project_context(params)
        result = self.source_review.get_source_file(project_root, self._strip_project_name_param(params))
        if result.get("ok"):
            hint = params.get("project_name") is None
            return self._with_project_identity(result, project_root, hint_project_name=hint)
        raise MCPToolInputError(
            str(result.get("error_code") or "SOURCE_FILE_ERROR"),
            str(result.get("message") or "读取源码文件失败。"),
        )

    def _tool_search_source(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, _ = self._resolve_read_only_project_context(params)
        result = self.source_review.search_source(project_root, self._strip_project_name_param(params))
        if result.get("ok"):
            hint = params.get("project_name") is None
            return self._with_project_identity(result, project_root, hint_project_name=hint)
        raise MCPToolInputError(
            str(result.get("error_code") or "SOURCE_SEARCH_ERROR"),
            str(result.get("message") or "搜索源码失败。"),
        )

    def _tool_get_git_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        project_root, _ = self._resolve_read_only_project_context(params)
        result = self.source_review.get_git_diff(project_root, self._strip_project_name_param(params))
        if result.get("ok"):
            hint = params.get("project_name") is None
            return self._with_project_identity(result, project_root, hint_project_name=hint)
        raise MCPToolInputError(
            str(result.get("error_code") or "GIT_DIFF_ERROR"),
            str(result.get("message") or "读取 git diff 失败。"),
        )

    def _tool_get_executor_inventory(self, params: dict[str, Any]) -> dict[str, Any]:
        result = load_executor_inventory(self.project_root)
        if result.get("ok"):
            return self._with_project_identity(result)
        raise MCPToolInputError(
            str(result.get("error_code") or "INVENTORY_ERROR"),
            str(result.get("message") or "读取执行器 inventory 失败。"),
        )

    def _tool_manage_executor_config(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_executor_config", params, require_managed=True)
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {
            "inspect_inventory",
            "probe_models_preview",
            "probe_models_apply",
            "set_default_profile_preview",
            "set_default_profile_apply",
        }:
            raise MCPToolInputError(
                "INVALID_ACTION",
                "action 必须是 inspect_inventory、probe_models_preview、probe_models_apply、set_default_profile_preview 或 set_default_profile_apply。",
            )
        manager = MCPExecutorConfigManager(self.project_root)
        result = manager.handle(action, params)
        self._record_workflow_if_needed("manage_executor_config", action, params, result)
        return result

    def _tool_manage_executor_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_executor_workflow", params, require_managed=True)

        action = params.get("action", "")
        project_path = params.get("project_root") or self.project_root
        provider = params.get("provider", "codex")
        model_raw = params.get("model")
        model = model_raw.strip() if isinstance(model_raw, str) else ""
        execution_mode = params.get("execution_mode", "run")
        preview_id = params.get("preview_id", "")
        max_diff_chars = self._bounded_int_param(params.get("max_diff_chars"), default=40000, minimum=1, maximum=80000)
        include_diff_summary = self._bool_param(params.get("include_diff_summary"), default=True)
        include_report_markdown = self._bool_param(params.get("include_report_markdown"), default=False)
        max_report_chars = self._bounded_int_param(params.get("max_report_chars"), default=30000, minimum=1, maximum=60000)
        reason_raw = params.get("reason")
        reason = reason_raw.strip() if isinstance(reason_raw, str) else ""
        executor_session_mode = params.get("executor_session_mode", "auto")
        max_iterations = self._bounded_int_param(params.get("max_iterations"), default=1, minimum=1, maximum=3)
        trusted_mode = self._bool_param(params.get("trusted_mode"), default=False)
        stop_on_acceptance_failure = self._bool_param(params.get("stop_on_acceptance_failure"), default=True)
        stop_on_scope_violation = self._bool_param(params.get("stop_on_scope_violation"), default=True)
        stop_on_diff_too_large = self._bool_param(params.get("stop_on_diff_too_large"), default=True)
        max_total_diff_chars = self._bounded_int_param(params.get("max_total_diff_chars"), default=80000, minimum=1, maximum=200000)
        allow_fix = self._bool_param(params.get("allow_fix"), default=False)
        allow_commit = self._bool_param(params.get("allow_commit"), default=False)
        run_id = params.get("run_id", "")
        poll_attempt_raw = params.get("poll_attempt")
        if poll_attempt_raw is not None:
            try:
                poll_attempt = int(poll_attempt_raw)
            except Exception:
                poll_attempt = 1
            if poll_attempt < 1:
                poll_attempt = 1
        else:
            poll_attempt = 1
        latest = self._bool_param(params.get("latest"), default=True)
        report_id = params.get("report_id", "")
        version = params.get("version", "")
        manual_fix_prompt_raw = params.get("manual_fix_prompt")
        manual_fix_prompt = manual_fix_prompt_raw.strip() if isinstance(manual_fix_prompt_raw, str) else ""
        validation_run_id = params.get("validation_run_id", "")
        section = params.get("section", "")
        include_markdown = self._bool_param(params.get("include_markdown"), default=False)
        max_chars = self._bounded_int_param(params.get("max_chars"), default=20000, minimum=1, maximum=60000)
        resolution = params.get("resolution", "")
        if not isinstance(action, str) or not action.strip():
            return self._with_project_identity({
                "ok": False,
                "error_code": "ACTION_REQUIRED",
                "message": "action 不能为空。支持：preflight、run_once_preview、run_once、run_bounded_preview、run_bounded、get_audit_package、refresh_audit_package、recheck_report_preview、recheck_report_apply、manual_fix_prompt_preview、manual_fix_prompt_apply、manual_validation_preview、manual_validation_apply、scope_mismatch_preview、scope_mismatch_apply、status。",
            })
        manager = MCPExecutorWorkflowManager(project_path)
        workflow_params = {
            "provider": provider,
            "model": model,
            "execution_mode": execution_mode,
            "preview_id": preview_id,
            "max_diff_chars": max_diff_chars,
            "include_diff_summary": include_diff_summary,
            "include_report_markdown": include_report_markdown,
            "max_report_chars": max_report_chars,
            "reason": reason,
            "max_iterations": max_iterations,
            "trusted_mode": trusted_mode,
            "stop_on_acceptance_failure": stop_on_acceptance_failure,
            "stop_on_scope_violation": stop_on_scope_violation,
            "stop_on_diff_too_large": stop_on_diff_too_large,
            "max_total_diff_chars": max_total_diff_chars,
            "allow_fix": allow_fix,
            "allow_commit": allow_commit,
            "run_id": run_id,
            "poll_attempt": poll_attempt,
            "latest": latest,
            "report_id": report_id,
            "version": version,
            "manual_fix_prompt": manual_fix_prompt,
            "validation_run_id": validation_run_id,
            "section": section,
            "include_markdown": include_markdown,
            "max_chars": max_chars,
            "resolution": resolution,
        }
        if action.strip().lower() == "run_once" or "executor_session_mode" in params:
            workflow_params["executor_session_mode"] = executor_session_mode
        result = manager.handle(action.strip().lower(), workflow_params)
        self._record_workflow_if_needed("manage_executor_workflow", action.strip().lower(), params, result)
        return self._with_project_identity(result)

    def _tool_manage_validation_run(self, params: dict[str, Any]) -> dict[str, Any]:
        action_raw = params.get("action")
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        if action not in {"inspect", "preview", "run", "status"}:
            raise MCPToolInputError(
                "INVALID_ACTION",
                "action 必须是 inspect、preview、run 或 status。",
            )
        if params.get("project_name") is not None:
            return self._route_project_name_tool("manage_validation_run", params, require_managed=True)
        manager = MCPValidationRunManager(self.project_root)
        result = manager.handle(action, params)
        self._record_workflow_if_needed("manage_validation_run", action, params, result)
        return self._with_project_identity(result)

    def _create_mcp_workflow_router(self) -> MCPWorkflowRouter:
        return MCPWorkflowRouter(
            project_root=self.project_root,
            source_review=self.source_review,
            analyze_state_fn=self._tool_analyze_project_state,
            plan_workflow_manager=MCPPlanWorkflowManager(self.project_root, self.source_review),
            project_patch_manager=MCPProjectPatchManager(self.project_root, self.source_review),
            project_docs_manager=MCPProjectDocsManager(self.project_root, self.source_review),
            git_history_manager=MCPGitHistoryManager(self.project_root, self.source_review),
            git_commit_manager=MCPGitCommitManager(self.project_root),
        )

    def _tool_run_mcp_workflow(self, params: dict[str, Any]) -> dict[str, Any]:
        workflow = _normalize_run_mcp_workflow_name(params.get("workflow"))
        if workflow not in _SUPPORTED_MCP_WORKFLOWS:
            raise MCPToolInputError("INVALID_WORKFLOW", f"未知 workflow：{workflow}")
        project_name = params.get("project_name")
        if project_name is not None:
            return self._route_project_name_tool("run_mcp_workflow", params, require_managed=True)

        result = self._create_mcp_workflow_router().handle(workflow, params)
        self._record_workflow_if_needed("run_mcp_workflow", workflow, params, result)
        return result

    def _tool_list_workflow_runs(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("list_workflow_runs", params, require_managed=True)
        limit = self._bounded_int_param(params.get("limit"), default=20, minimum=1, maximum=100)
        workflow_name_raw = params.get("workflow_name")
        workflow_name = workflow_name_raw.strip() if isinstance(workflow_name_raw, str) else None
        status_raw = params.get("status")
        status = status_raw.strip() if isinstance(status_raw, str) else None
        store = WorkflowRecordStore(self.project_root)
        return store.list_runs(limit=limit, workflow_name=workflow_name, status=status)

    def _tool_get_workflow_run(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("project_name") is not None:
            return self._route_project_name_tool("get_workflow_run", params, require_managed=True)
        workflow_id = params.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            return {"ok": False, "error_code": "INVALID_WORKFLOW_ID", "message": "workflow_id 必须是非空字符串。"}
        store = WorkflowRecordStore(self.project_root)
        return store.get_run(workflow_id.strip())

    def _record_workflow_if_needed(self, tool_name: str, action: str, params: dict[str, Any], result: dict[str, Any]) -> str | None:
        if not should_record_tool(tool_name, action):
            return None
        if not isinstance(result, dict):
            return None
        ret = record_tool_call(self.project_root, tool_name, action, params, result)
        warning = ret.get("warning")
        if warning:
            existing = result.get("workflow_record_warning")
            if existing:
                result["workflow_record_warning"] = f"{existing}; {warning}"
            else:
                result["workflow_record_warning"] = warning
        wf_id = ret.get("workflow_id")
        if isinstance(wf_id, str) and wf_id.strip():
            result["workflow_id"] = wf_id.strip()
            return wf_id.strip()
        return None

    def _result(self, req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _protocol_error(self, req_id: Any, code: int, error_code: str, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message,
                "data": {"error_code": error_code},
            },
        }

    def _tool_error(self, tool: str, error_code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": tool,
            "error_code": error_code,
            "message": message,
            "details": details or {},
        }

    def _parse_spec_json_or_legacy(self, params: dict[str, Any]) -> dict[str, Any]:
        spec: Any = None
        spec_json = params.get("spec_json")
        if isinstance(spec_json, str):
            try:
                spec = json.loads(spec_json)
            except Exception:
                raise MCPToolInputError(
                    "INVALID_SPEC_JSON",
                    "spec_json must be valid JSON",
                )
            if not isinstance(spec, dict):
                raise MCPToolInputError(
                    "INVALID_SPEC_JSON",
                    "spec_json must be valid JSON",
                )
            return spec
        if spec_json is not None:
            raise MCPToolInputError(
                "INVALID_SPEC_JSON",
                "spec_json must be a string",
            )
        legacy_spec = params.get("spec")
        if isinstance(legacy_spec, dict):
            spec = legacy_spec
        else:
            spec = params
        if not isinstance(spec, dict):
            raise MCPToolInputError(
                "INVALID_SPEC_JSON",
                "spec_json must be valid JSON",
            )
        return spec

    def _log(self, text: str) -> None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
