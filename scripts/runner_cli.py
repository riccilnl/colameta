import json
import os
import sys
import getpass
import signal
import subprocess
import threading
import time
import urllib.request
import webbrowser

# 允许直接执行 scripts/runner_cli.py 时导入仓库内模块。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts import runner_cli_env as cli_env
from scripts import runner_cli_output as cli_output
from scripts import runner_cli_registry as cli_registry
from scripts.runner_cli_usage import SIMPLE_START_MODES as _SIMPLE_START_MODES
from scripts.runner_cli_usage import USAGE_MESSAGE
from runner.mcp_decisions import MCPDecisionRecordsManager
from runner.http_server_utils import is_tcp_port_bindable, wait_for_tcp_port_bindable
from runner.runner_global_config import RunnerGlobalConfigStore
from runner.project_registry import ProjectRegistry, PROJECT_MODE_MANAGED
from runner.runner_paths import (
    PRIMARY_USER_CONFIG_DIRNAME,
    resolve_project_runner_plan_path,
    resolve_project_runner_rel_dir,
)
from runner.service_lifecycle_store import ServiceLifecycleStore


SERVICE_WAIT_TIMEOUT_SECONDS = 2.0
SERVICE_STOP_WAIT_TIMEOUT_SECONDS = 10.0
SERVICE_STOP_PROCESS_EXIT_TIMEOUT_SECONDS = 1.0
SERVICE_START_HEALTH_TIMEOUT_SECONDS = 15.0
SERVICE_WAIT_INTERVAL_SECONDS = 0.1
SERVICE_START_HEALTH_INITIAL_DELAY_SECONDS = 0.3
SERVICE_METADATA_FILENAME = "service.json"
SERVICE_PID_FILENAME = "service.pid"
SERVICE_LOG_FILENAME = "service.log"

def _resolve_path(path: str) -> str:
    return cli_env.resolve_path(path)


def _project_config_key(project_path: str) -> str:
    return cli_env.project_config_key(project_path, resolve_path_func=_resolve_path)


def _project_config_dir(project_path: str) -> str:
    return cli_env.project_config_dir(
        project_path,
        store_factory=RunnerGlobalConfigStore,
        resolve_path_func=_resolve_path,
    )


def _config_store_for_project(project_path: str | None = None) -> RunnerGlobalConfigStore:
    return cli_env.config_store_for_project(project_path, store_factory=RunnerGlobalConfigStore)


def _load_global_runner_config(
    command_name: str,
    include_auth_token: bool = True,
    project_path: str | None = None,
) -> dict[str, object]:
    return cli_env.load_global_runner_config(
        command_name,
        include_auth_token=include_auth_token,
        project_path=project_path,
        config_store_for_project_func=_config_store_for_project,
        store_factory=RunnerGlobalConfigStore,
    )

def _resolve_auth_mode(auth_mode: str | None, auth_token: str | None) -> str:
    return cli_env.resolve_auth_mode(auth_mode, auth_token)


def _validate_mcp_auth_options(
    command_name: str,
    auth_mode: str | None,
    auth_token: str | None,
    public_base_url: str | None,
    oauth_token_ttl_seconds: int,
) -> str | None:
    resolved = _resolve_auth_mode(auth_mode, auth_token)
    if resolved not in {"none", "token", "oauth"}:
        print(f"{command_name} 参数错误：--auth-mode 必须是 none、token 或 oauth。", file=sys.stderr)
        return None
    if resolved == "token" and not auth_token:
        print(f"{command_name} 参数错误：--auth-mode token 需要 --auth-token。", file=sys.stderr)
        return None
    if resolved == "oauth" and not public_base_url:
        print(f"{command_name} 参数错误：--auth-mode oauth 需要 --public-base-url。", file=sys.stderr)
        return None
    if oauth_token_ttl_seconds <= 0:
        print(f"{command_name} 参数错误：--oauth-token-ttl-seconds 必须是正整数。", file=sys.stderr)
        return None
    return resolved


def _extract_setup_relevant_options(args: list[str]) -> tuple[dict[str, object], set[str]]:
    return cli_env.extract_setup_relevant_options(args)


def _resolved_setup_config(command_name: str, args: list[str], project_path: str | None = None) -> dict[str, object]:
    return cli_env.resolved_setup_config(
        command_name,
        args,
        project_path=project_path,
        extract_setup_relevant_options_func=_extract_setup_relevant_options,
        load_global_runner_config_func=_load_global_runner_config,
    )


def _prompt_initial_global_config(
    store: RunnerGlobalConfigStore,
    *,
    input_func=input,
    getpass_func=getpass.getpass,
    stderr=None,
) -> dict[str, object]:
    return cli_env.prompt_initial_global_config(
        store,
        input_func=input_func,
        getpass_func=getpass_func,
        stderr=stderr or sys.stderr,
    )


def _maybe_interactive_global_config_setup(args: list[str], *, mode: str) -> int | None:
    return cli_env.maybe_interactive_global_config_setup(
        args,
        mode=mode,
        stdin=sys.stdin,
        stderr=sys.stderr,
        input_func=input,
        getpass_func=getpass.getpass,
        resolve_path_func=_resolve_path,
        resolved_setup_config_func=lambda command_name, argv, project_path: _resolved_setup_config(
            command_name,
            argv,
            project_path=project_path,
        ),
        config_store_for_project_func=_config_store_for_project,
        prompt_initial_global_config_func=_prompt_initial_global_config,
    )


def _is_port_available(host: str, port: int) -> bool:
    return is_tcp_port_bindable(host, port)


def _default_service_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _service_paths(project_path: str) -> dict[str, str]:
    return ServiceLifecycleStore(project_path).paths()


def _ensure_service_dir(project_path: str) -> dict[str, str]:
    return ServiceLifecycleStore(project_path).ensure_dir()


def _wait_for_port_available(
    host: str,
    port: int,
    *,
    label: str,
    timeout_seconds: float = SERVICE_STOP_WAIT_TIMEOUT_SECONDS,
) -> bool:
    def _on_wait(wait_timeout_seconds: float) -> None:
        print(
            f"{label} 正在等待端口 {host}:{port} 释放，最多 {wait_timeout_seconds:.1f} 秒。",
            file=sys.stderr,
        )

    return wait_for_tcp_port_bindable(
        host,
        port,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=SERVICE_WAIT_INTERVAL_SECONDS,
        on_wait=_on_wait,
    )


def _wait_for_process_exit(pid: int, *, timeout_seconds: float = SERVICE_STOP_PROCESS_EXIT_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while time.monotonic() < deadline:
        if not _is_pid_running(pid):
            return True
        time.sleep(SERVICE_WAIT_INTERVAL_SECONDS)
    return not _is_pid_running(pid)


def _is_pid_running(pid: int) -> bool:
    return ServiceLifecycleStore.is_pid_running(pid)


def _read_process_cmdline(pid: int) -> str | None:
    return ServiceLifecycleStore.read_process_cmdline(pid)


def _service_pid_matches_metadata(pid: int, metadata: dict[str, object]) -> bool | None:
    return ServiceLifecycleStore.pid_matches_metadata(
        pid,
        metadata,
        is_pid_running=_is_pid_running,
        read_process_cmdline=_read_process_cmdline,
    )


def _service_command_for_display(command: str, project_path: str | None = None) -> str:
    if project_path is None or os.path.realpath(project_path) == os.path.realpath(_default_service_project_root()):
        return f"./bin/colameta {command}".strip()
    return f'./bin/colameta {command} "{project_path}"'


def _read_service_metadata(project_path: str) -> dict[str, object] | None:
    return ServiceLifecycleStore(project_path).read_metadata()


def _write_service_metadata(project_path: str, payload: dict[str, object]) -> None:
    ServiceLifecycleStore(project_path).write_metadata(payload)


def _clear_service_metadata(project_path: str) -> None:
    ServiceLifecycleStore(project_path).clear_metadata()


def _probe_service_health(metadata: dict[str, object]) -> tuple[str | None, str | None]:
    web_state: str | None = None
    mcp_state: str | None = None
    if metadata.get("enable_web"):
        web_state = (
            "healthy"
            if _is_runner_web_console(str(metadata.get("web_host", "127.0.0.1")), int(metadata.get("web_port", 0)))
            else "starting"
        )
    if metadata.get("enable_mcp"):
        mcp_state = (
            "healthy"
            if _is_runner_mcp_server(str(metadata.get("mcp_host", "127.0.0.1")), int(metadata.get("mcp_port", 0)))
            else "starting"
        )
    return web_state, mcp_state


def _service_health_ready(metadata: dict[str, object], web_state: str | None, mcp_state: str | None) -> bool:
    web_ready = not metadata.get("enable_web") or web_state == "healthy"
    mcp_ready = not metadata.get("enable_mcp") or mcp_state == "healthy"
    return bool(web_ready and mcp_ready)


def _wait_for_service_health(
    metadata: dict[str, object],
    process: subprocess.Popen,
    *,
    timeout_seconds: float = SERVICE_START_HEALTH_TIMEOUT_SECONDS,
) -> tuple[bool, str, str | None, str | None]:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    web_state: str | None = None
    mcp_state: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False, "process_exited", web_state, mcp_state
        web_state, mcp_state = _probe_service_health(metadata)
        if _service_health_ready(metadata, web_state, mcp_state):
            return True, "healthy", web_state, mcp_state
        time.sleep(SERVICE_WAIT_INTERVAL_SECONDS)
    if process.poll() is not None:
        return False, "process_exited", web_state, mcp_state
    web_state, mcp_state = _probe_service_health(metadata)
    if _service_health_ready(metadata, web_state, mcp_state):
        return True, "healthy", web_state, mcp_state
    return False, "health_timeout", web_state, mcp_state


def _is_runner_web_console(host: str, port: int, timeout: int = 3) -> bool:
    try:
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{probe_host}:{port}/api/healthz"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return bool(data.get("service") == "colameta-web-console")
    except Exception:
        return False


def _is_runner_mcp_server(host: str, port: int, timeout: int = 3) -> bool:
    try:
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{probe_host}:{port}/healthz"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return bool(data.get("service") == "colameta-mcp")
    except Exception:
        return False


def _interactive_resolve_mcp_port(
    *,
    command_name: str,
    host: str,
    port: int,
    explicit_port: bool,
    input_func=None,
    known_occupied: bool = False,
) -> int | None:
    if input_func is None:
        input_func = input
    if not known_occupied and _is_port_available(host, port):
        return port

    flag = "--mcp-port" if command_name in {"serve", "default"} else "--port"
    if explicit_port:
        print(f"{command_name} 启动失败：端口 {host}:{port} 已被占用。", file=sys.stderr)
        print(f"请换一个端口，例如 {flag} {port + 1}。", file=sys.stderr)
        return None
    if _wait_for_port_available(host, port, label=command_name):
        return port
    if not sys.stdin.isatty():
        print(f"{command_name} 启动失败：默认端口 {host}:{port} 已被占用。", file=sys.stderr)
        print(f"非交互环境无法选择新端口；请显式指定 {flag}。", file=sys.stderr)
        return None

    candidate = port + 1
    print(f"{command_name} 检测到默认端口 {host}:{port} 已被占用。", file=sys.stderr)
    while True:
        raw = input_func(f"请输入新的 MCP HTTP 端口 [{candidate}]: ").strip()
        if not raw:
            selected = candidate
        else:
            try:
                selected = int(raw)
            except ValueError:
                print("端口必须是整数。", file=sys.stderr)
                continue
        if selected < 1 or selected > 65535:
            print("端口必须在 1..65535 范围内。", file=sys.stderr)
            continue
        if _is_port_available(host, selected):
            print(f"将使用 MCP HTTP 端口 {selected}。", file=sys.stderr)
            return selected
        print(f"端口 {host}:{selected} 也被占用，请再输入一个端口。", file=sys.stderr)
        candidate = selected + 1 if selected < 65535 else 8766


def _interactive_resolve_web_port(
    *,
    command_name: str,
    host: str,
    port: int,
    explicit_port: bool,
    input_func=None,
) -> int | None:
    if input_func is None:
        input_func = input
    if _is_port_available(host, port):
        return port

    flag = "--web-port"
    if explicit_port:
        print(f"{command_name} 启动失败：Web Console 端口 {host}:{port} 已被占用。", file=sys.stderr)
        print(f"请换一个端口，例如 {flag} {port + 1}。", file=sys.stderr)
        return None
    if _wait_for_port_available(host, port, label=command_name):
        return port
    if not sys.stdin.isatty():
        print(f"{command_name} 启动失败：Web Console 默认端口 {host}:{port} 已被占用。", file=sys.stderr)
        print(f"非交互环境无法选择新端口；请显式指定 {flag}。", file=sys.stderr)
        return None

    candidate = port + 1
    print(f"{command_name} 检测到 Web Console 默认端口 {host}:{port} 已被占用。", file=sys.stderr)
    while True:
        raw = input_func(f"请输入新的 Web Console 端口 [{candidate}]: ").strip()
        if not raw:
            selected = candidate
        else:
            try:
                selected = int(raw)
            except ValueError:
                print("端口必须是整数。", file=sys.stderr)
                continue
        if selected < 1 or selected > 65535:
            print("端口必须在 1..65535 范围内。", file=sys.stderr)
            continue
        if _is_port_available(host, selected):
            print(f"将使用 Web Console 端口 {selected}。", file=sys.stderr)
            return selected
        print(f"端口 {host}:{selected} 也被占用，请再输入一个端口。", file=sys.stderr)
        candidate = selected + 1 if selected < 65535 else 8800


def _print_line(label: str, text: str) -> None:
    cli_output.print_line(label, text, stderr=sys.stderr)


def _print_mcp_auth_status(auth_mode: str, public_base_url: str | None = None) -> None:
    cli_output.print_mcp_auth_status(auth_mode, public_base_url, stderr=sys.stderr)


def _read_runtime_public_base_url(project_path: str) -> str | None:
    return cli_env.read_runtime_public_base_url(project_path, path_module=os.path)


def _resolve_public_base_url(project_path: str, cli_value: str | None) -> tuple[str | None, str]:
    return cli_env.resolve_public_base_url(
        project_path,
        cli_value,
        config_store_for_project_func=_config_store_for_project,
        store_factory=RunnerGlobalConfigStore,
        read_runtime_public_base_url_func=_read_runtime_public_base_url,
    )


def _normalize_public_base_url(url: str) -> str:
    return cli_env.normalize_public_base_url(url)


def _is_local_http_url(url: str) -> bool:
    return cli_env.is_local_http_url(url)


def _print_registry_project_list() -> None:
    cli_output.print_registry_project_list(registry_factory=ProjectRegistry, stderr=sys.stderr)


def _print_default_start_summary(
    project_path: str,
    web_host: str,
    web_port: int,
    mcp_host: str,
    mcp_port: int,
    public_base_url: str | None,
    public_base_url_source: str,
    enable_web: bool,
    enable_mcp: bool,
    open_web: bool,
    web_disabled_reason: str | None = None,
) -> None:
    cli_output.print_default_start_summary(
        project_path=project_path,
        web_host=web_host,
        web_port=web_port,
        mcp_host=mcp_host,
        mcp_port=mcp_port,
        public_base_url=public_base_url,
        public_base_url_source=public_base_url_source,
        enable_web=enable_web,
        enable_mcp=enable_mcp,
        open_web=open_web,
        web_disabled_reason=web_disabled_reason,
        stderr=sys.stderr,
    )


def _prepare_default_start(project_path: str, args: list[str]) -> dict[str, object] | None:
    web_host = "0.0.0.0"
    web_port = 8799
    mcp_host = "0.0.0.0"
    mcp_port = 8765
    oauth_token_ttl_seconds = 3600
    open_web = True
    enable_web = True
    enable_mcp = True
    cli_public_base_url: str | None = None
    web_disabled_reason: str | None = None
    explicit_web_port = False
    explicit_mcp_port = False

    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--public-base-url":
            idx += 1
            if idx >= len(args):
                print("默认入口参数错误：--public-base-url 缺少值。", file=sys.stderr)
                return None
            cli_public_base_url = args[idx]
        elif token == "--web-host":
            idx += 1
            if idx >= len(args):
                print("默认入口参数错误：--web-host 缺少值。", file=sys.stderr)
                return None
            web_host = args[idx]
        elif token == "--web-port":
            idx += 1
            if idx >= len(args):
                print("默认入口参数错误：--web-port 缺少值。", file=sys.stderr)
                return None
            try:
                web_port = int(args[idx])
            except ValueError:
                print("默认入口参数错误：--web-port 必须是整数。", file=sys.stderr)
                return None
            explicit_web_port = True
        elif token == "--mcp-host":
            idx += 1
            if idx >= len(args):
                print("默认入口参数错误：--mcp-host 缺少值。", file=sys.stderr)
                return None
            mcp_host = args[idx]
        elif token == "--mcp-port":
            idx += 1
            if idx >= len(args):
                print("默认入口参数错误：--mcp-port 缺少值。", file=sys.stderr)
                return None
            try:
                mcp_port = int(args[idx])
            except ValueError:
                print("默认入口参数错误：--mcp-port 必须是整数。", file=sys.stderr)
                return None
            explicit_mcp_port = True
        elif token == "--oauth-token-ttl-seconds":
            idx += 1
            if idx >= len(args):
                print("默认入口参数错误：--oauth-token-ttl-seconds 缺少值。", file=sys.stderr)
                return None
            try:
                oauth_token_ttl_seconds = int(args[idx])
            except ValueError:
                print("默认入口参数错误：--oauth-token-ttl-seconds 必须是整数。", file=sys.stderr)
                return None
        elif token == "--no-open":
            open_web = False
        elif token == "--no-web":
            enable_web = False
        elif token == "--no-mcp":
            enable_mcp = False
        elif token == "--auth-mode":
            print("默认入口固定使用 OAuth。其他鉴权模式请使用 serve 命令。", file=sys.stderr)
            return None
        elif token == "--auth-token":
            print("默认入口使用 OAuth。token 模式请使用 serve --auth-mode token --auth-token。", file=sys.stderr)
            return None
        else:
            print(f"默认入口参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return None
        idx += 1

    if not enable_web and not enable_mcp:
        print("默认入口参数错误：至少启动 Web Console 或 MCP HTTP。", file=sys.stderr)
        return None

    runner_rel_dir = resolve_project_runner_rel_dir(project_path)
    plan_path = resolve_project_runner_plan_path(project_path)
    has_runner_plan = os.path.isfile(plan_path)
    if enable_web and not has_runner_plan:
        if enable_mcp:
            enable_web = False
            open_web = False
            web_disabled_reason = f"source-only: no {runner_rel_dir}/plan.json"
            print(f"Web Console skipped: this source-only project has no {runner_rel_dir}/plan.json.", file=sys.stderr)
            print("Use MCP manage_runner_plan inspect -> bootstrap_preview/import_preview -> apply", file=sys.stderr)
            print("  to onboard it into Runner.", file=sys.stderr)
            print("CLI import-plan-file remains an advanced fallback for existing plan files.", file=sys.stderr)
        else:
            print(f"缺少计划文件：{plan_path}", file=sys.stderr)
            print(f"当前项目尚未纳入 Runner：缺少 {runner_rel_dir}/plan.json。", file=sys.stderr)
            print("请通过 MCP manage_runner_plan 创建受控 plan（不要使用 --no-mcp），", file=sys.stderr)
            print("或使用 CLI import-plan-file 作为高级 fallback（仅限已有 plan JSON 文件）。", file=sys.stderr)
            return None

    try:
        public_base_url, public_base_url_source = _resolve_public_base_url(project_path, cli_public_base_url)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None
    if enable_mcp:
        if not public_base_url:
            print("缺少 public_base_url。默认入口会启动 OAuth MCP，需要 ChatGPT 可访问的 HTTPS URL。", file=sys.stderr)
            print("示例：", file=sys.stderr)
            print(f"./bin/colameta \"{project_path}\" --public-base-url \"https://your-domain.com\"", file=sys.stderr)
            print("如果只需要本地启动，请使用：", file=sys.stderr)
            print(f"./bin/colameta serve \"{project_path}\" --auth-mode none --open", file=sys.stderr)
            return None
        public_base_url = _normalize_public_base_url(public_base_url)
        if not (public_base_url.startswith("https://") or public_base_url.startswith("http://")):
            print("public_base_url 必须以 http:// 或 https:// 开头。", file=sys.stderr)
            return None
        if public_base_url.startswith("http://") and not _is_local_http_url(public_base_url):
            print("默认入口要求 HTTPS public_base_url。请使用 https:// 域名。", file=sys.stderr)
            return None
        if public_base_url.startswith("http://") and _is_local_http_url(public_base_url):
            print("提示：当前 public_base_url 使用本地 HTTP，仅适用于本地调试。", file=sys.stderr)
    else:
        public_base_url = _normalize_public_base_url(public_base_url) if public_base_url else None
        if public_base_url and not (
            public_base_url.startswith("https://") or public_base_url.startswith("http://")
        ):
            print("public_base_url 必须以 http:// 或 https:// 开头。", file=sys.stderr)
            return None

    serve_args = [
        "serve",
        project_path,
        "--web-host",
        web_host,
        "--mcp-host",
        mcp_host,
        "--oauth-token-ttl-seconds",
        str(oauth_token_ttl_seconds),
    ]
    if explicit_web_port:
        serve_args.extend(["--web-port", str(web_port)])
    if explicit_mcp_port:
        serve_args.extend(["--mcp-port", str(mcp_port)])
    if enable_mcp:
        serve_args.extend(["--auth-mode", "oauth", "--public-base-url", str(public_base_url)])
    if open_web:
        serve_args.append("--open")
    if not enable_web:
        serve_args.append("--no-web")
    if not enable_mcp:
        serve_args.append("--no-mcp")
    return {
        "project_path": project_path,
        "web_host": web_host,
        "web_port": web_port,
        "mcp_host": mcp_host,
        "mcp_port": mcp_port,
        "public_base_url": public_base_url,
        "public_base_url_source": public_base_url_source,
        "enable_web": enable_web,
        "enable_mcp": enable_mcp,
        "open_web": open_web,
        "web_disabled_reason": web_disabled_reason,
        "serve_args": serve_args,
    }


def _run_default_serve(project_path: str, args: list[str]) -> int:
    prepared = _prepare_default_start(project_path, args)
    if prepared is None:
        return 1
    return _run_service_start_from_command(prepared)


def _prepare_global_start(args: list[str]) -> dict[str, object] | None:
    project_path = _default_service_project_root()
    serve_args = ["serve", project_path]
    if args:
        serve_args.extend(args)

    web_host = "0.0.0.0"
    web_port = 8799
    mcp_host = "0.0.0.0"
    mcp_port = 8765
    enable_web = True
    enable_mcp = True
    public_base_url: str | None = None
    explicit_fields: set[str] = set()

    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--web-host":
            idx += 1
            if idx >= len(args):
                print("start 参数错误：--web-host 缺少值。", file=sys.stderr)
                return None
            web_host = args[idx]
            explicit_fields.add("web_host")
        elif token == "--web-port":
            idx += 1
            if idx >= len(args):
                print("start 参数错误：--web-port 缺少值。", file=sys.stderr)
                return None
            try:
                web_port = int(args[idx])
            except ValueError:
                print("start 参数错误：--web-port 必须是整数。", file=sys.stderr)
                return None
            explicit_fields.add("web_port")
        elif token == "--mcp-host":
            idx += 1
            if idx >= len(args):
                print("start 参数错误：--mcp-host 缺少值。", file=sys.stderr)
                return None
            mcp_host = args[idx]
            explicit_fields.add("mcp_host")
        elif token == "--mcp-port":
            idx += 1
            if idx >= len(args):
                print("start 参数错误：--mcp-port 缺少值。", file=sys.stderr)
                return None
            try:
                mcp_port = int(args[idx])
            except ValueError:
                print("start 参数错误：--mcp-port 必须是整数。", file=sys.stderr)
                return None
            explicit_fields.add("mcp_port")
        elif token == "--public-base-url":
            idx += 1
            if idx >= len(args):
                print("start 参数错误：--public-base-url 缺少值。", file=sys.stderr)
                return None
            public_base_url = args[idx]
            explicit_fields.add("public_base_url")
        elif token == "--no-web":
            enable_web = False
        elif token == "--no-mcp":
            enable_mcp = False
        elif token in {"--auth-mode", "--auth-token", "--oauth-token-ttl-seconds"}:
            idx += 1
            if idx >= len(args):
                print(f"start 参数错误：{token} 缺少值。", file=sys.stderr)
                return None
        idx += 1

    try:
        global_config = _load_global_runner_config("start", include_auth_token=True, project_path=project_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return None
    if "web_host" not in explicit_fields and global_config.get("web_host") is not None:
        web_host = str(global_config["web_host"])
    if "web_port" not in explicit_fields and global_config.get("web_port") is not None:
        web_port = int(global_config["web_port"])
    if "mcp_host" not in explicit_fields and global_config.get("mcp_host") is not None:
        mcp_host = str(global_config["mcp_host"])
    if "mcp_port" not in explicit_fields and global_config.get("mcp_port") is not None:
        mcp_port = int(global_config["mcp_port"])
    if "public_base_url" not in explicit_fields and global_config.get("public_base_url") is not None:
        public_base_url = str(global_config["public_base_url"])

    return {
        "project_path": project_path,
        "web_host": web_host,
        "web_port": web_port,
        "mcp_host": mcp_host,
        "mcp_port": mcp_port,
        "public_base_url": public_base_url,
        "enable_web": enable_web,
        "enable_mcp": enable_mcp,
        "serve_args": serve_args,
        "global_mode": True,
        "register_as_selected": False,
    }


def _run_service_start_from_command(prepared: dict[str, object]) -> int:
    project_path = str(prepared["project_path"])
    global_mode = bool(prepared.get("global_mode", False))
    register_as_selected = bool(prepared.get("register_as_selected", not global_mode))
    existing = _read_service_metadata(project_path)
    if existing is not None:
        pid = int(existing.get("pid", 0) or 0)
        if _is_pid_running(pid):
            web_state, mcp_state = _probe_service_health(existing)
            cli_output.print_service_status_summary(
                project_path=project_path,
                pid=pid,
                state="running",
                web_url=str(existing.get("web_url") or "") or None,
                web_state=web_state,
                mcp_url=str(existing.get("mcp_url") or "") or None,
                mcp_state=mcp_state,
                log_path=str(existing.get("log_path") or "") or None,
                stderr=sys.stderr,
            )
            return 0
        _clear_service_metadata(project_path)

    if prepared.get("enable_web"):
        web_host = str(prepared.get("web_host", "0.0.0.0"))
        web_port = int(prepared.get("web_port", 0))
        if not _is_port_available(web_host, web_port):
            print(f"start 启动失败：Web Console 端口 {web_host}:{web_port} 已被占用。", file=sys.stderr)
            return 1
    if prepared.get("enable_mcp"):
        mcp_host = str(prepared.get("mcp_host", "127.0.0.1"))
        mcp_port = int(prepared.get("mcp_port", 0))
        if not _is_port_available(mcp_host, mcp_port):
            print(f"start 启动失败：MCP HTTP 端口 {mcp_host}:{mcp_port} 已被占用。", file=sys.stderr)
            return 1

    paths = _ensure_service_dir(project_path)
    command = [
        sys.executable,
        os.path.abspath(__file__),
        *list(prepared["serve_args"]),
        "--service-child",
    ]
    if global_mode:
        command.append("--global-mode")
    if not register_as_selected:
        command.append("--no-register-selected")

    log_handle = open(paths["log"], "a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()

    time.sleep(SERVICE_START_HEALTH_INITIAL_DELAY_SECONDS)
    if process.poll() is not None:
        print(f"start 启动失败：服务进程已退出，详情见 {paths['log']}。", file=sys.stderr)
        return 1

    web_url = None
    if prepared.get("enable_web"):
        web_url = str(prepared.get("public_base_url") or f"http://{prepared['web_host']}:{prepared['web_port']}")
    mcp_url = None
    if prepared.get("enable_mcp"):
        mcp_url = f"http://{prepared['mcp_host']}:{prepared['mcp_port']}/mcp"

    payload: dict[str, object] = {
        "pid": process.pid,
        "project_root": project_path,
        "web_host": prepared.get("web_host"),
        "web_port": prepared.get("web_port"),
        "mcp_host": prepared.get("mcp_host"),
        "mcp_port": prepared.get("mcp_port"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": command,
        "log_path": paths["log"],
        "web_url": web_url,
        "mcp_url": mcp_url,
        "enable_web": bool(prepared.get("enable_web", True)),
        "enable_mcp": bool(prepared.get("enable_mcp", True)),
        "global_mode": global_mode,
    }
    _write_service_metadata(project_path, payload)
    healthy, health_status, web_state, mcp_state = _wait_for_service_health(payload, process)
    if not healthy:
        _clear_service_metadata(project_path)
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=SERVICE_STOP_WAIT_TIMEOUT_SECONDS)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=SERVICE_WAIT_TIMEOUT_SECONDS)
                except Exception:
                    pass
        if health_status == "process_exited":
            print(f"start 启动失败：服务进程已退出，详情见 {paths['log']}。", file=sys.stderr)
        else:
            print(
                "start 启动失败：服务健康检查未通过"
                f"（web={web_state or 'disabled'}, mcp={mcp_state or 'disabled'}），详情见 {paths['log']}。",
                file=sys.stderr,
            )
        return 1
    cli_output.print_service_start_summary(
        project_path=project_path,
        pid=process.pid,
        web_url=web_url,
        mcp_url=mcp_url,
        log_path=paths["log"],
        start_command=_service_command_for_display("serve", None if global_mode else project_path),
        status_command=_service_command_for_display("status", None if global_mode else project_path),
        stop_command=_service_command_for_display("stop", None if global_mode else project_path),
        stderr=sys.stderr,
    )
    return 0


def _prepare_source_only_service_start(
    project_path: str,
    args: list[str],
    *,
    global_mode: bool,
) -> dict[str, object] | None:
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return None
    mcp_host = "127.0.0.1"
    mcp_port = 8765
    explicit_fields: set[str] = set()
    serve_args = ["mcp-http-server", project_path]

    idx = 0
    while idx < len(args):
        token = args[idx]
        if token in {"--web-host", "--web-port", "--no-web", "--no-mcp"}:
            print("source-only 参数错误：该模式不支持 --web-host、--web-port、--no-web 或 --no-mcp。", file=sys.stderr)
            return None
        if token in {"--open", "--no-open"}:
            idx += 1
            continue
        if token == "--mcp-host":
            idx += 1
            if idx >= len(args):
                print("source-only 参数错误：--mcp-host 缺少值。", file=sys.stderr)
                return None
            mcp_host = args[idx]
            explicit_fields.add("mcp_host")
            serve_args.extend(["--host", args[idx]])
        elif token == "--mcp-port":
            idx += 1
            if idx >= len(args):
                print("source-only 参数错误：--mcp-port 缺少值。", file=sys.stderr)
                return None
            try:
                mcp_port = int(args[idx])
            except ValueError:
                print("source-only 参数错误：--mcp-port 必须是整数。", file=sys.stderr)
                return None
            explicit_fields.add("mcp_port")
            serve_args.extend(["--port", args[idx]])
        elif token in {"--auth-mode", "--auth-token", "--public-base-url", "--oauth-token-ttl-seconds"}:
            idx += 1
            if idx >= len(args):
                print(f"source-only 参数错误：{token} 缺少值。", file=sys.stderr)
                return None
            serve_args.extend([token, args[idx]])
        elif token == "--debug-actions":
            serve_args.append(token)
        else:
            print(f"source-only 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return None
        idx += 1

    try:
        global_config = _load_global_runner_config("start", include_auth_token=True, project_path=project_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return None
    if "mcp_host" not in explicit_fields and global_config.get("mcp_host") is not None:
        mcp_host = str(global_config["mcp_host"])
    if "mcp_port" not in explicit_fields and global_config.get("mcp_port") is not None:
        mcp_port = int(global_config["mcp_port"])

    return {
        "project_path": project_path,
        "web_host": "127.0.0.1",
        "web_port": 8799,
        "mcp_host": mcp_host,
        "mcp_port": mcp_port,
        "public_base_url": None,
        "enable_web": False,
        "enable_mcp": True,
        "serve_args": serve_args,
        "global_mode": global_mode,
        "register_as_selected": False,
        "service_mode": "source-only",
    }


def _run_service_start(args: list[str]) -> int:
    mode = "managed"
    idx = 1
    if len(args) > idx and args[idx] in _SIMPLE_START_MODES:
        mode = args[idx]
        idx += 1
    global_mode = len(args) <= idx or args[idx].startswith("-")
    if global_mode:
        project_path = _default_service_project_root()
        startup_args = args[idx:]
    else:
        project_path = _resolve_path(args[idx])
        if not os.path.isdir(project_path):
            print(f"项目目录不存在：{project_path}", file=sys.stderr)
            return 1
        startup_args = args[idx + 1:]

    if mode == "source-only":
        prepared = _prepare_source_only_service_start(project_path, startup_args, global_mode=global_mode)
        if prepared is None:
            return 1
        return _run_service_start_from_command(prepared)

    if global_mode:
        prepared = _prepare_global_start(startup_args)
        if prepared is None:
            return 1
        return _run_service_start_from_command(prepared)
    return _run_default_serve(project_path, startup_args)


def _run_service_logs(args: list[str]) -> int:
    project_path = _default_service_project_root()
    line_count = 80
    idx = 1
    if len(args) >= 2 and not args[1].startswith("-"):
        project_path = _resolve_path(args[1])
        idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--lines":
            idx += 1
            if idx >= len(args):
                print("logs 参数错误：--lines 缺少值。", file=sys.stderr)
                return 1
            try:
                line_count = int(args[idx])
            except ValueError:
                print("logs 参数错误：--lines 必须是整数。", file=sys.stderr)
                return 1
            if line_count <= 0:
                print("logs 参数错误：--lines 必须大于 0。", file=sys.stderr)
                return 1
        else:
            print(f"logs 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    log_path = _service_paths(project_path)["log"]
    if not os.path.isfile(log_path):
        print(f"暂无服务日志：{log_path}", file=sys.stderr)
        return 1
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        print(f"读取服务日志失败：{exc}", file=sys.stderr)
        return 1
    for line in lines[-line_count:]:
        print(line, end="")
    return 0


def _run_service_status(args: list[str]) -> int:
    if len(args) >= 2 and not args[1].startswith("-"):
        project_path = _resolve_path(args[1])
    else:
        project_path = _default_service_project_root()
    metadata = _read_service_metadata(project_path)
    if metadata is None:
        cli_output.print_service_status_summary(
            project_path=project_path,
            pid=None,
            state="stopped",
            web_url=None,
            web_state=None,
            mcp_url=None,
            mcp_state=None,
            log_path=_service_paths(project_path)["log"] if os.path.isfile(_service_paths(project_path)["log"]) else None,
            stderr=sys.stderr,
        )
        return 1
    pid = int(metadata.get("pid", 0) or 0)
    if not _is_pid_running(pid):
        web_state, mcp_state = _probe_service_health(metadata)
        cli_output.print_service_status_summary(
            project_path=project_path,
            pid=pid,
            state="stale",
            web_url=str(metadata.get("web_url") or "") or None,
            web_state=web_state,
            mcp_url=str(metadata.get("mcp_url") or "") or None,
            mcp_state=mcp_state,
            log_path=str(metadata.get("log_path") or "") or None,
            stderr=sys.stderr,
        )
        return 1
    web_state, mcp_state = _probe_service_health(metadata)
    cli_output.print_service_status_summary(
        project_path=project_path,
        pid=pid,
        state="running",
        web_url=str(metadata.get("web_url") or "") or None,
        web_state=web_state,
        mcp_url=str(metadata.get("mcp_url") or "") or None,
        mcp_state=mcp_state,
        log_path=str(metadata.get("log_path") or "") or None,
        stderr=sys.stderr,
    )
    return 0


def _run_service_stop(args: list[str]) -> int:
    if len(args) >= 2 and not args[1].startswith("-"):
        project_path = _resolve_path(args[1])
    else:
        project_path = _default_service_project_root()
    metadata = _read_service_metadata(project_path)
    if metadata is None:
        print("服务已停止。", file=sys.stderr)
        return 0
    pid = int(metadata.get("pid", 0) or 0)
    if not _is_pid_running(pid):
        _clear_service_metadata(project_path)
        print("服务已停止，陈旧 metadata 已清理。", file=sys.stderr)
        return 0

    identity_ok = _service_pid_matches_metadata(pid, metadata)
    if identity_ok is False:
        _clear_service_metadata(project_path)
        print(
            f"stop 失败：PID {pid} 不匹配当前 ColaMeta 服务记录，已清理陈旧 metadata。",
            file=sys.stderr,
        )
        return 1
    if identity_ok is None:
        print(f"stop 失败：无法验证 PID {pid} 的身份，已拒绝终止。请手动检查。", file=sys.stderr)
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_service_metadata(project_path)
        print("服务已停止，metadata 已清理。", file=sys.stderr)
        return 0
    except PermissionError:
        print(f"stop 失败：没有权限终止 PID {pid}。", file=sys.stderr)
        return 1
    if not _wait_for_process_exit(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"stop 失败：PID {pid} 未响应 SIGTERM，且没有权限强制终止。", file=sys.stderr)
            return 1
        if not _wait_for_process_exit(pid, timeout_seconds=SERVICE_WAIT_TIMEOUT_SECONDS):
            print(f"stop 失败：PID {pid} 仍在运行。", file=sys.stderr)
            return 1
    unreleased_ports: list[str] = []
    if metadata.get("enable_web"):
        web_host_for_release = str(metadata.get("web_host", "0.0.0.0"))
        web_port_for_release = int(metadata.get("web_port", 0))
        if not _wait_for_port_available(web_host_for_release, web_port_for_release, label="stop"):
            unreleased_ports.append(f"Web Console {web_host_for_release}:{web_port_for_release}")
    if metadata.get("enable_mcp"):
        mcp_host_for_release = str(metadata.get("mcp_host", "0.0.0.0"))
        mcp_port_for_release = int(metadata.get("mcp_port", 0))
        if not _wait_for_port_available(mcp_host_for_release, mcp_port_for_release, label="stop"):
            unreleased_ports.append(f"MCP HTTP {mcp_host_for_release}:{mcp_port_for_release}")
    if unreleased_ports:
        joined = "、".join(unreleased_ports)
        print(f"stop 失败：服务进程已退出，但端口尚未释放：{joined}。", file=sys.stderr)
        return 1
    _clear_service_metadata(project_path)
    print("服务已停止。", file=sys.stderr)
    return 0


def _run_service_restart(args: list[str]) -> int:
    global_mode = len(args) < 2 or args[1].startswith("-")
    if global_mode:
        project_path = _default_service_project_root()
        startup_args = args[1:]
    else:
        project_path = _resolve_path(args[1])
        if not os.path.isdir(project_path):
            print(f"项目目录不存在：{project_path}", file=sys.stderr)
            return 1
        startup_args = args[2:]
    stop_rc = _run_service_stop(["stop", project_path])
    if stop_rc != 0:
        return stop_rc
    if global_mode:
        prepared = _prepare_global_start(startup_args)
        if prepared is None:
            return 1
        return _run_service_start_from_command(prepared)
    return _run_default_serve(project_path, startup_args)


def _run_bootstrap(args: list[str]) -> int:
    force = "--force" in args
    filtered = [arg for arg in args if arg != "--force"]
    if len(filtered) < 2:
        print("bootstrap 缺少项目目录。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(filtered[1])
    try:
        from runner.bootstrap import BootstrapManager
    except Exception as e:
        print(f"启动失败：导入 Bootstrap 组件失败：{e}")
        return 1

    manager = BootstrapManager()
    result = manager.bootstrap(project_path, force=force)
    print(f"Bootstrap 完成：{result.project_root}")
    if result.created_files:
        print("已创建文件：")
        for path in result.created_files:
            print(f"- {path}")
    if result.overwritten_files:
        print("已覆盖文件：")
        for path in result.overwritten_files:
            print(f"- {path}")
    if result.skipped_files:
        print("已跳过文件：")
        for path in result.skipped_files:
            print(f"- {path}")
    print("下一步：")
    print(f"1. ./bin/colameta open-bootstrap \"{result.project_root}\"")
    print(f"2. 完成编辑后运行 ./bin/colameta validate-bootstrap \"{result.project_root}\"")
    print(f"3. 通过后启动 ./bin/colameta serve \"{result.project_root}\" --open")
    return 0


def _run_open_bootstrap(args: list[str]) -> int:
    if len(args) < 2:
        print("open-bootstrap 缺少项目目录。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    try:
        from runner.bootstrap import BootstrapManager
    except Exception as e:
        print(f"启动失败：导入 Bootstrap 组件失败：{e}")
        return 1

    manager = BootstrapManager()
    result = manager.open_bootstrap(project_path)
    if result.is_macos:
        print("已尝试用 TextEdit 打开以下文件：")
        for path in result.opened_files:
            print(f"- {path}")
        if result.open_errors:
            print("打开失败：")
            for item in result.open_errors:
                print(f"- {item}")
            return 1
        return 0

    print("当前系统不是 macOS，请手动打开以下文件：")
    for path in result.manual_files:
        print(f"- {path}")
    return 0


def _run_validate_bootstrap(args: list[str]) -> int:
    if len(args) < 2:
        print("validate-bootstrap 缺少项目目录。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    try:
        from runner.bootstrap import BootstrapManager
    except Exception as e:
        print(f"启动失败：导入 Bootstrap 组件失败：{e}")
        return 1

    manager = BootstrapManager()
    validation = manager.validate_bootstrap(project_path)
    print(manager.render_validate_report(validation))
    return 0 if validation.ok else 2


def _run_bridge_status(args: list[str]) -> int:
    if len(args) < 2:
        print("bridge-status 缺少项目目录。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    from runner.planning_bridge import PlanningBridge, PlanningBridgeError
    bridge = PlanningBridge()
    try:
        data = bridge.get_runner_status(project_path)
    except PlanningBridgeError as e:
        print(f"读取失败：{e}")
        return 1
    print(json_dumps(data))
    return 0


def _run_bridge_version_result(args: list[str]) -> int:
    if len(args) < 2:
        print("bridge-version-result 缺少项目目录。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    version = args[2] if len(args) >= 3 else None
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    from runner.planning_bridge import PlanningBridge, PlanningBridgeError
    bridge = PlanningBridge()
    try:
        data = bridge.get_version_result(project_path, version=version)
    except PlanningBridgeError as e:
        print(f"读取失败：{e}")
        return 1
    print(json_dumps(data))
    return 0


def _run_bridge_next_plan(args: list[str]) -> int:
    if len(args) < 2:
        print("bridge-next-plan 缺少项目目录。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    from runner.planning_bridge import PlanningBridge, PlanningBridgeError
    bridge = PlanningBridge()
    try:
        data = bridge.get_next_version_plan(project_path)
    except PlanningBridgeError as e:
        print(f"读取失败：{e}")
        return 1
    print(json_dumps(data))
    return 0


def _run_bridge_preview_insert(args: list[str]) -> int:
    if len(args) < 3:
        print("bridge-preview-insert 参数不足。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    spec_file = _resolve_path(args[2])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    if not os.path.isfile(spec_file):
        print(f"spec 文件不存在：{spec_file}")
        return 1
    from runner.planning_bridge import PlanningBridge, PlanningBridgeError
    bridge = PlanningBridge()
    try:
        with open(spec_file, "r", encoding="utf-8") as f:
            spec = json.loads(f.read())
    except Exception as e:
        print(f"读取 spec 失败：{e}")
        return 1
    try:
        data = bridge.preview_insert_version(project_path, spec)
    except PlanningBridgeError as e:
        print(f"预览失败：{e}")
        return 1
    print(json_dumps(data))
    return 0


def _run_bridge_apply_patch(args: list[str]) -> int:
    if len(args) < 3:
        print("bridge-apply-patch 参数不足。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    patch_id = args[2]
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    from runner.planning_bridge import PlanningBridge, PlanningBridgeError
    bridge = PlanningBridge()
    try:
        data = bridge.apply_plan_patch(project_path, patch_id)
    except PlanningBridgeError as e:
        print(f"应用失败：{e}")
        return 1
    print(json_dumps(data))
    return 0 if data.get("ok") else 2


def _run_bridge_preview_update(args: list[str]) -> int:
    if len(args) < 3:
        print("bridge-preview-update 参数不足。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    spec_file = _resolve_path(args[2])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    if not os.path.isfile(spec_file):
        print(f"spec 文件不存在：{spec_file}")
        return 1
    from runner.planning_bridge import PlanningBridge, PlanningBridgeError
    bridge = PlanningBridge()
    try:
        with open(spec_file, "r", encoding="utf-8") as f:
            spec = json.loads(f.read())
    except Exception as e:
        print(f"读取 spec 失败：{e}")
        return 1
    try:
        data = bridge.preview_update_version(project_path, spec)
    except PlanningBridgeError as e:
        print(f"预览失败：{e}")
        return 1
    print(json_dumps(data))
    return 0


def _run_bridge_doc_section(args: list[str]) -> int:
    if len(args) < 4:
        print("bridge-doc-section 参数不足。")
        print(USAGE_MESSAGE)
        return 1
    project_path = _resolve_path(args[1])
    file_path = args[2]
    heading = args[3]
    max_chars = 12000

    idx = 4
    while idx < len(args):
        token = args[idx]
        if token == "--max-chars":
            idx += 1
            if idx >= len(args):
                print("bridge-doc-section 参数错误：--max-chars 缺少值。")
                return 1
            try:
                max_chars = int(args[idx])
            except ValueError:
                print("bridge-doc-section 参数错误：--max-chars 必须是整数。")
                return 1
        else:
            print(f"bridge-doc-section 参数错误：未知参数 {token}")
            print(USAGE_MESSAGE)
            return 1
        idx += 1

    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}")
        return 1
    from runner.planning_bridge import PlanningBridge
    bridge = PlanningBridge()
    data = bridge.get_project_doc_section(
        project_path,
        {
            "file": file_path,
            "heading": heading,
            "max_chars": max_chars,
        },
    )
    print(json_dumps(data))
    return 0 if data.get("ok") else 2


def _run_mcp_server(args: list[str]) -> int:
    if len(args) < 2:
        print("mcp-server 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    try:
        from runner.mcp_server import MCPPlanningBridgeServer
    except Exception as e:
        print(f"启动失败：导入 MCP server 组件失败：{e}", file=sys.stderr)
        return 1
    server = MCPPlanningBridgeServer(project_path)
    try:
        server.validate_project()
    except Exception as e:
        print(f"启动失败：{e}", file=sys.stderr)
        return 1
    print(f"MCP Planning Bridge server 已启动：{project_path}", file=sys.stderr)
    print("传输：stdio JSON-RPC（stdout 为协议响应，stderr 为日志）", file=sys.stderr)
    return server.serve_stdio()


def _resolve_debug_actions(cli_has_flag: bool) -> bool:
    return cli_env.resolve_debug_actions(cli_has_flag)


def _run_mcp_http_server(args: list[str], allow_source_only: bool = False) -> int:
    if len(args) < 2:
        print("mcp-http-server 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    host = "127.0.0.1"
    port = 8765
    auth_mode: str | None = None
    auth_token: str | None = None
    public_base_url: str | None = None
    oauth_token_ttl_seconds = 3600
    debug_actions_flag = _resolve_debug_actions(False)
    explicit_fields: set[str] = set()

    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--host":
            idx += 1
            if idx >= len(args):
                print("mcp-http-server 参数错误：--host 缺少值。", file=sys.stderr)
                return 1
            host = args[idx]
            explicit_fields.add("host")
        elif token == "--port":
            idx += 1
            if idx >= len(args):
                print("mcp-http-server 参数错误：--port 缺少值。", file=sys.stderr)
                return 1
            try:
                port = int(args[idx])
            except ValueError:
                print("mcp-http-server 参数错误：--port 必须是整数。", file=sys.stderr)
                return 1
            explicit_fields.add("port")
        elif token == "--auth-token":
            idx += 1
            if idx >= len(args):
                print("mcp-http-server 参数错误：--auth-token 缺少值。", file=sys.stderr)
                return 1
            auth_token = args[idx]
            explicit_fields.add("auth_token")
        elif token == "--auth-mode":
            idx += 1
            if idx >= len(args):
                print("mcp-http-server 参数错误：--auth-mode 缺少值。", file=sys.stderr)
                return 1
            auth_mode = args[idx]
            explicit_fields.add("auth_mode")
        elif token == "--public-base-url":
            idx += 1
            if idx >= len(args):
                print("mcp-http-server 参数错误：--public-base-url 缺少值。", file=sys.stderr)
                return 1
            public_base_url = args[idx]
            explicit_fields.add("public_base_url")
        elif token == "--oauth-token-ttl-seconds":
            idx += 1
            if idx >= len(args):
                print("mcp-http-server 参数错误：--oauth-token-ttl-seconds 缺少值。", file=sys.stderr)
                return 1
            try:
                oauth_token_ttl_seconds = int(args[idx])
            except ValueError:
                print("mcp-http-server 参数错误：--oauth-token-ttl-seconds 必须是整数。", file=sys.stderr)
                return 1
            explicit_fields.add("oauth_token_ttl_seconds")
        elif token == "--debug-actions":
            debug_actions_flag = True
            explicit_fields.add("debug_actions")
        elif token in {"--service-child", "--global-mode", "--no-register-selected"}:
            pass
        else:
            print(f"mcp-http-server 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    try:
        try:
            global_config = _load_global_runner_config("mcp-http-server", include_auth_token=True, project_path=project_path)
        except ValueError:
            if not explicit_fields.intersection({"auth_mode", "auth_token", "public_base_url"}):
                raise
            global_config = {}
        if "host" not in explicit_fields and global_config.get("mcp_host") is not None:
            host = str(global_config["mcp_host"])
        if "port" not in explicit_fields and global_config.get("mcp_port") is not None:
            port = int(global_config["mcp_port"])
        options = {
            "auth_mode": auth_mode if auth_mode is not None else global_config.get("auth_mode"),
            "auth_token": auth_token if auth_token is not None else global_config.get("auth_token"),
            "public_base_url": public_base_url
            if public_base_url is not None
            else global_config.get("public_base_url"),
            "oauth_token_ttl_seconds": oauth_token_ttl_seconds
            if "oauth_token_ttl_seconds" in explicit_fields
            else (global_config.get("oauth_token_ttl_seconds") or oauth_token_ttl_seconds),
            "debug_actions": debug_actions_flag,
        }
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    auth_mode = options["auth_mode"]  # type: ignore[assignment]
    auth_token = options["auth_token"]  # type: ignore[assignment]
    public_base_url = options["public_base_url"]  # type: ignore[assignment]
    oauth_token_ttl_seconds = options["oauth_token_ttl_seconds"]  # type: ignore[assignment]
    debug_actions_flag = bool(options["debug_actions"])

    resolved_auth_mode = _validate_mcp_auth_options(
        "mcp-http-server",
        auth_mode,
        auth_token,
        public_base_url,
        oauth_token_ttl_seconds,
    )
    if resolved_auth_mode is None:
        return 1

    try:
        from runner.mcp_server import MCPPlanningBridgeServer
    except Exception as e:
        print(f"启动失败：导入 MCP HTTP server 组件失败：{e}", file=sys.stderr)
        return 1

    server = MCPPlanningBridgeServer(project_path)
    try:
        server.validate_project("source-only" if allow_source_only else None)
    except Exception as e:
        if allow_source_only and "缺少计划文件或 Git 仓库" in str(e):
            runner_rel_dir = resolve_project_runner_rel_dir(project_path)
            print(f"source-only 模式允许缺少 {runner_rel_dir}/plan.json，这是预期行为。", file=sys.stderr)
        else:
            print(f"启动失败：{e}", file=sys.stderr)
            return 1

    debug_actions = bool(debug_actions_flag)
    if debug_actions:
        print("Debug actions mode enabled.", file=sys.stderr)

    print(f"\U0001f9e9  Project: {project_path}", file=sys.stderr)
    mcp_url = f"http://{host}:{port}/mcp"
    print(f"\U0001f50c  MCP Endpoint: {mcp_url}", file=sys.stderr)
    _print_mcp_auth_status(resolved_auth_mode, public_base_url)
    print("\u2705  Ready. Press Ctrl-C to stop.", file=sys.stderr)
    return server.serve_http(
        host=host,
        port=port,
        auth_token=auth_token,
        auth_mode=resolved_auth_mode,
        public_base_url=public_base_url,
        oauth_token_ttl_seconds=oauth_token_ttl_seconds,
        debug_actions=debug_actions,
    )





def _run_web_console(args: list[str]) -> int:
    if len(args) < 2:
        print("web 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    host = "127.0.0.1"
    port = 8787
    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--host":
            idx += 1
            if idx >= len(args):
                print("web 参数错误：--host 缺少值。", file=sys.stderr)
                return 1
            host = args[idx]
        elif token == "--port":
            idx += 1
            if idx >= len(args):
                print("web 参数错误：--port 缺少值。", file=sys.stderr)
                return 1
            try:
                port = int(args[idx])
            except ValueError:
                print("web 参数错误：--port 必须是整数。", file=sys.stderr)
                return 1
        else:
            print(f"web 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    try:
        from runner.web_console import WebConsoleServer
    except Exception as e:
        print(f"启动失败：导入 Web Console 组件失败：{e}", file=sys.stderr)
        return 1
    server = WebConsoleServer(project_path)
    try:
        server.validate_project()
    except Exception as e:
        print(f"启动失败：{e}", file=sys.stderr)
        return 1
    display_url = "http://127.0.0.1:8799"
    print(f"MVP Runner Web Console: {display_url}", file=sys.stderr)
    print("Web Console 为本地控制台，运行、修复、提交等动作仍由用户手动触发。", file=sys.stderr)
    return server.serve_http(host=host, port=port)


def _run_combined_serve(args: list[str], project_mode: str | None = None, *, register_as_selected: bool = True, global_mode: bool = False) -> int:
    if len(args) < 2:
        print("serve 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1

    project_path = _resolve_path(args[1])
    web_host = "0.0.0.0"
    web_port = 8799
    mcp_host = "0.0.0.0"
    mcp_port = 8765
    auth_mode: str | None = None
    auth_token: str | None = None
    public_base_url: str | None = None
    oauth_token_ttl_seconds = 3600
    enable_web = True
    enable_mcp = True
    open_web = False
    debug_actions_flag = _resolve_debug_actions(False)
    explicit_fields: set[str] = set()

    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--web-host":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--web-host 缺少值。", file=sys.stderr)
                return 1
            web_host = args[idx]
            explicit_fields.add("web_host")
        elif token == "--web-port":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--web-port 缺少值。", file=sys.stderr)
                return 1
            try:
                web_port = int(args[idx])
            except ValueError:
                print("serve 参数错误：--web-port 必须是整数。", file=sys.stderr)
                return 1
            explicit_fields.add("web_port")
        elif token == "--mcp-host":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--mcp-host 缺少值。", file=sys.stderr)
                return 1
            mcp_host = args[idx]
            explicit_fields.add("mcp_host")
        elif token == "--mcp-port":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--mcp-port 缺少值。", file=sys.stderr)
                return 1
            try:
                mcp_port = int(args[idx])
            except ValueError:
                print("serve 参数错误：--mcp-port 必须是整数。", file=sys.stderr)
                return 1
            explicit_fields.add("mcp_port")
        elif token == "--auth-token":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--auth-token 缺少值。", file=sys.stderr)
                return 1
            auth_token = args[idx]
            explicit_fields.add("auth_token")
        elif token == "--auth-mode":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--auth-mode 缺少值。", file=sys.stderr)
                return 1
            auth_mode = args[idx]
            explicit_fields.add("auth_mode")
        elif token == "--public-base-url":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--public-base-url 缺少值。", file=sys.stderr)
                return 1
            public_base_url = args[idx]
            explicit_fields.add("public_base_url")
        elif token == "--oauth-token-ttl-seconds":
            idx += 1
            if idx >= len(args):
                print("serve 参数错误：--oauth-token-ttl-seconds 缺少值。", file=sys.stderr)
                return 1
            try:
                oauth_token_ttl_seconds = int(args[idx])
            except ValueError:
                print("serve 参数错误：--oauth-token-ttl-seconds 必须是整数。", file=sys.stderr)
                return 1
            explicit_fields.add("oauth_token_ttl_seconds")
        elif token == "--service-child":
            pass
        elif token == "--global-mode":
            global_mode = True
        elif token == "--no-register-selected":
            register_as_selected = False
        elif token == "--no-web":
            enable_web = False
        elif token == "--no-mcp":
            enable_mcp = False
        elif token == "--open":
            open_web = True
        else:
            print(f"serve 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    try:
        try:
            global_config = _load_global_runner_config("serve", include_auth_token=True, project_path=project_path)
        except ValueError:
            if not explicit_fields.intersection({"auth_mode", "auth_token", "public_base_url"}):
                raise
            global_config = {}
        if "web_host" not in explicit_fields and global_config.get("web_host") is not None:
            web_host = str(global_config["web_host"])
        if "web_port" not in explicit_fields and global_config.get("web_port") is not None:
            web_port = int(global_config["web_port"])
        if "mcp_host" not in explicit_fields and global_config.get("mcp_host") is not None:
            mcp_host = str(global_config["mcp_host"])
        if "mcp_port" not in explicit_fields and global_config.get("mcp_port") is not None:
            mcp_port = int(global_config["mcp_port"])
        options = {
            "auth_mode": auth_mode if auth_mode is not None else global_config.get("auth_mode"),
            "auth_token": auth_token if auth_token is not None else global_config.get("auth_token"),
            "public_base_url": public_base_url
            if public_base_url is not None
            else global_config.get("public_base_url"),
            "oauth_token_ttl_seconds": oauth_token_ttl_seconds
            if "oauth_token_ttl_seconds" in explicit_fields
            else (global_config.get("oauth_token_ttl_seconds") or oauth_token_ttl_seconds),
            "debug_actions": debug_actions_flag,
        }
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    auth_mode = options["auth_mode"]  # type: ignore[assignment]
    auth_token = options["auth_token"]  # type: ignore[assignment]
    public_base_url = options["public_base_url"]  # type: ignore[assignment]
    oauth_token_ttl_seconds = options["oauth_token_ttl_seconds"]  # type: ignore[assignment]
    debug_actions_flag = bool(options["debug_actions"])

    if not enable_web and not enable_mcp:
        print("serve 参数错误：至少需要启动 Web Console 或 MCP HTTP Server。", file=sys.stderr)
        return 1
    if open_web and not enable_web:
        print("serve 参数提示：--open 在 --no-web 模式下已忽略。", file=sys.stderr)
        open_web = False
    resolved_auth_mode = _resolve_auth_mode(auth_mode, auth_token)
    if enable_mcp:
        validated_auth_mode = _validate_mcp_auth_options(
            "serve",
            auth_mode,
            auth_token,
            public_base_url,
            oauth_token_ttl_seconds,
        )
        if validated_auth_mode is None:
            return 1
        resolved_auth_mode = validated_auth_mode

    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1

    project_registered = False
    if register_as_selected:
        try:
            ProjectRegistry().register_project(project_path, last_selected=True)
            project_registered = True
        except Exception as exc:
            print(f"项目登记失败：{exc}", file=sys.stderr)

    reused_web = False
    if enable_web and "web_port" not in explicit_fields:
        if not _is_port_available(web_host, web_port) and _is_runner_web_console(web_host, web_port):
            print(
                f"已复用现有 Web Console (http://{web_host}:{web_port})。"
                f"当前项目已登记，可通过 Web Console 项目切换器访问。",
                file=sys.stderr,
            )
            enable_web = False
            open_web = False
            reused_web = True

    if not enable_web and not enable_mcp and reused_web:
        return 0

    if enable_web:
        resolved_web_port = _interactive_resolve_web_port(
            command_name="serve",
            host=web_host,
            port=web_port,
            explicit_port="web_port" in explicit_fields,
        )
        if resolved_web_port is None:
            return 1
        web_port = resolved_web_port

    reused_mcp = False
    if enable_mcp:
        mcp_port_available = _is_port_available(mcp_host, mcp_port)
        if not mcp_port_available and _is_runner_mcp_server(mcp_host, mcp_port):
            print(
                f"已复用现有 MCP HTTP (http://{mcp_host}:{mcp_port}/mcp)。"
                "当前项目通过 registry 路由访问。",
                file=sys.stderr,
            )
            enable_mcp = False
            reused_mcp = True
        elif not mcp_port_available and reused_web and "mcp_port" not in explicit_fields:
            print(
                f"serve 启动失败：MCP HTTP 默认端口 {mcp_host}:{mcp_port} "
                "被其他服务占用，无法作为 ColaMeta 服务级入口复用。",
                file=sys.stderr,
            )
            return 1
        elif not mcp_port_available:
            resolved_mcp_port = _interactive_resolve_mcp_port(
                command_name="serve",
                host=mcp_host,
                port=mcp_port,
                explicit_port="mcp_port" in explicit_fields,
                known_occupied=True,
            )
            if resolved_mcp_port is None:
                return 1
            mcp_port = resolved_mcp_port

    if not enable_web and not enable_mcp and (reused_web or reused_mcp):
        return 0

    if register_as_selected and not project_registered:
        ProjectRegistry().register_project(project_path, last_selected=True)

    web_server = None
    mcp_server = None
    try:
        if enable_web:
            from runner.web_console import WebConsoleServer
            web_server = WebConsoleServer(project_path, service_mode=True)
            web_server.validate_project(project_mode)
        if enable_mcp:
            from runner.mcp_server import MCPPlanningBridgeServer
            mcp_server = MCPPlanningBridgeServer(project_path, service_mode=True)
            mcp_server.validate_project(project_mode)
    except Exception as e:
        print(f"启动失败：{e}", file=sys.stderr)
        return 1

    errors: list[str] = []
    error_event = threading.Event()
    shutdown_event = threading.Event()

    def _sigterm_handler(signum, frame):
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    def run_service(label: str, target) -> None:
        try:
            target()
        except Exception as e:
            errors.append(f"{label} 启动失败：{e}")
            error_event.set()

    threads: list[threading.Thread] = []
    cli_output.print_colamate_banner(stderr=sys.stderr)
    if global_mode:
        print("✅  Runner 已启动", file=sys.stderr)
    else:
        print(f"\U0001f9e9  Project: {project_path}", file=sys.stderr)
    if enable_web and web_server is not None:
        web_url = public_base_url or f"http://{web_host}:{web_port}"
        print(f"\U0001f310  Web Console: {web_url}", file=sys.stderr)
        threads.append(
            threading.Thread(
                target=run_service,
                args=("Web Console", lambda: web_server.serve_http(host=web_host, port=web_port)),
                name="colameta-web",
                daemon=True,
            )
        )
    if enable_mcp and mcp_server is not None:
        if public_base_url:
            print(f"\u2699\ufe0f  Actions API: {public_base_url.rstrip('/')}/openapi.json", file=sys.stderr)
        mcp_url = f"http://{mcp_host}:{mcp_port}/mcp"
        print(f"\U0001f50c  MCP Endpoint: {mcp_url}", file=sys.stderr)
        if resolved_auth_mode == "none":
            print("\U0001f513  Auth: disabled", file=sys.stderr)
        elif resolved_auth_mode == "token":
            print("\U0001f510  Auth: Bearer token enabled", file=sys.stderr)
        elif resolved_auth_mode == "oauth":
            print("\U0001f510  Auth: OAuth enabled", file=sys.stderr)
        threads.append(
            threading.Thread(
                target=run_service,
                args=(
                    "MCP HTTP Server",
                    lambda: mcp_server.serve_http(
                        host=mcp_host,
                        port=mcp_port,
                        auth_token=auth_token,
                        auth_mode=resolved_auth_mode,
                        public_base_url=public_base_url,
                        oauth_token_ttl_seconds=oauth_token_ttl_seconds,
                        debug_actions=bool(debug_actions_flag),
                    ),
                ),
                name="colameta-mcp-http",
                daemon=True,
            )
        )
    if global_mode:
        _print_registry_project_list()
    print("\u2705  Ready. Press Ctrl-C to stop.", file=sys.stderr)

    for thread in threads:
        thread.start()

    time.sleep(0.4)
    if error_event.is_set():
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    if open_web and enable_web:
        open_url = public_base_url.rstrip("/") if public_base_url else "http://127.0.0.1:8799"
        webbrowser.open(open_url)

    try:
        while not shutdown_event.is_set():
            if error_event.is_set():
                for error in errors:
                    print(error, file=sys.stderr)
                return 1
            if not any(thread.is_alive() for thread in threads):
                print("serve 已退出：所有服务均已停止。", file=sys.stderr)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown_event.set()

    if enable_web and web_server is not None and hasattr(web_server, '_httpd'):
        web_server._httpd.shutdown()
    if enable_mcp and mcp_server is not None and hasattr(mcp_server, '_httpd'):
        mcp_server._httpd.shutdown()
    for thread in threads:
        thread.join(timeout=5)
    print("\n已停止 Runner serve。", file=sys.stderr)
    return 0


def _run_source_only_start(project_path: str, args: list[str]) -> int:
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    forward_args = ["mcp-http-server", project_path]
    seen_no_open = False
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token in {"--web-host", "--web-port", "--no-web", "--no-mcp"}:
            print("source-only 参数错误：该模式不支持 --web-host、--web-port、--no-web 或 --no-mcp。", file=sys.stderr)
            return 1
        if token == "--no-open":
            seen_no_open = True
            idx += 1
            continue
        if token == "--open":
            idx += 1
            continue
        if token == "--mcp-host":
            idx += 1
            if idx >= len(args):
                print("source-only 参数错误：--mcp-host 缺少值。", file=sys.stderr)
                return 1
            forward_args.extend(["--host", args[idx]])
        elif token == "--mcp-port":
            idx += 1
            if idx >= len(args):
                print("source-only 参数错误：--mcp-port 缺少值。", file=sys.stderr)
                return 1
            forward_args.extend(["--port", args[idx]])
        elif token in {"--auth-mode", "--auth-token", "--public-base-url", "--oauth-token-ttl-seconds"}:
            idx += 1
            if idx >= len(args):
                print(f"source-only 参数错误：{token} 缺少值。", file=sys.stderr)
                return 1
            forward_args.extend([token, args[idx]])
        elif token == "--debug-actions":
            forward_args.append(token)
        else:
            print(f"source-only 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    setup_rc = _maybe_interactive_global_config_setup(forward_args, mode="source-only")
    if setup_rc is not None:
        return setup_rc

    runner_rel_dir = resolve_project_runner_rel_dir(project_path)
    plan_path = resolve_project_runner_plan_path(project_path)
    if not os.path.isfile(plan_path):
        print(f"当前项目缺少 {runner_rel_dir}/plan.json，这在 source-only 模式下是预期行为。", file=sys.stderr)
    if seen_no_open:
        print("source-only 模式不启动 Web Console，--no-open 已自动忽略。", file=sys.stderr)
    return _run_mcp_http_server(forward_args, allow_source_only=True)


def _run_managed_start(project_path: str, args: list[str]) -> int:
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    forward_args = ["serve", project_path]
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token in {"--no-web", "--no-mcp"}:
            print("managed 参数错误：该模式固定启动 Web Console 和 MCP HTTP Server，请使用 serve 获取更细粒度控制。", file=sys.stderr)
            return 1
        if token in {
            "--web-host",
            "--web-port",
            "--mcp-host",
            "--mcp-port",
            "--auth-mode",
            "--auth-token",
            "--public-base-url",
            "--oauth-token-ttl-seconds",
        }:
            idx += 1
            if idx >= len(args):
                print(f"managed 参数错误：{token} 缺少值。", file=sys.stderr)
                return 1
            forward_args.extend([token, args[idx]])
        elif token in {"--open", "--no-open", "--debug-actions"}:
            forward_args.append(token)
        else:
            print(f"managed 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    runner_rel_dir = resolve_project_runner_rel_dir(project_path)
    plan_path = resolve_project_runner_plan_path(project_path)
    if not os.path.isfile(plan_path):
        from runner.mcp_runner_plan import ensure_minimal_runner_managed_project

        ensure_result = ensure_minimal_runner_managed_project(project_path)
        if not ensure_result.get("ok"):
            message = str(ensure_result.get("message") or "自动创建最小 Runner 结构失败。")
            print(f"managed 启动失败：{message}", file=sys.stderr)
            return 1
        if ensure_result.get("created"):
            created_runner_dir = str(ensure_result.get("runner_dir") or runner_rel_dir)
            print(f"当前项目尚未纳入 Runner 管理，已创建最小 {created_runner_dir} 结构。", file=sys.stderr)

    setup_rc = _maybe_interactive_global_config_setup(forward_args, mode="managed")
    if setup_rc is not None:
        return setup_rc

    return _run_combined_serve(forward_args, project_mode="managed")


def _run_simple_start(project_path: str, mode: str, args: list[str]) -> int:
    if mode == "source-only":
        return _run_source_only_start(project_path, args)
    if mode == "managed":
        return _run_managed_start(project_path, args)
    print(f"未知简化启动模式：{mode}", file=sys.stderr)
    return 1


def _run_registry_add(args: list[str]) -> int:
    from runner.mcp_runner_plan import ensure_minimal_runner_managed_project

    return cli_registry.run_registry_add(
        args,
        usage_message=USAGE_MESSAGE,
        resolve_path_func=_resolve_path,
        registry_factory=ProjectRegistry,
        project_mode_managed=PROJECT_MODE_MANAGED,
        simple_start_modes=_SIMPLE_START_MODES,
        global_config_store_factory=RunnerGlobalConfigStore,
        prompt_initial_global_config_func=_prompt_initial_global_config,
        ensure_minimal_runner_managed_project_func=ensure_minimal_runner_managed_project,
        stderr=sys.stderr,
        stdout=sys.stdout,
        stdin=sys.stdin,
        getpass_module=getpass,
        json_dumps_func=json_dumps,
    )


def _run_registry_list(args: list[str]) -> int:
    return cli_registry.run_registry_list(
        args,
        usage_message=USAGE_MESSAGE,
        registry_factory=ProjectRegistry,
        json_dumps_func=json_dumps,
        stderr=sys.stderr,
        stdout=sys.stdout,
    )


def _run_registry_remove(args: list[str]) -> int:
    return cli_registry.run_registry_remove(
        args,
        usage_message=USAGE_MESSAGE,
        registry_factory=ProjectRegistry,
        json_dumps_func=json_dumps,
        stderr=sys.stderr,
        stdout=sys.stdout,
    )


def json_dumps(data: object) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, indent=2)


def _run_import_plan_file(args: list[str]) -> int:
    if len(args) < 3:
        print("import-plan-file 参数不足。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    file_path = _resolve_path(args[2])
    mode: str | None = None
    insert_after: str | None = None
    version: str | None = None
    idx = 3
    while idx < len(args):
        token = args[idx]
        if token == "--mode":
            idx += 1
            if idx >= len(args):
                print("import-plan-file 参数错误：--mode 缺少值。", file=sys.stderr)
                return 1
            mode = args[idx]
        elif token == "--insert-after":
            idx += 1
            if idx >= len(args):
                print("import-plan-file 参数错误：--insert-after 缺少值。", file=sys.stderr)
                return 1
            insert_after = args[idx]
        elif token == "--version":
            idx += 1
            if idx >= len(args):
                print("import-plan-file 参数错误：--version 缺少值。", file=sys.stderr)
                return 1
            version = args[idx]
        else:
            print(f"import-plan-file 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    try:
        from runner.plan_file_importer import preview_plan_file_import, PlanFileImportError
        result = preview_plan_file_import(
            project_path=project_path,
            file_path=file_path,
            mode=mode,
            insert_after=insert_after,
            version=version,
        )
        print(json_dumps(result))
        return 0
    except PlanFileImportError as e:
        print(json_dumps({"ok": False, "error_code": "IMPORT_FAILED", "message": str(e)}))
        return 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "IMPORT_ERROR", "message": f"导入异常：{e}"}))
        return 1


def _run_probe_models(args: list[str]) -> int:
    if len(args) < 2:
        print("probe-models 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.executor_inventory import probe_executor_inventory
        result = probe_executor_inventory(project_path)
        print(json_dumps(result))
        return 0
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "PROBE_FAILED", "message": f"探测失败：{e}"}))
        return 1


def _run_list_models(args: list[str]) -> int:
    project_path = _default_service_project_root()
    refresh = False
    idx = 1
    if len(args) > idx and not args[idx].startswith("-"):
        project_path = _resolve_path(args[idx])
        idx += 1
    while idx < len(args):
        token = args[idx]
        if token == "--refresh":
            refresh = True
        else:
            print(f"list-models 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        if refresh:
            from runner.executor_inventory import probe_executor_inventory
            result = probe_executor_inventory(project_path)
        else:
            from runner.executor_inventory import load_executor_inventory
            result = load_executor_inventory(project_path)
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        action = "刷新" if refresh else "读取"
        print(json_dumps({"ok": False, "error_code": "LIST_FAILED", "message": f"{action}失败：{e}"}))
        return 1


def _run_set_version_executor(args: list[str]) -> int:
    if len(args) < 4:
        print("set-version-executor 参数不足。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    version = args[2].strip()
    if not version:
        print("版本号不能为空。", file=sys.stderr)
        return 1
    provider: str | None = None
    idx = 3
    while idx < len(args):
        token = args[idx]
        if token == "--provider":
            idx += 1
            if idx >= len(args):
                print("set-version-executor 参数错误：--provider 缺少值。", file=sys.stderr)
                return 1
            provider = args[idx].strip().lower()
        else:
            print(f"set-version-executor 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1
    if not provider:
        print("请指定 --provider pi|codex|opencode。", file=sys.stderr)
        return 1
    if provider not in ("pi", "codex", "opencode"):
        print(f"不支持的执行器：{provider}，仅支持 pi、codex、opencode。", file=sys.stderr)
        return 1
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.planning_bridge import PlanningBridge, PlanningBridgeError
        spec = {"version": version, "execution": {"provider": provider}}
        bridge = PlanningBridge()
        result = bridge.preview_update_version(project_path, spec)
        patch_id = result.get("patch_id", "未知")
        print(json_dumps({
            "ok": True,
            "operation": "update_version",
            "version": version,
            "patch_id": patch_id,
            "message": "已生成版本执行器更新预览。请在 Web Console 中确认计划更新。",
        }))
        return 0
    except PlanningBridgeError as e:
        print(json_dumps({"ok": False, "error_code": "PREVIEW_FAILED", "message": str(e)}))
        return 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "SET_EXECUTOR_FAILED", "message": f"操作失败：{e}"}))
        return 1


def _run_create_execution_branch(args: list[str]) -> int:
    if len(args) < 4:
        print("create-execution-branch 参数不足。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    version = args[2].strip()
    if not version:
        print("版本号不能为空。", file=sys.stderr)
        return 1
    provider: str | None = None
    idx = 3
    while idx < len(args):
        token = args[idx]
        if token == "--provider":
            idx += 1
            if idx >= len(args):
                print("create-execution-branch 参数错误：--provider 缺少值。", file=sys.stderr)
                return 1
            provider = args[idx].strip().lower()
        else:
            print(f"create-execution-branch 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1
    if not provider:
        print("请指定 --provider pi|codex|opencode。", file=sys.stderr)
        return 1
    try:
        from runner.execution_branch import ExecutionBranchController
        controller = ExecutionBranchController(project_path)
        result = controller.create_branch(version, provider)
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "CREATE_BRANCH_FAILED", "message": f"操作失败：{e}"}))
        return 1


def _run_execution_branch_status(args: list[str]) -> int:
    if len(args) < 2:
        print("execution-branch-status 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.execution_branch import ExecutionBranchController
        controller = ExecutionBranchController(project_path)
        result = controller.get_status()
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "STATUS_FAILED", "message": f"读取失败：{e}"}))
        return 1


def _run_close_execution_branch(args: list[str]) -> int:
    if len(args) < 3:
        print("close-execution-branch 参数不足。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    close_status: str | None = None
    note: str | None = None
    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--status":
            idx += 1
            if idx >= len(args):
                print("close-execution-branch 参数错误：--status 缺少值。", file=sys.stderr)
                return 1
            close_status = args[idx].strip().lower()
        elif token == "--note":
            idx += 1
            if idx >= len(args):
                print("close-execution-branch 参数错误：--note 缺少值。", file=sys.stderr)
                return 1
            note = args[idx].strip()
        else:
            print(f"close-execution-branch 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1
    if not close_status:
        print("请指定 --status passed|failed|abandoned。", file=sys.stderr)
        return 1
    try:
        from runner.execution_branch import ExecutionBranchController
        controller = ExecutionBranchController(project_path)
        result = controller.close_branch(status=close_status, note=note)
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "CLOSE_FAILED", "message": f"操作失败：{e}"}))
        return 1


def _run_execution_branch_review(args: list[str]) -> int:
    if len(args) < 2:
        print("execution-branch-review 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.execution_branch import ExecutionBranchController
        controller = ExecutionBranchController(project_path)
        result = controller.get_review_summary()
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "REVIEW_FAILED", "message": f"操作失败：{e}"}))
        return 1


def _run_lint_plan(args: list[str]) -> int:
    if len(args) < 2:
        print("lint-plan 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.plan_standards_linter import PlanStandardsLinter
        result = PlanStandardsLinter().lint_project(project_path)
        print(json_dumps(result))
        if not result.get("ok"):
            return 1
        return 0 if result.get("status") == "passed" else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "LINT_FAILED", "message": f"lint 失败：{e}"}))
        return 1


def _run_executor_session_status(args: list[str]) -> int:
    if len(args) < 2:
        print("executor-session-status 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.executor_session import ExecutorSessionStore
        result = ExecutorSessionStore(project_path).get_status()
        print(json_dumps(result))
        return 0
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "SESSION_STATUS_FAILED", "message": f"读取失败：{e}"}))
        return 1


def _run_executor_session_reset(args: list[str]) -> int:
    if len(args) < 2:
        print("executor-session-reset 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    reason: str | None = None
    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--reason":
            idx += 1
            if idx >= len(args):
                print("executor-session-reset 参数错误：--reason 缺少值。", file=sys.stderr)
                return 1
            reason = args[idx]
        else:
            print(f"executor-session-reset 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1
    try:
        from runner.executor_session import ExecutorSessionStore
        result = ExecutorSessionStore(project_path).reset(reason=reason)
        print(json_dumps(result))
        return 0
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "SESSION_RESET_FAILED", "message": f"重置失败：{e}"}))
        return 1


def _run_executor_continuation_preview(args: list[str]) -> int:
    if len(args) < 2:
        print("executor-continuation-preview 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1
    try:
        from runner.executor_session import ExecutorSessionStore
        result = ExecutorSessionStore(project_path).get_continuation_preview()
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "CONTINUATION_PREVIEW_FAILED", "message": f"读取失败：{e}"}))
        return 1


def _run_executor_continuation_decision(args: list[str]) -> int:
    if len(args) < 2:
        print("executor-continuation-decision 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1

    provider: str | None = None
    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--provider":
            idx += 1
            if idx >= len(args):
                print("executor-continuation-decision 参数错误：--provider 缺少值。", file=sys.stderr)
                return 1
            provider = args[idx].strip().lower()
        else:
            print(f"executor-continuation-decision 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    if provider not in {"pi", "codex", "opencode"}:
        print("executor-continuation-decision 参数错误：--provider 仅支持 pi|codex|opencode。", file=sys.stderr)
        return 1

    try:
        from runner.executor_session import ExecutorSessionStore
        result = ExecutorSessionStore(project_path).get_continuation_decision(requested_provider=provider)
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "CONTINUATION_DECISION_FAILED", "message": f"读取失败：{e}"}))
        return 1


def _run_executor_resume_invocation_preview(args: list[str]) -> int:
    if len(args) < 2:
        print("executor-resume-invocation-preview 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    project_path = _resolve_path(args[1])
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1

    provider: str | None = None
    idx = 2
    while idx < len(args):
        token = args[idx]
        if token == "--provider":
            idx += 1
            if idx >= len(args):
                print("executor-resume-invocation-preview 参数错误：--provider 缺少值。", file=sys.stderr)
                return 1
            provider = args[idx].strip().lower()
        else:
            print(f"executor-resume-invocation-preview 参数错误：未知参数 {token}", file=sys.stderr)
            print(USAGE_MESSAGE, file=sys.stderr)
            return 1
        idx += 1

    if provider not in {"pi", "codex", "opencode"}:
        print("executor-resume-invocation-preview 参数错误：--provider 仅支持 pi|codex|opencode。", file=sys.stderr)
        return 1

    try:
        from runner.executor_session import ExecutorSessionStore
        result = ExecutorSessionStore(project_path).get_resume_invocation_preview(requested_provider=provider)
        print(json_dumps(result))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json_dumps({"ok": False, "error_code": "RESUME_INVOCATION_PREVIEW_FAILED", "message": f"读取失败：{e}"}))
        return 1


def _resolve_default_managed_project(
    registry: ProjectRegistry,
) -> tuple[str | None, str | None]:
    """Resolve the best default managed project from the registry.

    Returns (project_root, project_name) or (None, None).
    Preference order:
    1. last-selected managed project
    2. exactly one managed project with no prior selection
    """
    result = registry.list_projects()
    if not result.get("ok"):
        return None, None
    projects = result.get("projects", [])
    managed = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        if p.get("project_mode") != "managed":
            continue
        root = p.get("project_root")
        if not isinstance(root, str) or not root.strip():
            continue
        if not os.path.isdir(root):
            continue
        if not registry.is_runner_managed_project(root):
            continue
        managed.append(p)
    if not managed:
        return None, None

    def _sort_key(p):
        ts = p.get("last_selected_at")
        return ts if isinstance(ts, str) else ""

    managed.sort(key=_sort_key, reverse=True)

    if managed[0].get("last_selected_at"):
        return managed[0]["project_root"], managed[0].get("project_name")

    if len(managed) == 1:
        return managed[0]["project_root"], managed[0].get("project_name")

    return None, None


def _print_json_result(result: dict[str, object]) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _parse_decision_cli_flags(tokens: list[str]) -> tuple[dict[str, object] | None, str | None]:
    parsed: dict[str, object] = {}
    related_versions: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--clear-related-versions":
            parsed["related_versions"] = []
            index += 1
            continue
        if token == "--related-version":
            if index + 1 >= len(tokens):
                return None, "--related-version 缺少值。"
            related_versions.append(tokens[index + 1])
            index += 2
            continue
        if token in {"--title", "--decision", "--reason", "--status"}:
            if index + 1 >= len(tokens):
                return None, f"{token} 缺少值。"
            parsed[token[2:].replace("-", "_")] = tokens[index + 1]
            index += 2
            continue
        return None, f"未知参数 {token}"
    if related_versions:
        if parsed.get("related_versions") == []:
            return None, "--clear-related-versions 不能和 --related-version 同时使用。"
        parsed["related_versions"] = related_versions
    return parsed, None


def _run_decision_read(args: list[str]) -> int:
    if len(args) < 2:
        print("decision-read 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    manager = MCPDecisionRecordsManager(_resolve_path(args[1]))
    return _print_json_result(manager.read())


def _run_decision_add(args: list[str]) -> int:
    if len(args) < 2:
        print("decision-add 缺少项目目录。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    parsed, error = _parse_decision_cli_flags(args[2:])
    if error is not None or parsed is None:
        print(f"decision-add 参数错误：{error}", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    manager = MCPDecisionRecordsManager(_resolve_path(args[1]))
    return _print_json_result(
        manager.add(
            parsed.get("title"),
            parsed.get("decision"),
            parsed.get("reason"),
            parsed.get("related_versions"),
            parsed.get("status"),
        )
    )


def _run_decision_update(args: list[str]) -> int:
    if len(args) < 3:
        print("decision-update 缺少项目目录或 decision id。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    parsed, error = _parse_decision_cli_flags(args[3:])
    if error is not None or parsed is None:
        print(f"decision-update 参数错误：{error}", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    manager = MCPDecisionRecordsManager(_resolve_path(args[1]))
    return _print_json_result(manager.update(args[2], **parsed))


def _run_decision_delete(args: list[str]) -> int:
    if len(args) < 3:
        print("decision-delete 缺少项目目录或 decision id。", file=sys.stderr)
        print(USAGE_MESSAGE, file=sys.stderr)
        return 1
    manager = MCPDecisionRecordsManager(_resolve_path(args[1]))
    return _print_json_result(manager.delete(args[2]))


def _run_cloud(args: list[str]) -> int:
    if not args:
        print("colameta cloud 需要子命令：pair、status、reset 或 agent。", file=sys.stderr)
        print("用法：colameta cloud pair --relay-url <url> --pair-code <code>", file=sys.stderr)
        print("      colameta cloud status", file=sys.stderr)
        print("      colameta cloud reset", file=sys.stderr)
        print("      colameta cloud agent [--project-path <path>] [--dry-run]", file=sys.stderr)
        return 1
    subcmd = args[0]
    if subcmd == "pair":
        return _run_cloud_pair(args[1:])
    if subcmd == "status":
        return _run_cloud_status(args[1:])
    if subcmd == "reset":
        return _run_cloud_reset(args[1:])
    if subcmd == "agent":
        return _run_cloud_agent(args[1:])
    print(f"未知 cloud 子命令：{subcmd}", file=sys.stderr)
    return 1


def _run_cloud_pair(args: list[str]) -> int:
    relay_url = None
    pair_code = None
    i = 0
    while i < len(args):
        if args[i] == "--relay-url" and i + 1 < len(args):
            relay_url = args[i + 1]
            i += 2
        elif args[i] == "--pair-code" and i + 1 < len(args):
            pair_code = args[i + 1]
            i += 2
        else:
            print(f"未知参数：{args[i]}", file=sys.stderr)
            return 1
    if not relay_url or not pair_code:
        print("colameta cloud pair 需要 --relay-url 和 --pair-code。", file=sys.stderr)
        return 1
    from runner.cloud_pairing import CloudAgentCredential, save_credential
    from datetime import datetime, timezone
    import uuid
    credential = CloudAgentCredential(
        device_id=str(uuid.uuid4()),
        relay_url=relay_url,
        agent_token=pair_code,
        scopes=["mcp:read", "mcp:preview"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    result = save_credential(credential)
    if result.get("ok"):
        print(json.dumps({"ok": True, "message": "cloud agent credential 已保存。"}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1


def _run_cloud_status(args: list[str]) -> int:
    from runner.cloud_pairing import load_credential
    cred_result = load_credential()
    if not cred_result.get("ok"):
        print(cred_result.get("message", "未找到 credential"), file=sys.stderr)
        return 1
    credential = cred_result["credential"]
    masked = credential.mask_sensitive()
    print(json.dumps({
        "ok": True,
        "device_id": credential.device_id,
        "relay_url": credential.relay_url,
        "scopes": credential.scopes,
        "created_at": credential.created_at,
        "agent_token_masked": masked.get("agent_token", "***"),
    }, ensure_ascii=False, indent=2))
    return 0


def _run_cloud_reset(args: list[str]) -> int:
    from runner.cloud_pairing import delete_credential
    result = delete_credential()
    if result.get("ok"):
        print(json.dumps({"ok": True, "message": "cloud agent credential 已删除。"}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
    return 1


def _run_cloud_agent(args: list[str]) -> int:
    from runner.cloud_pairing import load_credential
    from runner.cloud_agent_client import CloudRelayToolBridge, MockRelayTransport

    dry_run = False
    project_path = None
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--dry-run":
            dry_run = True
            i += 1
        elif token == "--project-path" and i + 1 < len(args):
            project_path = _resolve_path(args[i + 1])
            i += 2
        elif not token.startswith("-"):
            project_path = _resolve_path(token)
            i += 1
        else:
            print(f"cloud agent 参数错误：未知参数 {token}", file=sys.stderr)
            return 1

    if project_path is None:
        print("cloud agent 需要 --project-path <path> 或直接传入项目路径。", file=sys.stderr)
        return 1
    if not os.path.isdir(project_path):
        print(f"项目目录不存在：{project_path}", file=sys.stderr)
        return 1

    cred_result = load_credential()
    if not cred_result.get("ok"):
        print(cred_result.get("message", "未找到 credential"), file=sys.stderr)
        return 1

    credential = cred_result["credential"]
    masked = credential.mask_sensitive()

    bridge = CloudRelayToolBridge(project_path, service_mode=True)

    if dry_run:
        transport = MockRelayTransport()
        transport.enqueue({
            "request_id": "dry-run-001",
            "tool_name": "list_registered_projects",
            "arguments": {},
            "scopes": ["mcp:read"],
        })
        count = bridge.process_messages(transport, max_count=1)
        response = transport.sent_messages[0] if transport.sent_messages else {}
        print(json.dumps({
            "ok": True,
            "mode": "dry-run",
            "device_id": credential.device_id,
            "relay_url": credential.relay_url,
            "agent_token_masked": masked.get("agent_token", "***"),
            "project_path": project_path,
            "dry_run_result": {
                "requests_processed": count,
                "response": response,
            },
        }, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps({
        "ok": True,
        "message": "cloud agent 已就绪。",
        "device_id": credential.device_id,
        "relay_url": credential.relay_url,
        "agent_token_masked": masked.get("agent_token", "***"),
        "project_path": project_path,
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        store = RunnerGlobalConfigStore()
        setup_status = store.needs_interactive_setup()
        if setup_status.get("ok") and setup_status.get("needs_setup"):
            missing = ", ".join(str(item) for item in setup_status.get("missing_fields", []))
            if sys.stdin.isatty():
                result = _prompt_initial_global_config(store, input_func=input, getpass_func=getpass.getpass)
                if not result.get("ok"):
                    print(str(result.get("message") or "Runner 全局配置初始化失败。"), file=sys.stderr)
                    return 1
                print("下一步：", file=sys.stderr)
                print("  ./bin/colameta add <project-name> <project-path> <source-only|managed>", file=sys.stderr)
                print("  ./bin/colameta list", file=sys.stderr)
                print("  ./bin/colameta start /path/to/project", file=sys.stderr)
                print("  ./bin/colameta serve /path/to/project [--open]", file=sys.stderr)
                return 0
            print(f"Runner 全局配置缺失（{missing}）。", file=sys.stderr)
            print("请通过 --public-base-url / --auth-mode / --auth-token 提供配置，", file=sys.stderr)
            print(f"或预先写入 ~/.config/{PRIMARY_USER_CONFIG_DIRNAME}/config.json。", file=sys.stderr)
            print(USAGE_MESSAGE)
            return 1

        return _run_service_start(["start"])

    cmd = sys.argv[1]
    if cmd in ("help", "--help", "-h"):
        print(USAGE_MESSAGE)
        return 0
    if cmd == "bootstrap":
        return _run_bootstrap(sys.argv[1:])
    if cmd == "open-bootstrap":
        return _run_open_bootstrap(sys.argv[1:])
    if cmd == "validate-bootstrap":
        return _run_validate_bootstrap(sys.argv[1:])
    if cmd == "bridge-status":
        return _run_bridge_status(sys.argv[1:])
    if cmd == "bridge-version-result":
        return _run_bridge_version_result(sys.argv[1:])
    if cmd == "bridge-next-plan":
        return _run_bridge_next_plan(sys.argv[1:])
    if cmd == "bridge-preview-insert":
        return _run_bridge_preview_insert(sys.argv[1:])
    if cmd == "bridge-preview-update":
        return _run_bridge_preview_update(sys.argv[1:])
    if cmd == "bridge-doc-section":
        return _run_bridge_doc_section(sys.argv[1:])
    if cmd == "bridge-apply-patch":
        return _run_bridge_apply_patch(sys.argv[1:])
    if cmd == "add":
        return _run_registry_add(sys.argv[1:])
    if cmd == "list":
        return _run_registry_list(sys.argv[1:])
    if cmd == "remove":
        return _run_registry_remove(sys.argv[1:])
    if cmd == "start":
        return _run_service_start(sys.argv[1:])
    if cmd == "stop":
        return _run_service_stop(sys.argv[1:])
    if cmd == "restart":
        return _run_service_restart(sys.argv[1:])
    if cmd == "status":
        return _run_service_status(sys.argv[1:])
    if cmd == "logs":
        return _run_service_logs(sys.argv[1:])
    if cmd == "mcp-server":
        return _run_mcp_server(sys.argv[1:])
    if cmd == "mcp-http-server":
        return _run_mcp_http_server(sys.argv[1:])
    if cmd == "web":
        return _run_web_console(sys.argv[1:])
    if cmd == "serve":
        return _run_combined_serve(sys.argv[1:])
    if cmd == "import-plan-file":
        return _run_import_plan_file(sys.argv[1:])
    if cmd == "probe-models":
        return _run_probe_models(sys.argv[1:])
    if cmd in {"models", "list-models"}:
        return _run_list_models(sys.argv[1:])
    if cmd == "set-version-executor":
        return _run_set_version_executor(sys.argv[1:])
    if cmd == "create-execution-branch":
        return _run_create_execution_branch(sys.argv[1:])
    if cmd == "execution-branch-status":
        return _run_execution_branch_status(sys.argv[1:])
    if cmd == "close-execution-branch":
        return _run_close_execution_branch(sys.argv[1:])
    if cmd == "execution-branch-review":
        return _run_execution_branch_review(sys.argv[1:])
    if cmd == "lint-plan":
        return _run_lint_plan(sys.argv[1:])
    if cmd == "executor-session-status":
        return _run_executor_session_status(sys.argv[1:])
    if cmd == "executor-session-reset":
        return _run_executor_session_reset(sys.argv[1:])
    if cmd == "executor-continuation-preview":
        return _run_executor_continuation_preview(sys.argv[1:])
    if cmd == "executor-continuation-decision":
        return _run_executor_continuation_decision(sys.argv[1:])
    if cmd == "executor-resume-invocation-preview":
        return _run_executor_resume_invocation_preview(sys.argv[1:])
    if cmd == "decision-read":
        return _run_decision_read(sys.argv[1:])
    if cmd == "decision-add":
        return _run_decision_add(sys.argv[1:])
    if cmd == "decision-update":
        return _run_decision_update(sys.argv[1:])
    if cmd == "decision-delete":
        return _run_decision_delete(sys.argv[1:])
    if cmd == "cloud":
        return _run_cloud(sys.argv[2:])

    if len(sys.argv) >= 3 and sys.argv[2] in _SIMPLE_START_MODES:
        project_path = _resolve_path(sys.argv[1])
        mode = sys.argv[2]
        return _run_simple_start(project_path, mode, sys.argv[3:])

    project_path = _resolve_path(sys.argv[1])
    if not os.path.isdir(project_path):
        print(f"未知命令或项目目录不存在：{sys.argv[1]}")
        print(USAGE_MESSAGE)
        return 1
    return _run_default_serve(project_path, sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
