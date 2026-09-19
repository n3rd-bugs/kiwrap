"""Tests for scripts/ci/* and scripts/engines.json (no KiCad needed; a `kicad`-marked test runs the real probe)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import collect_capabilities as cc
import pytest
import user_config_hash as uch

REPO = Path(__file__).resolve().parents[3]
ENGINES = REPO / "scripts" / "engines.json"


# ---------------------------------------------------------------------------------------- engines.json


def test_engines_json_is_pinned_with_real_hashes() -> None:
    pins = json.loads(ENGINES.read_text(encoding="utf-8"))
    for os_name in ("windows", "linux"):
        entry = pins["jre"][os_name]
        assert entry["url"].startswith("https://github.com/adoptium/temurin25-binaries/releases/download/")
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16)
        assert entry["size"] > 50_000_000
        assert entry["top_dir"] == pins["jre"]["release_name"] + "-jre"
    fr = pins["freerouting"]
    assert fr["url"].endswith("/v2.4.1/freerouting-2.4.1.jar")
    assert fr["sha256"] == "251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9"
    assert fr["requires_java"] == 25


def test_bootstrap_scripts_read_the_same_pin_file() -> None:
    ps1 = (REPO / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    sh = (REPO / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "engines.json" in ps1 and "engines.json" in sh
    # Both scripts must enforce the telemetry-off keys verified in the Freerouting 2.4.1 profile.
    for key in ("allow_telemetry", "disable_analytics", "allow_contact", "proxy_url"):
        assert key in ps1 and key in sh


# ---------------------------------------------------------------------------------------- user_config_hash


def test_fingerprint_absent_and_changes(tmp_path: Path) -> None:
    root = tmp_path / "kicad"
    assert uch.fingerprint(root) == "absent"
    (root / "10.0").mkdir(parents=True)
    (root / "10.0" / "kicad_common.json").write_text("{}", encoding="utf-8")
    first = uch.fingerprint(root)
    assert len(first) == 64
    assert uch.fingerprint(root) == first  # deterministic
    (root / "10.0" / "kicad_common.json").write_text('{"a": 1}', encoding="utf-8")
    assert uch.fingerprint(root) != first


def test_user_config_hash_cli_write_then_verify(tmp_path: Path) -> None:
    root = tmp_path / "kicad"
    root.mkdir()
    (root / "sym-lib-table").write_text("(sym_lib_table)", encoding="utf-8")
    snap = tmp_path / "before.txt"
    assert uch.main(["--root", str(root), "--write", str(snap)]) == 0
    assert uch.main(["--root", str(root), "--verify", str(snap)]) == 0
    (root / "fp-lib-table").write_text("(fp_lib_table)", encoding="utf-8")
    assert uch.main(["--root", str(root), "--verify", str(snap)]) == 1


def test_user_config_home_is_the_platform_seams() -> None:
    from kiwrap.platform import current

    assert uch.user_kicad_config_home() == current().user_kicad_config_home
    assert uch.user_kicad_config_home().name == "kicad"


def test_fingerprint_is_sandbox_tree_hash(tmp_path: Path) -> None:
    """One hasher backs T6, the pytest fixture and the CI fingerprint (content, not mtime)."""
    from kiwrap.sandbox import tree_hash

    root = tmp_path / "kicad"
    (root / "10.0").mkdir(parents=True)
    (root / "10.0" / "kicad_common.json").write_text("{}", encoding="utf-8")
    assert uch.fingerprint(root) == tree_hash(root)
    os.utime(root / "10.0" / "kicad_common.json", (0, 0))  # an mtime-only change is not a modification
    assert uch.fingerprint(root) == tree_hash(root)


# ---------------------------------------------------------------------------------------- collect_capabilities


def test_throwaway_env_points_all_three_homes_away_from_user_config() -> None:
    env, tmp = cc.throwaway_env()
    try:
        for key in ("KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", "KICAD_CACHE_HOME"):
            assert Path(env[key]).is_relative_to(Path(tmp.name))
        assert uch.user_kicad_config_home() not in {Path(env[k]) for k in env if k.startswith("KICAD_")}
    finally:
        tmp.cleanup()


def test_collect_records_capability_errors_without_losing_installs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_discover_with_kiwrap(probe: bool):
        rec = cc.InstallRecord(
            root=str(tmp_path),
            kicad_cli=str(tmp_path / "bin" / "kicad-cli"),
            version="10.0.6",
            source="explicit",
        )
        rec.error = "kiwrap.capability.capabilities_for is not implemented yet"
        return [rec], []

    monkeypatch.setattr(cc, "discover_with_kiwrap", fake_discover_with_kiwrap)
    report = cc.collect()
    assert report.discovery == "kiwrap.installation"
    assert report.installs[0].version == "10.0.6"
    assert report.installs[0].capabilities is None
    assert "not implemented" in (report.installs[0].error or "")


def test_collect_has_no_private_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scaffold-era glob discovery / version prober are gone: discovery rules live only in
    kiwrap.platform, and the collector fails loudly instead of silently diverging from them."""
    for name in ("glob_candidates", "cli_version", "discover_by_glob"):
        assert not hasattr(cc, name), name
    assert "sys.platform" not in Path(cc.__file__).read_text(encoding="utf-8")
    assert "sys.platform" not in Path(uch.__file__).read_text(encoding="utf-8")
    monkeypatch.setattr(cc, "discover", lambda: [])
    report = cc.collect(probe=False)
    assert report.discovery == "kiwrap.installation" and report.installs == []


def test_main_writes_json_and_require_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cc, "collect", lambda probe=True: cc.Report("now", "test", "kiwrap.installation", []))
    out = tmp_path / "caps.json"
    assert cc.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["installs"] == []
    assert cc.main(["--out", str(out), "--require-one"]) == 1


@pytest.mark.kicad
def test_real_probe_leaves_user_config_untouched() -> None:
    """End-to-end on a machine with KiCad: the collector finds >= 1 install and never touches the user config."""
    before = uch.fingerprint(uch.user_kicad_config_home())
    report = cc.collect(probe=False)
    after = uch.fingerprint(uch.user_kicad_config_home())
    assert before == after
    if not report.installs:
        pytest.skip("no KiCad installation on this machine")
    assert all(len(i.version.split(".")) == 3 for i in report.installs)


def test_run_pytest_wrapper_maps_exit_5_to_0() -> None:
    """The CI job `scripts-lint` runs `bash -n` and shellcheck on it; here only the contract is checked."""
    text = (REPO / "scripts" / "ci" / "run_pytest.sh").read_text(encoding="utf-8")
    assert 'if [ "$rc" -eq 5 ]' in text and "rc=0" in text
    assert 'uv run --no-sync pytest "$@"' in text


# -------------------------------------------------------------------------- build_kicad_python_wheel.py


def test_kicad_python_wheel_repair_rewrites_imports_stamps_and_retags(tmp_path: Path) -> None:
    """The repair step that makes the prebuilt kicad-python wheel importable without kiwrap's
    .pth hook (finding 5): protoc's bare `common.*` imports -> `kipy.proto.*`, the "unknown"
    KICAD_API_VERSION stamp replaced, the platform tag turned into py3-none-any, RECORD rewritten."""
    import base64
    import hashlib
    import zipfile

    import build_kicad_python_wheel as bw

    text, n = bw.rewrite_imports(
        "from google.protobuf import any_pb2 as g\n"
        "from common.types import base_types_pb2 as common_dot_types_dot_base__types__pb2\n"
        "from board import board_types_pb2 as board_dot_board__types__pb2\n"
        "import schematic.schematic_types_pb2\n"
        "from kipy.proto.common.types import enums_pb2\n"
    )
    assert n == 3 and "from kipy.proto.common.types import base_types_pb2 as" in text
    assert "from kipy.proto.board import board_types_pb2 as" in text and "import kipy.proto.schematic." in text
    assert "from google.protobuf import any_pb2" in text and "from kipy.proto.common.types import enums_pb2" in text

    wheel = tmp_path / "kicad_python-0.9.0.dev0-cp314-cp314-win_amd64.whl"
    files = {
        "kipy/__init__.py": b"",
        "kipy/kicad_api_version.py": b'# generated\nKICAD_API_VERSION = "unknown"\n',
        "kipy/proto/board/board_types_pb2.py": b"from common.types import base_types_pb2 as x\n",
        "kicad_python-0.9.0.dev0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: poetry-core\nRoot-Is-Purelib: false\nTag: cp314-cp314-win_amd64\n",
        "kicad_python-0.9.0.dev0.dist-info/METADATA": b"Name: kicad-python\n",
    }
    with zipfile.ZipFile(wheel, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr("kicad_python-0.9.0.dev0.dist-info/RECORD", "x,,\n")
    out, rewritten, stamped = bw.repair_wheel(wheel, "10.99.0-abcdef1234")
    assert out.name == "kicad_python-0.9.0.dev0-py3-none-any.whl" and not wheel.exists()
    assert rewritten == 1 and stamped
    with zipfile.ZipFile(out) as z:
        assert z.read("kipy/proto/board/board_types_pb2.py") == b"from kipy.proto.common.types import base_types_pb2 as x\n"
        assert b'KICAD_API_VERSION = "10.99.0-abcdef1234"' in z.read("kipy/kicad_api_version.py")
        meta = z.read("kicad_python-0.9.0.dev0.dist-info/WHEEL").decode()
        assert "Root-Is-Purelib: true" in meta and "Tag: py3-none-any" in meta
        record = z.read("kicad_python-0.9.0.dev0.dist-info/RECORD").decode().splitlines()
        for line in record:
            name, digest, size = line.split(",")
            if not digest:
                assert name.endswith("RECORD")
                continue
            data = z.read(name)
            expect = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert digest == f"sha256={expect}" and int(size) == len(data), name
    assert bw.pinned()[1] == "f79cebd1c09f6ef1d16e74d538da521e68c2d9f1"
