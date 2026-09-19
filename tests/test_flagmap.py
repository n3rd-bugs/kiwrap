"""`--help` parsing and the recursive help-tree probe, against the spike corpus and a fake kicad-cli.

The corpus in spikes/versioning-strategy/clihelp/ is the verbatim stdout+stderr of
`kicad-cli <path> --help` for 7.0 / 8.0 / 9.0 / 10.0 captured on 2026-09-18.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from kiwrap.backends.cli import flagmap
from kiwrap.capability import flatten_help_tree, parse_help, probe_help_tree
from kiwrap.platform.system import RunResult, System

CORPUS = Path(__file__).resolve().parent.parent / "spikes" / "versioning-strategy" / "clihelp"


def corpus(version: str, name: str) -> str:
    path = CORPUS / f"{version}__{name}.txt"
    if not path.exists():
        pytest.skip(f"spike corpus file {path.name} missing")
    return path.read_text(encoding="utf-8", errors="replace")


class TestParseHelp:
    def test_root_10(self):
        parsed = parse_help(corpus("10.0", "root"))
        assert parsed["command"] == "kicad-cli"
        assert parsed["subcommands"] == ["fp", "jobset", "pcb", "sch", "sym", "version"]
        assert parsed["flags"] == ["--version", "--help"]

    def test_root_7_tab_separated(self):
        parsed = parse_help(corpus("7.0", "root"))
        assert parsed["subcommands"] == ["fp", "pcb", "sch", "sym", "version"]
        assert "--help" in parsed["flags"] and "--version" in parsed["flags"]

    def test_leaf_7_error_line_before_usage_is_ignored(self):
        text = corpus("7.0", "pcb_export_gerbers")
        assert text.startswith("input:")  # stderr noise captured first on 7/8
        parsed = parse_help(text)
        assert parsed["command"] == "gerbers"
        assert parsed["positionals"] == ["input"]
        assert parsed["flags"][:3] == ["--help", "--output", "--layers"]
        assert {"--erd", "--exclude-refdes", "--cl", "--common-layers", "--board-plot-params"} <= set(
            parsed["flags"]
        )
        assert "-o" not in parsed["flags"] and "-h" not in parsed["flags"]
        assert parsed["subcommands"] == []

    def test_drc_10_has_refill_flags(self):
        parsed = parse_help(corpus("10.0", "pcb_drc"))
        assert parsed["command"] == "pcb drc"
        assert parsed["positionals"] == ["INPUT_FILE"]
        assert {"--refill-zones", "--save-board", "--format", "--exit-code-violations"} <= set(
            parsed["flags"]
        )
        assert parsed["description"].startswith("Runs the Design Rules Check")

    def test_drc_8_lacks_refill_flags(self):
        parsed = parse_help(corpus("8.0", "pcb_drc"))
        assert "--refill-zones" not in parsed["flags"]
        assert "--define-var" in parsed["flags"]

    def test_continuation_lines_do_not_create_flags(self):
        parsed = parse_help(corpus("10.0", "sch_export_netlist"))
        assert "--variant" in parsed["flags"]
        assert all(f.startswith("--") for f in parsed["flags"])

    def test_missing_subcommand_prints_parent_usage(self):
        # 7.0 has no `pcb drc`; asking for it prints the `pcb` usage (spike file kept it verbatim)
        parsed = parse_help(corpus("7.0", "pcb_drc"))
        assert parsed["command"] == "pcb"
        assert parsed["subcommands"] == ["export"]

    def test_hyphenated_subcommand_name(self):
        parsed = parse_help(corpus("10.0", "sch_export"))
        assert "python-bom" in parsed["subcommands"]

    def test_empty_text(self):
        parsed = parse_help("")
        assert parsed == {
            "subcommands": [],
            "flags": [],
            "positionals": [],
            "usage": "",
            "command": "",
            "description": "",
        }


# --- a fake kicad-cli for the recursive probe -----------------------------------------------------

FAKE_PAGES: dict[tuple[str, ...], tuple[int, str]] = {
    (): (
        0,
        "Usage: kicad-cli [--version] [--help] {pcb,version}\n\nOptional arguments:\n"
        "  -v, --version  prints version information and exits \n  -h, --help     Shows help message and exits \n\n"
        "Subcommands:\n  pcb           PCB\n  version       Reports the version info\n",
    ),
    ("pcb",): (
        0,
        "Usage: kicad-cli pcb [--help] {drc,export}\n\nPCB\n\nOptional arguments:\n  -h, --help  Shows help message and exits \n\n"
        "Subcommands:\n  drc        Runs DRC\n  export     Export utilities\n",
    ),
    ("pcb", "drc"): (
        1,
        "Usage: pcb drc [--help] [--output OUTPUT_FILE] [--refill-zones] INPUT_FILE\n\nRuns DRC\n\n"
        "Positional arguments:\n  INPUT_FILE   Input file \n\nOptional arguments:\n"
        "  -h, --help      Shows help message and exits \n  -o, --output    Output file \n  --refill-zones  Refill zones \n",
    ),
    ("pcb", "export"): (
        0,
        "Usage: export [--help] {gerbers}\n\nOptional arguments:\n  -h, --help  Shows help message and exits \n\n"
        "Subcommands:\n  gerbers    Plot gerbers\n",
    ),
    ("pcb", "export", "gerbers"): (
        1,
        "Usage: export gerbers [--help] [--layers LAYER_LIST] INPUT_FILE\n\nPositional arguments:\n  INPUT_FILE  Input file \n\n"
        "Optional arguments:\n  -h, --help    Shows help message and exits \n  -l, --layers  Layers \n",
    ),
    # `version` is listed by the root but this fake "version" prints its parent's usage -> dropped
    ("version",): (
        0,
        "Maximum number of positional arguments exceeded\nUsage: kicad-cli [--version] [--help] {pcb,version}\n",
    ),
}


class FakeCli(System):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[Mapping[str, str]] = []

    def run(
        self, argv: Iterable[str], *, env: Mapping[str, str], timeout_s: float = 60, cwd: Path | None = None
    ) -> RunResult:
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        self.envs.append(env)
        assert args[-1] == "--help"
        code, text = FAKE_PAGES.get(args[1:-1], (2, ""))
        return RunResult(args, code, text, "")


def test_probe_help_tree_recurses_and_drops_phantom_nodes():
    system = FakeCli()
    tree = probe_help_tree(Path("kicad-cli"), system, {"KICAD_CONFIG_HOME": "x"})
    assert tree["command"] == "" and tree["exit_code"] == 0
    assert set(tree["subcommands"]) == {"pcb"}  # "version" printed the parent usage -> not a node
    drc = tree["subcommands"]["pcb"]["subcommands"]["drc"]
    assert drc["flags"] == ["--help", "--output", "--refill-zones"] and drc["exit_code"] == 1
    gerbers = tree["subcommands"]["pcb"]["subcommands"]["export"]["subcommands"]["gerbers"]
    assert gerbers["command"] == "pcb export gerbers" and gerbers["positionals"] == ["INPUT_FILE"]
    assert all(e["KICAD_CONFIG_HOME"] == "x" for e in system.envs)
    assert ("kicad-cli", "pcb", "export", "gerbers", "--help") in system.calls

    flat = flatten_help_tree(tree)
    assert flat["cli.subcommands"] == ["pcb", "pcb drc", "pcb export", "pcb export gerbers"]
    assert flat["cli.flags.pcb export gerbers"] == ["--help", "--layers"]
    assert flat["cli.has_drc"] is True and flat["cli.has_erc"] is False
    assert flat["cli.drc_refill_flags"] is False  # --save-board missing
    assert flat["cli.help_exit_code"] == 1  # the max over all nodes: leaves exited 1


def test_probe_help_tree_without_usage_raises():
    class Broken(FakeCli):
        def run(self, argv, *, env, timeout_s=60, cwd=None):
            return RunResult(tuple(argv), 127, "", "not found")

    with pytest.raises(RuntimeError):
        probe_help_tree(Path("kicad-cli"), Broken(), {})


def test_flagmap_reexports_and_help_tree_without_config(tmp_path, monkeypatch):
    """`flagmap.help_tree(install)` with no ConfigRoot probes via the throwaway env and caches."""
    from kiwrap import capability
    from kiwrap.installation import KiCadInstallation

    assert flagmap.parse_help is parse_help
    system = FakeCli()
    monkeypatch.setattr(capability, "System", lambda: system)
    install = KiCadInstallation(tmp_path, tmp_path / "kicad-cli", tmp_path / "share", "10.0.0", "explicit")
    tree = flagmap.help_tree(install, storage_home=tmp_path / "home")
    assert "pcb" in tree["subcommands"]
    assert system.envs[0]["KICAD_CONFIG_HOME"].startswith(str(tmp_path / "home" / "probe-config"))
    calls = len(system.calls)
    again = flagmap.help_tree(install, storage_home=tmp_path / "home")
    assert again == tree and len(system.calls) == calls  # served from the cache
    flagmap.help_tree(install, storage_home=tmp_path / "home", refresh=True)
    assert len(system.calls) > calls


def test_help_tree_with_config_root_locks_and_restores_seed(tmp_path, monkeypatch):
    """Finding: the ConfigRoot form ran the whole probe under lock()/env() but never restored the
    seed kicad-cli rewrites on exit; every KiCad process runs env() -> lock() -> restore_seed()."""
    from kiwrap import capability
    from kiwrap.installation import KiCadInstallation
    from tests._fakes import FakeConfigRoot

    system = FakeCli()
    monkeypatch.setattr(capability, "System", lambda: system)
    install = KiCadInstallation(tmp_path, tmp_path / "kicad-cli", tmp_path / "share", "10.0.0", "explicit")
    root = FakeConfigRoot.create(install, tmp_path / "root")
    tree = flagmap.help_tree(install, root, storage_home=tmp_path / "home", refresh=True)
    assert "pcb" in tree["subcommands"]
    assert system.envs[0]["KICAD_CONFIG_HOME"] == str(root.path)
    assert root.calls.index("lock") < root.calls.index("env") or "env" in root.calls
    assert root.count("lock") == 1 and root.count("restore_seed") == 1
    assert root.calls[-1] == "restore_seed"


def test_t4_diff_trees_reports_every_kind_of_drift():
    """The conformance canary's differ (loaded by path: conformance/tests is not an importable package)."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "conformance" / "tests" / "t4_cli_help_snapshot.py"
    spec = importlib.util.spec_from_file_location("t4_cli_help_snapshot", path)
    assert spec and spec.loader
    t4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t4)

    old = probe_help_tree(Path("kicad-cli"), FakeCli(), {})
    assert t4.diff_trees(old, old) == []
    new = json.loads(json.dumps(old))
    new["subcommands"]["pcb"]["subcommands"]["drc"]["flags"].append("--save-board")
    new["subcommands"]["pcb"]["subcommands"]["drc"]["exit_code"] = 0
    del new["subcommands"]["pcb"]["subcommands"]["export"]["subcommands"]["gerbers"]
    new["subcommands"]["pcb"]["subcommands"]["render"] = {
        "command": "pcb render",
        "flags": ["--help"],
        "positionals": ["INPUT_FILE"],
        "description": "new",
        "exit_code": 0,
        "subcommands": {},
    }
    new["subcommands"]["pcb"]["description"] = "translated text must not count"
    lines = t4.diff_trees(old, new)
    assert any("removed: 'pcb export gerbers'" in line for line in lines)
    assert any("added: 'pcb render'" in line for line in lines)
    assert any("'pcb drc': flags +['--save-board']" in line for line in lines)
    assert any("'pcb drc': --help exit 1 -> 0" in line for line in lines)
    assert len(lines) == 4
