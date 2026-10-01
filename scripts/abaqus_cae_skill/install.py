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


def skill_files() -> dict[str, bytes]:
    """Assemble a self-contained Skill from either source or an installed wheel."""
    package = resources.files("abaqus_cae_skill")
    local_root = Path(__file__).resolve().parents[2]
    assets = ("SKILL.md", "agents/openai.yaml", "scripts/abaqus_cae.py", "LICENSE", "NOTICE.md",
              "assets/icon.png", "assets/icon.svg")
    from_source = all((local_root / name).is_file() for name in assets)
    files = {name: (local_root / name).read_bytes() if from_source else
             package.joinpath("skill", *name.split("/")).read_bytes() for name in assets}
    for module in package.iterdir():
        if module.is_file() and module.name.endswith(".py"):
            files["scripts/abaqus_cae_skill/" + module.name] = module.read_bytes()
    return files


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
    bundled_files = None
    if skill_target:
        bundled_files = skill_files()
        previous_skill = (previous or {}).get("skill") or {}
        if previous_skill.get("path") and str(skill_target) != previous_skill["path"]:
            raise ValueError("Skill already installed elsewhere; uninstall before changing its path")
        previous_files = previous_skill.get("files") or {}
        if previous_skill.get("path") and previous_skill.get("sha256"):
            previous_files = {previous_skill["path"]: previous_skill["sha256"], **previous_files}
        for name, content in bundled_files.items():
            destination = skill_target.parent / name
            if destination.exists() and destination.read_bytes() != content:
                if digest(destination.read_bytes()) != previous_files.get(str(destination)):
                    raise FileExistsError(f"Refusing to overwrite a modified Skill file: {destination}")
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
    if skill_target and bundled_files is not None:
        owned_files = {}
        for name, content in bundled_files.items():
            destination = skill_target.parent / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or destination.read_bytes() != content:
                destination.write_bytes(content)
            owned_files[str(destination)] = digest(content)
        receipt["skill"] = {"path": str(skill_target), "sha256": digest(bundled_files["SKILL.md"]),
                            "files": owned_files}
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
        skill = receipt["skill"]
        if skill.get("files"):
            owned.extend((Path(path), expected) for path, expected in skill["files"].items())
        else:
            owned.append((Path(skill["path"]), skill["sha256"]))
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
