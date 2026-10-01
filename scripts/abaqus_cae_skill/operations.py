"""Synchronous operations shared by the standalone CLI."""

from __future__ import annotations

from typing import Any

from .client import AbaqusBridgeClient
from .templates import _CAPTURE_CODE, _JOB_CODE, _ODB_CODE


def code_for(template: str, **values: Any) -> str:
    # Substitute in one pass: a user path containing another placeholder is literal.
    import re
    replacements = {"__" + key.upper() + "__": repr(value) for key, value in values.items()}
    return re.sub(r"__[A-Z_]+__", lambda match: replacements.get(match.group(), match.group()), template)


def execution_result(execution: dict[str, Any], operation: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {"ok": bool(execution.get("ok")),
                            "executionId": execution.get("executionId") or execution.get("requestId")}
    if data["ok"]:
        value = execution.get("return_value")
        if operation:
            if not isinstance(value, dict):
                raise ValueError("Bridge operation did not return an object")
            data.update(value)
            data.pop("imageBase64", None)
        else:
            data["value"] = value
    else:
        data["error"] = {"type": execution.get("error_type", "ExecutionError"),
                         "message": execution.get("message") or execution.get("core_error"),
                         "at": execution.get("primary_frame"), "line": execution.get("error_line"),
                         "code": execution.get("code_line"), "recovery": execution.get("recovery", {}),
                         "frames": execution.get("frames", [])}
        data["state"] = "EXECUTION_ERROR"
    for stream in ("stdout", "stderr"):
        if execution.get(stream):
            data[stream] = execution[stream]
    return data


def run_python(client: AbaqusBridgeClient, code: str, source_filename: str | None = None) -> dict[str, Any]:
    if not code.strip():
        raise ValueError("code must not be empty")
    return execution_result(client.execute(code, source_filename))


def set_workdir(client: AbaqusBridgeClient, path: str) -> dict[str, Any]:
    code = code_for("""
import os
target = __PATH__
if not os.path.isabs(target):
    raise ValueError('path must be absolute')
if not os.path.isdir(target):
    raise FileNotFoundError(target)
previous = os.getcwd()
os.chdir(target)
result = {'previous': previous, 'current': os.getcwd()}
""", path=path)
    return execution_result(client.execute(code), operation=True)


def monitor_job_status(client: AbaqusBridgeClient, job_name: str = "", terminate: bool = False,
                       since: float | None = None) -> dict[str, Any]:
    if terminate and not job_name.strip():
        raise ValueError("job-name is required for termination")
    if since is not None and not 0 <= since <= 4102444800:
        raise ValueError("since must be a Unix timestamp between 1970 and 2100")
    code = code_for(_JOB_CODE, job_name=job_name.strip(), terminate=terminate, since=since)
    return execution_result(client.execute(code), operation=True)


def inspect_odb(client: AbaqusBridgeClient, **values: Any) -> dict[str, Any]:
    if not 2 <= values["max_points"] <= 1000:
        raise ValueError("max-points must be between 2 and 1000")
    return execution_result(client.execute(code_for(_ODB_CODE, **values)), operation=True)


def capture_viewport(client: AbaqusBridgeClient, viewport_name: str, image_format: str,
                     save_path: str) -> dict[str, Any]:
    return execution_result(client.execute(code_for(_CAPTURE_CODE, viewport_name=viewport_name,
                           image_format=image_format, save_path=save_path)), operation=True)
