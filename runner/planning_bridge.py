import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from runner._internal_utils import run_git as _run_git, write_json_atomic
from runner.executor_registry import is_supported_execution_provider
from runner.git_history_reconcile import GitHistoryReconcileScanner
from runner.path_glob import match_any as glob_match_any, normalize as glob_normalize
from runner.runner_paths import (
    project_runner_dirnames,
    resolve_project_runner_dir,
)


class PlanningBridgeError(RuntimeError):
    pass


@dataclass
class BridgePaths:
    project_root: str
    runner_dir: str
    plan_file: str
    state_file: str
    logs_dir: str
    prompts_dir: str
    review_state_file: str
    patch_dir: str


class PlanningBridge:
    def _paths(self, project_path: str) -> BridgePaths:
        project_root = os.path.abspath(os.path.expanduser(project_path))
        runner_dir = resolve_project_runner_dir(project_root)
        return BridgePaths(
            project_root=project_root,
            runner_dir=runner_dir,
            plan_file=os.path.join(runner_dir, "plan.json"),
            state_file=os.path.join(runner_dir, "state.json"),
            logs_dir=os.path.join(runner_dir, "logs"),
            prompts_dir=os.path.join(runner_dir, "prompts"),
            review_state_file=os.path.join(runner_dir, "review-state.json"),
            patch_dir=os.path.join(runner_dir, "plan-patches"),
        )

    def get_runner_status(self, project_path: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        direct_summary = self._get_unreconciled_direct_versions_summary(paths.project_root)
        has_plan = os.path.isfile(paths.plan_file)
        has_state = os.path.isfile(paths.state_file)
        if not has_plan and not has_state:
            direct_count = int(direct_summary.get("unreconciled_direct_version_count", 0))
            return {
                "ok": True,
                "project": paths.project_root,
                "mode": "source_only",
                "has_plan": False,
                "has_state": False,
                "has_runner_state": False,
                "runner_status": None,
                "current_version": None,
                "current_version_status": None,
                "next_version": None,
                "next_version_status": None,
                "attempt": None,
                "last_error": None,
                "last_log_file": None,
                "updated_at": None,
                "has_pending_versions": False,
                "pending_versions": [],
                "next_not_started_version": None,
                "pending_count": 0,
                "unreconciled_direct_version_count": int(direct_summary.get("unreconciled_direct_version_count", 0)),
                "unreconciled_direct_versions": list(direct_summary.get("unreconciled_direct_versions", [])),
                "unreconciled_direct_scan_limit": int(direct_summary.get("scan_limit", 20)),
                "recommended_next_action": (
                    {"tool": "run_mcp_workflow", "action": "manual_git_history_review_required", "params": {"workflow": "project_status", "phase": "inspect"}}
                    if direct_count > 0 else None
                ),
            }
        if not has_plan:
            raise PlanningBridgeError(f"文件不存在：{paths.plan_file}")
        if not has_state:
            raise PlanningBridgeError(f"文件不存在：{paths.state_file}")

        plan = self._load_json(paths.plan_file)
        state = self._load_json(paths.state_file)
        version_state = self._get_current_version_state(state)
        pending_info = self._build_pending_versions(plan, state)
        pending_versions = pending_info.get("pending_versions", [])
        next_version = pending_versions[0].get("version") if pending_versions else None
        next_version_status = pending_versions[0].get("status") if pending_versions else None
        direct_count = int(direct_summary.get("unreconciled_direct_version_count", 0))
        return {
            "ok": True,
            "project": paths.project_root,
            "mode": "runner_managed",
            "has_plan": True,
            "has_state": True,
            "has_runner_state": True,
            "runner_status": state.get("status"),
            "current_version": state.get("current_version"),
            "current_version_status": version_state.get("status") if version_state else None,
            "next_version": next_version,
            "next_version_status": next_version_status,
            "attempt": state.get("attempt"),
            "last_error": state.get("last_error"),
            "last_log_file": state.get("last_log_file"),
            "updated_at": state.get("updated_at"),
            "has_pending_versions": bool(pending_info.get("has_pending_versions")),
            "pending_versions": pending_versions,
            "next_not_started_version": pending_info.get("next_not_started_version"),
            "pending_count": int(pending_info.get("pending_count", 0)),
            "unreconciled_direct_version_count": int(direct_summary.get("unreconciled_direct_version_count", 0)),
            "unreconciled_direct_versions": list(direct_summary.get("unreconciled_direct_versions", [])),
            "unreconciled_direct_scan_limit": int(direct_summary.get("scan_limit", 20)),
            "recommended_next_action": (
                {"tool": "run_mcp_workflow", "action": "manual_git_history_review_required", "params": {"workflow": "project_status", "phase": "inspect"}}
                if direct_count > 0 else None
            ),
        }

    def get_unreconciled_direct_versions_preview(
        self, project_path: str, scan_limit: int | None = None
    ) -> dict[str, Any]:
        project_root = os.path.abspath(os.path.expanduser(project_path))
        return GitHistoryReconcileScanner(project_root).scan_unreconciled_candidates(scan_limit=scan_limit)

    def build_pending_versions(self, project_path: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        plan = self._load_json(paths.plan_file)
        state = self._load_json(paths.state_file)
        return self._build_pending_versions(plan, state)

    def get_plan_overview(self, project_path: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        plan = self._load_json(paths.plan_file)
        versions = plan.get("versions", [])
        return {
            "project": paths.project_root,
            "project_name": plan.get("project_name"),
            "plan_version": plan.get("plan_version"),
            "version_count": len(versions),
            "versions": [
                {
                    "version": v.get("version"),
                    "name": v.get("name"),
                    "enabled": v.get("enabled", True),
                    "prompt_file": v.get("prompt_file"),
                }
                for v in versions
            ],
            "review_policy": plan.get("review_policy", {}),
            "commit_policy": plan.get("commit_policy", {}),
        }

    def get_version_result(self, project_path: str, version: str | None = None) -> dict[str, Any]:
        paths = self._paths(project_path)
        plan = self._load_json(paths.plan_file)
        state = self._load_json(paths.state_file)
        requested_version = version or state.get("current_version")
        if not requested_version:
            raise PlanningBridgeError("当前没有可读取的版本。")
        plan_version = self._find_plan_version(plan, requested_version)
        if not plan_version:
            raise PlanningBridgeError(f"计划中不存在版本：{requested_version}")
        version_state = self._find_version_state(state, requested_version)
        review_state = self._load_json_if_exists(paths.review_state_file)

        audit_path = self._resolve_audit_for_version(paths, state, version_state, requested_version)
        audit_summary = self._parse_audit_summary(audit_path, requested_version)
        scope_summary = {
            "status": "UNKNOWN",
            "outside_allowed_files": audit_summary.get("outside_allowed_files", []),
            "forbidden_changed_files": audit_summary.get("forbidden_changed_files", []),
        }
        if scope_summary["outside_allowed_files"] or scope_summary["forbidden_changed_files"]:
            scope_summary["status"] = "FAILED"
        elif audit_path:
            scope_summary["status"] = "PASSED_OR_NOT_REPORTED"

        last_error = state.get("last_error")
        last_log_file = state.get("last_log_file")
        error_matches_version = self._path_matches_version(last_log_file, requested_version) or self._error_mentions_version(last_error, requested_version)
        executor_summary = {
            "status": "UNKNOWN",
            "last_error": last_error if error_matches_version else None,
            "last_log_file": last_log_file if error_matches_version else None,
        }
        if error_matches_version and last_error:
            executor_summary["status"] = "FAILED"
        elif version_state and version_state.get("status") == "PASSED":
            executor_summary["status"] = "PASSED_OR_IDLE"

        acceptance_summary = {
            "status": "UNKNOWN",
            "failed_command_indexes": audit_summary.get("failed_command_indexes", []),
            "failed_command_details": audit_summary.get("failed_command_details", []),
            "audit_path": audit_path,
        }
        if version_state and version_state.get("status") == "PASSED":
            acceptance_summary["status"] = "PASSED"
        elif version_state and version_state.get("status") in ("FAILED_BLOCKED", "BLOCKED"):
            acceptance_summary["status"] = "FAILED"

        commit_status = {
            "committed": bool(version_state and version_state.get("commit_hash")),
            "commit_hash": version_state.get("commit_hash") if version_state else None,
            "committed_at": version_state.get("committed_at") if version_state else None,
            "commit_message": version_state.get("commit_message") if version_state else None,
            "commit_files": version_state.get("commit_files") if version_state else None,
        }
        review_status = self._build_review_status(review_state, requested_version)
        changed_files = self._safe_changed_files(paths.project_root, plan_version.get("allowed_files", []))
        evidence_paths = {
            "plan_file": paths.plan_file,
            "state_file": paths.state_file,
            "audit_file": audit_path,
            "last_log_file": last_log_file if error_matches_version else None,
            "review_state_file": paths.review_state_file if review_state else None,
        }
        risks: list[str] = []
        if audit_summary.get("stale_warning"):
            risks.append(audit_summary["stale_warning"])
        if not audit_path and acceptance_summary["status"] != "PASSED":
            risks.append("当前版本未找到匹配 audit 文件。")

        return {
            "project": paths.project_root,
            "current_version": state.get("current_version"),
            "requested_version": requested_version,
            "version_name": plan_version.get("name"),
            "runner_status": state.get("status"),
            "version_status": version_state.get("status") if version_state else "UNKNOWN",
            "commit_status": commit_status,
            "review_status": review_status,
            "acceptance_summary": acceptance_summary,
            "scope_summary": scope_summary,
            "executor_summary": executor_summary,
            "audit_summary": audit_summary,
            "changed_files": changed_files,
            "risks": risks,
            "evidence_paths": evidence_paths,
        }

    def get_next_version_plan(self, project_path: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        plan = self._load_json(paths.plan_file)
        state = self._load_json(paths.state_file)
        versions = plan.get("versions", [])
        if not versions:
            raise PlanningBridgeError("计划中没有版本。")
        current_version = state.get("current_version")
        current_index = self._find_version_index(versions, current_version)
        next_version = None
        for idx in range(current_index + 1, len(versions)):
            candidate = versions[idx]
            if candidate.get("enabled", True):
                next_version = candidate
                break
        if next_version is None:
            return {
                "project": paths.project_root,
                "current_version": current_version,
                "status": "NO_NEXT_VERSION",
                "message": "当前版本后没有启用的下一版本。",
            }

        prompt_file = next_version.get("prompt_file")
        prompt_excerpt = self._read_prompt_excerpt(prompt_file)
        return {
            "project": paths.project_root,
            "current_version": current_version,
            "next_version": next_version.get("version"),
            "next_version_name": next_version.get("name"),
            "description": next_version.get("description"),
            "allowed_files": next_version.get("allowed_files", []),
            "forbidden_files": next_version.get("forbidden_files", []),
            "acceptance_commands": next_version.get("acceptance_commands", []),
            "prompt_file": prompt_file,
            "prompt_excerpt": prompt_excerpt,
            "review_policy": plan.get("review_policy", {}),
        }

    def get_project_doc_section(self, project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
        paths = self._paths(project_path)
        file_value = spec.get("file")
        heading_value = spec.get("heading")
        max_chars_value = spec.get("max_chars", 12000)

        if not isinstance(file_value, str) or not file_value.strip():
            return {
                "ok": False,
                "error_code": "INVALID_FILE",
                "message": "file 必须是非空字符串。",
            }
        if not isinstance(heading_value, str) or not heading_value.strip():
            return {
                "ok": False,
                "error_code": "INVALID_HEADING",
                "message": "heading 必须是非空字符串。",
            }
        if isinstance(max_chars_value, bool) or not isinstance(max_chars_value, int):
            return {
                "ok": False,
                "error_code": "INVALID_MAX_CHARS",
                "message": "max_chars 必须是整数。",
            }
        if max_chars_value <= 0:
            return {
                "ok": False,
                "error_code": "INVALID_MAX_CHARS",
                "message": "max_chars 必须大于 0。",
            }
        max_chars = min(max_chars_value, 30000)

        rel_path = self._normalize_doc_file_path(file_value)
        if rel_path is None:
            return {
                "ok": False,
                "error_code": "FILE_NOT_ALLOWED",
                "message": "file 必须是项目内允许的相对 Markdown 路径。",
            }
        if not self._is_allowed_doc_path(rel_path):
            return {
                "ok": False,
                "error_code": "FILE_NOT_ALLOWED",
                "message": "file 不在允许读取的白名单内。",
            }

        abs_path = self._safe_join(paths.project_root, rel_path)
        if not os.path.exists(abs_path):
            return {
                "ok": False,
                "error_code": "FILE_NOT_FOUND",
                "message": f"文件不存在：{rel_path}",
            }
        if not os.path.isfile(abs_path):
            return {
                "ok": False,
                "error_code": "FILE_NOT_ALLOWED",
                "message": f"目标不是文件：{rel_path}",
            }

        try:
            text = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {
                "ok": False,
                "error_code": "FILE_READ_ERROR",
                "message": f"读取文件失败：{e}",
            }

        section = self._extract_markdown_section(text, heading_value)
        if not section.get("ok"):
            return {
                "ok": False,
                "error_code": "SECTION_NOT_FOUND",
                "message": section.get("message", "未找到指定 heading。"),
                "available_headings": section.get("available_headings", []),
            }

        content = str(section.get("content", ""))
        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        return {
            "ok": True,
            "file": rel_path,
            "heading": heading_value.strip(),
            "matched_heading": section.get("matched_heading"),
            "start_line": section.get("start_line"),
            "end_line": section.get("end_line"),
            "truncated": truncated,
            "content": content,
        }

    def preview_insert_version(self, project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
        paths = self._paths(project_path)
        plan = self._load_json(paths.plan_file)
        spec = dict(spec)
        self._validate_insert_spec(plan, spec)
        first_insert = spec.get("insert_after") == "__first__"
        patch_id = f"patch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        os.makedirs(paths.patch_dir, exist_ok=True)
        patch_path = self._safe_join(paths.patch_dir, f"{patch_id}.json")
        patch_payload = {
            "patch_id": patch_id,
            "operation": "insert_version",
            "created_at": datetime.now().isoformat(),
            "project_root": paths.project_root,
            "project_path": paths.project_root,
            "base_plan_signature": self._file_signature(paths.plan_file),
            "spec": spec,
        }
        write_json_atomic(patch_path, patch_payload)
        return {
            "ok": True,
            "operation": "insert_version",
            "patch_id": patch_id,
            "patch_path": patch_path,
            "inserted_version": spec.get("version"),
            "insert_after": None if first_insert else spec.get("insert_after"),
            "first_insert": first_insert,
            "preview": {
                "insert_after": None if first_insert else spec.get("insert_after"),
                "first_insert": first_insert,
                "version": spec.get("version"),
                "name": spec.get("name"),
                "allowed_files": spec.get("allowed_files", []),
                "acceptance_commands": spec.get("acceptance_commands", []),
                **({"allow_no_changes": spec.get("allow_no_changes")} if "allow_no_changes" in spec else {}),
            },
        }

    def preview_update_version(self, project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
        paths = self._paths(project_path)
        plan = self._load_json(paths.plan_file)
        version, updates, preview = self._validate_update_spec(paths, plan, spec)

        patch_id = f"patch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        os.makedirs(paths.patch_dir, exist_ok=True)
        patch_path = self._safe_join(paths.patch_dir, f"{patch_id}.json")
        patch_payload = {
            "patch_id": patch_id,
            "operation": "update_version",
            "created_at": datetime.now().isoformat(),
            "project_root": paths.project_root,
            "project_path": paths.project_root,
            "base_plan_signature": self._file_signature(paths.plan_file),
            "version": version,
            "updates": updates,
            "preview": preview,
        }
        write_json_atomic(patch_path, patch_payload)
        return {
            "ok": True,
            "operation": "update_version",
            "patch_id": patch_id,
            "patch_path": patch_path,
            "preview": preview,
        }

    def apply_plan_patch(self, project_path: str, patch_id: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        patch_payload, patch_path = self._load_patch(paths, patch_id)
        operation = str(patch_payload.get("operation", "insert_version"))
        project_path_match = self._is_patch_project_match(paths.project_root, patch_payload)
        if not project_path_match:
            result = {
                "ok": False,
                "status": "FAILED",
                "error_code": "PROJECT_PATH_MISMATCH",
                "message": "补丁 project_path 与当前项目不匹配。",
                "patch_id": patch_id,
                "patch_path": patch_path,
            }
            self._write_patch_status(
                patch_path=patch_path,
                patch_payload=patch_payload,
                status="FAILED",
                error_code="PROJECT_PATH_MISMATCH",
                message=result["message"],
            )
            return result
        current_signature = self._file_signature(paths.plan_file)
        base_signature = patch_payload.get("base_plan_signature")
        if current_signature != base_signature:
            result = {
                "ok": False,
                "status": "PATCH_STALE",
                "error_code": "PATCH_STALE",
                "message": "plan.json 已变化，补丁预览已过期，请重新 preview。",
                "patch_id": patch_id,
                "patch_path": patch_path,
            }
            self._write_patch_status(
                patch_path=patch_path,
                patch_payload=patch_payload,
                status="STALE",
                error_code="PATCH_STALE",
                message=result["message"],
            )
            return result
        try:
            plan = self._load_json(paths.plan_file)
            if operation == "update_version":
                version = str(patch_payload.get("version", "")).strip()
                if not version:
                    raise PlanningBridgeError("补丁缺少 version。")
                updates = patch_payload.get("updates")
                if not isinstance(updates, dict):
                    raise PlanningBridgeError("补丁缺少 updates。")
                _, normalized_updates, _ = self._validate_update_spec(paths, plan, {"version": version, **updates})
                changed_files = self._apply_update_version_patch(paths, plan, version, normalized_updates)
                self._write_plan_updated_marker(
                    paths=paths,
                    patch_id=patch_id,
                    operation="update_version",
                    changed_files=changed_files,
                )
                state_sync = self._sync_state_after_plan_change(paths, prefer_version=version)
                result = {
                    "ok": True,
                    "status": "APPLIED",
                    "operation": "update_version",
                    "patch_id": patch_id,
                    "updated_version": version,
                    "changed_files": changed_files,
                    "state_sync": state_sync,
                    "next_instruction": "回到 Web Console，重新载入计划；确认无误后继续下一版本或运行当前版本。",
                    "patch_path": patch_path,
                }
                self._write_patch_status(
                    patch_path=patch_path,
                    patch_payload=patch_payload,
                    status="APPLIED",
                    changed_files=changed_files,
                    apply_result_summary={
                        "operation": "update_version",
                        "updated_version": version,
                    },
                )
                return result

            spec = patch_payload.get("spec", {})
            self._validate_insert_spec(plan, spec)
            first_insert = spec.get("insert_after") == "__first__"
            changed_files = self._apply_insert_version_patch(paths, plan, spec)
            self._write_plan_updated_marker(
                paths=paths,
                patch_id=patch_id,
                operation="insert_version",
                changed_files=changed_files,
            )
            state_sync = self._sync_state_after_plan_change(paths, prefer_version=str(spec.get("version", "")).strip())

            result = {
                "ok": True,
                "status": "APPLIED",
                "patch_id": patch_id,
                "operation": "insert_version",
                "inserted_version": spec.get("version"),
                "first_insert": first_insert,
                "changed_files": changed_files,
                "state_sync": state_sync,
                "next_instruction": "回到 Web Console，进入新版本后运行当前版本。",
                "patch_path": patch_path,
            }
            self._write_patch_status(
                patch_path=patch_path,
                patch_payload=patch_payload,
                status="APPLIED",
                changed_files=changed_files,
                apply_result_summary={
                    "operation": "insert_version",
                    "inserted_version": spec.get("version"),
                },
            )
            return result
        except PlanningBridgeError as e:
            result = {
                "ok": False,
                "status": "FAILED",
                "error_code": "APPLY_FAILED",
                "message": str(e),
                "patch_id": patch_id,
                "patch_path": patch_path,
            }
            self._write_patch_status(
                patch_path=patch_path,
                patch_payload=patch_payload,
                status="FAILED",
                error_code="APPLY_FAILED",
                message=str(e),
            )
            return result
        except Exception as e:
            result = {
                "ok": False,
                "status": "FAILED",
                "error_code": "APPLY_INTERNAL_ERROR",
                "message": str(e),
                "patch_id": patch_id,
                "patch_path": patch_path,
            }
            self._write_patch_status(
                patch_path=patch_path,
                patch_payload=patch_payload,
                status="FAILED",
                error_code="APPLY_INTERNAL_ERROR",
                message=str(e),
            )
            return result

    def sync_state_after_plan_change(self, project_path: str, *, prefer_version: str | None = None) -> dict[str, Any]:
        return self._sync_state_after_plan_change(self._paths(project_path), prefer_version=prefer_version)

    def _sync_state_after_plan_change(
        self,
        paths: BridgePaths,
        *,
        prefer_version: str | None = None,
    ) -> dict[str, Any]:
        plan = self._load_json(paths.plan_file)
        state = self._load_json_if_exists(paths.state_file)
        if state is None:
            return {
                "ok": False,
                "error_code": "STATE_MISSING",
                "message": "state.json 不存在，无法同步 Runner 状态。",
                "current_version": None,
                "current_version_index": None,
                "synced_version_count": 0,
            }

        plan_versions = plan.get("versions", [])
        if not isinstance(plan_versions, list):
            plan_versions = []
        state_versions = state.get("versions", [])
        if not isinstance(state_versions, list):
            state_versions = []

        current_v = state.get("current_version")
        if isinstance(current_v, str) and current_v.strip():
            current_exists = any(v.get("version") == current_v for v in plan_versions if isinstance(v, dict))
            if not current_exists:
                return {
                    "ok": False,
                    "error_code": "CURRENT_VERSION_MISSING",
                    "message": f"state.current_version 在 plan 中不存在：{current_v}",
                    "current_version": current_v,
                    "current_version_index": state.get("current_version_index"),
                    "synced_version_count": len(state_versions),
                }
        else:
            current_v = None

        existing = {
            v.get("version", ""): v
            for v in state_versions
            if isinstance(v, dict) and v.get("version")
        }
        new_state_versions = []
        changed = False
        for pv in plan_versions:
            if not isinstance(pv, dict):
                continue
            v_str = pv.get("version", "")
            runtime = existing.pop(v_str, None)
            if runtime is not None:
                plan_name = pv.get("name", "")
                if runtime.get("name") != plan_name:
                    runtime["name"] = plan_name
                    changed = True
                new_state_versions.append(runtime)
            else:
                new_state_versions.append({
                    "version": v_str,
                    "name": pv.get("name", ""),
                    "status": "NOT_STARTED",
                    "attempt": 0,
                    "started_at": None,
                    "completed_at": None,
                    "last_run_id": None,
                    "last_prompt_file": None,
                    "last_audit_file": None,
                    "commit_hash": None,
                    "committed_at": None,
                    "commit_message": None,
                    "commit_files": None,
                    "metadata": None,
                    "note": None,
                })
                changed = True

        if existing:
            changed = True

        current_index = state.get("current_version_index", 0)
        if current_v:
            for idx, v in enumerate(plan_versions):
                if isinstance(v, dict) and v.get("version") == current_v:
                    if current_index != idx:
                        state["current_version_index"] = idx
                        current_index = idx
                        changed = True
                    break
        else:
            preferred = str(prefer_version or "").strip()
            selected_version = None
            selected_index = 0
            if preferred:
                for idx, v in enumerate(plan_versions):
                    if isinstance(v, dict) and v.get("version") == preferred and v.get("enabled", True) is not False:
                        selected_version = preferred
                        selected_index = idx
                        break
            if selected_version is None:
                for idx, v in enumerate(plan_versions):
                    if isinstance(v, dict) and v.get("enabled", True) is not False:
                        selected_version = v.get("version")
                        selected_index = idx
                        break
            if selected_version:
                state["current_version"] = selected_version
                state["current_version_index"] = selected_index
                current_v = selected_version
                current_index = selected_index
                changed = True

        version_order_changed = [v.get("version") for v in new_state_versions] != [v.get("version") for v in state_versions]
        if changed or version_order_changed:
            state["versions"] = new_state_versions
            state["status"] = state.get("status") or "READY"
            state["updated_at"] = datetime.now().isoformat()
            write_json_atomic(paths.state_file, state)

        return {
            "ok": True,
            "current_version": current_v,
            "current_version_index": current_index,
            "synced_version_count": len(new_state_versions),
            "state_file": paths.state_file,
        }

    def _write_plan_updated_marker(self, paths: BridgePaths, patch_id: str, operation: str, changed_files: list[str]) -> None:
        marker_path = self._safe_join(os.path.join(paths.runner_dir, "runtime"), "plan-updated.marker")
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(),
            "source": "mcp_planning_bridge",
            "patch_id": patch_id,
            "operation": operation,
            "changed_files": changed_files,
        }
        write_json_atomic(marker_path, payload)

    def get_plan_patch_status(self, project_path: str, patch_id: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        patch_payload, patch_path = self._load_patch(paths, patch_id)
        current_signature = self._file_signature(paths.plan_file)
        base_signature = patch_payload.get("base_plan_signature")
        stale = current_signature != base_signature
        return {
            "patch_id": patch_id,
            "patch_path": patch_path,
            "operation": patch_payload.get("operation", "insert_version"),
            "status": patch_payload.get("status", "PENDING"),
            "stale": stale,
            "project_path_match": self._is_patch_project_match(paths.project_root, patch_payload),
            "created_at": patch_payload.get("created_at"),
            "spec": patch_payload.get("spec", {}),
            "version": patch_payload.get("version"),
            "message": patch_payload.get("message"),
            "error_code": patch_payload.get("error_code"),
        }

    def list_plan_patches(self, project_path: str) -> dict[str, Any]:
        paths = self._paths(project_path)
        os.makedirs(paths.patch_dir, exist_ok=True)
        plan_signature = self._file_signature(paths.plan_file)
        patches: list[dict[str, Any]] = []
        for patch_file in sorted(Path(paths.patch_dir).glob("*.json")):
            patch_path = str(patch_file.resolve())
            try:
                payload = self._load_json(patch_path)
            except Exception as e:
                patches.append(
                    {
                        "patch_id": patch_file.stem,
                        "operation": "unknown",
                        "version": None,
                        "created_at": None,
                        "status": "FAILED",
                        "patch_path": patch_path,
                        "changed_fields": [],
                        "preview": {},
                        "is_stale": True,
                        "project_path_match": False,
                        "auto_apply_eligible": False,
                        "applied_at": None,
                        "error_code": "PATCH_READ_ERROR",
                        "message": str(e),
                    }
                )
                continue

            operation = str(payload.get("operation", "insert_version"))
            status = str(payload.get("status", "PENDING")).strip() or "PENDING"
            version = payload.get("version")
            if not version and isinstance(payload.get("spec"), dict):
                version = payload["spec"].get("version")
            project_path_match = self._is_patch_project_match(paths.project_root, payload)
            base_signature = payload.get("base_plan_signature")
            is_stale = not self._signatures_equal(base_signature, plan_signature)
            preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
            changed_fields = preview.get("changed_fields")
            if not isinstance(changed_fields, list):
                if operation == "update_version" and isinstance(payload.get("updates"), dict):
                    changed_fields = list(payload["updates"].keys())
                else:
                    changed_fields = []

            auto_apply_eligible = (
                status == "PENDING"
                and project_path_match
                and (not is_stale)
                and operation in ("insert_version", "update_version")
            )
            patches.append(
                {
                    "patch_id": payload.get("patch_id", patch_file.stem),
                    "operation": operation,
                    "version": version,
                    "created_at": payload.get("created_at"),
                    "status": status,
                    "patch_path": patch_path,
                    "changed_fields": changed_fields,
                    "preview": preview,
                    "is_stale": is_stale,
                    "project_path_match": project_path_match,
                    "auto_apply_eligible": auto_apply_eligible,
                    "applied_at": payload.get("applied_at"),
                    "error_code": payload.get("error_code"),
                    "message": payload.get("message"),
                }
            )
        patches.sort(key=lambda item: item.get("created_at") or "")
        return {
            "project": paths.project_root,
            "patch_count": len(patches),
            "patches": patches,
        }

    def auto_apply_pending_plan_patches(self, project_path: str, limit: int = 5) -> dict[str, Any]:
        return self._auto_apply_pending_plan_patches_batch(project_path, limit=limit)

    def _auto_apply_pending_plan_patches_batch(self, project_path: str, limit: int = 5) -> dict[str, Any]:
        from copy import deepcopy

        limit_count = max(1, int(limit))
        paths = self._paths(project_path)
        patch_list = self.list_plan_patches(project_path)
        eligible = [item for item in patch_list.get("patches", []) if item.get("auto_apply_eligible")]
        eligible = sorted(eligible, key=lambda item: item.get("created_at") or "")[:limit_count]
        total_patch_count = len(patch_list.get("patches", []))
        skipped_count = max(0, total_patch_count - len(eligible))

        if not eligible:
            return {
                "ok": True,
                "applied_count": 0,
                "failed_count": 0,
                "skipped_count": skipped_count,
                "results": [],
            }

        batch_base_signature = self._file_signature(paths.plan_file)
        plan = self._load_json(paths.plan_file)
        working_plan = deepcopy(plan)
        all_changed_files: list[str] = []
        batch_results: list[dict[str, Any]] = []
        failed_any = False

        for item in eligible:
            patch_id = str(item.get("patch_id", "")).strip()
            if not patch_id:
                batch_results.append({
                    "patch_id": None,
                    "ok": False,
                    "status": "FAILED",
                    "error_code": "INVALID_PATCH_ID",
                    "message": "patch_id 无效。",
                })
                failed_any = True
                continue

            try:
                patch_payload, patch_path = self._load_patch(paths, patch_id)
            except PlanningBridgeError as e:
                batch_results.append({
                    "patch_id": patch_id,
                    "ok": False,
                    "status": "FAILED",
                    "error_code": "PATCH_NOT_FOUND",
                    "message": str(e),
                })
                failed_any = True
                continue

            operation = str(patch_payload.get("operation", "insert_version"))
            project_path_match = self._is_patch_project_match(paths.project_root, patch_payload)
            if not project_path_match:
                batch_results.append({
                    "patch_id": patch_id,
                    "ok": False,
                    "status": "FAILED",
                    "error_code": "PROJECT_PATH_MISMATCH",
                    "message": "补丁 project_path 与当前项目不匹配。",
                })
                self._write_patch_status(
                    patch_path=patch_path, patch_payload=patch_payload,
                    status="FAILED", error_code="PROJECT_PATH_MISMATCH",
                    message="补丁 project_path 与当前项目不匹配。",
                )
                failed_any = True
                continue

            base_signature = patch_payload.get("base_plan_signature")
            if not self._signatures_equal(base_signature, batch_base_signature):
                batch_results.append({
                    "patch_id": patch_id,
                    "ok": False,
                    "status": "STALE",
                    "error_code": "PATCH_STALE",
                    "message": "plan.json 已变化，补丁预览已过期，请重新 preview。",
                })
                self._write_patch_status(
                    patch_path=patch_path, patch_payload=patch_payload,
                    status="STALE", error_code="PATCH_STALE",
                    message="plan.json 已变化，补丁预览已过期，请重新 preview。",
                )
                failed_any = True
                continue

            try:
                if operation == "update_version":
                    version = str(patch_payload.get("version", "")).strip()
                    if not version:
                        raise PlanningBridgeError("补丁缺少 version。")
                    updates = patch_payload.get("updates")
                    if not isinstance(updates, dict):
                        raise PlanningBridgeError("补丁缺少 updates。")
                    _, normalized_updates, _ = self._validate_update_spec(paths, plan, {"version": version, **updates})
                    changed = self._apply_update_version_to_plan(paths, working_plan, version, normalized_updates)
                else:
                    spec = patch_payload.get("spec", {})
                    self._validate_insert_spec(working_plan, spec)
                    changed = self._apply_insert_version_to_plan(paths, working_plan, spec, write_prompt=False)

                all_changed_files.extend(changed)
                batch_results.append({
                    "patch_id": patch_id,
                    "patch_path": patch_path,
                    "operation": operation,
                    "patch_payload": patch_payload,
                    "ok": True,
                    "status": "PENDING_APPLIED_IN_MEMORY",
                })
            except PlanningBridgeError as e:
                batch_results.append({
                    "patch_id": patch_id,
                    "ok": False,
                    "status": "FAILED",
                    "error_code": "APPLY_FAILED",
                    "message": str(e),
                })
                self._write_patch_status(
                    patch_path=patch_path, patch_payload=patch_payload,
                    status="FAILED", error_code="APPLY_FAILED", message=str(e),
                )
                failed_any = True
            except Exception as e:
                batch_results.append({
                    "patch_id": patch_id,
                    "ok": False,
                    "status": "FAILED",
                    "error_code": "APPLY_INTERNAL_ERROR",
                    "message": str(e),
                })
                self._write_patch_status(
                    patch_path=patch_path, patch_payload=patch_payload,
                    status="FAILED", error_code="APPLY_INTERNAL_ERROR", message=str(e),
                )
                failed_any = True

        if failed_any:
            for br in batch_results:
                if br.get("status") == "PENDING_APPLIED_IN_MEMORY":
                    br["status"] = "SKIPPED"
                    br["ok"] = False
                    br["error_code"] = "BATCH_ABORTED"
                    br["message"] = "批次中其他补丁失败，本补丁保持 PENDING，plan.json 未更改。"
            final_applied = 0
            final_failed = sum(1 for r in batch_results if r.get("ok") is False)
            return {
                "ok": True,
                "applied_count": final_applied,
                "failed_count": final_failed,
                "skipped_count": skipped_count,
                "results": batch_results,
            }

        changed_unique = list(dict.fromkeys(all_changed_files))
        for br in batch_results:
            if br.get("operation") == "insert_version":
                spec = br.get("patch_payload", {}).get("spec", {})
                prompt_file_abs = ""
                for version_obj in working_plan.get("versions", []):
                    if version_obj.get("version") == spec.get("version"):
                        prompt_file_abs = str(version_obj.get("prompt_file", ""))
                        break
                if prompt_file_abs:
                    self._write_insert_version_prompt(prompt_file_abs, spec)
        write_json_atomic(paths.plan_file, working_plan)
        self._write_plan_updated_marker_batch(paths=paths, patch_ids=[r["patch_id"] for r in batch_results], changed_files=changed_unique)
        self._sync_state_after_plan_change(paths)

        final_results: list[dict[str, Any]] = []
        for br in batch_results:
            patch_id = br["patch_id"]
            patch_path = br["patch_path"]
            patch_payload = br["patch_payload"]
            operation = br["operation"]

            self._write_patch_status(
                patch_path=patch_path,
                patch_payload=patch_payload,
                status="APPLIED",
                changed_files=changed_unique,
                apply_result_summary={
                    "operation": operation,
                },
            )

            result_item = {
                "patch_id": patch_id,
                "patch_path": patch_path,
                "ok": True,
                "status": "APPLIED",
                "operation": operation,
            }
            if operation == "update_version":
                result_item["version"] = patch_payload.get("version")
            elif operation == "insert_version":
                spec = patch_payload.get("spec", {})
                result_item["inserted_version"] = spec.get("version")
            final_results.append(result_item)

        return {
            "ok": True,
            "applied_count": len(final_results),
            "failed_count": 0,
            "skipped_count": skipped_count,
            "results": final_results,
        }

    def _write_plan_updated_marker_batch(self, paths: BridgePaths, patch_ids: list[str], changed_files: list[str]) -> None:
        marker_path = self._safe_join(os.path.join(paths.runner_dir, "runtime"), "plan-updated.marker")
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(),
            "source": "mcp_planning_bridge",
            "operation": "batch_apply_plan_patches",
            "patch_ids": patch_ids,
            "changed_files": changed_files,
        }
        write_json_atomic(marker_path, payload)

    def _normalize_execution_profile(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlanningBridgeError("execution 必须是 JSON 对象。")
        allowed = {
            "provider", "model", "model_name",
            "pi_model", "codex_model", "opencode_model",
            "lane", "capability_level", "notes",
        }
        unknown = set(value.keys()) - allowed
        if unknown:
            raise PlanningBridgeError(f"execution 包含不支持字段：{'、'.join(sorted(unknown))}")
        normalized: dict[str, Any] = {}
        for key in allowed:
            if key not in value:
                continue
            raw = value[key]
            if key == "provider":
                if not isinstance(raw, str) or not raw.strip():
                    raise PlanningBridgeError("execution.provider 必须是非空字符串。")
                provider_val = raw.strip().lower()
                if not is_supported_execution_provider(provider_val):
                    raise PlanningBridgeError("execution.provider 必须是 pi、codex 或 opencode。")
                normalized[key] = provider_val
            else:
                if not isinstance(raw, str) or not raw.strip():
                    raise PlanningBridgeError(f"execution.{key} 必须是非空字符串。")
                normalized[key] = raw.strip()
        return normalized

    def _build_plan_version_obj(self, paths: BridgePaths, spec: dict[str, Any]) -> dict[str, Any]:
        version = str(spec["version"]).strip()
        prompt_file_name = self._safe_prompt_filename(spec.get("prompt_file") or f"{version}.md")
        prompt_file_abs = self._safe_join(paths.prompts_dir, prompt_file_name)
        acceptance = self._normalize_acceptance_commands(spec.get("acceptance_commands", []))
        obj: dict[str, Any] = {
            "version": version,
            "name": str(spec["name"]).strip(),
            "description": str(spec.get("description", "")).strip(),
            "prompt_file": prompt_file_abs,
            "enabled": True,
            "context_files": spec.get("context_files", []),
            "allowed_files": spec.get("allowed_files", []),
            "forbidden_files": spec.get("forbidden_files", []),
            "acceptance_commands": acceptance,
            "manual_acceptance": spec.get("manual_acceptance", []),
            "out_of_scope": spec.get("out_of_scope", []),
        }
        execution = spec.get("execution")
        if execution is not None:
            obj["execution"] = execution
        if "allow_no_changes" in spec:
            obj["allow_no_changes"] = spec["allow_no_changes"]
        return obj

    def _apply_insert_version_patch(self, paths: BridgePaths, plan: dict[str, Any], spec: dict[str, Any]) -> list[str]:
        changed_files = self._apply_insert_version_to_plan(paths, plan, spec)
        write_json_atomic(paths.plan_file, plan)
        return changed_files

    def _apply_update_version_patch(
        self,
        paths: BridgePaths,
        plan: dict[str, Any],
        version: str,
        updates: dict[str, Any],
    ) -> list[str]:
        changed_files = self._apply_update_version_to_plan(paths, plan, version, updates)
        write_json_atomic(paths.plan_file, plan)
        return changed_files

    def _apply_insert_version_to_plan(
        self,
        paths: BridgePaths,
        plan: dict[str, Any],
        spec: dict[str, Any],
        *,
        write_prompt: bool = True,
    ) -> list[str]:
        versions = plan.get("versions", [])
        insert_after = spec["insert_after"]
        if insert_after == "__first__":
            after_idx = -1
        else:
            after_idx = self._find_version_index(versions, insert_after)
        new_version_obj = self._build_plan_version_obj(paths, spec)
        versions.insert(after_idx + 1, new_version_obj)
        plan["versions"] = versions

        prompt_file_abs = new_version_obj["prompt_file"]
        if write_prompt:
            self._write_insert_version_prompt(prompt_file_abs, spec)
        return [paths.plan_file, prompt_file_abs]

    def _write_insert_version_prompt(self, prompt_file_abs: str, spec: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(prompt_file_abs), exist_ok=True)
        self._write_text_atomic(prompt_file_abs, str(spec.get("prompt", "")).rstrip() + "\n")

    def _apply_update_version_to_plan(
        self,
        paths: BridgePaths,
        plan: dict[str, Any],
        version: str,
        updates: dict[str, Any],
    ) -> list[str]:
        versions = plan.get("versions", [])
        target_idx = self._find_version_index(versions, version)
        if target_idx < 0:
            raise PlanningBridgeError(f"计划中不存在版本：{version}")
        target = versions[target_idx]
        changed_files: list[str] = [paths.plan_file]

        prompt_path = self._safe_join(paths.prompts_dir, self._safe_prompt_filename(f"{version}.md"))
        if "prompt" in updates:
            os.makedirs(os.path.dirname(prompt_path), exist_ok=True)
            self._write_text_atomic(prompt_path, str(updates["prompt"]).rstrip() + "\n")
            target["prompt_file"] = prompt_path
            changed_files.append(prompt_path)

        for field in (
            "name",
            "description",
            "allowed_files",
            "acceptance_commands",
            "manual_acceptance",
            "out_of_scope",
            "context_files",
            "execution",
            "allow_no_changes",
        ):
            if field in updates:
                target[field] = updates[field]

        versions[target_idx] = target
        plan["versions"] = versions
        return changed_files

    def _normalize_acceptance_commands(self, acceptance_commands: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in acceptance_commands:
            if isinstance(item, str):
                normalized.append({
                    "command": item,
                    "timeout_seconds": 600,
                    "continue_on_failure": False,
                })
                continue
            if isinstance(item, dict):
                cmd = str(item.get("command", "")).strip()
                normalized.append({
                    "command": cmd,
                    "timeout_seconds": int(item.get("timeout_seconds", 600)),
                    "continue_on_failure": bool(item.get("continue_on_failure", False)),
                })
        return normalized

    def _validate_insert_spec(self, plan: dict[str, Any], spec: dict[str, Any]) -> None:
        versions = plan.get("versions", [])
        if not isinstance(versions, list):
            versions = []
        if not versions:
            insert_after_raw = spec.get("insert_after")
            if insert_after_raw is None or not str(insert_after_raw).strip():
                spec["insert_after"] = "__first__"
        required = ["version", "name", "description", "prompt", "allowed_files", "acceptance_commands"]
        if versions:
            required.insert(0, "insert_after")
        for field in required:
            if field not in spec:
                raise PlanningBridgeError(f"spec 缺少字段：{field}")
        insert_after = str(spec["insert_after"]).strip()
        version = str(spec["version"]).strip()
        if not version:
            raise PlanningBridgeError("version 不能为空。")
        if self._find_version_index(versions, version) >= 0:
            raise PlanningBridgeError(f"version 已存在：{version}")
        if insert_after == "__first__":
            if versions:
                raise PlanningBridgeError("__first__ 仅用于空 plan。")
        else:
            if self._find_version_index(versions, insert_after) < 0:
                raise PlanningBridgeError(f"insert_after 不存在：{insert_after}")
            self._validate_version_order(versions, insert_after, version)

        allowed_files = spec.get("allowed_files", [])
        if not isinstance(allowed_files, list) or not allowed_files:
            raise PlanningBridgeError("allowed_files 必须是非空列表。")
        acceptance = spec.get("acceptance_commands", [])
        if not isinstance(acceptance, list) or not acceptance:
            raise PlanningBridgeError("acceptance_commands 必须是非空列表。")
        normalized_acceptance = self._normalize_acceptance_commands(acceptance)
        if not normalized_acceptance:
            raise PlanningBridgeError("acceptance_commands 无有效命令。")
        risks = self._detect_acceptance_risks(normalized_acceptance)
        if risks:
            raise PlanningBridgeError("acceptance_commands 存在拆分风险：" + "；".join(risks))
        self._safe_prompt_filename(spec.get("prompt_file") or f"{version}.md")
        if "execution" in spec:
            spec["execution"] = self._normalize_execution_profile(spec["execution"])
        if "allow_no_changes" in spec and not isinstance(spec["allow_no_changes"], bool):
            raise PlanningBridgeError("allow_no_changes 必须是布尔值。")

    def _validate_update_spec(
        self,
        paths: BridgePaths,
        plan: dict[str, Any],
        spec: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        version = str(spec.get("version", "")).strip()
        if not version:
            raise PlanningBridgeError("spec 缺少字段：version")
        target = self._find_plan_version(plan, version)
        if not target:
            raise PlanningBridgeError(f"计划中不存在版本：{version}")

        update_fields = (
            "name",
            "description",
            "prompt",
            "allowed_files",
            "acceptance_commands",
            "manual_acceptance",
            "out_of_scope",
            "context_files",
            "execution",
            "allow_no_changes",
        )
        updates: dict[str, Any] = {}
        for field in update_fields:
            if field not in spec:
                continue
            value = spec[field]
            if field in ("name", "description", "prompt"):
                if not isinstance(value, str) or not value.strip():
                    raise PlanningBridgeError(f"{field} 必须是非空字符串。")
                updates[field] = value.strip()
                continue
            if field == "allowed_files":
                if not isinstance(value, list) or not value:
                    raise PlanningBridgeError("allowed_files 必须是非空列表。")
                if any(not isinstance(item, str) or not item.strip() for item in value):
                    raise PlanningBridgeError("allowed_files 必须是字符串列表。")
                updates[field] = [item.strip() for item in value]
                continue
            if field == "acceptance_commands":
                if not isinstance(value, list) or not value:
                    raise PlanningBridgeError("acceptance_commands 必须是非空列表。")
                normalized_acceptance = self._normalize_acceptance_commands(value)
                if not normalized_acceptance:
                    raise PlanningBridgeError("acceptance_commands 无有效命令。")
                risks = self._detect_acceptance_risks(normalized_acceptance)
                if risks:
                    raise PlanningBridgeError("acceptance_commands 存在拆分风险：" + "；".join(risks))
                updates[field] = normalized_acceptance
                continue
            if field == "execution":
                updates[field] = self._normalize_execution_profile(value)
                continue
            if field == "allow_no_changes":
                if not isinstance(value, bool):
                    raise PlanningBridgeError("allow_no_changes 必须是布尔值。")
                updates[field] = value
                continue
            if not isinstance(value, list):
                raise PlanningBridgeError(f"{field} 必须是字符串列表。")
            if any(not isinstance(item, str) for item in value):
                raise PlanningBridgeError(f"{field} 必须是字符串列表。")
            updates[field] = [item.strip() for item in value]

        if not updates:
            raise PlanningBridgeError("spec 至少需要一个可更新字段。")

        preview: dict[str, Any] = {
            "version": version,
            "changed_fields": list(updates.keys()),
            "existing_prompt_file": target.get("prompt_file"),
        }
        if "prompt" in updates:
            prompt_path = self._safe_join(paths.prompts_dir, self._safe_prompt_filename(f"{version}.md"))
            preview["prompt_file"] = prompt_path
            preview["prompt_file_overwrite"] = bool(os.path.exists(prompt_path))
        if "acceptance_commands" in updates:
            preview["acceptance_command_count"] = len(updates["acceptance_commands"])
        if "allowed_files" in updates:
            preview["allowed_files_count"] = len(updates["allowed_files"])
        if "allow_no_changes" in updates:
            preview["allow_no_changes"] = updates["allow_no_changes"]
        return version, updates, preview

    def _validate_version_order(self, versions: list[dict[str, Any]], insert_after: str, new_version: str) -> None:
        after_idx = self._find_version_index(versions, insert_after)
        if after_idx < 0:
            return
        next_version = None
        if after_idx + 1 < len(versions):
            next_version = versions[after_idx + 1].get("version")
        after_tuple = self._parse_version_num(insert_after)
        new_tuple = self._parse_version_num(new_version)
        next_tuple = self._parse_version_num(next_version) if next_version else None
        if after_tuple and new_tuple and new_tuple <= after_tuple:
            raise PlanningBridgeError("新版本号顺序不合理：应大于 insert_after。")
        if next_tuple and new_tuple and new_tuple >= next_tuple:
            raise PlanningBridgeError("新版本号顺序不合理：应小于后续版本。")

    def _detect_acceptance_risks(self, acceptance_commands: list[dict[str, Any]]) -> list[str]:
        risky_single_tokens = {"{", "}", "JSON", "PY", ")"}
        risks: list[str] = []
        var_defs: list[str] = []
        for idx, item in enumerate(acceptance_commands, start=1):
            cmd = str(item.get("command", "")).strip()
            if cmd in risky_single_tokens:
                risks.append(f"第{idx}条命令是单独标记：{cmd}")
            if "<<'JSON'" in cmd and "JSON" not in cmd.split("<<'JSON'", 1)[1]:
                risks.append(f"第{idx}条命令存在未闭合 JSON here-doc")
            if "<<'PY'" in cmd and "PY" not in cmd.split("<<'PY'", 1)[1]:
                risks.append(f"第{idx}条命令存在未闭合 PY here-doc")
            if "CHANGE_ID=$(python3 - <<'PY'" in cmd:
                risks.append(f"第{idx}条命令包含跨命令变量块风险")
            define_match = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=", cmd)
            if define_match:
                var_defs.append(define_match.group(1))
            for defined in var_defs:
                if f"${defined}" in cmd and not cmd.startswith(f"{defined}="):
                    risks.append(f"第{idx}条命令依赖跨命令变量 ${defined}")
        return risks

    def _safe_changed_files(self, project_root: str, allowed_patterns: list[str]) -> dict[str, Any]:
        result = self._run_git(["diff", "--name-only"], cwd=project_root)
        if result["code"] != 0:
            stderr = (result["stderr"] or "").strip()
            if "Not a git repository" in stderr:
                stderr = "未检测到 Git 仓库。"
            else:
                stderr = stderr[:300] if stderr else "git diff 失败"
            return {"status": "UNKNOWN", "files": [], "message": stderr}
        changed = [self._normalize_path(line) for line in result["stdout"].splitlines() if line.strip()]
        scoped: list[str] = []
        for path in changed:
            if self._matches_any(path, allowed_patterns):
                scoped.append(path)
        return {
            "status": "OK",
            "total_changed_count": len(changed),
            "allowed_changed_count": len(scoped),
            "allowed_changed_files": scoped,
        }

    def _resolve_audit_for_version(
        self,
        paths: BridgePaths,
        state: dict[str, Any],
        version_state: dict[str, Any] | None,
        requested_version: str,
    ) -> str | None:
        candidates: list[str] = []
        if version_state and version_state.get("last_audit_file"):
            candidates.append(version_state["last_audit_file"])
        if state.get("last_audit_file") and self._path_matches_version(state.get("last_audit_file"), requested_version):
            candidates.append(state["last_audit_file"])
        candidates.append(os.path.join(paths.logs_dir, f"{requested_version}-audit.md"))
        if os.path.isdir(paths.logs_dir):
            for fp in Path(paths.logs_dir).glob(f"{requested_version}*audit*.md"):
                candidates.append(str(fp))
        unique = []
        seen = set()
        for c in candidates:
            if not c:
                continue
            abs_c = os.path.abspath(c)
            if abs_c in seen:
                continue
            seen.add(abs_c)
            unique.append(abs_c)
        existing = [p for p in unique if os.path.exists(p)]
        if not existing:
            return None
        existing.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return existing[0]

    def _parse_audit_summary(self, audit_path: str | None, requested_version: str) -> dict[str, Any]:
        if not audit_path:
            return {
                "exists": False,
                "path": None,
                "mtime": None,
                "failed_command_indexes": [],
                "failed_command_details": [],
                "outside_allowed_files": [],
                "forbidden_changed_files": [],
            }
        text = Path(audit_path).read_text(encoding="utf-8", errors="replace")
        mtime = datetime.fromtimestamp(os.path.getmtime(audit_path)).strftime("%Y-%m-%d %H:%M:%S")
        failed_indexes: list[int] = []
        failed_details: list[dict[str, Any]] = []
        outside: list[str] = []
        forbidden: list[str] = []
        stale_warning = None
        if requested_version not in os.path.basename(audit_path):
            stale_warning = "audit 文件名与请求版本不一致，已按版本过滤策略处理。"
        lines = text.splitlines()
        current_section = ""
        current_failed: dict[str, Any] | None = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("### 越界文件"):
                current_section = "outside"
                continue
            if stripped.startswith("### 禁止文件变更"):
                current_section = "forbidden"
                continue
            if stripped.startswith("[#"):
                if current_failed:
                    failed_details.append(current_failed)
                current_failed = {"index": stripped, "original_command": None, "executed_command": None, "exit_code": None}
                m = re.search(r"#(\d+)", stripped)
                if m:
                    failed_indexes.append(int(m.group(1)))
                continue
            if current_failed is not None:
                if stripped.startswith("原始命令"):
                    current_failed["original_command"] = stripped.split("：", 1)[-1].strip()
                elif stripped.startswith("实际执行命令"):
                    current_failed["executed_command"] = stripped.split("：", 1)[-1].strip()
                elif stripped.startswith("退出码"):
                    current_failed["exit_code"] = stripped.split("：", 1)[-1].strip()
            if current_section == "outside" and stripped and stripped != "无":
                if not stripped.startswith("###"):
                    outside.append(stripped)
            if current_section == "forbidden" and stripped and stripped != "无":
                if not stripped.startswith("###"):
                    forbidden.append(stripped)
        if current_failed:
            failed_details.append(current_failed)
        return {
            "exists": True,
            "path": audit_path,
            "mtime": mtime,
            "failed_command_indexes": sorted(list(set(failed_indexes))),
            "failed_command_details": failed_details,
            "outside_allowed_files": outside,
            "forbidden_changed_files": forbidden,
            "stale_warning": stale_warning,
        }

    def _build_review_status(self, review_state: dict[str, Any] | None, version: str) -> dict[str, Any]:
        if not review_state:
            return {"is_checkpoint_reviewed": False, "last_reviewed_version": None, "last_review_file": None}
        return {
            "is_checkpoint_reviewed": review_state.get("last_reviewed_version") == version and bool(review_state.get("last_review_file")),
            "last_reviewed_version": review_state.get("last_reviewed_version"),
            "last_review_file": review_state.get("last_review_file"),
            "last_reviewed_at": review_state.get("last_reviewed_at"),
        }

    def _read_prompt_excerpt(self, prompt_file: str | None, max_lines: int = 20) -> str | None:
        if not prompt_file or not os.path.exists(prompt_file):
            return None
        try:
            text = Path(prompt_file).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        lines = text.splitlines()[:max_lines]
        return "\n".join(lines)

    def _load_patch(self, paths: BridgePaths, patch_id: str) -> tuple[dict[str, Any], str]:
        safe_name = f"{patch_id}.json"
        candidates = [
            self._safe_join(paths.patch_dir, safe_name),
            self._safe_join(os.path.join(paths.runner_dir, "patches"), safe_name),
        ]
        for path in candidates:
            if os.path.exists(path):
                payload = self._load_json(path)
                return payload, path
        raise PlanningBridgeError(f"找不到补丁：{patch_id}")

    def _file_signature(self, path: str) -> dict[str, Any]:
        stat = os.stat(path)
        content = Path(path).read_bytes()
        return {
            "path": path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _safe_prompt_filename(self, name: str) -> str:
        value = str(name).strip()
        if not value:
            raise PlanningBridgeError("prompt_file 不能为空。")
        norm = value.replace("\\", "/")
        pure = PurePosixPath(norm)
        if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 1:
            raise PlanningBridgeError("prompt_file 必须是 prompts 目录下的相对文件名。")
        return pure.name

    def _safe_join(self, base_dir: str, filename: str) -> str:
        candidate = os.path.abspath(os.path.join(base_dir, filename))
        base_abs = os.path.abspath(base_dir)
        if not (candidate == base_abs or candidate.startswith(base_abs + os.sep)):
            raise PlanningBridgeError("检测到非法路径。")
        return candidate

    def _write_text_atomic(self, path: str, content: str) -> None:
        dir_name = os.path.dirname(path) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            raise

    def _signatures_equal(self, sig_a: dict[str, Any] | None, sig_b: dict[str, Any] | None) -> bool:
        if not isinstance(sig_a, dict) or not isinstance(sig_b, dict):
            return False
        return (
            sig_a.get("size") == sig_b.get("size")
            and sig_a.get("mtime") == sig_b.get("mtime")
            and sig_a.get("sha256") == sig_b.get("sha256")
        )

    def _is_patch_project_match(self, project_root: str, patch_payload: dict[str, Any]) -> bool:
        patch_project = patch_payload.get("project_path") or patch_payload.get("project_root")
        if not isinstance(patch_project, str) or not patch_project.strip():
            return False
        return os.path.abspath(patch_project) == os.path.abspath(project_root)

    def _write_patch_status(
        self,
        patch_path: str,
        patch_payload: dict[str, Any],
        status: str,
        error_code: str | None = None,
        message: str | None = None,
        changed_files: list[str] | None = None,
        apply_result_summary: dict[str, Any] | None = None,
    ) -> None:
        updated = dict(patch_payload)
        now = datetime.now().isoformat()
        updated["status"] = status
        if status == "APPLIED":
            updated["applied_at"] = now
        elif status == "FAILED":
            updated["failed_at"] = now
        elif status == "STALE":
            updated["stale_at"] = now
        if error_code:
            updated["error_code"] = error_code
        if message:
            updated["message"] = message
        if changed_files is not None:
            updated["changed_files"] = changed_files
        if apply_result_summary is not None:
            updated["apply_result_summary"] = apply_result_summary
        write_json_atomic(patch_path, updated)

    def _path_matches_version(self, path: str | None, version: str | None) -> bool:
        if not path or not version:
            return False
        name = os.path.basename(path)
        return name.startswith(f"{version}-") or f"/{version}-" in path or f"\\{version}-" in path

    def _error_mentions_version(self, last_error: dict[str, Any] | None, version: str) -> bool:
        if not last_error:
            return False
        msg = str(last_error.get("message", ""))
        detail = str(last_error.get("detail", ""))
        return version in msg or version in detail

    def _load_json(self, path: str) -> dict[str, Any]:
        if not os.path.exists(path):
            raise PlanningBridgeError(f"文件不存在：{path}")
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            raise PlanningBridgeError(f"读取 JSON 失败：{path}，{e}") from e

    def _load_json_if_exists(self, path: str) -> dict[str, Any] | None:
        if not os.path.exists(path):
            return None
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None

    def _find_version_state(self, state: dict[str, Any], version: str) -> dict[str, Any] | None:
        for item in state.get("versions", []):
            if item.get("version") == version:
                return item
        return None

    def _get_current_version_state(self, state: dict[str, Any]) -> dict[str, Any] | None:
        current = state.get("current_version")
        if not current:
            return None
        return self._find_version_state(state, current)

    def _find_plan_version(self, plan: dict[str, Any], version: str) -> dict[str, Any] | None:
        for item in plan.get("versions", []):
            if item.get("version") == version:
                return item
        return None

    def _find_version_index(self, versions: list[dict[str, Any]], version: str | None) -> int:
        if version is None:
            return -1
        for idx, item in enumerate(versions):
            if item.get("version") == version:
                return idx
        return -1

    def _build_pending_versions(self, plan: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        plan_versions = plan.get("versions") if isinstance(plan.get("versions"), list) else []
        state_versions = state.get("versions") if isinstance(state.get("versions"), list) else []

        current_index = self._resolve_current_version_index(plan_versions, state)
        current_state_status = self._status_at_index(state_versions, current_index)
        include_current = current_index >= 0 and current_state_status == "NOT_STARTED"

        pending_versions: list[dict[str, Any]] = []
        for index, plan_version in enumerate(plan_versions):
            if not isinstance(plan_version, dict):
                continue
            enabled = plan_version.get("enabled", True) is not False
            if not enabled:
                continue
            state_status = self._status_at_index(state_versions, index)
            if state_status != "NOT_STARTED":
                continue
            if current_index >= 0:
                if index < current_index:
                    continue
                if index == current_index and not include_current:
                    continue
            allowed_files = plan_version.get("allowed_files") if isinstance(plan_version.get("allowed_files"), list) else []
            acceptance_commands = (
                plan_version.get("acceptance_commands")
                if isinstance(plan_version.get("acceptance_commands"), list)
                else []
            )
            version_value = str(plan_version.get("version") or "").strip()
            if not version_value:
                continue
            pending_versions.append(
                {
                    "version": version_value,
                    "name": str(plan_version.get("name") or version_value),
                    "index": index,
                    "status": state_status,
                    "enabled": enabled,
                    "prompt_file": plan_version.get("prompt_file"),
                    "allowed_files_count": len(allowed_files),
                    "acceptance_command_count": len(acceptance_commands),
                }
            )

        next_not_started_version = pending_versions[0]["version"] if pending_versions else None
        return {
            "has_pending_versions": bool(pending_versions),
            "pending_versions": pending_versions,
            "next_not_started_version": next_not_started_version,
            "pending_count": len(pending_versions),
        }

    def _resolve_current_version_index(self, versions: list[dict[str, Any]], state: dict[str, Any]) -> int:
        raw_index = state.get("current_version_index")
        if isinstance(raw_index, int) and 0 <= raw_index < len(versions):
            return raw_index
        current_version = state.get("current_version")
        return self._find_version_index(versions, current_version)

    def _status_at_index(self, state_versions: list[dict[str, Any]], index: int) -> str:
        if index < 0 or index >= len(state_versions):
            return ""
        item = state_versions[index]
        if not isinstance(item, dict):
            return ""
        return str(item.get("status") or "")

    def _get_unreconciled_direct_versions_summary(self, project_root: str) -> dict[str, Any]:
        result = GitHistoryReconcileScanner(project_root).scan_unreconciled_candidates()
        if not isinstance(result, dict) or not result.get("ok"):
            return {
                "unreconciled_direct_version_count": 0,
                "unreconciled_direct_versions": [],
                "scan_limit": 20,
            }
        candidates = result.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        summary_items: list[dict[str, Any]] = []
        for item in candidates[:5]:
            if not isinstance(item, dict):
                continue
            summary_items.append(
                {
                    "version": item.get("version"),
                    "commit_hash_short": item.get("commit_hash_short"),
                    "commit_message": item.get("commit_message"),
                    "ambiguous": bool(item.get("ambiguous")),
                }
            )
        return {
            "unreconciled_direct_version_count": len(candidates),
            "unreconciled_direct_versions": summary_items,
            "scan_limit": int(result.get("scan_limit", 20)),
        }

    def _normalize_path(self, path: str) -> str:
        return glob_normalize(path)

    def _normalize_doc_file_path(self, raw_path: str) -> str | None:
        value = raw_path.strip().replace("\\", "/")
        if value.startswith("./"):
            value = value[2:]
        pure = PurePosixPath(value)
        if pure.is_absolute():
            return None
        if any(part in ("", ".", "..") for part in pure.parts):
            return None
        return str(pure)

    def _is_allowed_doc_path(self, rel_path: str) -> bool:
        value = rel_path.strip()
        if value in {"AGENTS.md", "README.md", "docs/ARCHITECTURE.md", "docs/DEVELOPMENT_PLAN.md", "docs/Prompt.md"}:
            return True
        if value.startswith("docs/") and value.endswith(".md"):
            return True
        if any(value.startswith(f"{dirname}/prompts/") for dirname in project_runner_dirnames()) and value.endswith(".md"):
            return True
        return False

    def _bounded_int(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except Exception:
            return default
        return max(minimum, min(parsed, maximum))

    def _normalize_heading_text(self, heading: str) -> str:
        value = heading.strip()
        value = re.sub(r"^#+\s*", "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip().lower()

    def _extract_markdown_section(self, text: str, heading_query: str) -> dict[str, Any]:
        lines = text.splitlines()
        heading_pattern = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
        headings: list[dict[str, Any]] = []
        for idx, line in enumerate(lines):
            match = heading_pattern.match(line)
            if not match:
                continue
            raw_title = match.group(2).strip()
            headings.append(
                {
                    "line_index": idx,
                    "line_no": idx + 1,
                    "level": len(match.group(1)),
                    "raw_line": line.strip(),
                    "title_norm": self._normalize_heading_text(raw_title),
                }
            )

        target_norm = self._normalize_heading_text(heading_query)
        target: dict[str, Any] | None = None
        for item in headings:
            title_norm = str(item.get("title_norm", ""))
            if title_norm == target_norm or target_norm in title_norm:
                target = item
                break

        if target is None:
            return {
                "ok": False,
                "message": f"未找到 heading：{heading_query}",
                "available_headings": [item.get("raw_line", "") for item in headings[:200]],
            }

        start_idx = int(target["line_index"])
        start_level = int(target["level"])
        end_idx = len(lines)
        for item in headings:
            idx = int(item["line_index"])
            if idx <= start_idx:
                continue
            if int(item["level"]) <= start_level:
                end_idx = idx
                break

        content = "\n".join(lines[start_idx:end_idx]).strip("\n")
        return {
            "ok": True,
            "matched_heading": target.get("raw_line"),
            "start_line": int(target.get("line_no", 1)),
            "end_line": end_idx,
            "content": content,
        }

    def _matches_any(self, path: str, patterns: list[str]) -> bool:
        return glob_match_any(path, patterns)

    def _run_git(self, args: list[str], cwd: str) -> dict[str, Any]:
        rc, stdout, stderr = _run_git(args, cwd)
        return {"code": rc, "stdout": stdout, "stderr": stderr}

    def _parse_version_num(self, value: str | None) -> tuple[int, ...] | None:
        if not value or not isinstance(value, str):
            return None
        m = re.match(r"^v(\d+(?:\.\d+)*)$", value.strip())
        if not m:
            return None
        return tuple(int(x) for x in m.group(1).split("."))
