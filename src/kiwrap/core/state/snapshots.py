"""Snapshots: a private git store per project, one auto-commit per mutation.

docs/ARCHITECTURE.md §11: "auto-commit per mutation, `kw_snapshot restore` as universal undo".

The store is `<project>/.kiwrap/snapshots.git`, driven with explicit `--git-dir` / `--work-tree`
flags, never a `.git` in the project directory:

* `--git-dir` disables git's repository discovery, so a project inside the user's own repository
  (or inside the kiwrap checkout) can never resolve refs in, check out from, or commit into that
  enclosing repository - the failure mode of the earlier `.git`-in-project design, where a project
  that had no snapshots yet silently used whatever repository enclosed it.
* Only files kiwrap or KiCad own are ever staged (`owned_files`: `*.kicad_*` except `.kicad_prl`,
  the three `*-lib-table`s, `.kiwrap/state.json`); a project directory that is somebody's
  repository root keeps its `.env`, sources and work in progress out of kiwrap's history.
* `<project>/.kiwrap/.gitignore` hides the store, the lock and the temp dir from the user's own
  repository; `.kiwrap/state.json` stays visible on purpose (it is part of the project).

Every git call runs with an explicit identity, `core.autocrlf=false` (restores are byte-exact)
and `core.hooksPath` inside the store (a user's global hooks never run for kiwrap commits).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiwrap.core.state import KiwrapError

STORE_DIRNAME = "snapshots.git"
STATE_DIRNAME = ".kiwrap"

#: Written to `<project>/.kiwrap/.gitignore` so an enclosing user repository never sees the store.
KIWRAP_DIR_IGNORE = (
    "# written by kiwrap: the snapshot store, lock and temp dir are private\nsnapshots.git/\nlock\ntmp/\n"
)

#: Directories never walked for owned files (backups are KiCad's own history, `fab/` is export output).
_SKIP_DIR_NAMES = frozenset({".git", ".kiwrap", "fab", "node_modules"})
_SKIP_DIR_SUFFIX = "-backups"
_LIB_TABLE_SUFFIX = "-lib-table"
_GIT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Snapshot:
    sha: str
    message: str
    at: str  # ISO-8601 committer date


def git_executable() -> str:
    exe = shutil.which("git")
    if not exe:
        raise KiwrapError(
            "E_INTERNAL",
            "git is not on PATH",
            why="kiwrap snapshots are git commits in a private store under <project>/.kiwrap",
            fix="install git and make sure `git --version` works from the shell that starts kiwrap",
        )
    return exe


def store_dir(project_dir: Path) -> Path:
    """`<project>/.kiwrap/snapshots.git`, the private git dir of the project's snapshot history."""
    return Path(project_dir) / STATE_DIRNAME / STORE_DIRNAME


def has_store(project_dir: Path) -> bool:
    return (store_dir(project_dir) / "HEAD").is_file()


def is_repo_root(project_dir: Path) -> bool:
    """True when the project has a snapshot store (kept under its historical name for callers)."""
    return has_store(project_dir)


def _git_base(project_dir: Path) -> list[str]:
    store = store_dir(project_dir)
    return [
        git_executable(),
        f"--git-dir={store}",
        f"--work-tree={project_dir}",
        "-c",
        "user.name=kiwrap",
        "-c",
        "user.email=kiwrap@localhost",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
        "-c",
        f"core.hooksPath={store / 'hooks'}",
    ]


def _git(
    project_dir: Path, *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one git command against the project's store (never against an enclosing repository)."""
    project_dir = Path(project_dir)
    argv = [*_git_base(project_dir), *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(project_dir),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise KiwrapError(
            "E_TIMEOUT",
            f"git {' '.join(args[:2])} did not finish within {_GIT_TIMEOUT_S:.0f} s",
            why="the snapshot store is scanning a very large project directory",
            fix="keep KiCad projects in their own directory (not a home directory or a drive root)",
            data={"argv": ["git", *args]},
        ) from exc
    if check and proc.returncode != 0:
        raise KiwrapError(
            "E_INTERNAL",
            f"git {' '.join(args[:2])} failed ({proc.returncode}): {proc.stderr.strip()[:500]}",
            why="a snapshot operation on the project's private git store failed",
            fix="run the same git command with --git-dir=<project>/.kiwrap/snapshots.git to see the full error",
            data={"argv": ["git", *args], "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]},
        )
    return proc


def _require_store(project_dir: Path) -> None:
    if not has_store(project_dir):
        raise KiwrapError(
            "E_BAD_ARGS",
            f"{project_dir} has no snapshots yet",
            why="snapshots live in <project>/.kiwrap/snapshots.git, which is created by the first mutating tool",
            fix="run kw_snapshot(action='create') (or any mutating tool) first; kw_snapshot(action='list') shows what exists",
            example="kw_snapshot(action='create', project='<dir>', message='baseline')",
        )


#: the kiwrap-owned files under `.kiwrap/` that ARE part of the project: the stage state and the
#: agent's intent declaration (`kw_intent`, Phase 1c), so `kw_snapshot restore` brings both back
_STATE_FILES: tuple[str, ...] = (f"{STATE_DIRNAME}/state.json", f"{STATE_DIRNAME}/intent.json")


def is_owned_file(rel_path: str) -> bool:
    """Whether a project-relative POSIX path is a file kiwrap snapshots (see the module docstring)."""
    name = rel_path.rsplit("/", 1)[-1]
    if rel_path in _STATE_FILES:
        return True
    if name.endswith(_LIB_TABLE_SUFFIX):
        return True
    if ".kicad_" in name:
        suffix = name[name.rindex(".kicad_") :]
        return suffix != ".kicad_prl" and len(suffix) > len(".kicad_")
    return False


def owned_files(project_dir: Path) -> list[str]:
    """Project-relative POSIX paths of every owned file on disk (sorted), walking subdirectories
    but never `.git`, `.kiwrap` (except state.json), `*-backups`, `fab` or `node_modules`."""
    project_dir = Path(project_dir)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        rel_dir = Path(dirpath).relative_to(project_dir).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.endswith(_SKIP_DIR_SUFFIX)
        )
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if is_owned_file(rel):
                out.append(rel)
    for rel in _STATE_FILES:
        if (project_dir / rel).is_file():
            out.append(rel)
    return sorted(set(out))


def ensure_repo(project_dir: Path) -> bool:
    """Create the private store unless it exists. Returns True if created. Never touches a `.git`
    the project directory may already have (that is the user's repository, not kiwrap's)."""
    project_dir = Path(project_dir)
    if has_store(project_dir):
        return False
    store = store_dir(project_dir)
    store.parent.mkdir(parents=True, exist_ok=True)
    ignore = store.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text(KIWRAP_DIR_IGNORE, encoding="utf-8")
    _git(project_dir, "init", "-q")
    return True


def head(project_dir: Path) -> str | None:
    if not has_store(project_dir):
        return None
    proc = _git(project_dir, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return proc.stdout.strip() or None


def _stage_owned(project_dir: Path) -> None:
    """`git add -A` restricted to the owned files on disk plus everything already tracked (so a
    deleted owned file is staged as a deletion); paths go through stdin (no command-line limit)."""
    tracked = _git(project_dir, "ls-files", "-z").stdout.split("\0")
    paths = sorted(set(owned_files(project_dir)) | {p for p in tracked if p})
    if not paths:
        return
    _git(
        project_dir,
        "add",
        "-A",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        stdin="\0".join(paths) + "\0",
    )


def is_dirty(project_dir: Path) -> bool:
    """True when the owned DESIGN files differ from the last snapshot (or there is no snapshot yet
    but owned files exist). `.kiwrap/state.json` alone does not count: the epilogue rewrites it
    with the snapshot sha right after committing, so it is always one step ahead of HEAD. Used for
    the pre-mutation snapshot that keeps external edits undoable."""
    if not has_store(project_dir):
        return bool(owned_files(project_dir))
    _stage_owned(project_dir)
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=no").stdout
    return any(
        line[3:].strip() != f"{STATE_DIRNAME}/state.json" for line in status.splitlines() if line.strip()
    )


def create(project_dir: Path, message: str) -> str | None:
    """Stage the owned files and commit (returns the new sha), or the current HEAD when nothing changed."""
    project_dir = Path(project_dir)
    ensure_repo(project_dir)
    _stage_owned(project_dir)
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=no").stdout
    if not status.strip():
        return head(project_dir)
    _git(project_dir, "commit", "-q", "-m", message)
    return head(project_dir)


def list_snapshots(project_dir: Path, limit: int = 50) -> list[Snapshot]:
    if not has_store(project_dir) or head(project_dir) is None:
        return []
    proc = _git(project_dir, "log", f"--max-count={limit}", "--format=%H%x1f%cI%x1f%s")
    out: list[Snapshot] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) == 3:
            out.append(Snapshot(sha=parts[0], at=parts[1], message=parts[2]))
    return out


def resolve(project_dir: Path, ref: str) -> str:
    _require_store(project_dir)
    proc = _git(project_dir, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    sha = proc.stdout.strip()
    if not sha:
        known = [s.sha[:12] for s in list_snapshots(project_dir, 20)]
        raise KiwrapError(
            "E_BAD_ARGS",
            f"unknown snapshot {ref!r}",
            why="the ref is not a commit in the project's snapshot history",
            fix="use a sha from kw_snapshot(action='list')",
            candidates=known,
        )
    return sha


def _assert_store_is_ours(project_dir: Path) -> None:
    """Belt and braces before a checkout: the store's work tree must be this project directory."""
    top = _git(project_dir, "rev-parse", "--show-toplevel").stdout.strip()
    if top and Path(top).resolve() != Path(project_dir).resolve():
        raise KiwrapError(
            "E_INTERNAL",
            f"snapshot store work tree is {top}, not {project_dir}",
            why="refusing to check out into a directory that is not the project",
            fix="delete <project>/.kiwrap/snapshots.git and snapshot again",
        )


def restore(project_dir: Path, ref: str, *, tool: str = "kw_snapshot", commit: bool = True) -> str | None:
    """Put the owned files back to `ref` (universal undo) and, unless `commit=False` (the caller
    commits), record that as a new snapshot."""
    project_dir = Path(project_dir)
    sha = resolve(project_dir, ref)
    _assert_store_is_ours(project_dir)
    _git(project_dir, "checkout", "-q", sha, "--", ".")
    # files added after the snapshot (tracked in HEAD but absent in `sha`) are removed too
    proc = _git(project_dir, "diff", "--name-only", "--diff-filter=A", sha, "HEAD")
    for rel in proc.stdout.splitlines():
        p = project_dir / rel
        if p.exists():
            p.unlink()
    if not commit:
        return None
    return create(project_dir, f"[kiwrap] {tool} restore {sha[:12]}")


def diff(project_dir: Path, a: str, b: str | None = None) -> dict[str, Any]:
    """Names + stat of what changed between two snapshots (`b` defaults to the working tree)."""
    project_dir = Path(project_dir)
    sha_a = resolve(project_dir, a)
    sha_b = resolve(project_dir, b) if b else None
    if sha_b is None:
        _stage_owned(project_dir)  # so files added since `a` show up in the worktree diff
    args_names = ["diff", "--name-status", sha_a] + ([sha_b] if sha_b else [])
    args_stat = ["diff", "--stat", sha_a] + ([sha_b] if sha_b else [])
    names = _git(project_dir, *args_names).stdout.splitlines()
    files: list[dict[str, str]] = []
    for line in names:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files.append({"status": parts[0], "path": parts[1]})
    return {
        "from": sha_a,
        "to": sha_b or "worktree",
        "files": files,
        "stat": _git(project_dir, *args_stat).stdout,
    }
