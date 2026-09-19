"""Tests for `kiwrap.sandbox` (docs/VERIFICATION.md claim 10, docs/ARCHITECTURE.md §6).

Two halves:
- pure tests that need no KiCad (table parsing/merging, hashing, locking, seeding a fake install);
- per-install tests that seed a root in a throw-away storage home, run a real kicad-cli command
  with `ConfigRoot.env()` and assert the sandbox contract (tables resolve, seed restorable, the
  user's real config never modified).

The per-install tests take the root conftest's parametrised `install` (one session-wide
`discover()`; nothing is probed at collection time).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiwrap import sandbox
from kiwrap.installation import KiCadInstallation
from kiwrap.platform import Platform, current
from kiwrap.sandbox import ConfigRoot, LibRow, SandboxError, merge_lib_table, parse_lib_table, tree_hash


def _has_subcommand(root: ConfigRoot, group: str, leaf: str) -> bool:
    """Probe `kicad-cli <group> --help` (exit status ignored: 7.0/8.0 exit 1 on leaf --help)."""
    with root.lock():
        out = subprocess.run(
            [str(root.install.kicad_cli), group, "--help"],
            env=root.env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        root.restore_seed()
    return leaf in (out.stdout + out.stderr)


def _violation_types(report: Path) -> list[str]:
    data = json.loads(report.read_text(encoding="utf-8"))
    return [v["type"] for sheet in data.get("sheets", []) for v in sheet.get("violations", [])]


# ---------------------------------------------------------------------------------------------
# Pure tests
# ---------------------------------------------------------------------------------------------

TEMPLATE_SYM = (
    "(sym_lib_table\n"
    "\t(version 7)\n"
    '\t(lib (name "4xxx") (type "KiCad") (uri "${KICAD_SYMBOL_DIR}/4xxx.kicad_sym") (options "") (descr "4xxx"))\n'
    '\t(lib (name "74xx") (type "KiCad") (uri "${KICAD_SYMBOL_DIR}/74xx.kicad_sym") (options "") (descr "74xx"))\n'
    ")\n"
)

USER_SYM = (
    "(sym_lib_table\n"
    '  (lib (name "4xxx")(type "KiCad")(uri "${KICAD_SYMBOL_DIR}/4xxx.kicad_sym")(options "")(descr "dup"))\n'
    '  (lib (name MyLib)(type KiCad)(uri C:/libs/my.kicad_sym)(options "")(descr "custom")(hidden))\n'
    '  (lib (name "Esc\\"aped") (type "KiCad") (uri "${X}/e.kicad_sym") (options "") (descr "q"))\n'
    ")\n"
)


def test_parse_lib_table_handles_quoted_and_bare_atoms():
    head, rows = parse_lib_table(USER_SYM)
    assert head == "sym_lib_table"
    assert [r.name for r in rows] == ["4xxx", "MyLib", 'Esc"aped']
    assert rows[1] == LibRow(
        name="MyLib",
        type="KiCad",
        uri="C:/libs/my.kicad_sym",
        text='(lib (name MyLib)(type KiCad)(uri C:/libs/my.kicad_sym)(options "")(descr "custom")(hidden))',
    )


@pytest.mark.parametrize(
    "bad", ["(sym_lib_table", "sym_lib_table)", '(sym_lib_table (lib (name "x)))', "(a)(b)"]
)
def test_parse_lib_table_rejects_garbage(bad: str):
    with pytest.raises(SandboxError):
        parse_lib_table(bad)


def test_merge_appends_only_missing_rows_verbatim():
    merged = merge_lib_table(TEMPLATE_SYM, USER_SYM)
    head, rows = parse_lib_table(merged)
    assert head == "sym_lib_table"
    assert [r.name for r in rows] == ["4xxx", "74xx", "MyLib", 'Esc"aped']
    assert '(descr "dup")' not in merged  # the duplicate nickname kept the template row
    assert "(hidden)" in merged  # user row text copied untouched
    # idempotent: merging again adds nothing
    assert merge_lib_table(merged, USER_SYM) == merged
    assert merge_lib_table(TEMPLATE_SYM, "(sym_lib_table)") == TEMPLATE_SYM


def test_merge_skips_table_row_pointing_at_the_template(tmp_path: Path):
    template = tmp_path / "sym-lib-table"
    template.write_text(TEMPLATE_SYM, encoding="utf-8")
    user = (
        "(sym_lib_table\n\t(version 7)\n"
        f'\t(lib (name "KiCad") (type "Table") (uri "{template.as_posix()}") (options "") (descr "KiCad Default"))\n'
        '\t(lib (name "Other") (type "Table") (uri "C:/elsewhere/sym-lib-table") (options "") (descr ""))\n)\n'
    )
    merged = merge_lib_table(TEMPLATE_SYM, user, skip_table_uris=(template,))
    names = [r.name for r in parse_lib_table(merged)[1]]
    assert names == ["4xxx", "74xx", "Other"]


def test_merge_refuses_mismatched_heads():
    with pytest.raises(SandboxError):
        merge_lib_table(TEMPLATE_SYM, "(fp_lib_table)")


def test_tree_hash_is_order_independent_and_content_sensitive(tmp_path: Path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x.txt").write_bytes(b"1")
    (tmp_path / "a.txt").write_bytes(b"2")
    h1 = tree_hash(tmp_path)
    assert h1 == tree_hash(tmp_path)
    assert tree_hash(tmp_path / "nope") == tree_hash(tmp_path / "nope2")  # missing == empty
    (tmp_path / "b" / "x.txt").write_bytes(b"9")
    assert tree_hash(tmp_path) != h1


# ---- a fake install so seeding/env/lock/restore run without KiCad ----------------------------


def _fake_install(tmp_path: Path, *, with_design_block_template: bool = False) -> KiCadInstallation:
    root = tmp_path / "fake-kicad"
    share = root / "share" / "kicad"
    (share / "template").mkdir(parents=True)
    for d in ("symbols", "footprints", "3dmodels", "demos"):
        (share / d).mkdir()
    (share / "template" / "sym-lib-table").write_text(TEMPLATE_SYM, encoding="utf-8")
    (share / "template" / "fp-lib-table").write_text(
        '(fp_lib_table\n\t(version 7)\n\t(lib (name "Resistor_SMD") (type "KiCad") (uri "${KICAD_FOOTPRINT_DIR}/R.pretty") '
        '(options "") (descr ""))\n)\n',
        encoding="utf-8",
    )
    if with_design_block_template:
        (share / "template" / "design-block-lib-table").write_text(
            '(design_block_lib_table\n\t(version 7)\n\t(lib (name "DB") (type "KiCad") (uri "${KICAD_TEMPLATE_DIR}/db") '
            '(options "") (descr ""))\n)\n',
            encoding="utf-8",
        )
    (root / "bin").mkdir()
    cli = root / "bin" / current().kicad_cli_name()
    cli.write_bytes(b"")
    py = root / "bin" / current().python_name()
    py.write_bytes(b"")
    return KiCadInstallation(
        root=root, kicad_cli=cli, share=share, version="42.1.3", source="explicit", python=py
    )


@pytest.fixture
def fake_user_config(tmp_path: Path, monkeypatch) -> Path:
    """Redirect `Platform.user_kicad_config_home` / `temp_dir` to tmp so the fake tests touch nothing real."""
    real = current()
    user_home = tmp_path / "user-kicad"
    fake = Platform(
        name=real.name,
        exe_suffix=real.exe_suffix,
        storage_home=tmp_path / "storage",
        user_kicad_config_home=user_home,
        temp_dir=tmp_path / "tmp",
    )
    monkeypatch.setattr(sandbox, "current", lambda: fake)
    return user_home


def test_ensure_seeds_a_root_and_merges_user_rows(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    user_dir = fake_user_config / install.major_minor
    user_dir.mkdir(parents=True)
    (user_dir / "sym-lib-table").write_text(USER_SYM, encoding="utf-8")
    (user_dir / "fp-lib-table").write_text(
        '(fp_lib_table\n  (lib (name "MyFp")(type "KiCad")(uri "${MY_LIBS}/my.pretty")(options "")(descr ""))\n)\n',
        encoding="utf-8",
    )
    (user_dir / "kicad_common.json").write_text(
        json.dumps({"environment": {"vars": {"MY_LIBS": "C:/my/libs"}}, "meta": {"version": 3}, "api": {}}),
        encoding="utf-8",
    )
    before = tree_hash(fake_user_config)

    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)

    assert root.path == tmp_path / "storage" / "kicad-config" / sandbox.root_dirname(install)
    assert root.path.name.startswith("42.1.3-") and len(root.path.name) == len("42.1.3-") + 10
    assert root.versioned == root.path / "42.1"
    assert root.documents_home.is_dir() and root.cache_home.is_dir() and root.third_party_dir.is_dir()
    assert sorted(p.name for p in root.versioned.iterdir()) == sorted(sandbox.SEED_FILES)
    assert sorted(p.name for p in root.seed_dir.iterdir()) == sorted(sandbox.SEED_FILES)
    assert root.seed_intact()
    assert not root.prewarm_marker.exists()

    sym_names = [r.name for r in parse_lib_table((root.versioned / "sym-lib-table").read_text("utf-8"))[1]]
    assert sym_names == ["4xxx", "74xx", "MyLib", 'Esc"aped']
    fp_names = [r.name for r in parse_lib_table((root.versioned / "fp-lib-table").read_text("utf-8"))[1]]
    assert fp_names == ["Resistor_SMD", "MyFp"]
    head, rows = parse_lib_table((root.versioned / "design-block-lib-table").read_text("utf-8"))
    assert head == "design_block_lib_table" and rows == []

    common = json.loads((root.versioned / "kicad_common.json").read_text("utf-8"))
    assert common["api"] == {"enable_server": False, "interpreter_path": str(install.python)}
    assert common["do_not_show_again"]["update_check_prompt"] is True
    assert common["do_not_show_again"]["data_collection_prompt"] is True
    assert common["system"]["first_run_shown"] is True
    assert common["environment"]["vars"] == {"MY_LIBS": "C:/my/libs"}
    assert common["meta"] == {"filename": "kicad_common.json", "version": 3}

    # the user's config was only read
    assert tree_hash(fake_user_config) == before
    assert sandbox.user_config_hash(install) == tree_hash(user_dir)


def test_ensure_without_user_config_is_minimal_but_valid(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path, with_design_block_template=True)
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False, enable_api_server=True)
    common = json.loads((root.versioned / "kicad_common.json").read_text("utf-8"))
    assert common["api"]["enable_server"] is True
    assert common["environment"] == {"vars": None}
    assert "meta" not in common
    for name in sandbox.SEED_FILES[1:]:
        parse_lib_table((root.versioned / name).read_text("utf-8"))
    assert [
        r.name for r in parse_lib_table((root.versioned / "design-block-lib-table").read_text("utf-8"))[1]
    ] == ["DB"]


def test_ensure_merge_can_be_disabled(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    user_dir = fake_user_config / install.major_minor
    user_dir.mkdir(parents=True)
    (user_dir / "sym-lib-table").write_text(USER_SYM, encoding="utf-8")
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False, merge_user_tables=False)
    assert (root.versioned / "sym-lib-table").read_text("utf-8") == TEMPLATE_SYM


def test_ensure_tolerates_an_unparsable_user_table(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    user_dir = fake_user_config / install.major_minor
    user_dir.mkdir(parents=True)
    (user_dir / "sym-lib-table").write_text("(sym_lib_table (lib (name", encoding="utf-8")
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    assert (root.versioned / "sym-lib-table").read_text("utf-8") == TEMPLATE_SYM


def test_env_sets_kicad_vars_and_short_temp(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    env = root.env({"PATH": "x", "KICAD_CONFIG_HOME": "should-be-overridden"})
    assert env["PATH"] == "x"
    assert env["KICAD_CONFIG_HOME"] == str(root.path)
    assert env["KICAD_DOCUMENTS_HOME"] == str(root.documents_home)
    assert env["KICAD_CACHE_HOME"] == str(root.cache_home)
    assert env["KICAD42_SYMBOL_DIR"] == str(install.share / "symbols")
    assert env["KICAD42_FOOTPRINT_DIR"] == str(install.share / "footprints")
    assert env["KICAD42_3DMODEL_DIR"] == str(install.share / "3dmodels")
    assert env["KICAD42_TEMPLATE_DIR"] == str(install.share / "template")
    assert env["KICAD42_3RD_PARTY"] == str(root.third_party_dir)
    if current().is_windows:
        assert env["TEMP"] == env["TMP"] == str(root.temp_dir())
        assert Path(env["TEMP"]).is_dir()
        assert Path(env["TEMP"]).parent == tmp_path / "tmp"
        assert len(root.temp_dir().name) <= 16
    # the env() of a second call is identical (pure function of the root)
    assert root.env({"PATH": "x", "KICAD_CONFIG_HOME": "should-be-overridden"}) == env


def test_env_3rd_party_prefers_user_override_then_real_pcm_dir_then_sandbox(
    tmp_path: Path, fake_user_config: Path, monkeypatch
):
    """`KICAD<major>_3RD_PARTY` must let merged `${KICAD<major>_3RD_PARTY}/...` rows (PCM libraries)
    resolve: user `environment.vars` override > the user's real `<Documents>/KiCad/<mm>/3rdparty`
    (when it exists) > the sandbox's own empty dir. The real dir is never created."""
    install = _fake_install(tmp_path)
    fake = sandbox.current()  # the fake platform installed by `fake_user_config`
    docs = tmp_path / "Documents" / "KiCad"
    monkeypatch.setattr(
        sandbox,
        "current",
        lambda: Platform(
            name=fake.name,
            exe_suffix=fake.exe_suffix,
            storage_home=fake.storage_home,
            user_kicad_config_home=fake.user_kicad_config_home,
            temp_dir=fake.temp_dir,
            user_kicad_documents_home=docs,
        ),
    )
    prefix = f"KICAD{install.major}_"

    # 3. nothing on the user side -> sandbox dir (created), the real dir untouched
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    assert root.env()[prefix + "3RD_PARTY"] == str(root.sandbox_third_party_dir)
    assert root.sandbox_third_party_dir.is_dir()
    assert not docs.exists()

    # 2. the user's real PCM dir exists -> used, still never created/written by kiwrap
    real_3rd = docs / install.major_minor / "3rdparty"
    real_3rd.mkdir(parents=True)
    before = tree_hash(docs)
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    assert root.user_third_party_dir == real_3rd
    assert root.env()[prefix + "3RD_PARTY"] == str(real_3rd)
    assert tree_hash(docs) == before

    # 1. `environment.vars` in the user's kicad_common.json wins, for 3RD_PARTY and the share dirs
    user_dir = fake_user_config / install.major_minor
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "kicad_common.json").write_text(
        json.dumps(
            {
                "environment": {
                    "vars": {
                        prefix + "3RD_PARTY": "D:/pcm/3rdparty",
                        prefix + "FOOTPRINT_DIR": "D:/my/footprints",
                        "MY_LIBS": "D:/libs",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    env = root.env({prefix + "FOOTPRINT_DIR": "from-process-env"})
    assert env[prefix + "3RD_PARTY"] == "D:/pcm/3rdparty"
    assert env[prefix + "FOOTPRINT_DIR"] == "D:/my/footprints"
    assert env[prefix + "SYMBOL_DIR"] == str(install.share / "symbols")  # not overridden -> install default
    assert "MY_LIBS" not in env  # plain user vars reach KiCad through the seeded kicad_common.json
    common = json.loads((root.versioned / "kicad_common.json").read_text("utf-8"))
    assert common["environment"]["vars"]["MY_LIBS"] == "D:/libs"


def test_restore_seed_repairs_tampering_and_removes_stale_files(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    seeds = {n: (root.seed_dir / n).read_bytes() for n in sandbox.SEED_FILES}
    (root.versioned / "kicad_common.json").write_text("{}", encoding="utf-8")
    (root.versioned / "sym-lib-table").unlink()
    (root.versioned / "fp-info-cache").write_text("stale", encoding="utf-8")
    (root.versioned / "something.lck").write_text("stale", encoding="utf-8")
    (root.versioned / "eeschema.json").write_text("{}", encoding="utf-8")  # KiCad's own file: kept
    assert not root.seed_intact()
    root.restore_seed()
    assert root.seed_intact()
    assert {n: (root.versioned / n).read_bytes() for n in sandbox.SEED_FILES} == seeds
    assert not (root.versioned / "fp-info-cache").exists()
    assert not (root.versioned / "something.lck").exists()
    assert (root.versioned / "eeschema.json").exists()
    assert root.lock_path.exists()


def test_ensure_is_idempotent_and_re_derives_the_seed(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    r1 = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    first = (r1.versioned / "sym-lib-table").read_bytes()
    user_dir = fake_user_config / install.major_minor
    user_dir.mkdir(parents=True)
    (user_dir / "sym-lib-table").write_text(USER_SYM, encoding="utf-8")
    r2 = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    assert r1 == r2
    assert (r2.versioned / "sym-lib-table").read_bytes() != first
    assert "MyLib" in (r2.seed_dir / "sym-lib-table").read_text("utf-8")


def test_roots_are_keyed_on_version_and_install_root(tmp_path: Path, fake_user_config: Path):
    """Finding: two installs reporting the same version (Program Files + a portable/patched copy)
    shared one root, one seed and one lock; the root is keyed like the capability cache."""
    a = _fake_install(tmp_path / "a")
    b = _fake_install(tmp_path / "b")
    assert a.version == b.version and a.root != b.root
    ra = sandbox.ensure(a, storage_home=tmp_path / "storage", prewarm=False)
    rb = sandbox.ensure(b, storage_home=tmp_path / "storage", prewarm=False)
    assert ra.path != rb.path and ra.documents_home != rb.documents_home and ra.cache_home != rb.cache_home
    assert ra.lock_path != rb.lock_path and ra.seed_dir != rb.seed_dir
    common_a = json.loads((ra.versioned / "kicad_common.json").read_text("utf-8"))
    common_b = json.loads((rb.versioned / "kicad_common.json").read_text("utf-8"))
    assert common_a["api"]["interpreter_path"] == str(a.python)
    assert common_b["api"]["interpreter_path"] == str(b.python)
    assert sandbox.ensure(a, storage_home=tmp_path / "storage", prewarm=False) == ra  # stable key


def test_restore_seed_before_ensure_raises(tmp_path: Path):
    root = ConfigRoot(_fake_install(tmp_path), tmp_path / "r", tmp_path / "d", tmp_path / "c")
    with pytest.raises(SandboxError):
        root.restore_seed()


def test_lock_is_exclusive_and_times_out(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    with root.lock(timeout_s=1), pytest.raises(TimeoutError), root.lock(timeout_s=0.3):
        pass
    with root.lock(timeout_s=1):  # released after the first block
        pass


_HOLDER_SCRIPT = """
import sys
fh = open(sys.argv[1], "a+b")
try:
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
except ImportError:
    import msvcrt
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
print("locked", flush=True)
sys.stdin.readline()
"""


def test_lock_excludes_another_process(tmp_path: Path, fake_user_config: Path):
    install = _fake_install(tmp_path)
    root = sandbox.ensure(install, storage_home=tmp_path / "storage", prewarm=False)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(root.lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError), root.lock(timeout_s=0.5):
            pass
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.close()
        holder.wait(timeout=30)
    with root.lock(timeout_s=5):
        pass


# ---------------------------------------------------------------------------------------------
# Real KiCad installs
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def storage(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("kiwrap-home")


@pytest.fixture(scope="module")
def appdata_hash_guard() -> Iterator[None]:
    """The real user config tree (every version) must be byte-identical after the whole module."""
    home = current().user_kicad_config_home
    before = tree_hash(home)
    yield
    assert tree_hash(home) == before, f"{home} was modified by the sandbox tests"


@pytest.mark.kicad
@pytest.mark.slow
def test_real_install_sandbox_contract(
    install: KiCadInstallation, storage: Path, tmp_path: Path, appdata_hash_guard
):
    """ensure() + a library-resolving kicad-cli run, per installed KiCad (claim 10 / §6 acceptance)."""
    user_before = sandbox.user_config_hash(install)

    root = sandbox.ensure(install, storage_home=storage)
    assert root.prewarm_marker.is_file()
    assert root.seed_intact()
    for name in sandbox.SEED_FILES[1:]:
        parse_lib_table((root.versioned / name).read_text("utf-8"))
    json.loads((root.versioned / "kicad_common.json").read_text("utf-8"))
    seeds = {n: (root.seed_dir / n).read_bytes() for n in sandbox.SEED_FILES}
    sym_rows = parse_lib_table((root.versioned / "sym-lib-table").read_text("utf-8"))[1]
    assert len(sym_rows) > 100, "stock symbol libraries missing from the seeded table"
    assert len({r.name for r in sym_rows}) == len(sym_rows), "duplicate nicknames after merging user rows"

    # kicad-cli against a copy of the ecc83 demo (kicad-cli 10 writes a .kicad_prl next to the project)
    demo_src = install.dir("demos") / "ecc83"
    if not demo_src.is_dir():
        pytest.skip(f"{demo_src} not shipped by this install")
    demo = tmp_path / "ecc83"
    shutil.copytree(demo_src, demo)
    sch = demo / "ecc83-pp.kicad_sch"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    if _has_subcommand(root, "sch", "erc"):
        report = out_dir / "erc.json"
        argv = [str(install.kicad_cli), "sch", "erc", "--format", "json", "-o", str(report), str(sch)]
    else:  # 7.x has no `sch erc`; a netlist export still needs the tables to load the schematic
        report = out_dir / "netlist.xml"
        argv = [str(install.kicad_cli), "sch", "export", "netlist", "-o", str(report), str(sch)]

    with root.lock():
        proc = subprocess.run(argv, env=root.env(), capture_output=True, text=True, timeout=300, check=False)
        rewritten = (root.versioned / "kicad_common.json").read_bytes() != seeds["kicad_common.json"]
        root.restore_seed()
    assert proc.returncode == 0, proc.stderr
    assert report.is_file() and report.stat().st_size > 0
    if report.suffix == ".json":
        types = _violation_types(report)
        assert "lib_symbol_issues" not in types, types
        assert "footprint_link_issues" not in types, types
    assert rewritten, "kicad-cli did not rewrite kicad_common.json; the restore-seed rule may be obsolete"

    # restore_seed() brings back byte-identical seeds
    assert root.seed_intact()
    assert {n: (root.versioned / n).read_bytes() for n in sandbox.SEED_FILES} == seeds
    # nothing escaped into the user's real config
    assert sandbox.user_config_hash(install) == user_before
    # a second ensure() on a warm root is cheap and leaves the marker alone
    assert sandbox.ensure(install, storage_home=storage) == root
