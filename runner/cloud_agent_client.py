from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from runner.cloud_pairing import CloudAgentCredential, load_credential
from runner.mcp_server import MCPPlanningBridgeServer, PROJECT_NAME_REQUIRED_TOOLS


RELATED_ERROR_CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
RELATED_ERROR_CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
RELATED_ERROR_REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
RELATED_ERROR_TRANSPORT_CLOSED = "TRANSPORT_CLOSED"
RELATED_ERROR_INVALID_REQUEST = "INVALID_REQUEST"
RELATED_ERROR_TOOL_FAILED = "TOOL_FAILED"
RELATED_ERROR_BRIDGE_BUSY = "BRIDGE_BUSY"
RELATED_ERROR_HIGH_RISK_BLOCKED = "HIGH_RISK_ACTION_BLOCKED"
RELATED_ERROR_CONFIRMATION_REQUIRED = "LOCAL_CONFIRMATION_REQUIRED"

HIGH_RISK_SCOPES = frozenset({"mcp:commit", "mcp:plan"})


@dataclass
class RelayRequest:
    request_id: str
    tool_name: str
    arguments: dict[str, Any]
    scopes: list[str]
    project_name: str | None = None
    confirmation_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "scopes": self.scopes,
            "project_name": self.project_name,
        }
        if self.confirmation_token is not None:
            result["confirmation_token"] = self.confirmation_token
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RelayRequest:
        return cls(
            request_id=str(data.get("request_id", "")),
            tool_name=str(data.get("tool_name", "")),
            arguments=dict(data.get("arguments", {})),
            scopes=list(data.get("scopes", [])),
            project_name=data.get("project_name"),
            confirmation_token=data.get("confirmation_token"),
        )


@dataclass
class RelayResponse:
    request_id: str
    ok: bool
    tool: str
    data: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "request_id": self.request_id,
            "ok": self.ok,
            "tool": self.tool,
        }
        if self.data is not None:
            result["data"] = self.data
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.message is not None:
            result["message"] = self.message
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RelayResponse:
        return cls(
            request_id=str(data.get("request_id", "")),
            ok=bool(data.get("ok", False)),
            tool=str(data.get("tool", "")),
            data=data.get("data"),
            error_code=data.get("error_code"),
            message=data.get("message"),
        )


@dataclass
class ReceiveResult:
    message: dict[str, Any] | None = None
    timeout: bool = False
    closed: bool = False
    error: str | None = None


class RelayTransport(ABC):
    @abstractmethod
    def receive(self, *, timeout: float | None = None) -> ReceiveResult:
        ...

    @abstractmethod
    def send(self, response: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class MockRelayTransport(RelayTransport):
    def __init__(self) -> None:
        self._queue: list[dict[str, Any]] = []
        self._sent: list[dict[str, Any]] = []
        self._closed = False

    def enqueue(self, message: dict[str, Any]) -> None:
        self._queue.append(message)

    def receive(self, *, timeout: float | None = None) -> ReceiveResult:
        if self._closed:
            return ReceiveResult(closed=True)
        if self._queue:
            return ReceiveResult(message=self._queue.pop(0))
        return ReceiveResult(timeout=True)

    def send(self, response: dict[str, Any]) -> None:
        self._sent.append(response)

    def close(self) -> None:
        self._closed = True

    @property
    def sent_messages(self) -> list[dict[str, Any]]:
        return list(self._sent)

    @property
    def is_closed(self) -> bool:
        return self._closed


class CloudRelayToolBridge:
    def __init__(self, project_root: str, *, service_mode: bool = True, confirmation_token: str | None = None):
        self.project_root = os.path.abspath(os.path.expanduser(project_root))
        self.service_mode = service_mode
        self.confirmation_token = confirmation_token
        self._server: MCPPlanningBridgeServer | None = None

    def _get_server(self) -> MCPPlanningBridgeServer:
        if self._server is None:
            self._server = MCPPlanningBridgeServer(self.project_root, service_mode=self.service_mode)
        return self._server

    def _check_safety_policy(
        self,
        request: RelayRequest,
        effective_scopes: list[str],
    ) -> RelayResponse | None:
        server = self._get_server()
        required_scope = server.get_required_scope_for_tool(request.tool_name, request.arguments)

        if required_scope not in HIGH_RISK_SCOPES:
            return None

        if required_scope not in effective_scopes:
            return None

        if self.confirmation_token is not None and self.confirmation_token == request.confirmation_token:
            return None

        return RelayResponse(
            request_id=request.request_id,
            ok=False,
            tool=request.tool_name,
            error_code=RELATED_ERROR_HIGH_RISK_BLOCKED,
            message=f"高危操作被拦截：{request.tool_name} 需要 {required_scope}，但未提供本地确认。",
        )

    def handle_relay_request(self, request: RelayRequest, credential: CloudAgentCredential) -> RelayResponse:
        if not request.tool_name:
            return RelayResponse(
                request_id=request.request_id,
                ok=False,
                tool="",
                error_code="INVALID_TOOL",
                message="tool_name 不能为空。",
            )

        if self.service_mode and request.tool_name in PROJECT_NAME_REQUIRED_TOOLS:
            project_name = request.arguments.get("project_name")
            if not isinstance(project_name, str) or not project_name.strip():
                return RelayResponse(
                    request_id=request.request_id,
                    ok=False,
                    tool=request.tool_name,
                    error_code="PROJECT_NAME_REQUIRED",
                    message="服务模式下项目级工具必须显式提供 project_name。",
                )

        effective_scopes = [s for s in request.scopes if s in credential.scopes]

        blocked = self._check_safety_policy(request, effective_scopes)
        if blocked is not None:
            return blocked

        auth_context = {
            "mode": "cloud-relay",
            "device_id": credential.device_id,
            "scopes": effective_scopes,
        }

        server = self._get_server()
        result = server.call_tool_for_agent(request.tool_name, request.arguments, auth_context)

        return RelayResponse(
            request_id=request.request_id,
            ok=result.get("ok", False),
            tool=result.get("tool", request.tool_name),
            data=result.get("data"),
            error_code=result.get("error_code"),
            message=result.get("message"),
        )

    def process_relay_message(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_message, dict):
            return {
                "request_id": "",
                "ok": False,
                "error_code": RELATED_ERROR_INVALID_REQUEST,
                "message": "请求格式错误: 非 dict 消息",
            }

        cred_result = load_credential()
        if not cred_result.get("ok"):
            return {
                "request_id": str(raw_message.get("request_id", "")),
                "ok": False,
                "error_code": RELATED_ERROR_CREDENTIAL_MISSING,
                "message": cred_result.get("message", "未找到 credential"),
            }

        credential: CloudAgentCredential = cred_result["credential"]
        try:
            request = RelayRequest.from_dict(raw_message)
        except Exception as e:
            return {
                "request_id": str(raw_message.get("request_id", "")),
                "ok": False,
                "error_code": RELATED_ERROR_INVALID_REQUEST,
                "message": f"请求格式错误: {e}",
            }

        response = self.handle_relay_request(request, credential)
        return response.to_dict()

    def process_messages(
        self,
        transport: RelayTransport,
        *,
        max_count: int | None = None,
    ) -> int:
        count = 0
        while max_count is None or count < max_count:
            result = transport.receive(timeout=0)
            if result.closed:
                break
            if result.timeout:
                break
            if result.error:
                transport.send({
                    "request_id": "",
                    "ok": False,
                    "error_code": RELATED_ERROR_TRANSPORT_CLOSED,
                    "message": result.error,
                })
                break
            raw = result.message
            if raw is None:
                break
            response = self.process_relay_message(raw)
            transport.send(response)
            count += 1
        return count

    def process_one_message(
        self,
        transport: RelayTransport,
        *,
        timeout: float | None = None,
    ) -> bool:
        result = transport.receive(timeout=timeout)
        if result.closed or result.timeout or result.error:
            return False
        raw = result.message
        if raw is None:
            return False
        response = self.process_relay_message(raw)
        transport.send(response)
        return True
