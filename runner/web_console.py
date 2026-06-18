import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from runner.http_server_utils import ReusableThreadingHTTPServer
from runner.planning_bridge import PlanningBridge, PlanningBridgeError
from runner.executor_registry import (
    DEFAULT_EXECUTION_PROVIDER,
    get_executor_provider_display,
    is_supported_execution_provider,
    normalize_execution_provider,
)
from runner.executor_inventory import get_executor_inventory_summary
from runner.project_identity import build_project_identity
from runner.execution_branch import ExecutionBranchController
from runner.mcp_executor_workflow import (
    CLAIM_HEARTBEAT_INTERVAL_SECONDS,
    CLAIM_HEARTBEAT_STALE_MULTIPLIER,
    CLAIM_HEARTBEAT_STALE_MIN_SECONDS,
    CLAIMS_DIR,
    PREVIEWS_DIR,
)
from runner.execution_profile import resolve_version_execution_provider, get_version_execution_summary
from runner.workspace import ProjectWorkspace
from runner.plan_loader import PlanLoader
from runner.state_store import StateStore
from runner.state_machine import RunnerStateMachine
from runner.executor_run_workflow import ExecutorRunOnceService
from runner.mcp_git_commit import MCPGitCommitManager
from runner.mcp_git_remote import MCPGitRemoteManager
from runner.mcp_decisions import MCPDecisionRecordsManager
from runner.mcp_project_memory import MCPProjectMemoryManager
from runner.mcp_todolist import MCPTodoListManager
from runner.acceptance_workflow import AcceptanceRerunService
from runner.checkpoint_review_workflow import CheckpointReviewService
from runner.plan_reload_workflow import PlanReloadService
from runner.continue_version_workflow import ContinueNextVersionService
from runner.plan_patch_workflow import PlanPatchAutoApplyService
from runner.project_registry import ProjectRegistry
from runner.runner_settings import RunnerSettingsStore
from runner.executor_session import ExecutorSessionStore, select_executor_identity_for_display
from runner.runner_paths import resolve_project_runner_dir, resolve_project_runner_rel_dir
from runner.web_console_v2_assets import render_v2_index_page
from runner.core_orchestrator import WorkflowOrchestrator
from runner.core_output import CoreOutput
from runner.core_request import CoreRequest
from runner.web_console_presenter import (
    build_execution_display,
    build_executor_session_display,
    extract_model_display_from_plan_data,
)


class WebConsoleServer:
    def __init__(
        self,
        project_path: str,
        project_registry: ProjectRegistry | None = None,
        *,
        service_mode: bool = False,
    ):
        self.bridge = PlanningBridge()
        self.operation_lock = threading.Lock()
        self.operation_running = False
        self.operation_name = ""
        self.operation_started_at: str | None = None
        self.last_operation_result: dict[str, Any] | None = None
        self.job: dict[str, Any] = {"status": "idle"}
        self.pending_commit_preview: dict[str, Any] | None = None
        self.pending_run_preview: dict[str, Any] | None = None
        self.project_registry = project_registry or self._default_project_registry(project_path)
        self.service_mode = service_mode
        self.runner_settings_store = RunnerSettingsStore()
        self._set_project_root(project_path)
        self._settings_resolve_cache: dict[str, Any] = {}

    @classmethod
    def _default_project_registry(cls, project_path: str) -> ProjectRegistry:
        project_root = os.path.realpath(os.path.abspath(os.path.expanduser(project_path)))
        if cls._is_temporary_project_root(project_root):
            runtime_dir = os.path.join(resolve_project_runner_dir(project_root), "runtime")
            return ProjectRegistry(
                registry_path=os.path.join(runtime_dir, "project-registry.json"),
                user_settings_path=os.path.join(runtime_dir, "colameta-settings.json"),
            )
        return ProjectRegistry()

    @staticmethod
    def _is_temporary_project_root(project_root: str) -> bool:
        root = os.path.realpath(os.path.abspath(os.path.expanduser(project_root)))
        temp_root = os.path.realpath(tempfile.gettempdir())
        if root == temp_root or root.startswith(temp_root + os.sep):
            return True
        parts = set(root.split(os.sep))
        return "TemporaryItems" in parts or "Cleanup At Startup" in parts

    def _set_project_root(self, project_path: str) -> None:
        self.project_root = os.path.realpath(os.path.abspath(os.path.expanduser(project_path)))
        self.runner_dir = resolve_project_runner_dir(self.project_root)
        self.runner_rel_dir = resolve_project_runner_rel_dir(self.project_root)
        self.plan_file = os.path.join(self.runner_dir, "plan.json")
        self.state_file = os.path.join(self.runner_dir, "state.json")
        self.logs_dir = os.path.join(self.runner_dir, "logs")
        self.runtime_dir = os.path.join(self.runner_dir, "runtime")
        self.marker_file = os.path.join(self.runtime_dir, "plan-updated.marker")
        self.start_plan_mtime = self._safe_mtime(self.plan_file)
        self.start_marker_mtime = self._safe_mtime(self.marker_file)
        self.executor_session_store = ExecutorSessionStore(self.project_root)

    def _should_require_execution_branch(
        self,
        *,
        current_version: Any,
        resolved_provider: str,
        mainline_provider: str,
    ) -> bool:
        resolved = normalize_execution_provider(resolved_provider, default=DEFAULT_EXECUTION_PROVIDER)
        mainline = normalize_execution_provider(mainline_provider, default=DEFAULT_EXECUTION_PROVIDER)
        if resolved == "opencode":
            return True
        if resolved != mainline:
            return True
        if current_version is None or current_version.execution is None:
            return False
        override_provider = getattr(current_version.execution, "provider", None)
        if not isinstance(override_provider, str) or not override_provider.strip():
            return False
        normalized_override = normalize_execution_provider(override_provider, default=mainline)
        return normalized_override != mainline

    def validate_project(self, mode: str | None = None) -> None:
        if not os.path.isdir(self.project_root):
            raise PlanningBridgeError(f"项目目录不存在：{self.project_root}")
        if not os.path.isdir(self.runner_dir):
            raise PlanningBridgeError(f"缺少运行目录：{self.runner_dir}")
        if mode == "source-only":
            return
        if mode == "managed":
            if not os.path.exists(self.plan_file):
                raise PlanningBridgeError(
                    "当前项目尚未纳入 Runner 管理；后续版本会支持 managed 自动最小纳管。当前可先使用 source-only 模式启动 MCP，或通过 manage_runner_plan 完成纳管。"
                )
            return
        if not os.path.exists(self.plan_file):
            raise PlanningBridgeError(
                f"当前项目尚未纳入 Runner：缺少 {self.runner_rel_dir}/plan.json。\n"
                "请通过 MCP manage_runner_plan 创建受控 plan，\n"
                "或使用 CLI import-plan-file 作为高级 fallback。"
            )
        if not os.path.exists(self.state_file):
            raise PlanningBridgeError(f"缺少状态文件：{self.state_file}")

    def _now_iso(self) -> str:
        return datetime.now().astimezone().isoformat()

    def _operation_running_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "OPERATION_RUNNING",
            "message": f"当前有操作正在运行：{self.operation_name or 'unknown'}",
            "operation": {
                "name": self.operation_name or "unknown",
                "status": "running",
                "started_at": self.operation_started_at,
            },
        }

    def _operation_payload(self, operation_name: str, started_at: str | None, fn) -> dict[str, Any]:
        started_at = started_at or self._now_iso()
        try:
            result = fn()
            payload = {
                "ok": bool(result.get("ok", True)),
                "operation": operation_name,
                "status": "ok" if result.get("ok", True) else "failed",
                "started_at": started_at,
                "ended_at": self._now_iso(),
                "message": result.get("message", ""),
                "result": result,
                "error_code": result.get("error_code"),
            }
            self.last_operation_result = payload
            return payload
        except Exception as e:
            payload = {
                "ok": False,
                "operation": operation_name,
                "status": "failed",
                "started_at": started_at,
                "ended_at": self._now_iso(),
                "message": str(e),
                "result": {},
                "error_code": "OPERATION_FAILED",
            }
            self.last_operation_result = payload
            return payload

    def _run_operation(self, operation_name: str, fn) -> dict[str, Any]:
        with self.operation_lock:
            if self.operation_running:
                return self._operation_running_payload()
            if operation_name not in ("commit_preview", "commit_confirm"):
                self.pending_commit_preview = None
            if operation_name not in ("run_current_version_preview", "run_current_version_confirm"):
                self.pending_run_preview = None
            self.operation_running = True
            self.operation_name = operation_name
            self.operation_started_at = self._now_iso()
        started_at = self.operation_started_at
        try:
            return self._operation_payload(operation_name, started_at, fn)
        finally:
            with self.operation_lock:
                self.operation_running = False
                self.operation_name = ""
                self.operation_started_at = None

    def _load_runtime_context(self) -> tuple[ProjectWorkspace, Any, Any, StateStore, RunnerStateMachine]:
        workspace = ProjectWorkspace.from_project_path(self.project_root)
        workspace.ensure_directories()
        loader = PlanLoader()
        plan = loader.load_plan(workspace.plan_file)
        plan.project_root = workspace.workspace_root
        plan.logs_dir = workspace.logs_dir
        plan.runtime_dir = workspace.runtime_dir
        plan.state_file = workspace.state_file
        if not os.path.isabs(plan.rules_file):
            plan.rules_file = workspace.rules_file
        loader.validate_plan(plan)
        store = StateStore()
        state = store.load_state(workspace.state_file)
        machine = RunnerStateMachine(plan, state)
        return workspace, plan, state, store, machine

    def _to_rel(self, path: str | None) -> str:
        if not path:
            return "-"
        return self._to_project_relative(path)

    def serve_http(self, host: str = "127.0.0.1", port: int = 8787) -> int:
        server = self

        class WebConsoleHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, payload: dict[str, Any], status_code: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html_text: str) -> None:
                body = html_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json_body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    if length <= 0:
                        return {}
                    raw = self.rfile.read(length).decode("utf-8")
                    parsed = json.loads(raw)
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/":
                    self._send_html(server._render_v2_index_html())
                    return
                if path == "/legacy/":
                    self._send_json(
                        {"ok": False, "error_code": "GONE", "message": "旧 Web Console 已移除，请使用 /。"},
                        status_code=410,
                    )
                    return
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if path == "/api/healthz":
                    self._send_json({"ok": True, "service": "colameta-web-console"})
                    return
                if path == "/api/status":
                    self._send_json(server._api_status())
                    return
                if path == "/api/version-result":
                    self._send_json(server._api_version_result())
                    return
                if path == "/api/next-plan":
                    self._send_json(server._api_next_plan())
                    return
                if path == "/api/plan-overview":
                    self._send_json(server._api_plan_overview())
                    return
                if path == "/api/log-tail":
                    self._send_json(server._api_log_tail())
                    return
                if path == "/api/plan-patches":
                    self._send_json(server._api_plan_patches())
                    return
                if path == "/api/version-prompt":
                    version = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("version", [None])[0]
                    self._send_json(server._api_version_prompt(version=version))
                    return
                if path == "/api/job-status":
                    self._send_json(server._api_job_status())
                    return
                if path == "/api/project-registry":
                    self._send_json(server._api_project_registry())
                    return
                if path == "/v2/":
                    self._send_html(server._render_v2_index_html())
                    return
                if path == "/api/v2/status":
                    self._send_json(server._api_v2_status())
                    return
                if path == "/api/v2/health":
                    self._send_json(server._api_v2_health())
                    return
                self._send_json({"ok": False, "message": "not_found"}, status_code=404)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path == "/api/jobs/start":
                    body = self._read_json_body()
                    write_guard = server._validate_web_write_request(body)
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_start_job(body))
                    return
                if path == "/api/auto-apply-patches":
                    write_guard = server._validate_web_write_request(self._read_json_body())
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_auto_apply_patches())
                    return
                if path == "/api/run-current-version":
                    body = self._read_json_body()
                    write_guard = server._validate_web_write_request(body)
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_run_current_version())
                    return
                if path == "/api/fix-current-version":
                    write_guard = server._validate_web_write_request(self._read_json_body())
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_fix_current_version())
                    return
                if path == "/api/reload-plan":
                    write_guard = server._validate_web_write_request(self._read_json_body())
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_reload_plan())
                    return
                if path == "/api/continue-next-version":
                    write_guard = server._validate_web_write_request(self._read_json_body())
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_continue_next_version())
                    return
                if path == "/api/rerun-acceptance":
                    write_guard = server._validate_web_write_request(self._read_json_body())
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_rerun_acceptance())
                    return
                if path == "/api/checkpoint-review":
                    write_guard = server._validate_web_write_request(self._read_json_body())
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_checkpoint_review())
                    return
                if path == "/api/commit-preview":
                    self._send_json(server._api_commit_preview_with_project())
                    return
                if path == "/api/commit-confirm":
                    self._send_json(server._api_commit_confirm_with_project())
                    return
                if path == "/api/switch-executor":
                    body = self._read_json_body()
                    write_guard = server._validate_web_write_request(body)
                    if write_guard is not None:
                        self._send_json(write_guard)
                        return
                    self._send_json(server._api_switch_executor(body))
                    return
                if path == "/api/switch-project":
                    body = self._read_json_body()
                    self._send_json(server._api_switch_project(body))
                    return
                if path == "/api/project-identity/preview":
                    body = self._read_json_body()
                    self._send_json(server._api_project_identity_preview(body))
                    return
                if path == "/api/project-identity/apply":
                    body = self._read_json_body()
                    self._send_json(server._api_project_identity_apply(body))
                    return
                if path == "/api/v2/action":
                    body = self._read_json_body()
                    self._send_json(server._api_v2_action(body))
                    return
                self._send_json({"ok": False, "message": "not_found"}, status_code=404)

        httpd = ReusableThreadingHTTPServer((host, port), WebConsoleHandler)
        self._httpd = httpd
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
            httpd.server_close()
        return 0

    def _validate_preview_request(self, body: dict[str, Any] | None) -> dict[str, Any] | None:
        payload = body or {}
        if payload.get("project_root") is not None:
            return {
                "ok": False,
                "error_code": "INVALID_PARAMS",
                "message": "project_root is not allowed.",
            }
        return None

    def _validate_commit_confirm_request(self, body: dict[str, Any] | None) -> dict[str, Any] | None:
        payload = body or {}
        if payload.get("project_root") is not None:
            return {
                "ok": False,
                "error_code": "INVALID_PARAMS",
                "message": "project_root is not allowed.",
            }
        return None

    def _validate_web_write_request(self, body: dict[str, Any] | None) -> dict[str, Any] | None:
        payload = body or {}
        if payload.get("project_root") is not None:
            return {
                "ok": False,
                "error_code": "INVALID_PARAMS",
                "message": "project_root is not allowed.",
            }
        return None

    def _validate_run_preview_request(self, body: dict[str, Any] | None) -> dict[str, Any] | None:
        payload = body or {}
        if payload.get("project_root") is not None:
            return {
                "ok": False,
                "error_code": "INVALID_PARAMS",
                "message": "project_root is not allowed.",
            }
        return None

    def _is_pending_run_preview_expired(self, preview: dict[str, Any]) -> bool:
        created_ts = preview.get("created_ts")
        try:
            created = float(created_ts)
        except (TypeError, ValueError):
            return True
        now_ts = datetime.now(timezone.utc).timestamp()
        return (now_ts - created) > 3600

    def _validate_run_confirm_request(self, body: dict[str, Any] | None) -> dict[str, Any] | None:
        payload = body or {}
        if payload.get("project_root") is not None:
            return {
                "ok": False,
                "error_code": "INVALID_PARAMS",
                "message": "project_root is not allowed.",
            }
        preview = self.pending_run_preview
        if not isinstance(preview, dict):
            return {
                "ok": False,
                "error_code": "RUN_PREVIEW_REQUIRED",
                "message": "请先为当前项目生成运行预览。",
            }
        if self._is_pending_run_preview_expired(preview):
            self.pending_run_preview = None
            return {
                "ok": False,
                "error_code": "PREVIEW_EXPIRED",
                "message": "运行预览已过期，请重新生成。",
            }
        return None

    def _api_status(self) -> dict[str, Any]:
        try:
            data = self.bridge.get_runner_status(self.project_root)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        data["ok"] = True
        data["plan_reload_needed"] = self._is_plan_reload_needed()
        data["plan_file"] = self.plan_file
        data["mcp_hint"] = "MCP：只读 + 生成计划更新；应用由 Web Console 本地自动完成。"
        data["operation_running"] = self.operation_running
        data["operation_name"] = self.operation_name
        data["operation_started_at"] = self.operation_started_at
        data["last_operation_result"] = self.last_operation_result
        data["pending_commit_preview_ready"] = bool(
            self.pending_commit_preview
            and isinstance(self.pending_commit_preview.get("commit_files"), list)
            and len(self.pending_commit_preview.get("commit_files", [])) > 0
        )
        data["remote_git"] = self._api_remote_git_status()
        data["execution_display"] = self._api_execution_display()
        data["project_registry"] = self._api_project_registry()
        try:
            data["executor_session_status"] = self.executor_session_store.get_status()
        except Exception:
            data["executor_session_status"] = {
                "ok": False,
                "active": False,
                "message": "执行会话状态读取失败。",
            }
        try:
            data["executor_continuation_preview"] = self.executor_session_store.get_continuation_preview()
        except Exception:
            data["executor_continuation_preview"] = {
                "ok": False,
                "continuation_available": False,
                "message": "执行会话续接预览读取失败。",
            }
        current_provider = self._resolve_current_execution_provider(
            fallback_provider=data["execution_display"].get("provider", DEFAULT_EXECUTION_PROVIDER)
        )
        try:
            data["executor_continuation_decision"] = self.executor_session_store.get_continuation_decision(
                requested_provider=current_provider
            )
        except Exception:
            data["executor_continuation_decision"] = {
                "ok": False,
                "decision": "start_new_blocked",
                "continuation_available": False,
                "message": "执行会话续接决策读取失败。",
            }
        try:
            data["executor_resume_invocation_preview"] = self.executor_session_store.get_resume_invocation_preview(
                requested_provider=current_provider
            )
        except Exception:
            data["executor_resume_invocation_preview"] = {
                "ok": False,
                "resume_invocation_supported": False,
                "resume_invocation_verified": False,
                "resume_invocation_kind": "preview_unavailable",
                "command_preview": [],
                "message": "执行会话调用形态预览读取失败。",
            }
        data["executor_session_display"] = build_executor_session_display(
            executor_session_status=data.get("executor_session_status"),
            continuation_decision=data.get("executor_continuation_decision"),
            resume_invocation_preview=data.get("executor_resume_invocation_preview"),
            continuation_preview=data.get("executor_continuation_preview"),
        )
        data["executor_auto_resume_policy"] = {
            "policy": "auto_when_safe",
            "provider_scope": ["codex", "opencode"],
            "enabled_for_current_provider": current_provider in {"codex", "opencode"},
        }
        data["executor_inventory_summary"] = get_executor_inventory_summary(
            self.project_root,
            data["execution_display"].get("provider", DEFAULT_EXECUTION_PROVIDER),
        )
        data["project_identity"] = build_project_identity(self.project_root)
        try:
            branch_ctrl = ExecutionBranchController(self.project_root)
            data["execution_branch_status"] = branch_ctrl.get_status()
            review = branch_ctrl.get_review_summary()
            data["execution_branch_review"] = review
            _, plan, state, _, _ = self._load_runtime_context()
            settings_provider = data["execution_display"].get("provider", DEFAULT_EXECUTION_PROVIDER)
            cv = plan.versions[state.current_version_index] if state.current_version_index is not None and 0 <= state.current_version_index < len(plan.versions) else None
            resolved_provider = resolve_version_execution_provider(plan=plan, version=cv, fallback_provider=settings_provider)
            require_branch = self._should_require_execution_branch(
                current_version=cv,
                resolved_provider=resolved_provider,
                mainline_provider=settings_provider,
            )
            guard = branch_ctrl.validate_execution_ready(version=cv.version if cv else "", provider=resolved_provider, require_branch=require_branch)
            data["execution_branch_guard"] = guard
        except Exception:
            data["execution_branch_status"] = {"ok": False, "message": "读取执行安全分支状态失败。"}
            data["execution_branch_guard"] = {"required": False, "ok": False, "message": "读取执行安全分支状态失败。"}
            data["execution_branch_review"] = {"ok": False, "message": "读取执行安全分支审查摘要失败。"}
        try:
            _, plan, state, _, _ = self._load_runtime_context()
            current_version = plan.versions[state.current_version_index] if state.current_version_index is not None and 0 <= state.current_version_index < len(plan.versions) else None
            fallback_provider = data["execution_display"].get("provider", DEFAULT_EXECUTION_PROVIDER)
            settings = self.runner_settings_store.load_for_project(self.project_root, self.plan_file)
            data["version_execution_display"] = get_version_execution_summary(
                plan=plan, version=current_version, fallback_provider=fallback_provider, settings=settings,
            )
        except Exception:
            data["version_execution_display"] = None
        return data

    def _api_remote_git_status(self) -> dict[str, Any]:
        try:
            result = MCPGitRemoteManager(self.project_root).push_status()
        except Exception:
            return {
                "ok": False,
                "message": "无法读取远程 Git 状态。",
                "blockers": ["remote_git_status_unavailable"],
                "warnings": [],
                "commits": [],
            }
        return self._sanitize_remote_git_status(result)

    def _sanitize_remote_git_status(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {
                "ok": False,
                "message": "无法读取远程 Git 状态。",
                "blockers": ["remote_git_status_unavailable"],
                "warnings": [],
                "commits": [],
            }
        allowed_fields = {
            "ok",
            "action",
            "branch",
            "upstream",
            "remote_name",
            "remote_url_redacted",
            "head_short",
            "ahead",
            "behind",
            "working_tree_clean",
            "can_push",
            "can_preview",
            "blockers",
            "warnings",
            "message",
            "error_code",
        }
        sanitized = {key: self._redact_remote_git_display_value(result.get(key)) for key in allowed_fields if key in result}
        commits = []
        if isinstance(result.get("commits"), list):
            for item in result["commits"][:5]:
                if not isinstance(item, dict):
                    continue
                commits.append(
                    {
                        "short_hash": str(item.get("short_hash") or item.get("hash") or "")[:12],
                        "subject": str(self._redact_remote_git_display_value(item.get("subject") or ""))[:240],
                    }
                )
        sanitized["commits"] = commits
        sanitized.setdefault("blockers", [])
        sanitized.setdefault("warnings", [])
        return sanitized

    def _redact_remote_git_display_value(self, value: Any) -> Any:
        if isinstance(value, str):
            text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1***@", value)
            text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", text)
            return text
        if isinstance(value, list):
            return [self._redact_remote_git_display_value(item) for item in value]
        if isinstance(value, dict):
            return {str(k): self._redact_remote_git_display_value(v) for k, v in value.items()}
        return value

    def _api_version_result(self) -> dict[str, Any]:
        try:
            data = self.bridge.get_version_result(self.project_root)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        data["ok"] = True
        return data

    def _api_next_plan(self) -> dict[str, Any]:
        try:
            data = self.bridge.get_next_version_plan(self.project_root)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        data["ok"] = True
        return data

    def _api_plan_overview(self) -> dict[str, Any]:
        try:
            data = self.bridge.get_plan_overview(self.project_root)
        except Exception as e:
            return {"ok": False, "message": str(e)}
        data["ok"] = True
        state_readable = True
        state_message = ""
        try:
            data["version_rows"] = self._build_version_rows(data.get("versions", []))
        except Exception as e:
            state_readable = False
            state_message = str(e)
            data["version_rows"] = self._build_version_rows_from_plan(data.get("versions", []))
        data["state_readable"] = state_readable
        data["state_message"] = state_message
        return data

    def _api_version_prompt(self, version: str | None = None) -> dict[str, Any]:
        if not version or not isinstance(version, str) or not version.strip():
            return {"ok": False, "error_code": "VERSION_REQUIRED", "message": "version 参数不能为空。"}
        version = version.strip()
        plan = self._read_json_file(self.plan_file)
        if not plan:
            return {"ok": False, "error_code": "PLAN_NOT_FOUND", "message": "plan.json 不存在。"}
        target_ver = None
        for v in plan.get("versions", []):
            if v.get("version") == version:
                target_ver = v
                break
        if target_ver is None:
            return {"ok": False, "error_code": "VERSION_NOT_FOUND", "message": f"版本 {version} 不存在于 plan 中。", "version": version}
        prompt_file_abs, resolve_error = self._resolve_prompt_file(target_ver.get("prompt_file"), version)
        if resolve_error == "PROMPT_NOT_FOUND":
            return {"ok": False, "error_code": "PROMPT_NOT_FOUND", "message": f"版本 {version} 的 prompt 文件不存在。", "version": version, "prompt_file": None}
        if resolve_error == "PROMPT_FILE_UNSAFE":
            return {"ok": False, "error_code": "PROMPT_FILE_UNSAFE", "message": "prompt 文件路径不安全。", "version": version}
        try:
            text = Path(prompt_file_abs).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {"ok": False, "error_code": "PROMPT_READ_ERROR", "message": "读取 prompt 文件失败。", "version": version}
        char_count = len(text)
        line_count = text.count("\n") + 1 if text else 0
        truncated = False
        max_chars = 50000
        if char_count > max_chars:
            text = text[:max_chars]
            truncated = True
            char_count = max_chars
        return {
            "ok": True,
            "version": version,
            "version_name": str(target_ver.get("name") or target_ver.get("description") or ""),
            "prompt_file": self._to_project_relative(prompt_file_abs),
            "content": text,
            "char_count": char_count,
            "line_count": line_count,
            "truncated": truncated,
            "report": self._version_prompt_report_payload(version),
        }

    def _version_prompt_report_payload(self, version: str) -> dict[str, Any]:
        try:
            from runner.executor_read import handle_inspect_executor_activity
            report_result = handle_inspect_executor_activity(
                self.project_root,
                "get_report",
                {
                    "version": version,
                    "latest": True,
                    "include_markdown": True,
                    "max_report_chars": 50000,
                },
            )
        except Exception as exc:
            return {
                "available": False,
                "error_code": "REPORT_READ_ERROR",
                "message": f"读取报告失败：{exc}",
            }
        if not report_result.get("ok"):
            return {
                "available": False,
                "error_code": str(report_result.get("error_code") or "REPORT_NOT_FOUND"),
                "message": str(report_result.get("message") or "该版本暂无执行器报告。"),
            }
        report = report_result.get("report") if isinstance(report_result.get("report"), dict) else {}
        markdown_file = str(report.get("markdown_file") or "")
        return {
            "available": True,
            "report_id": str(report.get("report_id") or ""),
            "status": str(report.get("status") or ""),
            "provider": str(report.get("provider") or ""),
            "report_file": self._to_project_relative(markdown_file) if markdown_file else "",
            "content": str(report_result.get("report_markdown") or ""),
            "truncated": bool(report_result.get("truncated", False)),
        }

    def _build_version_rows_from_plan(self, plan_versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for plan_item in plan_versions:
            version_id = str(plan_item.get("version", ""))
            prompt_file_abs, resolve_error = self._resolve_prompt_file(plan_item.get("prompt_file"), version_id)
            prompt_excerpt = None
            prompt_missing = True
            display_path = None
            if prompt_file_abs is not None:
                prompt_excerpt = self._read_prompt_excerpt(prompt_file_abs)
                prompt_missing = False
                display_path = self._to_project_relative(prompt_file_abs)
            rows.append(
                {
                    "version": version_id,
                    "name": plan_item.get("name"),
                    "enabled": bool(plan_item.get("enabled", True)),
                    "is_current": False,
                    "runtime_status": None,
                    "attempt": 0,
                    "commit_hash": None,
                    "is_checkpoint": False,
                    "reviewed": False,
                    "prompt_file": display_path,
                    "prompt_excerpt": prompt_excerpt,
                    "prompt_missing": prompt_missing,
                }
            )
        return rows

    def _api_log_tail(self) -> dict[str, Any]:
        state = self._read_json_file(self.state_file)
        if not state:
            return {"ok": False, "message": "状态文件读取失败。"}
        raw_log_path = state.get("last_log_file")
        if not isinstance(raw_log_path, str) or not raw_log_path.strip():
            return {
                "ok": True,
                "log_path": None,
                "log_path_rel": None,
                "tail": "",
                "message": "暂无日志。",
            }
        log_path = os.path.abspath(raw_log_path.strip())
        logs_root = os.path.abspath(self.logs_dir)
        if not (log_path == logs_root or log_path.startswith(logs_root + os.sep)):
            return {
                "ok": False,
                "log_path": raw_log_path,
                "message": f"日志路径不在 {self.runner_rel_dir}/logs 目录内。",
            }
        if not os.path.exists(log_path):
            return {
                "ok": True,
                "log_path": log_path,
                "log_path_rel": self._to_project_relative(log_path),
                "tail": "",
                "message": "日志文件不存在。",
            }
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {
                "ok": False,
                "log_path": log_path,
                "log_path_rel": self._to_project_relative(log_path),
                "message": f"日志读取失败：{e}",
            }
        lines = text.splitlines()
        tail_lines = lines[-120:]
        tail_text = "\n".join(tail_lines)
        if len(tail_text) > 12000:
            tail_text = tail_text[-12000:]
        return {
            "ok": True,
            "log_path": log_path,
            "log_path_rel": self._to_project_relative(log_path),
            "tail": tail_text,
            "line_count": len(tail_lines),
            "char_count": len(tail_text),
            "message": "",
        }

    def _api_plan_patches(self) -> dict[str, Any]:
        try:
            data = self.bridge.list_plan_patches(self.project_root)
        except Exception as e:
            return {"ok": False, "message": str(e), "patches": []}
        data["ok"] = True
        return data

    def _api_auto_apply_patches(self) -> dict[str, Any]:
        if self.operation_running:
            return self._operation_running_payload()
        service = PlanPatchAutoApplyService(self.project_root)
        return service.auto_apply()

    def _job_operation_callable(self, operation: str):
        operations = {
            "run_current_version": lambda: self._api_execute_current_version("run", wrap=False),
            "fix_current_version": lambda: self._api_execute_current_version("fix", wrap=False),
            "rerun_acceptance": lambda: self._api_rerun_acceptance(wrap=False),
            "checkpoint_review": lambda: self._api_checkpoint_review(wrap=False),
            "commit_confirm": lambda: self._api_commit_confirm(wrap=False),
        }
        return operations.get(operation)

    def _api_start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation") or "").strip()
        fn = self._job_operation_callable(operation)
        if fn is None:
            return {
                "ok": False,
                "error_code": "INVALID_JOB_OPERATION",
                "message": "当前操作不可用。",
            }

        with self.operation_lock:
            if self.operation_running or self.job.get("status") == "running":
                return {
                    "ok": False,
                    "error_code": "JOB_ALREADY_RUNNING",
                    "message": "当前已有操作正在进行，请稍后再试。",
                }
            if operation not in ("commit_confirm",):
                self.pending_commit_preview = None
            job_id = uuid.uuid4().hex
            started_at = self._now_iso()
            self.operation_running = True
            self.operation_name = operation
            self.operation_started_at = started_at
            self.job = {
                "job_id": job_id,
                "operation": operation,
                "status": "running",
                "started_at": started_at,
                "ended_at": None,
                "message": "已开始处理。",
                "result": {},
                "error_code": "",
            }

        thread = threading.Thread(target=self._run_job_worker, args=(job_id, operation, fn), daemon=True)
        thread.start()
        return {
            "ok": True,
            "job_id": job_id,
            "operation": operation,
            "status": "running",
            "message": "已开始处理。",
        }

    def _run_job_worker(self, job_id: str, operation: str, fn) -> None:
        started_at = self.operation_started_at or self._now_iso()
        payload = self._operation_payload(operation, started_at, fn)
        status = "passed" if payload.get("ok") else "failed"
        with self.operation_lock:
            self.job = {
                "job_id": job_id,
                "operation": operation,
                "status": status,
                "started_at": payload.get("started_at"),
                "ended_at": payload.get("ended_at"),
                "message": payload.get("message", ""),
                "result": payload.get("result", {}),
                "error_code": payload.get("error_code") or "",
            }
            self.operation_running = False
            self.operation_name = ""
            self.operation_started_at = None

    def _api_job_status(self) -> dict[str, Any]:
        with self.operation_lock:
            job = dict(self.job) if self.job else {"status": "idle"}
        if not job:
            job = {"status": "idle"}
        return {"ok": True, "job": job}

    def _api_switch_executor(self, body: dict[str, Any]) -> dict[str, Any]:
        provider = (body or {}).get("provider", "").strip().lower()
        if not is_supported_execution_provider(provider):
            return {"ok": False, "message": f"不支持的执行器：{provider}"}
        try:
            from runner.runner_settings import RunnerSettings
            saved = self.runner_settings_store.save_settings_for_project(
                self.project_root,
                RunnerSettings(execution_provider=provider),
            )
            return {
                "ok": True,
                "provider": provider,
                "provider_display": get_executor_provider_display(provider),
                "settings_file": saved.get("settings_file"),
            }
        except Exception as e:
            return {"ok": False, "message": f"执行器切换失败：{e}"}

    def _load_execution_provider(self, workspace: ProjectWorkspace) -> str:
        settings = self.runner_settings_store.load_for_project(workspace.workspace_root, workspace.plan_file)
        if is_supported_execution_provider(settings.execution_provider):
            return settings.execution_provider
        return DEFAULT_EXECUTION_PROVIDER

    def _api_execution_display(self) -> dict[str, str]:
        try:
            workspace = ProjectWorkspace.from_project_path(self.project_root)
            provider = self._load_execution_provider(workspace)
        except Exception:
            provider = DEFAULT_EXECUTION_PROVIDER
        provider_display = get_executor_provider_display(provider)

        model_display = "默认模型"
        try:
            plan_data = self._read_json_file(self.plan_file) or {}
            model_display = extract_model_display_from_plan_data(plan_data)
        except Exception:
            model_display = "默认模型"

        return build_execution_display(
            provider=provider,
            provider_display=provider_display,
            model_display=model_display,
        )

    def _resolve_current_execution_provider(self, fallback_provider: str | None = None) -> str:
        fallback = normalize_execution_provider(
            fallback_provider or DEFAULT_EXECUTION_PROVIDER,
            default=DEFAULT_EXECUTION_PROVIDER,
        )
        try:
            workspace, plan, state, _, _ = self._load_runtime_context()
            current_version = (
                plan.versions[state.current_version_index]
                if state.current_version_index is not None and 0 <= state.current_version_index < len(plan.versions)
                else None
            )
            mainline = self._load_execution_provider(workspace)
            return resolve_version_execution_provider(
                plan=plan,
                version=current_version,
                fallback_provider=mainline,
            )
        except Exception:
            return fallback

    def _build_run_once_message(self, *, run_status: str, scope_status: str, execution_mode: str) -> str:
        mode = str(execution_mode or "run").strip().lower()
        run = str(run_status or "").strip().upper()
        scope = str(scope_status or "").strip().upper()
        is_fix = mode == "fix"
        if is_fix and run == "PASSED" and scope == "BLOCKED_BY_SCOPE_VIOLATION":
            return "当前版本修复完成，验收通过，改动范围校验失败。"
        if is_fix and run == "PASSED":
            return "当前版本修复完成，验收通过。"
        if is_fix:
            return "当前版本修复完成，验收未通过。"
        if run == "PASSED" and scope == "BLOCKED_BY_SCOPE_VIOLATION":
            return "当前版本运行完成，验收通过，改动范围校验失败。"
        if run == "PASSED":
            return "当前版本运行完成，验收通过。"
        return "当前版本运行完成，验收未通过。"

    def _api_execute_current_version(self, mode: str, wrap: bool = True) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            workspace, plan, state, _, _ = self._load_runtime_context()

            if not state.current_version:
                return {
                    "ok": False,
                    "error_code": "NO_CURRENT_VERSION",
                    "message": "当前没有可执行版本。",
                }

            if state.status == "VERSION_PASSED":
                return {
                    "ok": False,
                    "error_code": "VERSION_ALREADY_PASSED",
                    "message": "当前版本已通过。可按“进入下一版本”继续，或按“重新测试”复检。",
                }

            if state.status == "COMPLETED":
                return {
                    "ok": False,
                    "error_code": "ALL_COMPLETED",
                    "message": "所有版本已完成。",
                }

            if mode == "run" and state.status == "BLOCKED_BY_ACCEPTANCE_FAILURE":
                return {
                    "ok": False,
                    "error_code": "ACCEPTANCE_BLOCKED",
                    "message": "当前测试失败。请按“重新测试”重跑，或回到终端进入修复流程。",
                }

            if state.status == "BLOCKED_BY_MAX_FIX_ATTEMPTS":
                return {
                    "ok": False,
                    "error_code": "MAX_FIX_ATTEMPTS_REACHED",
                    "message": "当前版本已达到最大修复次数，流程已暂停。",
                }

            settings_provider = self._load_execution_provider(workspace)
            current_version = plan.versions[state.current_version_index] if state.current_version_index is not None and 0 <= state.current_version_index < len(plan.versions) else None
            provider = resolve_version_execution_provider(
                plan=plan, version=current_version, fallback_provider=settings_provider,
            )
            is_fix = mode == "fix" or state.status == "FIX_PROMPT_READY"

            if mode == "fix" and state.status != "FIX_PROMPT_READY":
                return {
                    "ok": False,
                    "error_code": "FIX_PROMPT_NOT_READY",
                    "message": "当前版本尚未进入修复执行阶段。请先在终端按 F 准备修复提示词。",
                }

            service = ExecutorRunOnceService(self.project_root)
            execution_mode = "fix" if is_fix else "run"
            run_ret = service.run_once(
                provider=provider,
                execution_mode=execution_mode,
                include_diff_summary=True,
                include_report_markdown=False,
                max_report_chars=30000,
                reason="web_console",
            )
            if not run_ret.get("ok"):
                return {
                    "ok": False,
                    "error_code": str(run_ret.get("error_code") or "EXECUTOR_FAILED"),
                    "message": str(run_ret.get("message") or "执行器运行失败。"),
                    "provider": provider,
                    "execution_mode": execution_mode,
                    "classification": run_ret.get("classification"),
                    "blocks": run_ret.get("blocks", []),
                    "warnings": run_ret.get("warnings", []),
                    "log_path": run_ret.get("log_path", ""),
                }

            run_status = str(run_ret.get("run_status") or "")
            scope_status = str(run_ret.get("scope_status") or "NOT_CHECKED")
            message = self._build_run_once_message(
                run_status=run_status,
                scope_status=scope_status,
                execution_mode=execution_mode,
            )
            lineage = {}
            report_summary = run_ret.get("report_summary", {})
            if isinstance(report_summary, dict) and isinstance(report_summary.get("execution_lineage"), dict):
                lineage = report_summary.get("execution_lineage", {})

            return {
                "ok": True,
                "message": message,
                "provider": provider,
                "execution_mode": execution_mode,
                "run_status": run_status,
                "runner_status": run_ret.get("runner_status"),
                "audit_file": run_ret.get("audit_file", ""),
                "scope_status": scope_status,
                "failed_command_indexes": run_ret.get("failed_command_indexes", []),
                "command_results": run_ret.get("command_results", []),
                "log_path": run_ret.get("log_path", ""),
                "summary_path": run_ret.get("summary_path", ""),
                "summary": "",
                "attempted_resume": bool(lineage.get("attempted_resume", False)),
                "used_resume": bool(lineage.get("used_resume", False)),
                "fallback_to_new_session": bool(lineage.get("fallback_to_new_session", False)),
                "resume_failed_reason": lineage.get("resume_failed_reason"),
                "command_shape": lineage.get("command_shape"),
                "version": run_ret.get("version") or state.current_version,
            }

        operation_name = "fix_current_version" if mode == "fix" else "run_current_version"
        if not wrap:
            return _do()
        return self._run_operation(operation_name, _do)

    def _api_run_current_version(self) -> dict[str, Any]:
        return self._api_execute_current_version("run")

    def _api_run_current_version_preview(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            preview_id = f"run-{uuid.uuid4().hex}"
            self.pending_run_preview = {
                "preview_id": preview_id,
                "created_at": self._now_iso(),
                "created_ts": datetime.now(timezone.utc).timestamp(),
            }
            return {
                "ok": True,
                "message": "运行预览已生成。",
                "preview_id": preview_id,
            }

        payload = self._run_operation("run_current_version_preview", _do)
        if isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, dict):
                preview_id = result.get("preview_id")
                if preview_id:
                    payload["preview_id"] = preview_id
        return payload

    def _api_run_current_version_confirm_with_project(self) -> dict[str, Any]:
        return self._api_run_current_version()

    def _api_fix_current_version(self) -> dict[str, Any]:
        return self._api_execute_current_version("fix")

    def _api_reload_plan(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            service = PlanReloadService(self.project_root)
            result = service.reload_plan()
            if result.get("ok"):
                self.start_plan_mtime = self._safe_mtime(self.plan_file)
                self.start_marker_mtime = self._safe_mtime(self.marker_file)
            return result

        return self._run_operation("reload_plan", _do)

    def _api_continue_next_version(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            service = ContinueNextVersionService(self.project_root)
            result = service.continue_next_version()
            if result.get("ok"):
                self.start_plan_mtime = self._safe_mtime(self.plan_file)
                self.start_marker_mtime = self._safe_mtime(self.marker_file)
            return result

        return self._run_operation("continue_next_version", _do)

    def _api_rerun_acceptance(self, wrap: bool = True) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            service = AcceptanceRerunService(self.project_root)
            result = service.rerun_acceptance()
            return result

        if not wrap:
            return _do()
        return self._run_operation("rerun_acceptance", _do)

    def _api_checkpoint_review(self, wrap: bool = True) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            service = CheckpointReviewService(self.project_root)
            result = service.run_review()
            return result

        if not wrap:
            return _do()
        return self._run_operation("checkpoint_review", _do)

    def _api_commit_preview(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            _, plan, state, _, machine = self._load_runtime_context()

            if state.status != "VERSION_PASSED":
                self.pending_commit_preview = None
                return {
                    "ok": False,
                    "status": "error",
                    "error_code": "VERSION_NOT_PASSED",
                    "message": "当前版本未通过，不能提交。",
                }

            manager = MCPGitCommitManager(self.project_root)

            readiness = manager.readiness()
            if not readiness.get("ok"):
                self.pending_commit_preview = None
                return {
                    "ok": False,
                    "status": "error",
                    "error_code": str(readiness.get("error_code", "READINESS_FAILED")).upper(),
                    "message": readiness.get("message", "提交就绪检查失败。"),
                }

            commit_blockers: list[str] = readiness.get("commit_blockers", [])
            blocked_files: list[str] = readiness.get("blocked_files", [])
            excluded_files: list[str] = readiness.get("excluded_files", [])
            files_to_commit: list[str] = readiness.get("files_to_commit", [])

            if commit_blockers:
                self.pending_commit_preview = None
                result: dict[str, Any] = {
                    "ok": False,
                    "status": "blocked",
                    "error_code": "COMMIT_PREVIEW_BLOCKED",
                    "message": "提交被阻断。",
                    "commit_blockers": commit_blockers,
                    "commit_warnings": readiness.get("commit_warnings", []),
                    "blocked_files": sorted(blocked_files),
                    "excluded_files": sorted(excluded_files),
                    "files_to_commit": sorted(files_to_commit),
                }
                if blocked_files:
                    result["reason_if_blocked"] = "blocked_files_present"
                return result

            current_plan_version = (
                plan.versions[state.current_version_index]
                if state.current_version_index is not None
                and 0 <= state.current_version_index < len(plan.versions)
                else None
            )
            version_label = current_plan_version.version if current_plan_version else (state.current_version or "未知")
            version_name = current_plan_version.name if current_plan_version else ""
            commit_title = f"{version_label} {version_name}".strip()
            audit_rel = self._to_rel(machine.get_current_audit_file())
            commit_body = f"Runner accepted version: {version_label}\nAcceptance: passed\nAudit: {audit_rel}"
            commit_message = f"{commit_title}\n\n{commit_body}"

            preview_result = manager.preview(message=commit_message)
            if not preview_result.get("ok"):
                self.pending_commit_preview = None
                return {
                    "ok": False,
                    "status": "error",
                    "error_code": str(preview_result.get("error_code", "PREVIEW_FAILED")).upper(),
                    "message": preview_result.get("message", "提交预览生成失败。"),
                }

            preview_id = preview_result.get("preview_id")
            diff_hash = preview_result.get("diff_hash")
            final_files_to_commit = preview_result.get("files_to_commit", files_to_commit)
            final_excluded_files = preview_result.get("excluded_files", excluded_files)

            self.pending_commit_preview = {
                "preview_id": preview_id,
                "message": commit_title,
                "commit_files": sorted(final_files_to_commit),
                "excluded_files": sorted(final_excluded_files),
                "diff_hash": diff_hash,
                "version": state.current_version,
                "project_root": os.path.abspath(self.project_root),
            }

            return {
                "ok": True,
                "message": "提交预览已生成。",
                "status": "ready",
                "scope_status": "NOT_CHECKED",
                "commit_message": {
                    "title": commit_title,
                    "body": commit_body,
                },
                "commit_title": commit_title,
                "commit_body": commit_body,
                "version": state.current_version,
                "commit_files": sorted(final_files_to_commit),
                "excluded_files": sorted(final_excluded_files),
                "preview_id": preview_id,
                "diff_hash": diff_hash,
                "reason_if_blocked": "",
            }

        return self._run_operation("commit_preview", _do)

    def _api_commit_preview_with_project(self) -> dict[str, Any]:
        return self._api_commit_preview()

    def _api_commit_confirm(self, wrap: bool = True) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            _, plan, state, _, _ = self._load_runtime_context()

            if state.status != "VERSION_PASSED":
                self.pending_commit_preview = None
                return {
                    "ok": False,
                    "status": "failed",
                    "error_code": "VERSION_NOT_PASSED",
                    "message": "当前版本未通过，不能提交。",
                }

            preview = self.pending_commit_preview
            if not preview or not preview.get("preview_id"):
                self.pending_commit_preview = None
                return {
                    "ok": False,
                    "status": "failed",
                    "error_code": "COMMIT_PREVIEW_REQUIRED",
                    "message": "请先查看改动说明",
                }

            preview_id = str(preview.get("preview_id", "")).strip()
            manager = MCPGitCommitManager(self.project_root)
            commit_result = manager.commit(preview_id=preview_id)

            if not commit_result.get("ok"):
                self.pending_commit_preview = None
                error_code = str(commit_result.get("error_code", "COMMIT_FAILED"))
                return {
                    "ok": False,
                    "status": "failed",
                    "error_code": error_code,
                    "message": commit_result.get("message", "代码提交失败"),
                    "details": commit_result,
                }

            commit_hash = commit_result.get("commit_hash", "")
            commit_message = commit_result.get("message", "")
            committed_files = commit_result.get("committed_files", [])
            verify_clean = bool(commit_result.get("verify_clean", False))
            verify_summary = commit_result.get("verify_summary") if isinstance(commit_result.get("verify_summary"), dict) else {}
            remaining_uncommitted_files = commit_result.get("remaining_uncommitted_files", [])
            state_update = commit_result.get("commit_state_update") or {
                "ok": True,
                "skipped": True,
                "reason": "commit_state_update_unavailable",
            }

            self.pending_commit_preview = None

            return {
                "ok": True,
                "status": "committed",
                "message": str(verify_summary.get("one_line") or "代码提交完成"),
                "preview_id": commit_result.get("preview_id", preview_id),
                "commit_hash": commit_hash,
                "commit_hash_short": commit_result.get("commit_hash_short", str(commit_hash)[:8]),
                "commit_message": commit_message,
                "commit_files": committed_files,
                "committed_files": committed_files,
                "verify_clean": verify_clean,
                "clean_status": "clean" if verify_clean else "dirty",
                "verify_summary": verify_summary,
                "remaining_uncommitted_files": remaining_uncommitted_files,
                "commit_state_update": state_update,
            }

        if not wrap:
            return _do()
        return self._run_operation("commit_confirm", _do)

    def _api_commit_confirm_with_project(self) -> dict[str, Any]:
        return self._api_commit_confirm()

    def _is_plan_reload_needed(self) -> bool:
        current_plan_mtime = self._safe_mtime(self.plan_file)
        current_marker_mtime = self._safe_mtime(self.marker_file)
        if current_plan_mtime and self.start_plan_mtime and current_plan_mtime > self.start_plan_mtime:
            return True
        if current_marker_mtime and self.start_marker_mtime and current_marker_mtime > self.start_marker_mtime:
            return True
        if current_marker_mtime and not self.start_marker_mtime:
            return True
        return False

    def _safe_mtime(self, path: str) -> float | None:
        try:
            return os.path.getmtime(path)
        except Exception:
            return None

    def _settings_resolve_signature(self, provider: str) -> tuple[Any, ...]:
        candidate_paths = [
            self.plan_file,
            os.path.join(self.runner_dir, "runner-settings.json"),
            self.runner_settings_store.user_settings_path(),
        ]
        return tuple(
            [self.project_root, provider.strip().lower()]
            + [(path, self._safe_mtime(path)) for path in candidate_paths]
        )

    def _resolve_prompt_file(self, prompt_file: Any, version: str) -> tuple[str | None, str | None]:
        prompts_dir = os.path.join(self.runner_dir, "prompts")
        prompt_file_abs = None
        if prompt_file and isinstance(prompt_file, str) and prompt_file.strip():
            candidate = prompt_file if os.path.isabs(prompt_file) else os.path.join(self.project_root, prompt_file)
            if os.path.isfile(candidate):
                prompt_file_abs = candidate
        if not prompt_file_abs:
            fallback = os.path.join(prompts_dir, f"{version}.md")
            if os.path.isfile(fallback):
                prompt_file_abs = fallback
        if not prompt_file_abs:
            return None, "PROMPT_NOT_FOUND"
        real_prompts = os.path.realpath(prompts_dir)
        real_file = os.path.realpath(prompt_file_abs)
        if not real_file.startswith(real_prompts + os.sep):
            return None, "PROMPT_FILE_UNSAFE"
        return prompt_file_abs, None

    def _read_prompt_excerpt(self, prompt_path: str, max_lines: int = 10, max_chars: int = 300) -> str | None:
        try:
            text = Path(prompt_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        lines = text.splitlines()[:max_lines]
        excerpt = "\n".join(lines)
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars] + "…"
        return excerpt

    def _read_json_file(self, path: str) -> dict[str, Any] | None:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _build_plan_version_list_for_v2(self) -> list[dict[str, Any]]:
        plan = self._read_json_file(self.plan_file) or {}
        state = self._read_json_file(self.state_file) or {}
        plan_versions = plan.get("versions", [])
        state_versions_map: dict[str, dict[str, Any]] = {}
        for item in state.get("versions", []):
            vid = item.get("version")
            if isinstance(vid, str):
                state_versions_map[vid] = item
        current_version = state.get("current_version")
        rows: list[dict[str, Any]] = []
        for pv in plan_versions:
            vid = str(pv.get("version", ""))
            sv = state_versions_map.get(vid, {})
            rows.append({
                "version": vid,
                "name": pv.get("name"),
                "description": pv.get("description"),
                "enabled": bool(pv.get("enabled", True)),
                "is_current": vid == current_version,
                "runtime_status": sv.get("status"),
            })
        return rows

    def _build_version_rows(self, plan_versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        state = self._read_json_file(self.state_file) or {}
        review_state = self._read_json_file(os.path.join(self.runner_dir, "review-state.json")) or {}
        review_policy = self._read_json_file(self.plan_file) or {}
        checkpoint_versions = set((review_policy.get("review_policy") or {}).get("after_versions") or [])
        state_map: dict[str, dict[str, Any]] = {}
        for item in state.get("versions", []):
            version_id = item.get("version")
            if isinstance(version_id, str):
                state_map[version_id] = item

        current_version = state.get("current_version")
        reviewed_version = review_state.get("last_reviewed_version")
        has_review_file = bool(review_state.get("last_review_file"))
        rows: list[dict[str, Any]] = []
        for plan_item in plan_versions:
            version_id = str(plan_item.get("version", ""))
            runtime = state_map.get(version_id, {})
            is_checkpoint = version_id in checkpoint_versions
            reviewed = bool(is_checkpoint and reviewed_version == version_id and has_review_file)
            prompt_file_abs, resolve_error = self._resolve_prompt_file(plan_item.get("prompt_file"), version_id)
            prompt_excerpt = None
            prompt_missing = True
            display_path = None
            if prompt_file_abs is not None:
                prompt_excerpt = self._read_prompt_excerpt(prompt_file_abs)
                prompt_missing = False
                display_path = self._to_project_relative(prompt_file_abs)
            rows.append(
                {
                    "version": version_id,
                    "name": plan_item.get("name"),
                    "enabled": bool(plan_item.get("enabled", True)),
                    "is_current": version_id == current_version,
                    "runtime_status": runtime.get("status"),
                    "attempt": runtime.get("attempt"),
                    "commit_hash": runtime.get("commit_hash"),
                    "is_checkpoint": is_checkpoint,
                    "reviewed": reviewed,
                    "prompt_file": display_path,
                    "prompt_excerpt": prompt_excerpt,
                    "prompt_missing": prompt_missing,
                }
            )
        return rows

    def _to_project_relative(self, path: str) -> str:
        abs_path = os.path.abspath(path)
        root = os.path.abspath(self.project_root)
        if abs_path == root:
            return "."
        if abs_path.startswith(root + os.sep):
            return abs_path[len(root) + 1 :]
        return abs_path

    def _api_project_registry(self) -> dict[str, Any]:
        registry = self.project_registry.list_projects()
        for p in registry.get("projects", []):
            root = p.get("project_root", "")
            p["available"] = bool(root) and os.path.isdir(root) and self.project_registry.is_runner_managed_project(root)
            p["is_temp"] = bool(root) and self.project_registry.is_temp_path(root)
        return registry

    def _api_switch_project(self, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = body or {}
        with self.operation_lock:
            if self.operation_running or self.job.get("status") == "running":
                return {
                    "ok": False,
                    "error_code": "OPERATION_RUNNING",
                    "message": "当前有操作正在运行，不能切换项目。",
                    "active_project_root": self.project_root,
                }

        selected = self.project_registry.select_project(
            project_id=payload.get("project_id") if isinstance(payload.get("project_id"), str) else None,
            project_root=payload.get("project_root") if isinstance(payload.get("project_root"), str) else None,
        )
        if not selected.get("ok"):
            selected["active_project_root"] = self.project_root
            return selected

        project = selected.get("project") if isinstance(selected.get("project"), dict) else {}
        new_root = project.get("project_root")
        if not isinstance(new_root, str) or not new_root.strip():
            return {
                "ok": False,
                "error_code": "PROJECT_SWITCH_FAILED",
                "message": "登记项目缺少 project_root。",
                "active_project_root": self.project_root,
            }

        previous_root = self.project_root
        try:
            self._set_project_root(new_root)
            self.pending_commit_preview = None
            self.pending_run_preview = None
            self.job = {"status": "idle"}
            return {
                "ok": True,
                "message": "项目已切换。",
                "previous_project_root": previous_root,
                "project": project,
                "registry_path": self.project_registry.registry_path(),
                "status": self._api_v2_status(),
            }
        except Exception as exc:
            self._set_project_root(previous_root)
            return {
                "ok": False,
                "error_code": "PROJECT_SWITCH_FAILED",
                "message": str(exc),
                "active_project_root": self.project_root,
            }

    def _api_project_identity_preview(self, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = body or {}
        with self.operation_lock:
            if self.operation_running or self.job.get("status") == "running":
                return {
                    "ok": False,
                    "action": "project_identity_preview",
                    "blockers": ["当前有操作正在运行，不能编辑项目身份。"],
                }
        return self.project_registry.preview_project_identity_migration(
            project_id=payload.get("project_id") if isinstance(payload.get("project_id"), str) else None,
            current_project_root=self.project_root,
            new_project_name=str(payload.get("new_project_name") or ""),
            new_display_name=(
                payload.get("new_display_name")
                if isinstance(payload.get("new_display_name"), str)
                else None
            ),
            new_project_root=str(payload.get("new_project_root") or ""),
        )

    def _api_project_identity_apply(self, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = body or {}
        preview_id = payload.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            return {
                "ok": False,
                "action": "project_identity_apply",
                "error_code": "PREVIEW_ID_REQUIRED",
                "message": "apply 需要有效的 preview_id。",
            }
        with self.operation_lock:
            if self.operation_running or self.job.get("status") == "running":
                return {
                    "ok": False,
                    "action": "project_identity_apply",
                    "error_code": "OPERATION_RUNNING",
                    "message": "当前有操作正在运行，不能编辑项目身份。",
                }
        result = self.project_registry.apply_project_identity_migration(preview_id)
        if result.get("ok"):
            project = result.get("project")
            new_root = project.get("project_root") if isinstance(project, dict) else None
            if isinstance(new_root, str) and new_root.strip():
                self._set_project_root(new_root)
            result["project_registry"] = self._api_project_registry()
        return result

    # ---- Web v2 methods ----

    def _json_safe(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._json_safe(v) for v in obj]
        if hasattr(obj, "__dataclass_fields__"):
            return {k: self._json_safe(getattr(obj, k)) for k in obj.__dataclass_fields__}
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        return str(obj)

    def _api_v2_live_run(self) -> dict[str, Any]:
        from runner.executor_read import handle_inspect_executor_activity
        session_status: dict[str, Any] | None = None
        try:
            session_status = self.executor_session_store.get_status()
        except Exception:
            session_status = None
        try:
            inspect_result = handle_inspect_executor_activity(
                self.project_root, "latest_run_status", {}
            )
            live_data = inspect_result.get("live")
            if isinstance(live_data, dict) and inspect_result.get("ok") and live_data.get("available"):
                self._enrich_live_run_progress_status(live_data)
                return self._with_executor_identity_display(
                    live_data,
                    session_status=session_status,
                )
            stale = inspect_result.get("stale_orphan_claim")
            stale_msg = inspect_result.get("message", "")
            if isinstance(stale, dict):
                return {
                    "available": False,
                    "stale_orphan_claim": stale,
                    "stale_orphan_message": stale_msg,
                }
            if not isinstance(live_data, dict):
                return {"available": False, "warning": "live field is not a dict"}
        except Exception:
            pass
        return {"available": False}

    def _enrich_live_run_progress_status(self, live_data: dict[str, Any]) -> None:
        try:
            from runner.executor_status import analyze_meaningful_progress
            events = live_data.get("events")
            progress = analyze_meaningful_progress(events if isinstance(events, list) else [])
            live_data["last_meaningful_progress"] = progress
            diagnostics = live_data.get("diagnostics")
            if not isinstance(diagnostics, list):
                diagnostics = []
            heartbeat = live_data.get("heartbeat")
            heartbeat_stale = bool(heartbeat.get("stale")) if isinstance(heartbeat, dict) else False
            claim_status = str(live_data.get("claim_status") or "").upper()
            if claim_status == "RUNNING" and not heartbeat_stale and progress.get("available") and progress.get("stale"):
                if "HEARTBEAT_ONLY_WITH_STALE_PROGRESS" not in diagnostics:
                    diagnostics.append("HEARTBEAT_ONLY_WITH_STALE_PROGRESS")
                live_data["provider_status"] = "stalled_without_provider_error"
                live_data["terminal_reason"] = "executor_stalled_without_provider_error"
                live_data["progress_stalled"] = True
            live_data["diagnostics"] = diagnostics
        except Exception:
            pass

    def _executor_model_for_display(
        self,
        *,
        provider: str,
        live_run: dict[str, Any],
        session_record: dict[str, Any],
    ) -> str:
        claim = live_run.get("claim") if isinstance(live_run.get("claim"), dict) else {}
        candidates = [
            live_run.get("executor_model"),
            live_run.get("model"),
            live_run.get("model_name"),
            claim.get("model"),
            claim.get("model_name"),
            session_record.get("model"),
            session_record.get("model_name"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        cache_key = self._settings_resolve_signature(provider)
        resolved: Any = self._settings_resolve_cache.get(cache_key)
        if resolved is None:
            resolved = self.runner_settings_store.resolve_for_project(self.project_root, self.plan_file)
            self._settings_resolve_cache.clear()
            self._settings_resolve_cache[cache_key] = resolved
        profile = resolved.settings.executor_profile
        if profile and profile.model:
            profile_provider = (profile.provider or "").strip().lower()
            if not profile_provider or profile_provider == provider.strip().lower():
                return profile.model.strip()
        return ""

    def _with_executor_identity_display(
        self,
        live_run: dict[str, Any] | None,
        *,
        session_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        live = dict(live_run) if isinstance(live_run, dict) else {"available": False}
        if not live.get("available"):
            return live
        status = session_status if isinstance(session_status, dict) else {}
        record = status.get("record") if isinstance(status.get("record"), dict) else {}
        claim = live.get("claim") if isinstance(live.get("claim"), dict) else {}
        provider = (
            claim.get("provider")
            or live.get("provider")
            or record.get("provider")
            or ""
        )
        provider_text = str(provider or "")
        model_text = self._executor_model_for_display(
            provider=provider_text,
            live_run=live,
            session_record=record,
        )
        live["executor_model"] = model_text
        live["executor_display"] = f"{provider_text} + {model_text}" if provider_text and model_text else provider_text
        identity = select_executor_identity_for_display(
            run_identity=live,
            session_record=record,
            provider=provider_text,
            fallback_value=live.get("session_id_full"),
        )
        live["session_identity_present"] = bool(identity.get("identity_present") is True)
        live["session_identity_value"] = str(identity.get("identity_value") or "")
        live["session_identity_kind"] = str(identity.get("identity_kind") or "")
        live["session_identity_label"] = str(identity.get("identity_label") or "会话标识")
        live["session_identity_source"] = str(identity.get("identity_source") or "")
        if not isinstance(live.get("session_id_full"), str) or not str(live.get("session_id_full") or "").strip():
            live["session_id_full"] = live["session_identity_value"]
        return live

    def _api_v2_status(self) -> dict[str, Any]:
        orchestrator = WorkflowOrchestrator(self.project_root)
        core_output = orchestrator.handle("project_status", {"include_reports": True})
        result = self._json_safe(core_output)
        fs_pi = (result.get("fact_snapshot") or {}).get("project_identity")
        if isinstance(fs_pi, dict) and fs_pi.get("project_name"):
            result["project_identity"] = dict(fs_pi)
        else:
            result["project_identity"] = build_project_identity(self.project_root)
        registry_data = self._api_project_registry()
        registry_data["projects"] = [
            p for p in registry_data.get("projects", [])
            if p.get("project_mode") == "managed"
        ]
        registry_data["project_count"] = len(registry_data["projects"])
        result["project_registry"] = registry_data
        try:
            todo_result = MCPTodoListManager(self.project_root).read()
        except Exception as exc:
            todo_result = {
                "ok": False,
                "error_code": "TODO_READ_FAILED",
                "message": str(exc),
                "items": [],
                "item_count": 0,
                "total_item_count": 0,
                "planned_count": 0,
                "done_count": 0,
                "path": f"{self.runner_rel_dir}/todolist.json",
            }
        result["todolist"] = self._json_safe(todo_result)
        try:
            decision_result = MCPDecisionRecordsManager(self.project_root).read()
        except Exception as exc:
            decision_result = {
                "ok": False,
                "error_code": "DECISION_READ_FAILED",
                "message": str(exc),
                "decisions": [],
                "decision_count": 0,
                "path": f"{self.runner_rel_dir}/decisions.json",
            }
        result["decisions"] = self._json_safe(decision_result)
        try:
            memory_result = MCPProjectMemoryManager(self.project_root).read()
        except Exception as exc:
            memory_result = {
                "ok": False,
                "error_code": "MEMORY_READ_FAILED",
                "message": str(exc),
                "content": "",
                "content_chars": 0,
                "path": f"{self.runner_rel_dir}/memory.md",
            }
        result["memory"] = self._json_safe(memory_result)
        live_run = self._api_v2_live_run()
        result["live_run"] = live_run
        if not (isinstance(live_run, dict) and live_run.get("available") is True):
            self._enrich_latest_report_identity(result)
        try:
            result["plan_versions"] = self._build_plan_version_list_for_v2()
        except Exception:
            result["plan_versions"] = []
        result["operation_running"] = self.operation_running
        result["operation_name"] = self.operation_name
        result["operation_started_at"] = self.operation_started_at
        result["last_operation_result"] = self.last_operation_result
        return result

    def _enrich_latest_report_identity(self, result: dict[str, Any]) -> None:
        try:
            snapshot = result.get("fact_snapshot")
            if not isinstance(snapshot, dict):
                return
            lr_box = snapshot.get("latest_report")
            if not isinstance(lr_box, dict) or not lr_box.get("available"):
                return
            latest = lr_box.get("latest")
            if not isinstance(latest, dict):
                return
            report_id = str(latest.get("report_id") or "").strip()
            if not report_id:
                return
            from runner.executor_read import handle_inspect_executor_activity
            detail = handle_inspect_executor_activity(
                self.project_root,
                "get_report",
                {"report_id": report_id, "include_markdown": False},
            )
            if not detail.get("ok"):
                return
            report = detail.get("report")
            if not isinstance(report, dict):
                return
            lineage = report.get("execution_lineage")
            run_id = ""
            if isinstance(lineage, dict):
                run_id = str(lineage.get("run_id") or "")
                if not latest.get("run_id"):
                    latest["run_id"] = run_id
                if not latest.get("preview_id"):
                    latest["preview_id"] = str(lineage.get("preview_id") or "")
                if not latest.get("executor_model"):
                    latest["executor_model"] = str(lineage.get("model") or "")
                if not latest.get("session_id_full"):
                    latest["session_id_full"] = str(lineage.get("session_id_full") or "")
                if not latest.get("session_id_full"):
                    identity = self._latest_report_session_identity_from_current_session(
                        latest=latest,
                        lineage=lineage,
                    )
                    if identity.get("identity_present") is True:
                        latest["session_id_full"] = str(identity.get("identity_value") or "")
                        latest["session_identity_value"] = str(identity.get("identity_value") or "")
                        latest["session_identity_kind"] = str(identity.get("identity_kind") or "")
                        latest["session_identity_label"] = str(identity.get("identity_label") or "会话标识")
                        latest["session_identity_source"] = str(identity.get("identity_source") or "")
                if not latest.get("session_mode"):
                    used_resume = lineage.get("used_resume") is True
                    latest["session_mode"] = "resume" if used_resume else "new"
                    latest["session_mode_label"] = "续接" if used_resume else "新开"
            changed = report.get("changed_files")
            if isinstance(changed, list) and not latest.get("changed_files"):
                latest["changed_files"] = [str(f) for f in changed if isinstance(f, str)]
            events = report.get("events")
            if isinstance(events, list) and not isinstance(latest.get("events"), list):
                latest["events"] = events
        except Exception:
            pass

    def _latest_report_session_identity_from_current_session(
        self,
        *,
        latest: dict[str, Any],
        lineage: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            status = self.executor_session_store.get_status()
        except Exception:
            status = {}
        record = status.get("record") if isinstance(status, dict) and isinstance(status.get("record"), dict) else {}
        if not record:
            return {"identity_present": False}

        latest_provider = str(latest.get("provider") or "").strip()
        record_provider = str(record.get("provider") or "").strip()
        if latest_provider and record_provider and latest_provider != record_provider:
            return {"identity_present": False}

        latest_version = str(latest.get("version") or "").strip()
        record_version = str(record.get("version") or "").strip()
        if latest_version and record_version and latest_version != record_version:
            return {"identity_present": False}

        return select_executor_identity_for_display(
            run_identity={"report": {"execution_lineage": lineage}},
            session_record=record,
            provider=latest_provider or record_provider,
            fallback_value=str(latest.get("session_id_full") or ""),
        )

    def _build_registry_action_outcome(
        self,
        result: dict[str, Any],
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if result.get("ok"):
            return {
                "ok": True,
                "source": "web_v2",
                "action": result.get("action", "project_registry"),
                "status": "succeeded",
                "risk_level": "info",
                "action_outcome": {
                    "code": "SUCCESS",
                    "message": result.get("message", ""),
                },
                "removed_count": result.get("removed_count", 0),
                "project_count": result.get("project_count", 0),
                "project_registry": self._api_project_registry(),
            }
        return {
            "ok": False,
            "source": "web_v2",
            "action": "project_registry",
            "status": "failed",
            "risk_level": "error",
            "action_outcome": {
                "code": "FAILED",
                "message": result.get("message", ""),
                "error_code": error_code or result.get("error_code", "REGISTRY_ACTION_FAILED"),
            },
            "project_registry": self._api_project_registry(),
        }

    def _handle_registry_action(self, action_name: str, next_action: dict[str, Any]) -> dict[str, Any] | None:
        if action_name == "project_registry_unregister":
            with self.operation_lock:
                if self.operation_running or self.job.get("status") == "running":
                    return self._build_registry_action_outcome(
                        {"ok": False, "message": "当前有操作正在运行，不能操作登记列表。", "error_code": "OPERATION_RUNNING"},
                    )
            params = next_action.get("params") or {}
            project_id = params.get("project_id") if isinstance(params.get("project_id"), str) else None
            project_root = params.get("project_root") if isinstance(params.get("project_root"), str) else None
            if project_root and os.path.realpath(project_root) == self.project_root:
                return self._build_registry_action_outcome(
                    {
                        "ok": False,
                        "message": "不能移除当前正在使用的项目。请先切换到其他项目后再操作。",
                        "error_code": "CANNOT_REMOVE_BOUND_PROJECT",
                    },
                )
            result = self.project_registry.unregister_project(
                project_id=project_id,
                project_root=project_root,
            )
            return self._build_registry_action_outcome(result)

        if action_name == "project_registry_prune_unavailable":
            result = self.project_registry.prune_unavailable_projects(preserve_project_root=self.project_root)
            return self._build_registry_action_outcome(result)

        if action_name == "project_registry_prune_temporary":
            result = self.project_registry.prune_temporary_projects(preserve_project_root=self.project_root)
            return self._build_registry_action_outcome(result)

        return None

    def _api_v2_action(self, body: dict[str, Any]) -> dict[str, Any]:
        next_action = body.get("next_action")
        if not isinstance(next_action, dict):
            return {
                "ok": False,
                "source": "web_v2",
                "action": "action",
                "status": "failed",
                "risk_level": "error",
                "action_outcome": {
                    "code": "FAILED",
                    "message": "请求缺少 next_action。",
                    "error_code": "MISSING_NEXT_ACTION",
                },
                "blockers": ["请求参数中未提供 next_action。"],
                "display_summary": {
                    "title": "请求错误",
                    "status_text": "failed",
                    "primary_message": "请求缺少 next_action，无法构造 CoreRequest。",
                    "next_step_text": "请刷新后重试。",
                    "detail_refs": [],
                },
            }

        action_name = (next_action.get("action") or "").lower()
        registry_result = self._handle_registry_action(action_name, next_action)
        if registry_result is not None:
            return registry_result

        core_request = CoreRequest.from_web_action(
            next_action,
            client_context=body.get("client_context"),
            raw_payload=body,
        )

        if core_request.write_intent:
            target_action = (core_request.target_scope or {}).get("action", "unknown") if core_request.write_intent else "unknown"
            return self._json_safe(CoreOutput(
                ok=False,
                source="web_v2",
                action="action",
                status="blocked",
                risk_level="blocked",
                action_outcome={
                    "code": "FAILED",
                    "message": f"Web v2 第一版暂不支持写入型动作：{target_action}。",
                    "error_code": "WRITE_INTENT_BLOCKED",
                },
                blockers=[f"写入型动作 {target_action} 已被 Web v2 第一版安全拦截。请通过 MCP 或旧 Web 执行。"],
                display_summary={
                    "title": "写入已拦截",
                    "status_text": "blocked",
                    "primary_message": f"动作 {target_action} 是写入型操作，Web v2 第一版不做写入。",
                    "next_step_text": "请使用 MCP 客户端或旧 Web Console 执行此操作。",
                    "detail_refs": [],
                },
                audit={
                    "source": "web_v2",
                    "workflow": "action",
                    "phase": None,
                },
            ))

        orchestrator = WorkflowOrchestrator(self.project_root)
        core_output = orchestrator.handle_request(core_request)
        return self._json_safe(core_output)

    @staticmethod
    def _api_v2_health() -> dict[str, Any]:
        return {"ok": True, "version": "v2"}

    def _render_v2_index_html(self) -> str:
        return render_v2_index_page()
