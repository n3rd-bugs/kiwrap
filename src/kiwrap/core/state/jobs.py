"""Thread-pool job queue for tool calls that may exceed a client's tool timeout.

docs/ARCHITECTURE.md §9: "jobs over ~20 s return `job_id` (Codex defaults to a 60 s tool
timeout)". Jobs run in a small `ThreadPoolExecutor`; state lives in memory and is mirrored as
JSON under `<storage_home>/jobs/<job_id>.json` so a restarted server can still answer
`kw_job(job_id)` for finished jobs. Cancellation is cooperative: the job function receives a
`JobHandle` and is expected to check `handle.cancelled` / call `handle.progress()` between
steps; a queued job that is cancelled never starts.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiwrap.core.state import KiwrapError

JOB_STATES = ("queued", "running", "done", "failed", "cancelled")


class JobCancelled(Exception):
    """Raised inside a job function via `handle.check()` when cancel was requested."""


@dataclass
class JobRecord:
    job_id: str
    name: str
    state: str = "queued"
    progress: float = 0.0
    message: str = ""
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobHandle:
    def __init__(self, queue: JobQueue, job_id: str):
        self._queue, self.job_id = queue, job_id
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise JobCancelled(self.job_id)

    def progress(self, fraction: float, message: str = "") -> None:
        self._queue._update(self.job_id, progress=max(0.0, min(1.0, fraction)), message=message)


class JobQueue:
    def __init__(self, storage_dir: Path | None = None, *, max_workers: int = 2):
        self.storage_dir = storage_dir
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kiwrap-job")
        self._jobs: dict[str, JobRecord] = {}
        self._handles: dict[str, JobHandle] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

    # ---- persistence ------------------------------------------------------------------------

    def _persist(self, rec: JobRecord) -> None:
        if self.storage_dir is None:
            return
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            (self.storage_dir / f"{rec.job_id}.json").write_text(
                json.dumps(rec.to_dict(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # in-memory state stays authoritative; persistence is best effort

    def _load_persisted(self, job_id: str) -> JobRecord | None:
        if self.storage_dir is None:
            return None
        p = self.storage_dir / f"{job_id}.json"
        if not p.exists():
            return None
        try:
            return JobRecord(**json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            rec = self._jobs[job_id]
            for k, v in fields.items():
                setattr(rec, k, v)
            self._persist(rec)

    # ---- API --------------------------------------------------------------------------------

    def submit(self, name: str, fn: Callable[[JobHandle], dict[str, Any]]) -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        rec = JobRecord(job_id=job_id, name=name)
        handle = JobHandle(self, job_id)
        with self._lock:
            self._jobs[job_id] = rec
            self._handles[job_id] = handle
            self._persist(rec)
        self._futures[job_id] = self._pool.submit(self._run, job_id, fn, handle)
        return rec

    def _run(self, job_id: str, fn: Callable[[JobHandle], dict[str, Any]], handle: JobHandle) -> None:
        if handle.cancelled:
            self._update(job_id, state="cancelled", finished_at=time.time())
            return
        self._update(job_id, state="running", started_at=time.time())
        try:
            result = fn(handle)
            self._update(job_id, state="done", progress=1.0, result=result, finished_at=time.time())
        except JobCancelled:
            self._update(job_id, state="cancelled", finished_at=time.time())
        except KiwrapError as exc:
            self._update(job_id, state="failed", error=exc.to_dict(), finished_at=time.time())
        except Exception as exc:  # noqa: BLE001 - a job must never take the pool thread down
            err = KiwrapError(
                "E_INTERNAL",
                f"{type(exc).__name__}: {exc}",
                data={"traceback": traceback.format_exc()[-4000:]},
            )
            self._update(job_id, state="failed", error=err.to_dict(), finished_at=time.time())

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            rec = self._jobs.get(job_id)
        if rec is None:
            rec = self._load_persisted(job_id)
        if rec is None:
            with self._lock:
                known = list(self._jobs)[-10:]
            raise KiwrapError(
                "E_BAD_ARGS",
                f"unknown job_id {job_id!r}",
                why="job ids come from a tool that returned `job_id` (async run)",
                fix="use the job_id returned by the tool that started the job",
                candidates=known,
            )
        return rec

    def cancel(self, job_id: str) -> JobRecord:
        rec = self.get(job_id)
        handle = self._handles.get(job_id)
        if handle is not None and rec.state in ("queued", "running"):
            handle._cancel.set()
            fut = self._futures.get(job_id)
            if rec.state == "queued" and fut is not None and fut.cancel():
                self._update(job_id, state="cancelled", finished_at=time.time())
        return self.get(job_id)

    def wait(self, job_id: str, timeout_s: float) -> JobRecord:
        fut = self._futures.get(job_id)
        if fut is not None:
            # the outcome (incl. a timeout) is recorded on the JobRecord; nothing to do with the exception
            with contextlib.suppress(Exception):
                fut.result(timeout=max(0.0, timeout_s))
        return self.get(job_id)

    def list(self) -> list[JobRecord]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda r: r.created_at)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
