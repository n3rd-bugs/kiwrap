"""Installation discovery: fake-System unit tests for every source (Windows + Linux) and live tests
against the installed KiCads, including the never-touch-%APPDATA% guarantee."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

import pytest

from kiwrap import installation
from kiwrap.installation import (
    SOURCES,
    KiCadInstallation,
    NoKiCadError,
    describe,
    discover,
    find,
    normalise_version,
    probe_env,
    version_tuple,
)
from kiwrap.platform import Platform
from kiwrap.platform.linux import flatpak_wrapper_script
from kiwrap.platform.system import RegistryEntry, RunResult, System

# --- pure helpers ---------------------------------------------------------------------------------


def test_normalise_version():
    assert normalise_version("10.0.0\n") == "10.0.0"
    assert normalise_version("10.0.0-1") == "10.0.0"
    assert normalise_version("10.99.0-unknown-abcdef1\r\n") == "10.99.0"
    assert normalise_version("KiCad 9.0.5+dfsg") == "9.0.5"
    assert normalise_version("garbage") is None
    assert version_tuple("10.0.6") == (10, 0, 6) and version_tuple("7.0.6-1") == (7, 0, 6)


def test_installation_properties(tmp_path):
    inst = KiCadInstallation(
        tmp_path, tmp_path / "bin" / "kicad-cli", tmp_path / "share" / "kicad", "10.0.6", "explicit"
    )
    assert inst.major == 10 and inst.major_minor == "10.0" and inst.version_tuple == (10, 0, 6)
    assert inst.dir("symbols") == tmp_path / "share" / "kicad" / "symbols"
    assert inst.key() == f"10.0.6@{tmp_path}"


def test_probe_env_points_every_kicad_home_under_storage(tmp_path):
    env = probe_env(tmp_path, {"PATH": "p", "KICAD_CONFIG_HOME": "user-value"})
    assert env["PATH"] == "p"
    for key in ("KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", "KICAD_CACHE_HOME"):
        assert Path(env[key]).is_relative_to(tmp_path / installation.PROBE_CONFIG_DIRNAME), key
    assert len({env["KICAD_CONFIG_HOME"], env["KICAD_DOCUMENTS_HOME"], env["KICAD_CACHE_HOME"]}) == 3


# --- a fake machine -------------------------------------------------------------------------------


class _PathKeyed(dict):
    """dict keyed by path strings, normalised so '/usr/bin/x' and WindowsPath('/usr/bin/x') agree."""

    @staticmethod
    def _norm(key):
        return Path(key).as_posix()

    def __setitem__(self, key, value):
        super().__setitem__(self._norm(key), value)

    def get(self, key, default=None):
        return super().get(self._norm(key), default)


class FakeMachine(System):
    """An in-memory filesystem + PATH + registry + kicad-cli responses."""

    def __init__(
        self, *, files: Iterable[str] = (), dirs: Iterable[str] = (), env: dict[str, str] | None = None
    ):
        self.files = {Path(f) for f in files}
        self.dirs = {Path(d) for d in dirs}
        self.env = env or {}
        self.versions = _PathKeyed()  # kicad-cli path -> stdout of `version --format plain`
        self.registry: list[RegistryEntry] = []
        self.which_map: dict[str, Path] = {}
        self.flatpak_apps: list[str] = []
        self.runs: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
        self.written: dict[Path, str] = {}
        self.extract_creates = _PathKeyed()

    def environ(self):
        return self.env

    def exists(self, path):
        return path in self.files or path in self.dirs

    def is_file(self, path):
        return path in self.files

    def is_dir(self, path):
        return path in self.dirs

    def listdir(self, path):
        return sorted(p for p in self.files | self.dirs if p.parent == path)

    def glob(self, base, pattern):
        out = []
        for p in sorted(self.files | self.dirs):
            if p.is_relative_to(base) and p != base:
                rel = PurePosixPath(p.relative_to(base).as_posix())
                if fnmatch.fnmatchcase(str(rel), pattern):
                    out.append(p)
        return out

    def which(self, name):
        return self.which_map.get(name)

    def path_entries(self):
        return [Path(p) for p in self.env.get("PATH", "").split(":") if p]

    def home(self):
        return Path("/home/u")

    def run(self, argv, *, env, timeout_s=60, cwd=None):
        args = tuple(str(a) for a in argv)
        self.runs.append((args, env))
        if args[0] == "flatpak":
            return RunResult(args, 0, "\n".join(self.flatpak_apps) + "\n", "")
        if args[1:] == ("version", "--format", "plain"):
            out = self.versions.get(args[0])
            return RunResult(args, 0, out + "\n", "") if out else RunResult(args, 1, "", "boom")
        if args[1:] == ("--appimage-extract",):
            for created in self.extract_creates.get(args[0], []):
                self.files.add(Path(created))
            return RunResult(args, 0, "", "")
        return RunResult(args, 2, "", f"unexpected {args}")

    def write_text(self, path, text, *, executable=False):
        self.written[path] = text
        self.files.add(path)

    def mkdir(self, path):
        self.dirs.add(path)

    def registry_uninstall_entries(self):
        return list(self.registry)


def win_platform(tmp_path: Path) -> Platform:
    return Platform("windows", ".exe", tmp_path / "home", tmp_path / "appdata-kicad", tmp_path / "tmp")


def linux_platform(tmp_path: Path) -> Platform:
    return Platform("linux", "", tmp_path / "home", tmp_path / "config-kicad", tmp_path / "tmp")


def add_windows_install(
    m: FakeMachine, root: str, version: str, *, hive: str = "HKLM", wow: bool = False, registry=True
):
    r = Path(root)
    m.dirs.add(r)
    m.files |= {
        r / "bin" / "kicad-cli.exe",
        r / "bin" / "python.exe",
        r / "bin" / "Lib" / "site-packages" / "_pcbnew.pyd",
    }
    m.dirs.add(r / "share" / "kicad")
    m.versions[str(r / "bin" / "kicad-cli.exe")] = version
    if registry:
        key = (
            ("SOFTWARE\\WOW6432Node\\" if wow else "SOFTWARE\\")
            + "Microsoft\\Windows\\CurrentVersion\\Uninstall\\KiCad "
            + version[:-2]
        )
        m.registry.append(RegistryEntry(hive, key, "KiCad " + version[:-2], version, str(r)))


class TestWindowsDiscovery:
    def test_registry_program_files_path_and_explicit(self, tmp_path):
        m = FakeMachine(env={"ProgramFiles": "C:\\Program Files", "PATH": ""})
        add_windows_install(m, "C:/Program Files/KiCad/7.0", "7.0.6", wow=True)
        add_windows_install(m, "C:/Program Files/KiCad/10.0", "10.0.0")
        add_windows_install(m, "C:/Program Files/KiCad/9.0", "9.0.5", registry=False)  # only via glob
        add_windows_install(m, "D:/nightly", "10.99.0-unknown-abc", registry=False)  # only on PATH
        m.which_map["kicad-cli.exe"] = Path("D:/nightly/bin/kicad-cli.exe")
        add_windows_install(m, "E:/explicit/8.0", "8.0.1", registry=False)
        m.registry.append(RegistryEntry("HKLM", "x\\Other", "Other App", "1.0", "C:/Other"))  # ignored
        m.registry.append(
            RegistryEntry("HKLM", "x\\KiCad 6.0", "KiCad 6.0", "6.0.11", "C:/gone")
        )  # no binary

        installs = discover(
            [Path("E:/explicit/8.0")],
            platform=win_platform(tmp_path),
            system=m,
            storage_home=tmp_path / "home",
        )
        assert [i.version for i in installs] == ["10.99.0", "10.0.0", "9.0.5", "8.0.1", "7.0.6"]
        by = {i.version: i for i in installs}
        assert by["10.0.0"].source == "registry" and by["7.0.6"].source == "registry"
        assert "WOW6432Node" in by["7.0.6"].extra["registry_key"]
        assert by["9.0.5"].source == "program_files"
        assert by["10.99.0"].source == "path" and by["10.99.0"].extra["version_raw"] == "10.99.0-unknown-abc"
        assert by["8.0.1"].source == "explicit"
        for inst in installs:
            assert inst.kicad_cli == inst.root / "bin" / "kicad-cli.exe"
            assert (
                inst.python == inst.root / "bin" / "python.exe" and inst.python_dll_dir == inst.root / "bin"
            )
            assert inst.share == inst.root / "share" / "kicad"
            assert inst.source in SOURCES
        # exactly one `version --format plain` per distinct root, all with the throwaway config home
        version_runs = [(a, e) for a, e in m.runs if a[1:] == ("version", "--format", "plain")]
        assert len(version_runs) == 5
        assert all(
            Path(e["KICAD_CONFIG_HOME"]).is_relative_to(tmp_path / "home" / "probe-config")
            for _, e in version_runs
        )

    def test_explicit_accepts_root_bin_or_exe(self, tmp_path):
        m = FakeMachine(env={"ProgramFiles": "C:\\Program Files"})
        add_windows_install(m, "C:/Program Files/KiCad/10.0", "10.0.0", registry=False)
        p = win_platform(tmp_path)
        for spec in (
            "C:/Program Files/KiCad/10.0",
            "C:/Program Files/KiCad/10.0/bin",
            "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe",
        ):
            found = discover([Path(spec)], platform=p, system=m, storage_home=tmp_path)
            assert (
                len(found) == 1
                and found[0].source == "explicit"
                and found[0].root == Path("C:/Program Files/KiCad/10.0")
            )
        assert (
            discover([Path("C:/nowhere")], platform=p, system=m, storage_home=tmp_path)[0].source
            == "program_files"
        )

    def test_explicit_file_must_be_named_kicad_cli(self, tmp_path):
        """Finding: `find('C:/.../notkicad.cmd')` executed the script and adopted it as the install.
        An explicit FILE is only a candidate when it is literally kicad-cli(.exe); it is never run
        otherwise."""
        m = FakeMachine(env={"ProgramFiles": r"C:\Program Files"})
        planted = Path("C:/downloads/notkicad.cmd")
        m.files.add(planted)
        m.versions[str(planted)] = "10.0.0"
        p = win_platform(tmp_path)
        assert discover([planted], platform=p, system=m, storage_home=tmp_path) == []
        assert not any(a[0] == str(planted) for a, _ in m.runs), "the planted file was executed"
        renamed = Path("D:/portable/bin/KICAD-CLI.EXE")  # case-insensitive on Windows
        m.files.add(renamed)
        m.versions[str(renamed)] = "10.0.0"
        found = discover([renamed], platform=p, system=m, storage_home=tmp_path)
        assert len(found) == 1 and found[0].source == "explicit" and found[0].root == Path("D:/portable")

    def test_broken_binary_is_skipped_and_zero_installs_is_fine(self, tmp_path):
        m = FakeMachine(env={"ProgramFiles": "C:\\Program Files"})
        add_windows_install(m, "C:/Program Files/KiCad/10.0", "10.0.0", registry=False)
        m.versions[str(Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"))] = None
        assert discover(platform=win_platform(tmp_path), system=m, storage_home=tmp_path) == []
        assert discover(platform=win_platform(tmp_path), system=FakeMachine(), storage_home=tmp_path) == []


class TestLinuxDiscovery:
    """Designed-only Linux sources (kiwrap.platform.linux), exercised with a fake machine."""

    def test_distro_prefixes(self, tmp_path):
        m = FakeMachine(
            files=[
                "/usr/bin/kicad-cli",
                "/usr/bin/python3",
                "/opt/kicad9/bin/kicad-cli",
                "/usr/local/bin/kicad-cli",
            ],
            dirs=["/opt/kicad9", "/opt/other"],
        )
        m.versions.update(
            {
                "/usr/bin/kicad-cli": "10.0.6",
                "/opt/kicad9/bin/kicad-cli": "9.0.5",
                "/usr/local/bin/kicad-cli": "8.0.1",
            }
        )
        installs = discover(platform=linux_platform(tmp_path), system=m, storage_home=tmp_path / "home")
        by = {i.version: i for i in installs}
        assert list(by) == ["10.0.6", "9.0.5", "8.0.1"]
        usr = by["10.0.6"]
        assert usr.root == Path("/usr") and usr.share == Path("/usr/share/kicad") and usr.source == "apt"
        assert usr.python == Path("/usr/bin/python3") and usr.python_dll_dir is None
        assert by["9.0.5"].root == Path("/opt/kicad9") and by["9.0.5"].python is None
        assert by["8.0.1"].root == Path("/usr/local") and by["8.0.1"].extra["prefix"] == str(
            Path("/usr/local")
        )

    def test_flatpak_generates_wrappers(self, tmp_path):
        m = FakeMachine(dirs=["/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad"])
        m.which_map["flatpak"] = Path("/usr/bin/flatpak")
        m.flatpak_apps = ["org.gimp.GIMP", "org.kicad.KiCad"]
        home = tmp_path / "home"
        wrapper = home / "flatpak" / "org.kicad.KiCad" / "kicad-cli"
        m.versions[str(wrapper)] = "10.0.6"
        installs = discover(platform=linux_platform(tmp_path), system=m, storage_home=home)
        assert len(installs) == 1
        inst = installs[0]
        assert (
            inst.source == "flatpak"
            and inst.kicad_cli == wrapper
            and inst.extra == {"flatpak_id": "org.kicad.KiCad"}
        )
        assert inst.root == Path("/var/lib/flatpak/app/org.kicad.KiCad/current/active/files")
        assert inst.share == inst.root / "share" / "kicad"
        assert inst.python == wrapper.parent / "python3"
        script = m.written[wrapper]
        assert script.startswith("#!/bin/sh\n") and '--command=kicad-cli org.kicad.KiCad "$@"' in script
        assert '${KICAD_CONFIG_HOME:+"--env=KICAD_CONFIG_HOME=$KICAD_CONFIG_HOME"}' in script
        assert "--command=python3" in m.written[wrapper.parent / "python3"]
        assert (("flatpak", "list", "--app", "--columns=application"), m.env) in m.runs

    def test_flatpak_absent(self, tmp_path):
        m = FakeMachine()
        m.which_map["flatpak"] = Path("/usr/bin/flatpak")
        m.flatpak_apps = ["org.gimp.GIMP"]
        assert discover(platform=linux_platform(tmp_path), system=m, storage_home=tmp_path) == []
        assert not m.written

    def test_snap(self, tmp_path):
        m = FakeMachine(files=["/snap/bin/kicad.kicad-cli", "/snap/kicad/current/usr/bin/python3"])
        m.versions["/snap/bin/kicad.kicad-cli"] = "9.0.5"
        (inst,) = discover(platform=linux_platform(tmp_path), system=m, storage_home=tmp_path)
        assert inst.source == "snap" and inst.root == Path("/snap/kicad/current")
        assert inst.share == Path("/snap/kicad/current/usr/share/kicad") and inst.python == Path(
            "/snap/kicad/current/usr/bin/python3"
        )

    def test_appimage_is_extracted_not_executed(self, tmp_path):
        m = FakeMachine(
            files=["/home/u/Apps/KiCad-10.0.6-x86_64.AppImage", "/home/u/Apps/other.AppImage"],
            dirs=["/home/u/Apps"],
            env={"PATH": "/home/u/Apps:/usr/bin"},
        )
        home = tmp_path / "home"
        extracted = home / "appimage" / "KiCad-10.0.6-x86_64.AppImage" / "squashfs-root" / "usr"
        m.extract_creates["/home/u/Apps/KiCad-10.0.6-x86_64.AppImage"] = [
            str(extracted / "bin" / "kicad-cli"),
            str(extracted / "bin" / "python3"),
        ]
        m.versions[str(extracted / "bin" / "kicad-cli")] = "10.0.6"
        (inst,) = discover(platform=linux_platform(tmp_path), system=m, storage_home=home)
        assert (
            inst.source == "appimage"
            and inst.root == extracted
            and inst.kicad_cli == extracted / "bin" / "kicad-cli"
        )
        assert inst.python == extracted / "bin" / "python3" and inst.extra["appimage"].endswith(".AppImage")
        argvs = [(Path(a[0]).as_posix(), *a[1:]) for a, _ in m.runs]
        assert ("/home/u/Apps/KiCad-10.0.6-x86_64.AppImage", "--appimage-extract") in argvs
        assert not any(
            a[0].endswith(".AppImage") and a[1] == "version" for a in argvs
        )  # never run the GUI entry point
        # second discovery reuses the extraction
        m.runs.clear()
        discover(platform=linux_platform(tmp_path), system=m, storage_home=home)
        assert not any(a[1:] == ("--appimage-extract",) for a, _ in m.runs)

    def test_path_dedups_against_distro(self, tmp_path):
        m = FakeMachine(files=["/usr/bin/kicad-cli"], env={"PATH": "/usr/bin"})
        m.versions["/usr/bin/kicad-cli"] = "10.0.6"
        m.which_map["kicad-cli"] = Path("/usr/bin/kicad-cli")
        installs = discover(platform=linux_platform(tmp_path), system=m, storage_home=tmp_path)
        assert len(installs) == 1 and installs[0].source == "apt"


def test_flatpak_wrapper_script_is_posix_sh():
    script = flatpak_wrapper_script("kicad-cli")
    assert script.startswith("#!/bin/sh\n")
    assert script.count("--env=") == 5 and script.rstrip().endswith('"$@"')
    assert "--filesystem=host" in script


# --- find() / describe() ------------------------------------------------------------------------------


def fake_installs(tmp_path: Path) -> list[KiCadInstallation]:
    def mk(version: str, name: str) -> KiCadInstallation:
        root = tmp_path / name
        return KiCadInstallation(
            root, root / "bin" / "kicad-cli.exe", root / "share" / "kicad", version, "registry"
        )

    return [mk("10.0.6", "ten6"), mk("10.0.0", "ten0"), mk("9.0.5", "nine"), mk("8.0.1", "eight")]


class TestFind:
    def test_specs(self, tmp_path):
        installs = fake_installs(tmp_path)
        assert find(None, installs).version == "10.0.6"
        assert find("", installs).version == "10.0.6"
        assert find("10", installs).version == "10.0.6"
        assert find("10.0", installs).version == "10.0.6"
        assert find("10.0.0", installs).version == "10.0.0"
        assert find("9", installs).version == "9.0.5"
        assert find(str(tmp_path / "eight"), installs).version == "8.0.1"
        assert find(str(tmp_path / "nine" / "bin" / "kicad-cli.exe"), installs).version == "9.0.5"
        assert find(str(tmp_path / "nine" / "bin"), installs).version == "9.0.5"

    def test_no_match_raises_with_found_list(self, tmp_path):
        installs = fake_installs(tmp_path)
        with pytest.raises(NoKiCadError) as exc:
            find("7", installs)
        assert exc.value.spec == "7" and exc.value.found == installs and "10.0.6" in str(exc.value)
        with pytest.raises(NoKiCadError):
            find(None, [])
        with pytest.raises(NoKiCadError):
            find(str(tmp_path / "missing"), installs)

    def test_path_spec_probes_when_not_discovered(self, tmp_path, monkeypatch):
        installs = fake_installs(tmp_path)
        extra = KiCadInstallation(
            tmp_path / "x",
            tmp_path / "x" / "bin" / "kicad-cli",
            tmp_path / "x" / "share",
            "10.99.0",
            "explicit",
        )
        monkeypatch.setattr(
            installation, "discover", lambda explicit=(), **kw: [extra] if explicit else installs
        )
        assert find(str(tmp_path / "x"), installs) is extra
        with pytest.raises(NoKiCadError):
            find("10.99", None)  # numeric specs never probe paths
        with pytest.raises(NoKiCadError):
            find(str(tmp_path / "x"), installs, probe_paths=False)  # the MCP tools never probe a path


def test_describe(tmp_path):
    installs = fake_installs(tmp_path)
    text = describe(installs)
    lines = text.splitlines()
    assert lines[0].split()[:3] == ["version", "source", "root"] and len(lines) == 2 + len(installs)
    assert lines[2].startswith("10.0.6")
    with_caps = describe(installs, lambda i: {"cli.has_drc": i.major >= 8, "swig.available": True})
    assert with_caps.splitlines()[0].split()[-7:] == [
        "erc",
        "drc",
        "render",
        "jobset",
        "upgrade",
        "swig",
        "ipc",
    ]
    assert with_caps.splitlines()[2].split()[-7:] == ["n", "y", "n", "n", "n", "y", "n"]
    assert describe([]) == "no KiCad installation found"


# --- live ------------------------------------------------------------------------------------------------


def tree_hash(root: Path) -> str:
    """SHA-256 over sorted relative paths + bytes of every file under root ('' when absent)."""
    if not root.exists():
        return ""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@pytest.mark.kicad
def test_live_discovery_fields(installs):
    versions = [i.version for i in installs]
    assert versions == sorted(versions, key=version_tuple, reverse=True)
    assert len({i.root for i in installs}) == len(installs)
    for inst in installs:
        assert inst.kicad_cli.is_file() and inst.kicad_cli.parent == inst.root / "bin"
        assert inst.share.is_dir() and inst.dir("template").is_dir() and inst.dir("symbols").is_dir()
        assert inst.source in SOURCES
        assert inst.major_minor == inst.root.name  # C:/Program Files/KiCad/<major.minor>
        assert inst.python is not None and inst.python.is_file() and inst.python_dll_dir == inst.root / "bin"
        assert inst.extra.get("display_version", inst.version).startswith(inst.version)


@pytest.mark.kicad
def test_live_expected_versions_present(installs):
    """This machine has 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0; skip gracefully elsewhere."""
    expected = {"7.0.6", "8.0.1", "9.0.5", "10.0.0"}
    found = {i.version for i in installs}
    if not expected <= found:
        pytest.skip(f"reference machine layout not present (found {sorted(found)})")
    by = {i.version: i for i in installs}
    assert by["7.0.6"].source == "registry" and "WOW6432Node" in by["7.0.6"].extra["registry_key"]
    for v in ("8.0.1", "9.0.5", "10.0.0"):
        assert by[v].source == "registry" and "WOW6432Node" not in by[v].extra["registry_key"]
    assert find("10", installs).version == "10.0.0" and find("7.0", installs).version == "7.0.6"


@pytest.mark.kicad
def test_live_user_kicad_config_untouched(installs, storage_home):
    """Hard rule: discover() + capabilities_for(refresh=True) on every install leave
    %APPDATA%/kicad (the user's real config root) byte-for-byte unchanged."""
    from kiwrap.capability import capabilities_for
    from kiwrap.platform import current

    user_root = current().user_kicad_config_home
    if not user_root.exists():
        pytest.skip("no user KiCad config on this machine")
    before = tree_hash(user_root)
    home = storage_home / "untouched"
    found = discover(storage_home=home)
    assert [i.version for i in found] == [i.version for i in installs]
    for inst in found:
        caps = capabilities_for(inst, refresh=True, storage_home=home)
        assert caps.probed
    assert tree_hash(user_root) == before
    # and everything kicad-cli wrote went under the storage home's probe-config
    assert (home / installation.PROBE_CONFIG_DIRNAME / "config").is_dir()
    assert os.environ.get("KICAD_CONFIG_HOME") is None or not Path(
        os.environ["KICAD_CONFIG_HOME"]
    ).is_relative_to(user_root)
