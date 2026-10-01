"""JSON CLI for installation and the six active-CAE operations."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tokenize
from pathlib import Path
from typing import Any

from . import __version__, operations
from .client import AbaqusBridgeClient, BridgeError
from .config import config_root, read_json, session_records
from .install import install, uninstall


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(prog="abaqus-cae")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config-dir", help="Installation/session directory (or ABAQUS_CAE_CONFIG_DIR)")
    root.add_argument("--session", help="CAE session ID or unique ID prefix")
    root.add_argument("--port", type=int, help="Explicit loopback port; normally discovered automatically")
    root.add_argument("--timeout", type=float, default=60, help="Execution timeout in seconds")
    sub = root.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install", help="Install the auto-starting GUI plugin")
    setup.add_argument("--plugin-dir")
    setup.add_argument("--plugin-name")
    setup.add_argument("--port", dest="install_port", type=int, help="Preferred port; 0 selects at CAE startup")
    setup.add_argument("--skill-dir", help="Agent skills parent directory; optionally install SKILL.md too")
    sub.add_parser("uninstall", help="Remove only unchanged files owned by this installation")
    sub.add_parser("doctor", help="Show package, plugin and configuration details")
    sub.add_parser("sessions", help="Discover live CAE bridges and identify stale records")
    sub.add_parser("ping", help="Get the active CAE session details")
    run = sub.add_parser("run-python", help="Run code in the selected live CAE kernel")
    inputs = run.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--code")
    inputs.add_argument("--file", help="Script file read by this CLI; executed inside CAE")
    inputs.add_argument("--stdin", action="store_true")
    directory = sub.add_parser("set-workdir")
    directory.add_argument("path")
    job = sub.add_parser("monitor-job-status")
    job.add_argument("--job-name", default="")
    job.add_argument("--terminate", action="store_true")
    job.add_argument("--since", type=float)
    odb = sub.add_parser("inspect-odb")
    odb.add_argument("odb_path")
    for option in ("step", "variable", "set-name", "component", "history-region", "history-variable"):
        odb.add_argument("--" + option, default="")
    odb.add_argument("--frame", type=int, default=-1)
    odb.add_argument("--max-points", type=int, default=200)
    image = sub.add_parser("capture-viewport")
    image.add_argument("--viewport-name", default="")
    image.add_argument("--format", choices=("PNG", "TIFF", "SVG", "EPS", "PS"), default="PNG")
    image.add_argument("--out", required=True, help="Image output path; its parent must exist")
    return root


def discover(directory: Path) -> list[dict[str, Any]]:
    records = session_records(directory)
    for record in records:
        try:
            client = AbaqusBridgeClient(port=int(record["port"]), timeout=1,
                                       session_id=record["sessionId"])
            live = client.request("describe")
            record["reachable"] = live.get("sessionId") == record["sessionId"] and live.get("installationId") == record.get("installationId")
            if record["reachable"]:
                record.update({key: live[key] for key in ("port", "guiPid", "startedAt") if key in live})
        except (OSError, BridgeError, ValueError) as exc:
            record["reachable"] = False
            record["connectionError"] = str(exc)
    return records


def selected_client(args: argparse.Namespace, directory: Path) -> AbaqusBridgeClient:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if args.session:
            raise ValueError("Use either --session or --port")
        return AbaqusBridgeClient(port=args.port, timeout=args.timeout)
    live = [record for record in discover(directory) if record["reachable"]]
    if args.session:
        live = [record for record in live if record["sessionId"].startswith(args.session)]
    if len(live) != 1:
        if not live:
            raise ValueError("No matching live CAE bridge. Install the plugin, start CAE, and run sessions.")
        raise ValueError("Multiple CAE sessions. Select one using --session: " + ", ".join(record["sessionId"][:8] for record in live))
    return AbaqusBridgeClient(port=int(live[0]["port"]), timeout=args.timeout, session_id=live[0]["sessionId"])


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    directory = config_root(args.config_dir)
    command = args.command
    if command == "install":
        return install(directory, args.plugin_dir, args.plugin_name,
                       args.install_port if args.install_port is not None else args.port, args.skill_dir)
    if command == "uninstall":
        return uninstall(directory)
    if command == "sessions":
        return {"ok": True, "sessions": discover(directory)}
    if command == "doctor":
        receipt_path = directory / "installation.json"
        receipt = read_json(receipt_path) if receipt_path.exists() else None
        return {"ok": True, "version": __version__, "python": sys.executable,
                "configDirectory": str(directory), "installation": receipt,
                "pluginExists": bool(receipt and Path(receipt["pluginPath"]).is_file())}
    source_filename = None
    code = None
    if command == "run-python":
        if args.file:
            path = Path(args.file).expanduser().resolve(strict=True)
            with tokenize.open(path) as handle:
                code = handle.read()
            source_filename = str(path)
        elif args.stdin:
            code = sys.stdin.read()
        else:
            code = args.code
        if not code or not code.strip():
            raise ValueError("code must not be empty")
    client = selected_client(args, directory)
    if command == "ping":
        session = client.ping()
        return {"ok": True, "executionId": session.get("executionId"), "session": session}
    if command == "run-python":
        return operations.run_python(client, code, source_filename)
    if command == "set-workdir":
        return operations.set_workdir(client, args.path)
    if command == "monitor-job-status":
        return operations.monitor_job_status(client, args.job_name, args.terminate, args.since)
    if command == "inspect-odb":
        keys = ("odb_path", "step", "frame", "variable", "set_name", "component", "history_region", "history_variable", "max_points")
        return operations.inspect_odb(client, **{key: getattr(args, key) for key in keys})
    if command == "capture-viewport":
        return operations.capture_viewport(client, args.viewport_name, args.format,
                                           str(Path(args.out).expanduser().resolve()))
    raise ValueError(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        args = parser().parse_args(argv)
        output = dispatch(args)
        exit_code = 0 if output.get("ok") else 1
    except Exception as exc:
        output = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        if isinstance(exc, BridgeError):
            output.update(executionId=exc.execution_id, state=exc.state)
            output["error"]["type"] = exc.error_type
            exit_code = 3 if exc.state == "UNKNOWN" else 1
        else:
            exit_code = 2
    print(json.dumps(output, ensure_ascii=False, allow_nan=False, default=str))
    raise SystemExit(exit_code)
