---
name: abaqus-cae-skill
description: Install and use a local bridge to inspect or modify the active Abaqus/CAE session, run Python code or script files, query jobs and ODB results, and capture viewports. Supports collaborative edits to existing models.
---

# Abaqus/CAE

Use `abaqus-cae` to operate the selected, running CAE session. It executes Python
in the CAE kernel with `mdb` and `session`; it does not launch a separate model.
Choose modelling and analysis methods from the user's request.

## Installation

Install this repository with `uv tool install <repository-path>` (or `uv sync`
and prefix commands with `uv run` when working from source). Use
`abaqus-cae --help` and `<command> --help` for arguments.

Run `abaqus-cae install --plugin-dir <CAE-plugin-directory>
--plugin-name <unique_name_plugin.py> --port <chosen-port>`.
Choose the directory, Python module filename and port for this machine; inspect
existing files first. Omit name/port for collision-resistant defaults, or use
`--port 0` for an OS-selected port at startup. Installation never overwrites an
unrelated or modified plugin. Add `--skill-dir <agent-skills-parent>` to install
this Skill too. The default plugin directory is `~/abaqus_plugins`.

The plugin auto-starts when CAE starts normally. An already running CAE needs a
restart to load it; preserve the user's unsaved work. No Start menu click or
environment-file edit is required. `doctor` shows the recorded installation.
`uninstall` removes only unchanged files belonging to it.

## Connection

Run `abaqus-cae sessions`. With one live session it is selected automatically;
with several use `abaqus-cae --session <id-or-unique-prefix> <command>`.
Global options go **before** the command: `--config-dir`, `--session`, `--port`,
`--timeout`. `ABAQUS_CAE_CONFIG_DIR` also selects a configuration directory.
`--port` explicitly connects to a loopback port; normally use session discovery.

## Tools

| Command | Purpose and inputs |
|---|---|
| `ping` | CAE version, process, models and viewports. |
| `run-python --code <code>` | Execute inline Python, including small edits. |
| `run-python --file <path>` | Read a script on this machine and execute it inside CAE; supports `__file__`, `__main__` and sibling imports. |
| `run-python --stdin` | Execute multiline Python from standard input. |
| `set-workdir <absolute-path>` | Change the CAE working directory to an existing directory. |
| `monitor-job-status [--job-name NAME] [--since UNIX_SECONDS] [--terminate]` | List jobs or inspect one job's state/log evidence; optionally request its termination. |
| `inspect-odb PATH [--step NAME] [--frame -1] [--variable U] [--component U2] [--set-name SET]` | Read ODB metadata or a field summary. Also supports history-region, history-variable and max-points. |
| `capture-viewport --out PATH [--viewport-name NAME] [--format PNG]` | Save PNG/TIFF/SVG/EPS/PS; parent directory must exist, extension must match format. Read the returned `savedPath` with the agent's image capability. |

The three Python inputs are mutually exclusive and share a persistent execution
namespace; all use the current CAE model database. Expressions return their value;
statements can assign `result` for the returned value. `result` is cleared for each
request. File execution temporarily sets its file/main/import context; it does
not change the CAE working directory.

Commands return one JSON object with `ok`, operation data, and `executionId` for
executions. Python output appears in `stdout`/`stderr` fields. Exit codes: 0
success, 1 execution/connection failure, 2 input/setup error, 3 unknown execution
outcome. `NOT_STARTED`/`CANCELLED` means the request did not execute; `UNKNOWN`
means it may have executed. Errors do not roll back model changes. There is no
automatic retry. Job `state` is distinct from the command's `ok`.
