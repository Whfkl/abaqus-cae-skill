# Validation — 2026-10-01

## Automated checks

- 13 behavioral tests pass with Python 3.14.4.
- Skill Creator's `quick_validate.py` accepts the single-file Skill.
- Wheel and source distribution build successfully. The wheel includes the GUI
  plugin and SKILL.md and declares **no runtime dependencies**.
- A fresh virtual environment installed the wheel without dependencies, then
  installed, diagnosed and uninstalled the plugin and Skill from outside the
  source directory. This verifies the packaged resources and CLI entrypoint.
- Coverage includes non-overwriting installation, owned-file updates/uninstall,
  Skill-file preservation, deferred automatic startup and GUI-thread dispatch,
  persistent variables, cleared `result`, code/file/stdin input, Chinese filenames,
  sibling imports, temporary script context, source-file error location, execution
  failure exit codes, unknown-outcome exit code without retries, request identity,
  session selection, cancelled queued requests, and port-conflict discovery.

## Real Abaqus/CAE 2024

Validated a dedicated GUI session on Windows using Abaqus Python 3.10.5:

- Normal CAE startup imported the installed `*_plugin.py`; the bridge started
  automatically without clicking a menu. Installation used an isolated plugin
  directory and configuration directory; no user environment file was changed.
- `ping` reported dispatch on `MainThread` and the real `ABQcaeK.exe` kernel.
  A final plugin reinstall/restart also verified release `2024`, working-directory
  and session/installation metadata.
- A Chinese-named script imported a sibling module and created a small truss in
  the active model database. A separate inline call changed its elastic modulus.
  Stdin execution observed the same model and confirmed no `__file__` leakage.
- An intentional script exception returned exit 1 with the actual file and line.
- The job progressed through SUBMITTED/RUNNING and a brief conflicting-evidence
  UNKNOWN state before COMPLETED. No command automatically retried the solve.
- `inspect-odb` read the solved displacement field: maximum U1
  `4.99999998737621e-7 m`; analytical `FL/(EA) = 5e-7 m`; relative error
  `2.524757882144471e-9` for this small linear transport/API test.
- `capture-viewport` saved a visually inspected PNG (907 × 390, 1,062 bytes).

The first attempt launched CAE inside the restricted execution environment.
Solver startup reported that available CPUs were `-1`; modelling and bridge calls
still worked. A new dedicated session in the normal user environment solved
successfully (machine reports 12 logical processors). Treat the first attempt as
an environment limitation, not a passed solver check.

Local evidence and model/INP/ODB/image artifacts are under
`.verification/cae/live-normal/`; they are ignored by Git. The reproducible
integration runner is `tests/live_smoke.py`. Start a dedicated empty CAE session,
then supply its exact ID:

```powershell
uv run python tests/live_smoke.py --config-dir <config-directory> --session <id> --workdir <test-directory>
```

## Limits

Real validation covered one CAE version and one small linear job. It does not
establish general engineering accuracy. Multiple-session ambiguity and port
conflict were exercised with the simulated bridge; termination of a running
solver, all ODB history/set options, and TIFF/SVG/EPS/PS have not been validated
against a real solve. CAE modes suppressing plugin discovery are unsupported.
