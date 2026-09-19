"""G14 / G13 (docs/UPSTREAM.md): the kicad-python patches kiwrap carries and the UpdateItems hang guard.

G14 is Python-only. `patches/kicad-python/` holds the git format-patches kiwrap proposes upstream
(the protoc/protoletariat Windows build fix and `KiCadServer(env=)`); the unit tests here keep
them well-formed and free of the prose the KiCad reviewers reject, and record whether the kipy in
this environment already has `env=` (it does not while the pin is f79cebd1). The wheel-level proof
of patch 0001 is `scripts/ci/build_kicad_python_wheel.py --skip-verify` plus the bare-venv import;
the manual evidence is in docs/upstream/drafts/g14-mr-generate-protos.md.

G13: `ipc.schematic_update_shared_lib_symbol_hangs` is a matrix value, not a probe, so the guard is
the matrix entry itself (true on 10.99.0, verified; false on 11.0.0 = "fixed upstream before GA"
until proven). Found while writing this file: `seed_for` maps a 10.99.x nightly straight to the
11.0.0 entry, so the nightly's live capability record read false; `seed_for` now seeds a nightly
from the matrix's own entry for that exact version when there is one (the test below pins it).
The live test runs the minimal repro
(patches/repro/g13_repro.py, through `HeadlessSession`) on every headless-schematic install and
asserts the matrix entry predicts the outcome: where the hang is recorded, the second unit of the
multi-unit symbol must never be answered (and the session must still tear the process down);
where it is recorded as fixed, the update must answer. When a nightly bump fixes the bug this
test is what flips the matrix value.
"""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from kiwrap.capability import load_matrix, seed_for

REPO = Path(__file__).resolve().parents[1]
PATCH_DIR = REPO / "patches" / "kicad-python"
REPRO = REPO / "patches" / "repro" / "g13_repro.py"
HANG_KEY = "ipc.schematic_update_shared_lib_symbol_hangs"

# Lines the KiCad tool-generated-content policy rejects in commit messages (docs/dev/upstream-workflow.md
# section 2): only a human-written `Assisted-by:` trailer is allowed, never an AI co-author.
_FORBIDDEN_TRAILERS = re.compile(
    r"^(Co-Authored-By|Generated-by|Signed-off-by):", re.IGNORECASE | re.MULTILINE
)


# ---- G14: the patch series ------------------------------------------------------------------------


def _patches() -> list[Path]:
    return sorted(PATCH_DIR.glob("*.patch"))


def test_patch_series_is_present_and_ordered():
    names = [p.name for p in _patches()]
    assert names, "patches/kicad-python holds no .patch files"
    assert [n[:4] for n in names] == [f"{i:04d}" for i in range(1, len(names) + 1)], names
    assert (PATCH_DIR / "README.md").is_file()


@pytest.mark.parametrize("patch", _patches(), ids=lambda p: p.name)
def test_patch_is_a_git_format_patch_with_kicad_conventions(patch: Path):
    text = patch.read_text(encoding="utf-8")
    assert text.startswith("From ") and "\nSubject: [PATCH] " in text, "not a git format-patch"
    subject = re.search(r"^Subject: \[PATCH\] (.+)$", text, re.MULTILINE).group(1)
    assert len(subject) <= 72, subject
    assert subject[0].isupper() and not subject.endswith("."), subject
    header, _, body = text.partition("\n---\n")
    assert not _FORBIDDEN_TRAILERS.search(header), "AI attribution / sign-off trailer in the commit message"
    assert "diff --git" in body
    # Every file the patch touches is kicad-python's, never the kicad submodule or generated protos.
    files = re.findall(r"^diff --git a/(\S+) b/", body, re.MULTILINE)
    assert files and all(f.startswith(("kipy/", "tools/", "build.py")) for f in files), files
    assert not any(f.startswith("kipy/proto/") for f in files), files


def test_generate_protos_patch_targets_the_two_windows_bugs():
    patch = next(p for p in _patches() if "generate-proto" in p.name.lower() or "proto" in p.name.lower())
    text = patch.read_text(encoding="utf-8")
    assert "tools/generate_protos.py" in text
    # the real compiler path instead of the execv launcher, and protol fed from the descriptor set
    assert "+    from protoc import PROTOC_EXE as _default_protoc" in text
    assert '+            "--descriptor_set_out=" + descriptor_set,' in text
    assert '+                "raw",' in text


def test_server_env_patch_adds_the_parameter_to_both_entry_points():
    patch = next(p for p in _patches() if "env" in p.name.lower())
    text = patch.read_text(encoding="utf-8")
    assert "kipy/server.py" in text and "kipy/kicad.py" in text
    assert text.count("+        env: Mapping[str, str] | None = None,") == 2
    assert "+            env=self._env," in text


@pytest.mark.network
def test_patches_apply_to_the_pinned_kicad_python(tmp_path):
    """`git apply --check` against the pinned tree's files (fetched shallow); network needed, so
    skipped when git cannot reach GitLab quickly."""
    from kiwrap.backends.ipc import KICAD_PYTHON_PIN

    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    clone = tmp_path / "kicad-python"
    clone.mkdir()
    try:
        subprocess.run(["git", "init", "-q"], cwd=clone, check=True, timeout=30)
        subprocess.run(
            [
                "git",
                "fetch",
                "-q",
                "--depth",
                "1",
                "https://gitlab.com/kicad/code/kicad-python.git",
                KICAD_PYTHON_PIN,
            ],
            cwd=clone,
            check=True,
            timeout=120,
            capture_output=True,
        )
        subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=clone, check=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"cannot fetch the kicad-python pin: {exc}")
    result = subprocess.run(
        ["git", "apply", "--check", *map(str, _patches())],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---- G14 (b): does this environment's kipy already carry `env=`? ---------------------------------


def test_kicad_server_env_parameter_reaches_popen(monkeypatch):
    """Green in both states: skipped (documented) while the pinned kipy lacks the parameter, a
    real check once the patched client is installed (the wheel job or a new pin)."""
    kipy_server = pytest.importorskip("kipy.server")
    params = inspect.signature(kipy_server.KiCadServer.__init__).parameters
    if "env" not in params:
        pytest.skip(
            "installed kicad-python has no KiCadServer(env=); patches/kicad-python/0002 is pending upstream"
        )
    seen: dict[str, object] = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw.get("env")
            self.stdout = self.stderr = None

        def poll(self):
            return 0

    monkeypatch.setattr(kipy_server.subprocess, "Popen", FakePopen)
    server = kipy_server.KiCadServer("kicad-cli", socket_path="ipc-test.sock", env={"KICAD_CONFIG_HOME": "X"})
    server.start()
    assert seen["env"] == {"KICAD_CONFIG_HOME": "X"}
    assert seen["cmd"][:2] == ["kicad-cli", "api-server"]


# ---- G13: the guard -------------------------------------------------------------------------------


def test_repro_script_is_standalone_kipy():
    """The repro shipped for the upstream issue must not import kiwrap (maintainers run it bare)."""
    text = REPRO.read_text(encoding="utf-8")
    assert "import kiwrap" not in text and "from kiwrap" not in text
    assert "update_items" in text


def test_hang_capability_is_recorded_for_the_nightly_and_cleared_for_ga():
    """The matrix entry is the guard: `10.99.0` (verified live, 2026-09-19) says the hang exists,
    `11.0.0` says it does not (a placeholder until GA is probed)."""
    m = load_matrix()
    assert m.versions["10.99.0"][HANG_KEY] is True
    assert m.provenance["10.99.0"][HANG_KEY] == "verified"
    assert m.versions["11.0.0"][HANG_KEY] is False


def test_seed_for_the_nightly_carries_the_verified_hang_value():
    """Regression (2026-09-19): seed_for mapped every 10.99.x to the 11.0.0 entry and never read
    the matrix's own 10.99.0 entry, so the stock nightly's live record said the hang was absent."""
    seed, values, exact, _note = seed_for("10.99.0")
    assert seed == "10.99.0" and exact
    assert values[HANG_KEY] is True


def _sequence(schematic, reference: str = "U1") -> list[tuple[int, bool, float, str]]:
    """UpdateItems on unit a, unit a again, unit b of `reference`; (unit, answered, seconds, error)."""
    symbols = schematic.get_symbols()
    units = sorted((s for s in symbols if s.reference_field.text.value == reference), key=lambda s: s.unit)
    assert len(units) >= 2, f"{reference} is not a multi-unit symbol in this schematic"
    out = []
    for item in (units[0], units[0], units[1]):
        t0 = time.monotonic()
        try:
            schematic.update_items(item)
            out.append((item.unit, True, time.monotonic() - t0, ""))
        except Exception as exc:  # noqa: BLE001 - kipy raises its own ConnectionError on timeout
            out.append((item.unit, False, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"))
            break
    return out


@pytest.mark.kicad
@pytest.mark.kicad_version("10.99")
def test_hang_guard_matches_the_live_repro(installs, storage_home, tmp_path):
    from kiwrap import sandbox
    from kiwrap.backends.ipc.sessions import HeadlessSession, close_all, live_sessions
    from kiwrap.capability import capabilities_for
    from kiwrap.sandbox import user_config_hash

    targets = [
        i
        for i in installs
        if capabilities_for(i, storage_home=storage_home).has("ipc.headless")
        and capabilities_for(i, storage_home=storage_home).has("ipc.schematic")
    ]
    if not targets:
        pytest.skip("no install with a headless schematic API")
    before = {i.key(): user_config_hash(i) for i in targets}
    try:
        matrix = load_matrix()
        for install in targets:
            caps = capabilities_for(install, storage_home=storage_home)
            # The verified matrix entry for this exact version is the guard; the live record
            # agrees since seed_for reads the nightly's own entry (a bench build of the same
            # version shares it).
            expected_hang = bool(matrix.versions.get(install.version, caps)[HANG_KEY])
            if install.version in matrix.versions:
                assert caps[HANG_KEY] == expected_hang
            src = install.dir("demos") / "ecc83"
            if not src.is_dir():
                pytest.skip("ecc83 demo not shipped")
            project = tmp_path / install.root.name / "ecc83"
            shutil.copytree(src, project)
            root = sandbox.ensure(install, storage_home=storage_home)
            session = HeadlessSession(
                install, project / "ecc83-pp.kicad_sch", config_root=root, request_timeout_ms=5000
            )
            with session as s:
                steps = _sequence(s.schematic())
                assert steps[0][1] and steps[1][1], f"{install.version}: the first unit must update: {steps}"
                answered = steps[2][1]
                assert answered is not expected_hang, (
                    f"{install.version}: {HANG_KEY}={expected_hang} but the second unit "
                    f"{'answered' if answered else 'never answered'}: {steps}"
                )
                if expected_hang:
                    assert s.running, "the wedged server is alive (a hang, not a crash)"
                    assert steps[2][2] >= 4.5, "the failure is the request timeout, not an error reply"
            assert not session.running, f"{install.version}: close() must terminate a wedged server"
    finally:
        close_all()
    assert live_sessions() == []
    for i in targets:
        assert user_config_hash(i) == before[i.key()], (
            "a headless session modified the user's real KiCad config"
        )
