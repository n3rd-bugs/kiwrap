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
    assert (
        "from kipy.proto.board import board_types_pb2 as" in text and "import kipy.proto.schematic." in text
    )
    assert (
        "from google.protobuf import any_pb2" in text
        and "from kipy.proto.common.types import enums_pb2" in text
    )

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
        assert (
            z.read("kipy/proto/board/board_types_pb2.py")
            == b"from kipy.proto.common.types import base_types_pb2 as x\n"
        )
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


# ------------------------------------------- build_kicad_python_wheel.py: locating, checking (S2, 2026-09-25)

PIN_SHA = "f79cebd1c09f6ef1d16e74d538da521e68c2d9f1"
PIN_URL = "https://gitlab.com/kicad/code/kicad-python.git"


def _fake_wheel(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("kipy/__init__.py", b"")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
        z.writestr(
            "kicad_python-0.9.0.dev0.dist-info/WHEEL",
            "Root-Is-Purelib: false\nTag: cp311-cp311-linux_x86_64\n",
        )
        z.writestr("kicad_python-0.9.0.dev0.dist-info/RECORD", "x,,\n")
    return path


def test_find_built_wheel_resolves_uvs_relative_line_against_the_clone(tmp_path: Path) -> None:
    """CI run 36013216332: `--out wheelhouse` (relative) made uv write <clone>/wheelhouse/<wheel> and print
    a relative "Successfully built" line; the old glob in the repository found nothing."""
    import build_kicad_python_wheel as bw

    clone, out = tmp_path / "clone", tmp_path / "repo" / "wheelhouse"
    out.mkdir(parents=True)
    wheel = _fake_wheel(clone / "wheelhouse" / "kicad_python-0.9.0.dev0-cp311-cp311-linux_x86_64.whl")
    log = "Building wheel...\nSuccessfully built wheelhouse/kicad_python-0.9.0.dev0-cp311-cp311-linux_x86_64.whl\n"
    assert bw.find_built_wheel(log, clone, out, since=0.0) == wheel.resolve()


def test_find_built_wheel_falls_back_to_fresh_files_and_names_what_it_searched(tmp_path: Path) -> None:
    import build_kicad_python_wheel as bw

    clone, out = tmp_path / "clone", tmp_path / "out"
    out.mkdir()
    stale = _fake_wheel(out / "kicad_python-0.8.0-py3-none-any.whl")
    os.utime(stale, (1_000_000, 1_000_000))
    with pytest.raises(SystemExit, match="produced no kicad_python wheel") as exc:
        bw.find_built_wheel(
            "Successfully built nowhere/kicad_python-9-x.whl\n", clone, out, since=2_000_000.0
        )
    assert str(out) in str(exc.value) and "Successfully built nowhere" in str(exc.value)
    fresh = _fake_wheel(clone / "dist" / "kicad_python-0.9.0.dev0-cp314-cp314-win_amd64.whl")
    assert (
        bw.find_built_wheel("no summary line\n", clone, out, since=fresh.stat().st_mtime) == fresh.resolve()
    )


def _fake_uv(tmp_path: Path, where: str) -> list[str]:
    """A stand-in for `uv build --wheel --out-dir X`: writes a platform-tagged wheel either into X ("out")
    or, like the relative-path case, into <cwd>/wheelhouse ("cwd") and prints uv's summary line."""
    import sys

    script = tmp_path / "fake_uv.py"
    script.write_text(
        "import sys, zipfile, pathlib\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] == ['build', '--wheel'], args\n"
        "out = pathlib.Path(args[args.index('--out-dir') + 1])\n"
        f"target = out if {where!r} == 'out' else pathlib.Path('wheelhouse')\n"
        "target.mkdir(parents=True, exist_ok=True)\n"
        "w = target / 'kicad_python-0.9.0.dev0-cp311-cp311-linux_x86_64.whl'\n"
        "with zipfile.ZipFile(w, 'w') as z:\n"
        "    z.writestr('kipy/__init__.py', b'')\n"
        "print('fatal: No names found, cannot describe anything.', file=sys.stderr)\n"
        "print(f'Successfully built {w}', file=sys.stderr)\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


@pytest.mark.parametrize("where", ["out", "cwd"])
def test_build_passes_an_absolute_out_dir_and_moves_a_stray_wheel_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    import build_kicad_python_wheel as bw

    monkeypatch.setattr(bw, "_uv", lambda: _fake_uv(tmp_path, where))
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.chdir(tmp_path)
    wheel = bw.build(clone, Path("wheelhouse"))  # relative, exactly as CI passes it
    assert (
        wheel == (tmp_path / "wheelhouse" / "kicad_python-0.9.0.dev0-cp311-cp311-linux_x86_64.whl").resolve()
    )
    assert wheel.is_file() and not list(clone.rglob("*.whl"))


def test_build_raises_when_uv_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    import build_kicad_python_wheel as bw

    monkeypatch.setattr(bw, "_uv", lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(SystemExit, match="exit code 3"):
        bw.build(tmp_path, tmp_path / "out")


def test_remove_stale_leaves_an_empty_wheelhouse(tmp_path: Path) -> None:
    import build_kicad_python_wheel as bw

    _fake_wheel(tmp_path / "kicad_python-0.8.0-py3-none-any.whl")
    (tmp_path / "kicad_python-0.8.0-py3-none-any.whl.sha256").write_text("x", encoding="utf-8")
    (tmp_path / bw.PIN_FILE).write_text("x", encoding="utf-8")
    (tmp_path / "other.txt").write_text("keep", encoding="utf-8")
    assert len(bw.remove_stale(tmp_path)) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["other.txt"]


def test_stub_imports_become_protoletariats_relative_package_imports() -> None:
    """The Linux-built CI wheel's stubs say `from .. import board` (protoletariat); the Windows repair
    must produce the same, or a local mypy misses what CI reports (jobs.py ValueType, 2026-09-25)."""
    import build_kicad_python_wheel as bw

    stub = (
        "import board.board_types_pb2\n"
        "import builtins\n"
        "import common.types.base_types_pb2\n"
        "import common.types.enums_pb2\n"
        "import google.protobuf.message\n"
        "    import typing_extensions\n"
        "x: common.types.enums_pb2.Units.ValueType\n"
    )
    text, n = bw.rewrite_stub_imports(stub, depth=1)
    assert n == 3
    assert text.splitlines()[:4] == [
        "from .. import board",
        "import builtins",
        "from .. import common",
        "import google.protobuf.message",
    ]
    assert "x: common.types.enums_pb2.Units.ValueType" in text and "    import typing_extensions" in text
    text2, _ = bw.rewrite_stub_imports("import common.types.base_types_pb2\r\n", depth=2)
    assert text2 == "from ... import common\n"


def test_repair_rewrites_stubs_by_depth(tmp_path: Path) -> None:
    import zipfile

    import build_kicad_python_wheel as bw

    wheel = _fake_wheel(
        tmp_path / "kicad_python-0.9.0.dev0-cp314-cp314-win_amd64.whl",
        {
            "kipy/proto/board/board_jobs_pb2.pyi": b"import common.types.jobs_pb2\n",
            "kipy/proto/common/types/jobs_pb2.pyi": b"import common.types.base_types_pb2\n",
        },
    )
    out, rewritten, _ = bw.repair_wheel(wheel, "10.99.0-abc")
    assert rewritten == 2
    with zipfile.ZipFile(out) as z:
        assert z.read("kipy/proto/board/board_jobs_pb2.pyi") == b"from .. import common\n"
        assert z.read("kipy/proto/common/types/jobs_pb2.pyi") == b"from ... import common\n"


def _good_wheelhouse(tmp_path: Path) -> Path:
    import hashlib

    import build_kicad_python_wheel as bw

    house = tmp_path / "wheelhouse"
    wheel = _fake_wheel(house / "kicad_python-0.9.0.dev0-py3-none-any.whl")
    bw.write_sidecars(wheel, PIN_URL, PIN_SHA)
    digest = (house / f"{wheel.name}.sha256").read_text(encoding="utf-8").split()[0]
    assert digest == hashlib.sha256(wheel.read_bytes()).hexdigest()
    return house


def test_check_wheelhouse_accepts_the_pinned_locked_wheel(tmp_path: Path) -> None:
    import build_kicad_python_wheel as bw

    house = _good_wheelhouse(tmp_path)
    report = bw.check_wheelhouse(house, expect_pin=PIN_SHA)
    assert report.problems == [] and report.version == bw.locked()[0] == "0.9.0.dev0"
    assert report.pin == f"{PIN_URL}@{PIN_SHA}"
    assert bw.main(["--check", str(house), "--expect-pin", PIN_SHA]) == 0


def test_check_wheelhouse_reports_every_mismatch(tmp_path: Path) -> None:
    import build_kicad_python_wheel as bw

    house = _good_wheelhouse(tmp_path)
    assert any("KICAD_PYTHON_PIN" in p for p in bw.check_wheelhouse(house, expect_pin="0" * 40).problems)
    (house / bw.PIN_FILE).write_text(f"{PIN_URL}@{'1' * 40}\n", encoding="utf-8")
    assert any(bw.PIN_FILE in p for p in bw.check_wheelhouse(house).problems)
    bw.write_sidecars(next(house.glob("*.whl")), PIN_URL, PIN_SHA)
    (house / "kicad_python-0.9.0.dev0-py3-none-any.whl.sha256").write_text(
        "0" * 64 + "  x\n", encoding="utf-8"
    )
    assert any("hashes to" in p for p in bw.check_wheelhouse(house).problems)
    _fake_wheel(house / "kicad_python-0.8.0-cp311-cp311-linux_x86_64.whl")
    assert any("exactly one" in p for p in bw.check_wheelhouse(house).problems)
    assert bw.main(["--check", str(house)]) == 1


def test_check_wheelhouse_rejects_an_unrepaired_or_wrong_version_wheel(tmp_path: Path) -> None:
    import build_kicad_python_wheel as bw

    house = tmp_path / "wheelhouse"
    wheel = _fake_wheel(house / "kicad_python-0.8.0-cp311-cp311-linux_x86_64.whl")
    bw.write_sidecars(wheel, PIN_URL, PIN_SHA)
    problems = bw.check_wheelhouse(house).problems
    assert any("py3-none-any" in p for p in problems)
    assert any("uv.lock locks kicad-python 0.9.0.dev0" in p for p in problems)


def test_lock_entry_is_at_the_pyproject_pin() -> None:
    import build_kicad_python_wheel as bw

    version, source = bw.locked()
    assert source.endswith("#" + bw.pinned()[1]) and version


# ---------------------------------------------------------------------- sync_with_wheelhouse.sh (S2)


def _git_bash() -> str | None:
    import shutil

    bash = shutil.which("bash")
    # C:\Windows\System32\bash.exe is WSL's launcher, not a POSIX shell over this file system
    return None if bash is None or "system32" in bash.lower() else bash


@pytest.mark.parametrize("count", [0, 2])
def test_sync_script_requires_exactly_one_wheel(tmp_path: Path, count: int) -> None:
    import subprocess

    bash = _git_bash()
    if bash is None:
        pytest.skip("no POSIX bash on PATH")
    for i in range(count):
        _fake_wheel(tmp_path / f"kicad_python-0.{i}.0-py3-none-any.whl")
    env = {**os.environ, "KIWRAP_WHEELHOUSE": tmp_path.as_posix()}
    proc = subprocess.run(
        [bash, (REPO / "scripts" / "ci" / "sync_with_wheelhouse.sh").as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"found {count}" in proc.stderr and "build_kicad_python_wheel.py" in proc.stderr


# ---------------------------------------------------------------------- .github/workflows/ci.yml (S2)

CI_YML = REPO / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict[str, str]:
    """Job id -> its YAML text (two-space job keys under `jobs:`; no YAML parser in the dev deps)."""
    import re

    text = CI_YML.read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z0-9-]+):\n", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_ci_lint_never_waits_for_the_wheel_and_kipy_users_do() -> None:
    """Run 36013216332: the wheel job failed and lint/unit/kicad-windows were all SKIPPED. ruff and
    import-linter must not depend on it; mypy, unit and kicad-windows import kipy and do."""
    jobs = _jobs()
    assert {"kicad-python-wheel", "lint", "typecheck", "unit", "kicad-windows", "extension", "vsix"} <= set(
        jobs
    )
    assert "needs:" not in jobs["lint"] and "download-artifact" not in jobs["lint"]
    assert "--no-install-package kicad-python" in jobs["lint"]
    assert "ruff check" in jobs["lint"] and "lint-imports" in jobs["lint"]
    for job in ("typecheck", "unit", "kicad-windows"):
        assert "needs: kicad-python-wheel" in jobs[job], job
        assert "sync_with_wheelhouse.sh" in jobs[job], job
    assert "mypy --platform win32" in jobs["typecheck"]


def test_ci_kicad_windows_bootstraps_engines_where_the_tests_look() -> None:
    """Review finding 13: with KIWRAP_HOME set for the job, bootstrap.ps1 put the JRE and Freerouting
    under $RUNNER_TEMP while every test looks in %LOCALAPPDATA%\\kiwrap (conftest replaces KIWRAP_HOME),
    so each Freerouting test skipped silently on CI."""
    job = _jobs()["kicad-windows"]
    assert "KIWRAP_HOME=" not in job and "GITHUB_ENV" not in job
    assert "bootstrap.ps1 -SkipSync" in job and "bootstrap.ps1 -Check" in job


def test_ci_wheel_job_checks_and_ships_the_pin() -> None:
    wheel = _jobs()["kicad-python-wheel"]
    assert "build_kicad_python_wheel.py --out wheelhouse" in wheel
    assert "build_kicad_python_wheel.py --check wheelhouse" in wheel
    assert "wheelhouse/kicad_python.pin" in wheel and "if-no-files-found: error" in wheel
    assert "hashFiles('scripts/ci/build_kicad_python_wheel.py')" in wheel  # a script change rebuilds


def test_ci_extension_integration_skips_only_by_explicit_opt_in() -> None:
    ext = _jobs()["extension"]
    assert 'KIWRAP_IT_SKIP_WITHOUT_KICAD: "1"' in ext and "test:integration" in ext
    run_test = (REPO / "extension" / "test" / "integration" / "runTest.ts").read_text(encoding="utf-8")
    assert "KIWRAP_IT_SKIP_WITHOUT_KICAD" in run_test and "env[SKIP_ENV] === '1'" in run_test


def test_ci_every_referenced_script_exists() -> None:
    import re

    text = CI_YML.read_text(encoding="utf-8")
    for rel in sorted(set(re.findall(r"scripts[/\\][\w./\\-]+\.(?:py|sh|ps1)", text))):
        assert (REPO / rel.replace("\\", "/")).is_file(), rel
