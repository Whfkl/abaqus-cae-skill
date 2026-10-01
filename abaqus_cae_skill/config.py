"""Installation receipts and per-CAE-session discovery."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def config_root(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    override = os.environ.get("ABAQUS_CAE_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    return base / "abaqus-cae-skill"


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".abaqus-cae-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def session_records(root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted((root / "sessions").glob("*.json")):
        try:
            record = read_json(path)
            if record.get("schema") != 1 or record.get("host") != "127.0.0.1":
                continue
            if not record.get("sessionId") or not 1 <= int(record["port"]) <= 65535:
                continue
            record["manifestPath"] = str(path)
            records.append(record)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return records
