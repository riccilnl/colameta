import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from runner._internal_utils import now_iso as _now_iso
from runner.confirmation_store import ConfirmationStore
from runner.core_confirmation import confirmation_apply_guard, confirmation_fact_from_preview_artifact
from runner.param_utils import bounded_int
from runner.runner_paths import (
    primary_project_runner_relpath,
    resolve_project_runner_path,
    resolve_project_runner_rel_dir,
)
from runner.tool_result import apply_result, error_result, ok_result, preview_result


PREVIEW_TTL_SECONDS = 3600
PREVIEWS_DIR = "prompt-file-previews"
PROMPTS_DIR = primary_project_runner_relpath("prompts")
PREVIEWS_RELATIVE_DIR = os.path.join("runtime", PREVIEWS_DIR)


class MCPPromptFileError(Exception):
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        self.message = message


class MCPPromptFileManager:
    def __init__(self, project_root: str):
        self.project_root = os.path.abspath(os.path.expanduser(project_root))
        self._previews_root = resolve_project_runner_path(self.project_root, PREVIEWS_RELATIVE_DIR)
        preview_dir = os.path.join(resolve_project_runner_rel_dir(self.project_root), PREVIEWS_RELATIVE_DIR)
        self._store = ConfirmationStore(self.project_root, preview_dir, PREVIEW_TTL_SECONDS)

    def handle(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "preview":
            try:
                return self._preview(params)
            except MCPPromptFileError as e:
                return {"ok": False, "error_code": e.error_code, "message": e.message}
        if action == "apply":
            return self._apply(params)
        if action == "status":
            return self._status(params)
        if action == "discard":
            return self._discard(params)
        return error_result("UNKNOWN_ACTION", "不支持的 action。支持：preview、apply、status、discard。")

    def _validate_version(self, version: str) -> str:
        if not isinstance(version, str) or not version.strip():
            raise MCPPromptFileError("VERSION_REQUIRED", "version 不能为空。")
        v = version.strip()
        if "/" in v or "\\" in v or ".." in v or v.startswith("."):
            raise MCPPromptFileError("INVALID_VERSION", "version 包含非法字符（/、\\、..）。")
        if os.path.isabs(v):
            raise MCPPromptFileError("INVALID_VERSION", "version 不能是绝对路径。")
        return v

    def _target_path(self, version: str) -> str:
        return resolve_project_runner_path(self.project_root, "prompts", f"{version}.md")

    def _normalize_string_list(self, value: Any, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise MCPPromptFileError("INVALID_METADATA", f"{field} 必须是字符串列表。")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise MCPPromptFileError("INVALID_METADATA", f"{field} 必须是非空字符串列表。")
            if "\n" in item or "\r" in item:
                raise MCPPromptFileError("INVALID_METADATA", f"{field} 不能包含换行符。")
            result.append(item.strip())
        return result

    def _normalize_acceptance_commands(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise MCPPromptFileError("INVALID_METADATA", "acceptance_commands 必须是列表。")
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                command = item.strip()
            elif isinstance(item, dict):
                command = str(item.get("command") or "").strip()
            else:
                command = ""
            if not command:
                raise MCPPromptFileError("INVALID_METADATA", "acceptance_commands 必须包含非空 command。")
            if "\n" in command or "\r" in command:
                raise MCPPromptFileError("INVALID_METADATA", "acceptance_commands command 不能包含换行符。")
            result.append(command)
        return result

    def _normalize_execution(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MCPPromptFileError("INVALID_METADATA", "execution 必须是对象。")
        provider = str(value.get("provider") or "").strip()
        if not provider:
            return {}
        if provider not in ("pi", "codex", "opencode"):
            raise MCPPromptFileError("INVALID_METADATA", "execution.provider 必须是 pi、codex 或 opencode。")
        return {"provider": provider}

    def _normalize_optional_bool(self, params: dict[str, Any], field: str) -> bool | None:
        if field not in params or params.get(field) is None:
            return None
        value = params.get(field)
        if not isinstance(value, bool):
            raise MCPPromptFileError("INVALID_METADATA", f"{field} 必须是布尔值。")
        return value

    def _insert_from_prompt_file_params(
        self,
        prompt_filename: str,
        plan_metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "action": "insert_from_prompt_file_preview",
            "prompt_file": prompt_filename,
        }
        if not isinstance(plan_metadata, dict):
            return params
        for field in (
            "insert_after",
            "version",
            "name",
            "description",
            "allowed_files",
            "forbidden_files",
            "acceptance_commands",
            "manual_acceptance",
            "out_of_scope",
            "context_files",
            "execution",
            "allow_no_changes",
        ):
            if field in plan_metadata and plan_metadata.get(field) is not None:
                params[field] = plan_metadata.get(field)
        return params

    def _strip_existing_front_matter(self, content: str) -> str:
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return content
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                return "\n".join(lines[idx + 1:])
        return content

    def _with_front_matter(self, content: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        allowed_files = self._normalize_string_list(params.get("allowed_files"), "allowed_files")
        acceptance_commands = self._normalize_acceptance_commands(params.get("acceptance_commands"))
        execution = self._normalize_execution(params.get("execution"))
        allow_no_changes = self._normalize_optional_bool(params, "allow_no_changes")
        if not allowed_files and not acceptance_commands and not execution and allow_no_changes is None:
            return content, {}
        body = self._strip_existing_front_matter(content).lstrip("\n")
        lines = ["---"]
        metadata: dict[str, Any] = {}
        if allowed_files:
            lines.append("allowed_files:")
            lines.extend([f"  - {item}" for item in allowed_files])
            metadata["allowed_files"] = allowed_files
        if acceptance_commands:
            lines.append("acceptance_commands:")
            lines.extend([f"  - {item}" for item in acceptance_commands])
            metadata["acceptance_commands"] = acceptance_commands
        if allow_no_changes is not None:
            lines.append(f"allow_no_changes: {'true' if allow_no_changes else 'false'}")
            metadata["allow_no_changes"] = allow_no_changes
        if execution:
            lines.append("execution:")
            lines.append(f"  provider: {execution['provider']}")
            metadata["execution"] = execution
        lines.append("---")
        lines.append(body)
        return "\n".join(lines), metadata

    def _preview(self, params: dict[str, Any]) -> dict[str, Any]:
        version = self._validate_version(params.get("version", ""))
        content = params.get("content")
        if not isinstance(content, str) or not content.strip():
            return error_result("CONTENT_REQUIRED", "content 不能为空。")
        plan_metadata = params.get("plan_metadata")
        if plan_metadata is not None and not isinstance(plan_metadata, dict):
            return error_result("INVALID_METADATA", "plan_metadata 必须是对象。")
        write_front_matter = bool(params.get("write_front_matter", True))
        try:
            if write_front_matter:
                content, metadata = self._with_front_matter(content, params)
            else:
                metadata = {}
        except MCPPromptFileError as e:
            return error_result(e.error_code, e.message)
        overwrite = bool(params.get("overwrite", False))
        reason = params.get("reason")
        max_preview_chars = bounded_int(params.get("max_preview_chars"), 200, 1, 5000)

        target_file = self._target_path(version)
        target_rel = os.path.relpath(target_file, self.project_root)

        warnings: list[str] = []
        if os.path.isfile(target_file) and not overwrite:
            warnings.append("目标文件已存在且 overwrite=false，apply 将不会覆盖。")
        elif os.path.isfile(target_file) and overwrite:
            warnings.append("目标文件已存在，apply 将覆盖。")

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        content_preview = content[:max_preview_chars]

        preview_id = self._generate_preview_id(version)
        created_at = _now_iso()
        expires_at = self._now_iso_ts(PREVIEW_TTL_SECONDS)

        preview_record = {
            "preview_id": preview_id,
            "action": "prompt_file_preview",
            "project_root": self.project_root,
            "version": version,
            "target_file": target_rel,
            "content_hash": content_hash,
            "content": content,
            "overwrite": overwrite,
            "reason": reason,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        if isinstance(plan_metadata, dict):
            preview_record["plan_metadata"] = plan_metadata
        self._write_preview(preview_id, preview_record)

        result: dict[str, Any] = preview_result(
            "preview",
            preview_id,
            status="preview_ready",
            risk_level="preview",
            version=version,
            target_file=target_rel,
            content_hash=content_hash,
            content_preview=content_preview,
            created_at=created_at,
            expires_at=expires_at,
            overwrite=overwrite,
            warnings=warnings,
            recommended_next_action={
                "tool": "manage_prompt_file",
                "action": "apply",
                "params": {"action": "apply", "preview_id": preview_id},
                "reason": "使用 preview_id 应用此 prompt 文件。",
                "requires_confirmation": True,
            },
        )
        if metadata:
            result["front_matter"] = metadata
        if isinstance(plan_metadata, dict):
            result["plan_metadata"] = plan_metadata
        result["next_actions"] = [
            {
                "tool": "manage_prompt_file",
                "action": "discard",
                "params": {"action": "discard", "preview_id": preview_id},
                "reason": "废弃此错误 preview，不写入文件。",
                "requires_confirmation": True,
            },
        ]
        if reason:
            result["reason"] = reason
        return result

    def _apply(self, params: dict[str, Any]) -> dict[str, Any]:
        preview_id = params.get("preview_id", "")
        if not isinstance(preview_id, str) or not preview_id.strip():
            return error_result("PREVIEW_ID_REQUIRED", "apply 需要 preview_id。请先调用 preview 获取。")
        preview_id = preview_id.strip()

        guard = confirmation_apply_guard(self._store, preview_id, project_root=self.project_root)
        if not guard["ok"]:
            ec = guard["error_code"]
            if ec == "PREVIEW_NOT_FOUND":
                return error_result("PREVIEW_NOT_FOUND", f"preview_id={preview_id} 不存在或已过期。请重新调用 preview。")
            if ec == "PROJECT_MISMATCH":
                return error_result("PROJECT_ROOT_MISMATCH", "preview 项目根目录与当前不匹配。")
            if ec == "PREVIEW_EXPIRED":
                self._delete_preview(preview_id)
                return error_result("PREVIEW_EXPIRED", f"preview_id={preview_id} 已过期。请重新调用 preview。")
        preview = guard["payload"]

        version = str(preview.get("version") or "")
        try:
            version = self._validate_version(version)
        except MCPPromptFileError as e:
            return error_result(e.error_code, e.message)

        target_file = self._target_path(version)
        target_rel = os.path.relpath(target_file, self.project_root)
        overwrite = bool(preview.get("overwrite", False))

        if os.path.isfile(target_file) and not overwrite:
            return error_result("FILE_EXISTS", f"目标文件 {target_rel} 已存在且 overwrite=false。请使用 overwrite=true 重新 preview。")

        content = str(preview.get("content") or "")
        if not content.strip():
            return error_result("CONTENT_EMPTY", "preview 记录中 content 为空。")

        prompts_dir = resolve_project_runner_path(self.project_root, "prompts")
        os.makedirs(prompts_dir, exist_ok=True)
        tmp = ""
        try:
            fd, tmp = tempfile.mkstemp(prefix=".prompt-", suffix=".md", dir=prompts_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, target_file)
        except Exception:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            return error_result("WRITE_FAILED", f"写入目标文件 {target_rel} 失败。")

        self._delete_preview(preview_id)

        prompt_filename = os.path.basename(target_file)
        plan_metadata = preview.get("plan_metadata")
        insert_params = self._insert_from_prompt_file_params(prompt_filename, plan_metadata)

        result = apply_result(
            "apply",
            preview_id,
            status="succeeded",
            risk_level="commit",
            version=version,
            target_file=target_rel,
            message=f"Prompt 文件已写入 {target_rel}。",
            next_actions=[
                {
                    "tool": "manage_prompt_file",
                    "action": "status",
                    "params": {"action": "status", "preview_id": preview_id},
                    "reason": "确认文件写入状态。",
                    "requires_confirmation": False,
                },
                {
                    "tool": "manage_plan_version",
                    "action": "insert_from_prompt_file_preview",
                    "params": insert_params,
                    "reason": f"从 prompt 文件 {prompt_filename} 生成 plan insert preview。",
                    "requires_confirmation": True,
                },
            ],
        )
        if isinstance(plan_metadata, dict):
            result["plan_metadata"] = plan_metadata
        return result

    def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        preview_id = params.get("preview_id", "")
        if not isinstance(preview_id, str) or not preview_id.strip():
            return error_result("PREVIEW_ID_REQUIRED", "status 需要 preview_id。")
        preview_id = preview_id.strip()

        preview = self._read_preview(preview_id)
        if preview is None:
            return error_result("PREVIEW_NOT_FOUND", f"preview_id={preview_id} 不存在或已过期。")

        version = str(preview.get("version") or "")
        target_file = self._target_path(version)
        target_rel = os.path.relpath(target_file, self.project_root)
        content_hash = str(preview.get("content_hash") or "")
        created_at = str(preview.get("created_at") or "")
        expires_at = str(preview.get("expires_at") or "")
        now = _now_iso()
        expired = bool(expires_at and now > expires_at)
        exists = os.path.isfile(target_file)

        warnings: list[str] = []
        if expired:
            warnings.append("preview 已过期，apply 将失败。请重新调用 preview。")
        if exists:
            warnings.append("目标文件已存在。")

        result = ok_result(
            "status",
            status="succeeded",
            risk_level="info",
            preview_id=preview_id,
            version=version,
            target_file=target_rel,
            content_hash=content_hash,
            created_at=created_at,
            expires_at=expires_at,
            expired=expired,
            exists=exists,
            warnings=warnings,
        )

        fact = confirmation_fact_from_preview_artifact(preview)
        if fact is not None:
            result["confirmation"] = fact.to_dict()

        return result

    def _discard(self, params: dict[str, Any]) -> dict[str, Any]:
        preview_id = params.get("preview_id", "")
        if not isinstance(preview_id, str) or not preview_id.strip():
            return error_result("PREVIEW_ID_REQUIRED", "discard 需要 preview_id。请先调用 preview 获取。")
        preview_id = preview_id.strip()

        preview = self._read_preview(preview_id)
        if preview is None:
            return error_result("PREVIEW_NOT_FOUND", f"preview_id={preview_id} 不存在或已过期。")

        if preview.get("project_root") != self.project_root:
            return error_result("PROJECT_ROOT_MISMATCH", "preview 项目根目录与当前不匹配。")

        version = str(preview.get("version") or "")
        try:
            version = self._validate_version(version)
        except MCPPromptFileError as e:
            return error_result(e.error_code, e.message)
        target_file = self._target_path(version)
        target_rel = os.path.relpath(target_file, self.project_root)
        content_hash = str(preview.get("content_hash") or "")

        self._delete_preview(preview_id)

        return ok_result(
            "discard",
            status="succeeded",
            preview_id=preview_id,
            target_file=target_rel,
            content_hash=content_hash,
            message=f"Prompt 预览已废弃 preview_id={preview_id}。",
        )

    def _generate_preview_id(self, version: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_version = version.replace(".", "_").replace("-", "_")[:20]
        return f"prompt_preview_{safe_version}_{ts}_{os.urandom(4).hex()}"

    def _write_preview(self, preview_id: str, record: dict[str, Any]) -> None:
        self._store.write(preview_id, record)

    def _read_preview(self, preview_id: str) -> dict[str, Any] | None:
        return self._store.read(preview_id)

    def _delete_preview(self, preview_id: str) -> None:
        self._store.delete(preview_id)

    def _now_iso_ts(self, add_seconds: int = 0) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=add_seconds)).astimezone().isoformat()
