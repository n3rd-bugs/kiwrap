"""Per-project cross-process mutation lock (docs/ARCHITECTURE.md §11 "per-project mutation lock").

Boring and portable: an `O_CREAT | O_EXCL` lock file at `<project>/.kiwrap/lock` holding the
owner's pid, hostname and timestamp. No `fcntl`/`msvcrt` (only `kiwrap.platform` may branch on
the OS). A lock older than `stale_after_s` is treated as abandoned (a crashed tool call) and is
broken; a tool call never holds the lock for that long because every kicad-cli run has its own
timeout. Breaking is serialised through an `O_EXCL` break token and done by RENAMING the stale
file away and checking its identity (inode + mtime) before deleting it, so two waiters that both
saw the same stale lock can never end up with one of them deleting the other's fresh lock (the
check-then-unlink TOCTOU that let two processes hold the mutation lock at once).

The same module refuses to mutate a project a KiCad GUI is editing: KiCad writes `~<file>.lck`
next to an open file (§11 editor-lock refusal -> E_FILE_LOCKED).
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

from kiwrap.core.state import KiwrapError
from kiwrap.core.state.project import Project


class ProjectLock:
    def __init__(
        self, lock_path: Path, *, timeout_s: float = 60.0, stale_after_s: float = 3600.0, poll_s: float = 0.1
    ):
        self.lock_path = lock_path
        self.timeout_s, self.stale_after_s, self.poll_s = timeout_s, stale_after_s, poll_s
        self._held = False

    def _try_acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"pid={os.getpid()}\nhost={socket.gethostname()}\nat={time.time():.3f}\n")
        self._held = True
        return True

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > self.stale_after_s

    def _break_stale(self) -> None:
        """Remove an abandoned lock without ever removing a live one.

        Only the waiter that creates `<lock>.break` (O_EXCL) breaks; it re-checks staleness, moves
        the lock file aside atomically, and deletes the moved file only if it is the very file it
        judged stale (same inode and mtime). A live lock that slipped into the window is moved back.
        """
        token = self.lock_path.with_name(self.lock_path.name + ".break")
        try:
            fd = os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            with contextlib.suppress(OSError):  # a breaker that died mid-way must not wedge everyone
                if time.time() - token.stat().st_mtime > self.stale_after_s:
                    token.unlink()
            return
        os.close(fd)
        moved = self.lock_path.with_name(f"{self.lock_path.name}.stale-{os.getpid()}")
        try:
            try:
                before = self.lock_path.stat()
            except FileNotFoundError:
                return
            if time.time() - before.st_mtime <= self.stale_after_s:
                return
            try:
                self.lock_path.replace(moved)
            except FileNotFoundError:
                return
            try:
                after = moved.stat()
            except FileNotFoundError:
                return
            same = (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
            if same or time.time() - after.st_mtime > self.stale_after_s:
                with contextlib.suppress(OSError):
                    moved.unlink()
            else:
                with contextlib.suppress(OSError):
                    moved.replace(self.lock_path)
        finally:
            with contextlib.suppress(OSError):
                token.unlink()

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_s
        while True:
            if self._try_acquire():
                return
            if self._is_stale():
                self._break_stale()
                continue
            if time.monotonic() >= deadline:
                owner = ""
                with contextlib.suppress(OSError):
                    owner = self.lock_path.read_text(encoding="utf-8").replace("\n", " ").strip()
                raise KiwrapError(
                    "E_FILE_LOCKED",
                    f"project is locked by another kiwrap call ({owner or 'unknown owner'})",
                    why="only one mutating tool may run against a project at a time",
                    fix="wait for the other call (or kw_job) to finish; delete <project>/.kiwrap/lock if no kiwrap process is running",
                    data={"lock": self.lock_path.as_posix()},
                )
            time.sleep(self.poll_s)

    def release(self) -> None:
        if self._held:
            self._held = False
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def refuse_if_editor_open(project: Project) -> None:
    """E_FILE_LOCKED when KiCad holds a `~*.lck` next to a design file (§11)."""
    locks = project.editor_locks()
    if locks:
        raise KiwrapError(
            "E_FILE_LOCKED",
            f"KiCad has {len(locks)} file(s) of this project open",
            why="editing a file on disk while a KiCad editor holds it would be overwritten on the next GUI save",
            fix="close the file in KiCad (or delete the stale ~*.lck file if KiCad is not running) and retry",
            candidates=[p.name for p in locks],
        )


@contextlib.contextmanager
def project_mutation(project: Project, *, timeout_s: float = 60.0) -> Iterator[None]:
    """Lock + editor-lock refusal, the prologue of every mutating tool."""
    refuse_if_editor_open(project)
    with ProjectLock(project.lock_path, timeout_s=timeout_s):
        yield
