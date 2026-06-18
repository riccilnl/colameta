from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from runner.runner_paths import user_config_dir


CREDENTIAL_FILENAME = "cloud-agent.json"


@dataclass
class CloudAgentCredential:
    device_id: str
    relay_url: str
    agent_token: str
    scopes: list[str]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CloudAgentCredential:
        return cls(
            device_id=str(data.get("device_id", "")),
            relay_url=str(data.get("relay_url", "")),
            agent_token=str(data.get("agent_token", "")),
            scopes=list(data.get("scopes", [])),
            created_at=str(data.get("created_at", "")),
        )

    def mask_sensitive(self) -> dict[str, Any]:
        d = self.to_dict()
        token = d.get("agent_token", "")
        if len(token) > 8:
            d["agent_token"] = token[:4] + "..." + token[-4:]
        else:
            d["agent_token"] = "***"
        return d


def _credential_path() -> str:
    return os.path.join(user_config_dir(), CREDENTIAL_FILENAME)


def save_credential(credential: CloudAgentCredential) -> dict[str, Any]:
    path = _credential_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(credential.to_dict(), f, indent=2, ensure_ascii=False)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error_code": "WRITE_FAILED", "message": str(e)}


def load_credential() -> dict[str, Any]:
    path = _credential_path()
    if not os.path.exists(path):
        return {"ok": False, "error_code": "NOT_FOUND", "message": "未找到 cloud agent credential，请先执行 colameta cloud pair。"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        credential = CloudAgentCredential.from_dict(data)
        return {"ok": True, "credential": credential}
    except Exception as e:
        return {"ok": False, "error_code": "READ_FAILED", "message": str(e)}


def delete_credential() -> dict[str, Any]:
    path = _credential_path()
    if not os.path.exists(path):
        return {"ok": True, "message": "credential 文件不存在，无需删除。"}
    try:
        os.remove(path)
        return {"ok": True, "message": "credential 已删除。"}
    except Exception as e:
        return {"ok": False, "error_code": "DELETE_FAILED", "message": str(e)}
