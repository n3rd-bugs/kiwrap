"""Capability matrix, seeding, probes and cache (fakes), plus live self-consistency on installed KiCads."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from unittest import mock

import pytest

from kiwrap import capability
from kiwrap.capability import (
    Capabilities,
    Unsupported,
    cache_file,
    capabilities_for,
    load_matrix,
    matrix_path,
    probe_static,
    seed_for,
)
from kiwrap.installation import KiCadInstallation
from kiwrap.platform.system import RunResult, System

DOCUMENTED_KEYS = [
    "cli.subcommands",
    "cli.has_erc",
    "cli.has_drc",
    "cli.has_render",
    "cli.has_jobset",
    "cli.has_sch_pcb_upgrade",
    "cli.drc_refill_flags",
    "cli.erc_json_pos_scale",
    "cli.gerbers_creates_outdir",
    "cli.writes_prl_on_read",
    "cli.help_exit_code",
    "cli.sch_stamp_gate_needs_generator_version",
    "swig.available",
    "swig.shim",
    "swig.ses_import_headless",
    "swig.write_drc_report_ok",
    "swig.needs_newboard_for_fill",
    "ipc.available",
    "ipc.headless",
    "ipc.schematic",
    "ipc.schematic_write_crashes",
    "ipc.commit_needs_document",
    "ipc.single_fixed_pipe",
    "formats.sch",
    "formats.pcb",
    "formats.sym",
    "formats.pro",
    "formats.pcb_layer_ids_v2",
    "formats.generator_version_token",
    "libs.ships_wrl",
    "rules.default_min_track_width_mm",
    "rules.drc_must_fix",
    "rules.drc_review",
    "rules.drc_cosmetic_prefix",
    "setup.wizard_needs_three_tables",
    "setup.prompt_flags",
]


# --- the JSON matrix -------------------------------------------------------------------------------


class TestMatrix:
    def test_every_documented_key_exists_for_every_version(self):
        m = load_matrix()
        assert len(m.known()) >= 4
        for ver in m.known():
            missing = [k for k in DOCUMENTED_KEYS if k not in m.versions[ver]]
            assert not missing, f"{ver} lacks {missing}"
            for cmd in m.versions[ver]["cli.subcommands"]:
                assert f"cli.flags.{cmd}" in m.versions[ver], (ver, cmd)

    def test_provenance_covers_every_key_with_known_values(self):
        m = load_matrix()
        for ver in m.known():
            assert set(m.provenance[ver]) == set(m.versions[ver]), ver
            assert set(m.provenance[ver].values()) <= {"verified", "docs"}, ver

    def test_inheritance_is_resolved(self):
        raw = json.loads(matrix_path().read_text(encoding="utf-8"))
        assert raw["schema"] == capability.MATRIX_SCHEMA
        m = load_matrix()
        for ver, entry in raw["versions"].items():
            base = entry.get("inherits")
            if base:
                for key, value in m.versions[base].items():
                    assert m.versions[ver][key] == entry["values"].get(key, value)

    def test_versions_are_full_semvers_in_order(self):
        m = load_matrix()
        assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in m.known())
        majors = [int(v.split(".")[0]) for v in m.known()]
        assert majors == sorted(majors)

    def test_monotone_facts_across_known_versions(self):
        """Capabilities the docs describe as '>= X' never disappear in a newer version; formats only grow."""
        m = load_matrix()
        order = m.known()
        for key in (
            "cli.has_erc",
            "cli.has_drc",
            "cli.has_render",
            "cli.has_jobset",
            "ipc.available",
            "formats.pcb_layer_ids_v2",
            "formats.generator_version_token",
        ):
            seen_true = False
            for ver in order:
                seen_true = seen_true or bool(m.versions[ver][key])
                assert not (seen_true and not m.versions[ver][key]), (key, ver)
        for key in ("formats.sch", "formats.pcb", "formats.sym", "formats.pro"):
            stamps = [m.versions[v][key] for v in order]
            assert stamps == sorted(stamps), key


class TestSeedFor:
    def test_exact_match(self):
        m = load_matrix()
        newest = m.known()[-1]
        seed, values, exact, note = seed_for(newest)
        assert seed == newest and exact and note == ""
        assert values["seed.version"] == newest and values["seed.exact"] is True

    def test_highest_known_at_or_below(self):
        m = load_matrix()
        known = m.known()
        v = known[0]
        major, minor, patch = (int(p) for p in v.split("."))
        seed, values, exact, note = seed_for(f"{major}.{minor}.{patch + 1}")
        assert seed == v and not exact and "at or below" in note

    def test_older_than_every_known_of_major(self):
        m = load_matrix()
        v = m.known()[0]
        major, minor, patch = (int(p) for p in v.split("."))
        if patch == 0:
            pytest.skip("oldest known version has patch 0; nothing older to seed")
        seed, _values, exact, note = seed_for(f"{major}.{minor}.0")
        assert seed == v and not exact and "older than every known" in note

    def test_nightly_seeds_from_the_next_major(self):
        """Finding: 10.99.x (a nightly of 11) seeded from 10.0.7 and inherited SWIG=yes / no
        headless api-server / the 10.0 format epochs."""
        m = load_matrix()
        seed, values, exact, note = seed_for("10.99.0")
        assert seed == "11.0.0" and not exact and "nightly of major 11" in note
        assert values["seed.version"] == "11.0.0" and values["seed.note"] == note
        assert values == {**m.versions["11.0.0"], "seed.version": "11.0.0", "seed.exact": False, "seed.note": note}
        # a nightly of a major the matrix does not know yet still lands on the closest entry
        seed, _values, _exact, note = seed_for("11.99.0")
        assert seed == "11.0.0" and "nightly of major 12" in note

    def test_unknown_major_falls_back(self):
        m = load_matrix()
        top = int(m.known()[-1].split(".")[0])
        seed, _values, exact, note = seed_for(f"{top + 5}.0.0")
        assert seed == m.known()[-1] and not exact and "no known release" in note
        seed, _values, exact, note = seed_for("1.0.0")
        assert seed == m.known()[0] and not exact


def test_write_cache_uses_a_unique_temp_name(tmp_path):
    """Finding: a fixed `<file>.json.tmp` collided between two processes probing the same install."""
    from kiwrap.capability import _write_cache

    target = tmp_path / "caps" / "x.json"
    seen: list[str] = []
    real_replace = Path.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    with mock.patch("pathlib.Path.replace", spy):
        _write_cache(target, {"a": 1})
        _write_cache(target, {"a": 2})
    assert json.loads(target.read_text())["a"] == 2
    assert len(seen) == 2 and seen[0] != seen[1] and all(n.endswith(".tmp") for n in seen)
    assert not list((tmp_path / "caps").glob("*.tmp"))


# --- probes + cache with fakes ------------------------------------------------------------------------

ROOT_HELP = (
    "Usage: kicad-cli [--help] {sch}\n\nOptional arguments:\n  -h, --help  Shows help message and exits \n\n"
    "Subcommands:\n  sch        Schematics\n"
)
SCH_HELP = (
    "Usage: kicad-cli sch [--help] {erc}\n\nOptional arguments:\n  -h, --help  Shows help message and exits \n\n"
    "Subcommands:\n  erc        Runs ERC\n"
)
ERC_HELP = (
    "Usage: sch erc [--help] [--format FORMAT] INPUT_FILE\n\nPositional arguments:\n  INPUT_FILE  Input file \n\n"
    "Optional arguments:\n  -h, --help  Shows help message and exits \n  --format    json or report \n"
)


class FakeSystem(System):
    """Real filesystem (pytest tmp_path) + canned kicad-cli."""

    def __init__(self, pages: dict[tuple[str, ...], str] | None = None, fail: bool = False) -> None:
        self.pages = (
            pages if pages is not None else {(): ROOT_HELP, ("sch",): SCH_HELP, ("sch", "erc"): ERC_HELP}
        )
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[Mapping[str, str]] = []

    def run(
        self, argv: Iterable[str], *, env: Mapping[str, str], timeout_s: float = 60, cwd: Path | None = None
    ) -> RunResult:
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        self.envs.append(env)
        if self.fail:
            return RunResult(args, 127, "", "cannot execute")
        return RunResult(args, 0, self.pages.get(args[1:-1], ""), "")


def make_install(tmp_path: Path, version: str, *, swig: bool = True, ipc: bool = True) -> KiCadInstallation:
    root = tmp_path / "kicad"
    (root / "bin" / "Lib" / "site-packages").mkdir(parents=True)
    (root / "bin" / "kicad-cli.exe").write_bytes(b"x")
    python = root / "bin" / "python.exe"
    python.write_bytes(b"x")
    if swig:
        (root / "bin" / "Lib" / "site-packages" / "_pcbnew.pyd").write_bytes(b"x")
    share = root / "share" / "kicad"
    (share / "schemas").mkdir(parents=True)
    if ipc:
        (share / "schemas" / "api.v1.schema.json").write_text("{}")
    return KiCadInstallation(
        root, root / "bin" / "kicad-cli.exe", share, version, "explicit", python, root / "bin"
    )


def test_probe_static_detects_swig_and_ipc(tmp_path):
    inst = make_install(tmp_path, "10.0.0")
    values = probe_static(inst, System())
    assert values["swig.available"] is True and values["swig.module"].endswith("_pcbnew.pyd")
    assert values["swig.python"] == str(inst.python) and values["ipc.available"] is True

    inst2 = make_install(tmp_path / "b", "10.0.0", swig=False, ipc=False)
    values = probe_static(inst2, System())
    assert values == {
        "swig.available": False,
        "swig.module": "",
        "swig.python": str(inst2.python),
        "ipc.available": False,
    }


def test_capabilities_for_seeds_probes_and_caches(tmp_path):
    version = load_matrix().known()[-1]
    inst = make_install(tmp_path, version)
    home = tmp_path / "home"
    system = FakeSystem()

    caps = capabilities_for(inst, storage_home=home, system=system)
    assert isinstance(caps, Capabilities) and caps.probed and caps.install is inst
    assert caps["seed.version"] == version and caps["seed.exact"] is True
    # probe results override the seed's cli.* wholesale
    assert caps["cli.subcommands"] == ["sch", "sch erc"]
    assert caps.flags("sch erc") == ["--help", "--format"] and caps.flags("pcb drc") == []
    assert caps.has("cli.has_erc") and not caps.has("cli.has_drc")
    assert not any(k.startswith("cli.flags.pcb") for k in caps)
    assert caps["swig.available"] is True and caps["ipc.available"] is True
    assert caps["probe.ok"] is True
    # non-cli seed values survive
    assert caps["formats.sch"] == load_matrix().versions[version]["formats.sch"]
    # every kicad-cli call used the throwaway config home under the storage home
    assert all(Path(e["KICAD_CONFIG_HOME"]).is_relative_to(home / "probe-config") for e in system.envs)
    assert all(e["KICAD_DOCUMENTS_HOME"] != e["KICAD_CONFIG_HOME"] for e in system.envs)

    path = cache_file(inst, home)
    assert path.exists() and path.parent == home / "capabilities"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == capability.CACHE_SCHEMA and raw["install"] == inst.key() and raw["probed"] is True

    calls = len(system.calls)
    cached = capabilities_for(inst, storage_home=home, system=system)
    assert dict(cached) == dict(caps) and cached.probed and len(system.calls) == calls

    capabilities_for(inst, storage_home=home, system=system, refresh=True)
    assert len(system.calls) > calls

    # a changed kicad-cli binary invalidates the cache
    inst.kicad_cli.write_bytes(b"xy")
    calls = len(system.calls)
    capabilities_for(inst, storage_home=home, system=system)
    assert len(system.calls) > calls


def test_capabilities_for_keeps_seed_when_probe_fails(tmp_path):
    version = load_matrix().known()[-1]
    inst = make_install(tmp_path, version)
    caps = capabilities_for(inst, storage_home=tmp_path / "home", system=FakeSystem(fail=True))
    assert caps.probed is False and caps["probe.ok"] is False
    assert caps["cli.subcommands"] == load_matrix().versions[version]["cli.subcommands"]
    assert caps["swig.available"] is True  # static probes still ran
    # a failed probe is cached as such and re-probed on refresh
    again = capabilities_for(inst, storage_home=tmp_path / "home", system=FakeSystem())
    assert again.probed is False
    again = capabilities_for(inst, storage_home=tmp_path / "home", system=FakeSystem(), refresh=True)
    assert again.probed is True


def test_capabilities_for_inexact_seed_note(tmp_path):
    known = load_matrix().known()
    major, minor, patch = (int(p) for p in known[-1].split("."))
    inst = make_install(tmp_path, f"{major}.{minor}.{patch + 3}")
    caps = capabilities_for(inst, storage_home=tmp_path / "home", system=FakeSystem())
    assert caps["seed.exact"] is False and caps["seed.version"] == known[-1] and caps["seed.note"]


def test_capabilities_mapping_and_unsupported(tmp_path):
    inst = make_install(tmp_path, "10.0.0")
    caps = Capabilities(inst, {"a": 1, "cli.flags.x y": ["--z"]}, probed=False)
    assert len(caps) == 2 and list(caps) == ["a", "cli.flags.x y"] and caps["a"] == 1
    assert caps.flags("x y") == ["--z"] and caps.has("a") and not caps.has("nope")
    assert caps.to_json()["install"] == inst.key()
    err = Unsupported("cli.has_drc", inst, oldest_kicad_with_it="8.0.0", fallback="swig")
    assert "cli.has_drc" in str(err) and "8.0.0" in str(err) and "swig" in str(err)


# --- live: installed KiCads agree with the matrix -------------------------------------------------

STAMP_RE = re.compile(rb"\(version (\d{8})\)")


def shipped_stamps(install: KiCadInstallation, suffix: str) -> list[int]:
    stamps = []
    for sub in ("template", "demos"):
        for file in install.dir(sub).rglob(f"*.{suffix}"):
            m = STAMP_RE.search(file.read_bytes()[:400])
            if m:
                stamps.append(int(m.group(1)))
    return stamps


def shipped_pro_meta(install: KiCadInstallation) -> list[int]:
    out = []
    for sub in ("template", "demos"):
        for file in install.dir(sub).rglob("*.kicad_pro"):
            try:
                out.append(int(json.loads(file.read_text(encoding="utf-8"))["meta"]["version"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return out


@pytest.mark.kicad
def test_live_probe_agrees_with_matrix(installs, storage_home):
    """The matrix's cli.* keys for an installed release were produced by this same probe; a mismatch
    means kicad-cli drifted (or the parser did). Also checks swig/ipc presence and the format stamps."""
    matrix = load_matrix()
    for inst in installs:
        caps = capabilities_for(inst, refresh=True, storage_home=storage_home)
        assert caps.probed and caps["probe.ok"]
        if not caps["seed.exact"]:
            continue  # a point release newer than the matrix; only the seed rule applies
        seed = matrix.versions[inst.version]
        for key in seed:
            if key.startswith("cli."):
                assert caps[key] == seed[key], (inst.version, key)
        assert caps["swig.available"] == seed["swig.available"], inst.version
        assert caps["ipc.available"] == seed["ipc.available"], inst.version
        # shipped templates/demos never carry a stamp newer than what the release writes
        for suffix, key in (
            ("kicad_sch", "formats.sch"),
            ("kicad_pcb", "formats.pcb"),
            ("kicad_sym", "formats.sym"),
        ):
            stamps = shipped_stamps(inst, suffix)
            assert stamps and max(stamps) <= caps[key], (inst.version, key)
        metas = shipped_pro_meta(inst)
        assert metas and max(metas) == caps["formats.pro"], inst.version
