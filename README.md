# abaqus-cae-skill

A thin Agent Skill and a dependency-free Python CLI for the **currently running
Abaqus/CAE session**. Run inline code, script files or stdin; make small edits to
an existing model; inspect jobs/ODB data; save viewport images.

The standalone GUI plugin starts with CAE. No MCP server is needed. The execution
bridge and diagnostics derive from [Abaqus-Control-MCP](https://github.com/Whfkl/Abaqus-Control-MCP);
see [NOTICE.md](NOTICE.md) and [LICENSE](LICENSE).

## Install

Requires Python 3.10+ and Abaqus/CAE 2024+ (the plugin uses CAE's Python 3).
This repository is a complete Codex Skill directory following the
[official OpenAI skill layout](https://learn.chatgpt.com/docs/build-skills):

```text
abaqus-cae-skill/
├── SKILL.md
├── agents/openai.yaml
├── scripts/
│   ├── abaqus_cae.py
│   └── abaqus_cae_skill/
├── pyproject.toml
└── README.md
```

The bundled CLI runs directly, without installing a Python package:

```powershell
python scripts/abaqus_cae.py install --skill-dir "$env:USERPROFILE\.agents\skills"
```

This installs the CAE plugin and the complete self-contained Skill, including
`agents/openai.yaml` and its executable scripts. Codex can also use a copy of this
repository in a user or repository `.agents/skills/abaqus-cae-skill` directory.
The Skill remains usable after the source checkout is removed; invoke
`python <installed-skill>/scripts/abaqus_cae.py <command>`.

For a globally available `abaqus-cae` command, optionally install the CLI:

```powershell
uv tool install .
abaqus-cae install --skill-dir "$env:USERPROFILE\.agents\skills"
```

Choose the actual agent's skills directory; the Codex path above is an example.
`--plugin-dir`, `--plugin-name` and `--port` allow an agent to choose installation
settings. Defaults use `~/abaqus_plugins`, a unique `*_plugin.py` filename and a
locally available preferred port. `--port 0` selects at runtime. On a preferred
port conflict the plugin binds another port and publishes it for discovery.
Repeated installation updates its own unchanged file; unrelated/edited files are
never overwritten. No `abaqus_v6.env` modification is required.

Start CAE normally (restart an existing session after saving work), then:

```powershell
abaqus-cae sessions
abaqus-cae ping
abaqus-cae run-python --code "result = list(mdb.models.keys())"
abaqus-cae run-python --file "D:\project\edit_model.py"
abaqus-cae capture-viewport --out "D:\project\viewport.png"
```

With multiple CAE sessions: `abaqus-cae --session ID run-python --code "..."`.
Global connection options go before the command. Commands emit JSON; `--help`
lists all six operations and management commands. Instructions remain in one
file: [SKILL.md](SKILL.md); UI metadata is in [agents/openai.yaml](agents/openai.yaml).

`SKILL.md` and the bundled Python CLI are reusable by other agents that load
Agent Skills and can run local commands. `agents/openai.yaml` supplies OpenAI
UI metadata; it does not implement other agents or their installation/discovery
rules. Use the target agent's supported skill directory and loading mechanism.

Configuration defaults to `%LOCALAPPDATA%\abaqus-cae-skill` on Windows or
`~/.config/abaqus-cae-skill` elsewhere. Override with `ABAQUS_CAE_CONFIG_DIR` or
`--config-dir`. Receipts record ownership; session manifests record actual ports
and instance IDs. Discovery probes instance identity, so stale records are not
silently selected. `doctor` inspects installation; `uninstall` removes only its
unchanged plugin and optionally installed Skill. A running bridge stays loaded
until CAE closes.

## Execution

`--code`, `--file` and `--stdin` are mutually exclusive inputs to the same live
kernel. File execution preserves its filename for errors, temporarily supplies
`__file__`/`__main__` and sibling imports, and leaves CAE's working directory
unchanged. Expressions return their value; statements can set `result`. Variables
persist between calls. Python exceptions return nonzero; timeout outcomes are
explicit and do not trigger retries. Partial model changes are not rolled back.

Viewport capture returns a local file path, which the agent reads as an image.
Job inspection combines CAE status and log evidence; it can return `UNKNOWN`.
The CLI does not prescribe engineering workflows, materials, meshes or validation
criteria. Those belong to the user's task.

## Develop and validate

```powershell
uv sync
uv run abaqus-cae --help
uv build
```

Local `test/` and `tests/` directories are intentionally ignored by Git and
excluded from distribution. When present locally, run them with
`uv run python -m unittest discover -s tests -v`. They exercise installation
ownership, the socket protocol and queued GUI dispatch with a simulated CAE
kernel. Automatic startup depends on normal plugin discovery; CAE launch modes
that suppress plugins are unsupported.
