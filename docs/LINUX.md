# Linux support: what is decided, what is verified, what the CI job proves

Status: **design + packaging facts verified, runtime behaviour on Linux not yet verified.** Every
KiCad experiment behind [ARCHITECTURE.md](ARCHITECTURE.md) and [VERIFICATION.md](VERIFICATION.md)
ran on Windows 11 (KiCad 7.0.6 / 8.0.1 / 9.0.5 / 10.0.0). Decision **D12**
([DECISIONS.md](DECISIONS.md)) makes Linux a certified platform from Phase 0 anyway, so this page
is the contract between that decision and the evidence we actually have.

## 1. What D12 requires

| Requirement (from D12 / ARCHITECTURE.md §6, §14) | Where it lands |
|---|---|
| A platform seam from day one; no `sys.platform` outside it | `kiwrap.platform` (`current()`, `Platform.storage_home`, `user_kicad_config_home`, `temp_dir`); Linux paths: storage `$XDG_DATA_HOME/kiwrap` (override `KIWRAP_HOME`), user config `$XDG_CONFIG_HOME/kicad` (read-only), temp `$XDG_RUNTIME_DIR/kiwrap` or `/tmp/kiwrap-<uid>` |
| A Phase 0 Linux spike on a **GitHub-hosted runner** before any Linux claim enters the capability record | `.github/workflows/ci.yml` job `kicad-linux` (§4) |
| CI matrix gains a Linux job | same job, plus `unit (ubuntu-24.04)` and `lint` |
| JRE packaging per platform | `scripts/engines.json` `jre.linux` (Temurin 25.0.4.1+1 tar.gz, sha256 pinned) consumed by `scripts/bootstrap.sh` |
| Bootstrap for Linux | `scripts/bootstrap.sh` (`--check`, `--skip-sync`, `--storage-home`) |
| Same hard rules: never write `~/.config/kicad`, every kicad-cli call sandboxed | `scripts/ci/user_config_hash.py` before/after assertion in the job; `kiwrap.installation.probe_env` |
| Version isolation identical to Windows (capability keys, not versions) | `kiwrap.capability` seeded from the same matrix; Linux quirks become new keys when the spike finds them |

## 2. Verified today (2026-09-19) — packaging and bootstrap only

- **PPA exists and publishes 10.0.6 for Ubuntu 24.04 (noble, amd64):** Launchpad
  `~kicad/+archive/ubuntu/kicad-10.0-releases` publishes `kicad 10.0.6~ubuntu24.04.1` (also
  jammy 22.04, resolute 26.04; 10.0.4 for questing). Package set: `kicad`, `kicad-libraries`
  (1 KB metapackage that only *Recommends* `kicad-symbols`, `kicad-footprints`,
  `kicad-templates`, `kicad-packages3d`), `kicad-demos` (73 MB), `kicad-packages3d` (280 MB
  download, 3.3 GB installed — not needed), `kicad-doc-*`, `kicad-dbg`.
- **No separate python3 bindings package.** The `kicad` .deb (47.5 MB) itself ships
  `/usr/lib/python3/dist-packages/pcbnew.py` and `_pcbnew.so`, `/usr/bin/kicad-cli`,
  `/usr/bin/kicad`, `/usr/share/kicad`, `/usr/lib/x86_64-linux-gnu/kicad`; it depends on
  `libpython3.12t64` and `python3-wxgtk4.0`, i.e. the SWIG module is built for the **system**
  Python 3.12 — there is no bundled interpreter, which is why `KiCadInstallation.python` on
  distro Linux is `/usr/bin/python3` and `python_dll_dir` is `None`.
- **`scripts/bootstrap.sh` runs on Ubuntu 24.04** (WSL2 on this machine, `KIWRAP_HOME=/tmp/kiwrap-home`):
  installed uv 0.12.17 privately, downloaded and hash-verified Temurin JRE 25.0.4.1+1
  (`1731a34b…`) and `freerouting-2.4.1.jar` (`251101c3…`), wrote the telemetry-off profile, and
  `java -jar freerouting-2.4.1.jar --user_data_path=… --gui.enabled=false -help` printed
  `Freerouting v2.4.1 (build-date: 2026-09-03)`; the log shows `Configured network proxy:
  127.0.0.1:9` and `Analytics are disabled`. A second run downloaded nothing; `--check` exits 0/2
  as documented. (`uv sync` was skipped in that run to protect the Windows `.venv` in the shared
  checkout — CI runs it for real.)
- **Build dependencies** for a Linux source build are listed in the PPA's source package
  ([dev/kicad-source-build.md](dev/kicad-source-build.md)).

## 3. Not verified yet (each item becomes a test in the Phase 0 spike)

| Area | Assumption in the design | How the spike will settle it |
|---|---|---|
| kicad-cli parity | The 7–10 quirk table (ERC JSON ×100, `--help` exit codes, gerbers outdir creation, `.kicad_prl` writes, `--exit-code-violations`=5) holds on the Linux binaries | run T4/T6 probes on the PPA build; differences become capability keys, never `if linux` |
| `KICAD_CONFIG_HOME` semantics | KiCad appends `<major.minor>/` on Linux exactly as on Windows; `KICAD_DOCUMENTS_HOME` / `KICAD_CACHE_HOME` honoured; wizard/prompt flags identical | `sandbox.ensure()` + `kicad-cli version` + a GUI start under Xvfb; assert only the sandbox root changes |
| User config root | `$XDG_CONFIG_HOME/kicad/<major.minor>` (`~/.config/kicad/10.0`) | `user_config_hash.py` before/after every job step (already in CI) |
| SWIG under system Python | `import pcbnew` works with `/usr/bin/python3` (3.12 on noble); the swigdriver's shims (`v8plus`, `wx_noassert`) behave the same; `faulthandler` + watchdog semantics identical | `swigdriver` T7 suite under `/usr/bin/python3`; capability `swig.available` seeded from the probe, not the version |
| ABI/locale traps | No `os.add_dll_directory` needed; `LC_ALL`/`LANG` may change number formatting in reports | run T1/T6 under `LANG=C.UTF-8` and `de_DE.UTF-8` |
| IPC socket | Unix socket `ipc://<tempdir>/kicad/api.sock` (wx `GetTempDir()` → `/tmp` or `$TMPDIR`); because the socket **is** a file on Linux, KiCad's `api-<pid>.sock` fallback actually triggers for a second instance (unlike the Windows named pipe); `sun_path` limit 108 bytes → short `TMPDIR` | start a sandboxed `pcbnew` under Xvfb with `api.enable_server`, connect with kipy 0.8.0, then a second instance; record the path and the fallback |
| GUI under Xvfb | `xvfb-run -a` + GTK3 + the seeded config root starts pcbnew without the Setup wizard; `run_action`, commits and save work; crash supervision by PID works | `gui`-marked tests under `xvfb-run` (CI runs them evidence-only today, §4) |
| Flatpak `org.kicad.KiCad` | `flatpak run --command=kicad-cli org.kicad.KiCad` behaves like the native CLI given a wrapper script; env vars must be forwarded with `--env`; sandbox paths need `--filesystem` | separate spike; no CI job until the native path is green |
| Snap / AppImage | `/snap/bin/kicad-cli` (strict confinement may block `KICAD_CONFIG_HOME` paths outside `$SNAP_USER_DATA`); AppImage needs `--appimage-extract` to reach `share/kicad` | design only; lowest priority |
| Freerouting on Linux | headless routing identical to Windows (same jar, private JRE) | route the 30-net spike board (`spikes/freerouting-verify`) on the runner and diff the SES |
| Distros other than Ubuntu 24.04 | nothing | out of scope for Phase 0; Fedora/Arch when a user asks |

## 4. Discovery, paths, socket, Xvfb — the plan

**Discovery order (Linux, `kiwrap.installation.discover`)**: explicit roots → distro packages
(`/usr/bin/kicad-cli`, source `apt`) → `/usr/local/bin`, `/opt/kicad*/bin` → Flatpak
(`flatpak info org.kicad.KiCad`, source `flatpak`, `kicad_cli` = generated wrapper under
`<storage>/flatpak/`) → Snap (`/snap/bin/kicad-cli`) → AppImage on PATH (extracted under
`<storage>/appimage/<sha256>/`) → `kicad-cli` on PATH. Version always from
`kicad-cli version --format plain` run with the probe env (`<storage>/probe-config`), never from
package metadata.

**Paths per install**: `root=/usr`, `share=/usr/share/kicad` (`symbols/`, `footprints/`,
`template/`, `demos/` from `kicad-demos`, `schemas/`), `python=/usr/bin/python3`,
`python_dll_dir=None`. Corpus files come from `share/demos` at test time as on Windows.

**Config roots**: `KICAD_CONFIG_HOME=<storage>/kicad-config` (KiCad appends `10.0/`),
`KICAD_DOCUMENTS_HOME=<storage>/documents`, `KICAD_CACHE_HOME=<storage>/cache`, seeded by
`kiwrap.sandbox.ensure()` from `/usr/share/kicad/template/*-lib-table` and the user's
`~/.config/kicad/10.0` tables merged **read-only** (D7).

**Socket / live sessions (Phase 3)**: `TMPDIR=<Platform.temp_dir>` (short) for the sandboxed
GUI; expected socket `ipc://$TMPDIR/kicad/api.sock`, verified by `GetNamedPipeServerProcessId`'s
Linux equivalent — `ss -xlp | grep api.sock` or `/proc/<pid>/fd` — before a session binds.

**Xvfb**: `xvfb-run -a --server-args="-screen 0 1600x1200x24" <cmd>` around every `gui` test;
one GUI at a time (existing rule); kill by PID; `GDK_BACKEND=x11`; `NO_AT_BRIDGE=1` to silence
accessibility-bus warnings. The runner image already provides GTK3 and Mesa (llvmpipe).

**Freerouting**: `scripts/bootstrap.sh` → `<storage>/engines/jre/jdk-25.0.4.1+1-jre/bin/java`,
`<storage>/engines/freerouting/freerouting-2.4.1.jar`, profile `<storage>/freerouting/freerouting.json`
(`profile.allow_telemetry=false`, `profile.allow_contact=false`,
`usage_and_diagnostic_data.disable_analytics=true`, `network.proxy_url=http://127.0.0.1:9` to
black-hole the unconditional GitHub version check in 2.4.1 — no settings key exists for it).

## 5. Exactly what the `kicad-linux` CI job proves (and does not)

Job `kicad-linux` in `.github/workflows/ci.yml`, `ubuntu-24.04`, 60 min timeout, skippable with
the `workflow_dispatch` input `skip_kicad`:

| Step | Proves |
|---|---|
| `add-apt-repository ppa:kicad/kicad-10.0-releases` + `apt-get install --no-install-recommends kicad kicad-libraries kicad-symbols kicad-footprints kicad-templates kicad-demos xvfb` | the PPA install path works on a hosted runner; the package set kiwrap needs is < 200 MB (no 3D models) |
| `kicad-cli version --format plain` with `KICAD_CONFIG_HOME/DOCUMENTS/CACHE` under `$RUNNER_TEMP` | the CLI runs headless; the version read that discovery relies on works |
| `python3 -c "import pcbnew; print(pcbnew.GetBuildVersion())"` | the SWIG module imports under the **system** interpreter — the Linux `swigdriver` precondition |
| `user_config_hash.py --write` … `--verify` around everything else | no step created or modified `~/.config/kicad` (it hashes to `absent` before and after on a fresh runner) |
| `bash scripts/bootstrap.sh --skip-sync` then `--check` | JRE 25 + Freerouting download, hash verification, profile write and the Freerouting smoke run succeed on Linux; idempotent (`--check` exits 0) |
| `run_pytest.sh -m "kicad and not gui"` | every headless `kicad`-marked test the other components ship passes on the PPA build (this is where kicad-cli parity, format epochs and the SWIG driver get their first Linux evidence — the *content* of that evidence is whatever those test files assert) |
| `xvfb-run -a run_pytest.sh -m "kicad and gui"` (`continue-on-error: true`) | evidence only: whether sandboxed GUIs start under Xvfb; failures do not block until the Phase 0 spike promotes them |
| `uv run python -m kiwrap.installation` | Linux discovery finds the apt install with the right root/share/python |
| `collect_capabilities.py --require-one` → artifact `capabilities-linux` | the probed capability record for the Linux binary is inspectable per run and diffable against Windows |

It does **not** prove: Flatpak/Snap/AppImage discovery, IPC live sessions (needs a GUI + kipy
under Xvfb — Phase 3), anything about other distributions or about KiCad 7/8/9 on Linux (only
10.0.6 is installed; the 9.0 PPA can be added as a second job when 9 is certified in Phase 2),
or performance. A green job therefore means "KiCad 10 headless on Ubuntu 24.04 behaves as the
tests describe", nothing broader — and every Linux-specific difference the job surfaces must be
recorded as a capability key with a `[verified linux]` note in ARCHITECTURE.md §2 before any tool
depends on it.
