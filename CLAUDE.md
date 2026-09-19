# kiwrap — working conventions

Read `docs/ARCHITECTURE.md` (§2 capability matrix, §6 version isolation, §11 receipts/gates, §14
SWIG checklist) and `docs/DECISIONS.md` before changing anything. `docs/VERIFICATION.md` is the
evidence; `spikes/` holds the scripts that produced it — reuse them.

## Layout

```
src/kiwrap/            single MIT distribution, src layout (hatchling)
  platform/            Windows/Linux seam — the ONLY place allowed to look at sys.platform
  installation.py      KiCadInstallation + discover()/find()
  capability.py        capabilities_for(install) keyed on FULL semver; data/kicad_version_matrix.json
  sandbox.py           sandboxed KICAD_CONFIG_HOME roots; user config is read-only
  formats/             zero-dependency: sexpr CST, epochs, report/netlist parsers (no kiwrap imports)
  backends/cli/        kicad-cli adapters + quirk normalisation   backends/swig/  driver launcher
  core/                MCP server, kw_* tools, state (receipts, errors, stages, locks, snapshots, jobs)
swigdriver/            GPL-3.0-or-later, stdlib only, run by <install>/bin/python.exe; never imported by src/
conformance/           T1–T9 certification tests + corpus + snapshots (per KiCad version)
tests/                 unit tests (no KiCad needed)
scripts/               bootstrap.ps1 / bootstrap.sh, seed_config_root.py, ci helpers
```

## Rules

- Python 3.11+, `uv` workspace: `uv sync --all-extras`, `uv run pytest`, `uv run ruff check`,
  `uv run lint-imports`. Do not add dependencies without saying so in your report.
- **No KiCad version numbers outside `kiwrap/backends/` and `kiwrap/data/`.** Branch on capability
  keys (`caps.has("cli.has_drc")`), never on `install.major`.
- `kiwrap.formats` imports nothing from the rest of kiwrap and no third-party packages.
- Every KiCad process runs through `kiwrap.sandbox` (`ConfigRoot.env()`, `lock()`, `restore_seed()`).
  **Never write to the user's `%APPDATA%\kicad` / `~/.config/kicad`.** Tests assert its hash.
- Never launch a KiCad GUI in tests unless the test is marked `gui`; never more than one at a time;
  always with a sandbox root and a short TEMP; always kill the process you started (by PID).
- Tests needing KiCad are marked `kicad` (auto-skip if none discovered) or `kicad_version("10.0")`.
  Use `tests/conftest.py` fixtures: `installs`, `install10`, `config_root(install)`, `corpus`.
- Corpus files that come from KiCad's own demos are copied from `<install>/share/kicad/demos`
  at test time (not committed). Hand-written minimal files live in `conformance/corpus/`.
- Tool results are `Receipt`s (`kiwrap.core.state`); failures are `KiwrapError`s with a stable
  code and a `fix`. Positions are always millimetres; ERC JSON positions from kicad-cli 8–10 must be
  multiplied by `cli.erc_json_pos_scale` (100) — including inside `description` strings.
- Windows paths: use `pathlib`, quote nothing manually, pass argv lists to `subprocess`.
- Scratch/temporary files go under `Platform.temp_dir` or pytest's `tmp_path`, never the repo.
- Style: ruff (line length 110), type hints everywhere, dataclasses over dicts, docstrings that say
  which verified fact a piece of code encodes (cite `docs/VERIFICATION.md` claim numbers).
- Do not commit; the orchestrating session commits. Do not edit files outside your assigned paths;
  if a contract in `src/kiwrap/**/__init__.py`, `installation.py`, `capability.py`, `sandbox.py`
  or `core/state/__init__.py` is wrong for you, implement against it anyway and report the change
  you need.
