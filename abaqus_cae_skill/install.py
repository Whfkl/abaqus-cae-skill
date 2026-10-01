"""Install standalone GUI code without overwriting unrelated plugins."""

from __future__ import annotations

import hashlib
import keyword
import socket
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .config import read_json, write_json


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def choose_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def install(root: Path, plugin_dir: str | None = None, plugin_name: str | None = None,
            port: int | None = None, skill_dir: str | None = None) -> dict[str, Any]:
    receipt_path = root / "installation.json"
    previous = read_json(receipt_path) if receipt_path.exists() else None
    installation_id = previous["installationId"] if previous else uuid.uuid4().hex
    directory = Path(plugin_dir).expanduser().resolve() if plugin_dir else (
        Path(previous["pluginPath"]).parent if previous else Path.home() / "abaqus_plugins")
    name = plugin_name or (Path(previous["pluginPath"]).name if previous else
                           f"abaqus_cae_{installation_id[:8]}_plugin.py")
    if (Path(name).name != name or not name.endswith("_plugin.py")
            or not name[:-3].isidentifier() or keyword.iskeyword(name[:-3])):
        raise ValueError("plugin-name must be a Python module filename ending in _plugin.py")
    selected_port = port if port is not None else (previous["preferredPort"] if previous else choose_port())
    if not 0 <= selected_port <= 65535:
        raise ValueError("port must be between 0 (OS-selected) and 65535")
    target = directory / name
    old_target = Path(previous["pluginPath"]) if previous else None
    if old_target is not None and target != old_target:
        raise ValueError("Installation already exists at %s; uninstall it before changing its path" % old_target)
    if target.exists() and (not previous or digest(target.read_bytes()) != previous["pluginSha256"]):
        raise FileExistsError(f"Refusing to overwrite an unrelated or modified plugin: {target}")
    settings = {"schema": 1, "installationId": installation_id, "preferredPort": selected_port,
                "configDirectory": str(root), "pluginPath": str(target), "version": __version__}
    original = resources.files("abaqus_cae_skill").joinpath("gui_plugin.py").read_text(encoding="utf-8")
    marker = "# __ABAQUS_CAE_INSTALL_CONFIG__"
    if marker not in original:
        raise RuntimeError("GUI template has no installation marker")
    source = original.replace(marker, "_INSTALL_CONFIG = " + repr(settings))
    data = source.encode("utf-8")
    skill_target = Path(skill_dir).expanduser().resolve() / "abaqus-cae-skill" / "SKILL.md" if skill_dir else None
    skill_bytes = None
    if skill_target:
        packaged = resources.files("abaqus_cae_skill").joinpath("skill/SKILL.md")
        local = Path(__file__).resolve().parent.parent / "skills/abaqus-cae-skill/SKILL.md"
        skill_bytes = local.read_bytes() if local.is_file() else packaged.read_bytes()
        previous_skill = (previous or {}).get("skill") or {}
        if previous_skill.get("path") and str(skill_target) != previous_skill["path"]:
            raise ValueError("Skill already installed elsewhere; uninstall before changing its path")
        if skill_target.exists() and skill_target.read_bytes() != skill_bytes:
            if str(skill_target) != previous_skill.get("path") or digest(skill_target.read_bytes()) != previous_skill.get("sha256"):
                raise FileExistsError(f"Refusing to overwrite a modified Skill: {skill_target}")
    directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # The receipt hash was checked above; this is an update of our own file.
        temporary = target.with_suffix(".tmp")
        with temporary.open("xb") as handle:
            handle.write(data)
        temporary.replace(target)
    else:
        with target.open("xb") as handle:
            handle.write(data)
    receipt = {**settings, "pluginSha256": digest(data)}
    if skill_target and skill_bytes is not None:
        skill_target.parent.mkdir(parents=True, exist_ok=True)
        skill_target.write_bytes(skill_bytes)
        receipt["skill"] = {"path": str(skill_target), "sha256": digest(skill_bytes)}
    elif previous and previous.get("skill"):
        receipt["skill"] = previous["skill"]
    write_json(receipt_path, receipt)
    return {"ok": True, "installation": receipt, "configPath": str(receipt_path),
            "next": "Start Abaqus/CAE normally. The bridge starts automatically; then run abaqus-cae sessions."}


def uninstall(root: Path) -> dict[str, Any]:
    receipt_path = root / "installation.json"
    if not receipt_path.exists():
        return {"ok": True, "removed": [], "message": "No installation registered"}
    receipt = read_json(receipt_path)
    owned = [(Path(receipt["pluginPath"]), receipt["pluginSha256"])]
    if receipt.get("skill"):
        owned.append((Path(receipt["skill"]["path"]), receipt["skill"]["sha256"]))
    for path, expected in owned:
        if path.exists() and digest(path.read_bytes()) != expected:
            raise ValueError(f"Refusing to remove a modified file: {path}")
    removed = []
    for path, _ in owned:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    receipt_path.unlink()
    return {"ok": True, "removed": removed, "message": "Running CAE sessions keep their loaded bridge until closed"}
