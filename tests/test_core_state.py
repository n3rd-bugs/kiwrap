"""Unit tests for kiwrap.core.state: project, stages, gates, locks, snapshots (real git), jobs, receipts."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from kiwrap.core.state import STAGES, KiwrapError, Receipt
from kiwrap.core.state import snapshots as snap
from kiwrap.core.state.gates import GATES, Force, check_gate
from kiwrap.core.state.jobs import JobHandle, JobQueue
from kiwrap.core.state.locks import ProjectLock, project_mutation, refuse_if_editor_open
from kiwrap.core.state.project import Project, ProjectState
from kiwrap.core.state.receipts import ErrorOut, ReceiptOut, error_to_result, png_size, receipt_to_result
from kiwrap.core.state.stages import check_external_edit, downgrade_below, next_suggested, record_stage
from tests.test_core_fakes import png_bytes

HAVE_GIT = shutil.which("git") is not None


def make_project(tmp_path: Path, name: str = "proj") -> Project:
    d = tmp_path / name
    d.mkdir()
    (d / f"{name}.kicad_pro").write_text("{}")
    (d / f"{name}.kicad_sch").write_text("(kicad_sch (version 1))")
    (d / f"{name}.kicad_pcb").write_text("(kicad_pcb (version 1))")
    return Project.from_path(d)


# ---- project ---------------------------------------------------------------------------------------


def test_project_from_dir_and_pro_path(tmp_path: Path):
    p = make_project(tmp_path)
    assert p.name == "proj" and p.root_sch.exists() and p.board.exists()
    assert Project.from_path(p.pro_path) == p
    assert p.state_path == p.dir / ".kiwrap" / "state.json"


def test_project_not_found_variants(tmp_path: Path):
    with pytest.raises(KiwrapError) as e:
        Project.from_path(tmp_path / "missing")
    assert e.value.code == "E_PROJECT_NOT_FOUND"
    (tmp_path / "empty").mkdir()
    with pytest.raises(KiwrapError) as e:
        Project.from_path(tmp_path / "empty")
    assert e.value.code == "E_PROJECT_NOT_FOUND" and "kw_project" in e.value.fix
    (tmp_path / "empty" / "a.kicad_pro").write_text("{}")
    (tmp_path / "empty" / "b.kicad_pro").write_text("{}")
    with pytest.raises(KiwrapError) as e:
        Project.from_path(tmp_path / "empty")
    assert [Path(c).name for c in e.value.candidates] == ["a.kicad_pro", "b.kicad_pro"]


def test_state_round_trip_and_corruption(tmp_path: Path):
    p = make_project(tmp_path)
    assert p.load_state() == ProjectState()
    st = ProjectState(stage="erc_clean", hashes={"erc_clean": {"proj.kicad_sch": "x"}}, target_kicad="10.0.0")
    p.save_state(st)
    assert p.load_state() == st
    p.state_path.write_text("{not json")
    with pytest.raises(KiwrapError) as e:
        p.load_state()
    assert e.value.code == "E_INTERNAL"
    p.state_path.write_text(json.dumps({"stage": "bogus"}))
    assert p.load_state().stage == "created"


# ---- stages ----------------------------------------------------------------------------------------


def test_record_and_external_edit_downgrade(tmp_path: Path):
    p = make_project(tmp_path)
    st = ProjectState()
    record_stage(p, st, "erc_clean")
    record_stage(p, st, "drc_clean")
    assert st.stage == "drc_clean" and set(st.hashes) == {"erc_clean", "drc_clean"}
    # no edit -> nothing happens
    chk = check_external_edit(p, st)
    assert not chk.external_edit and st.stage == "drc_clean"
    # a hand edit of the schematic invalidates erc_clean AND everything after it (linear machine)
    p.root_sch.write_text("(kicad_sch (version 2))")
    chk = check_external_edit(p, st)
    assert chk.external_edit and chk.changed_files == ["proj.kicad_sch"]
    assert chk.invalidated_stages == ["erc_clean"]
    assert st.stage == "sch_written" and st.hashes == {}


def test_board_edit_keeps_erc(tmp_path: Path):
    p = make_project(tmp_path)
    st = ProjectState()
    record_stage(p, st, "erc_clean")
    record_stage(p, st, "drc_clean")
    record_stage(p, st, "exported")
    p.board.write_text("changed")
    chk = check_external_edit(p, st)
    assert chk.invalidated_stages == ["drc_clean", "exported"]
    assert st.stage == "erc_clean" and set(st.hashes) == {"erc_clean"}


def test_missing_file_counts_as_changed(tmp_path: Path):
    p = make_project(tmp_path)
    st = ProjectState()
    record_stage(p, st, "erc_clean")
    p.root_sch.unlink()
    assert check_external_edit(p, st).external_edit


def test_downgrade_below_and_next_suggested(tmp_path: Path):
    p = make_project(tmp_path)
    st = ProjectState()
    record_stage(p, st, "erc_clean")
    record_stage(p, st, "drc_clean")
    downgrade_below(st, "drc_clean")
    assert st.stage == "erc_clean" and "drc_clean" not in st.hashes
    downgrade_below(st, "erc_clean")
    assert st.stage == "sch_written"
    assert [next_suggested(s) for s in STAGES][:3] == ["kw_sch_check", "kw_sch_check", "kw_pcb_drc"]


def test_record_stage_does_not_go_backwards(tmp_path: Path):
    p = make_project(tmp_path)
    st = ProjectState(stage="exported")
    record_stage(p, st, "erc_clean")
    assert st.stage == "exported" and "erc_clean" in st.hashes


# ---- gates -----------------------------------------------------------------------------------------


def test_gates_and_force():
    assert set(GATES) == {"kw_pcb_sync", "kw_pcb_route_auto", "kw_fab_export"}
    assert check_gate("kw_fab_export", "drc_clean") is None
    assert check_gate("kw_fab_export", "exported") is None
    assert check_gate("kw_env", "created") is None
    with pytest.raises(KiwrapError) as e:
        check_gate("kw_fab_export", "erc_clean")
    assert e.value.code == "E_GATE" and e.value.data["required_stage"] == "drc_clean"
    assert check_gate("kw_fab_export", "created", Force.parse({"reason": "proto"})) == "proto"
    assert Force.parse(None) is None
    assert Force.parse("because").reason == "because"
    for bad in ({}, {"reason": "  "}, "", 42):
        with pytest.raises(KiwrapError) as e:
            Force.parse(bad)
        assert e.value.code == "E_BAD_ARGS"


# ---- locks -----------------------------------------------------------------------------------------


def test_project_lock_exclusive_and_timeout(tmp_path: Path):
    lock_path = tmp_path / ".kiwrap" / "lock"
    with ProjectLock(lock_path, timeout_s=0.3):
        assert lock_path.exists()
        with pytest.raises(KiwrapError) as e:
            ProjectLock(lock_path, timeout_s=0.3).acquire()
        assert e.value.code == "E_FILE_LOCKED"
    assert not lock_path.exists()


def test_project_lock_stale_is_broken(tmp_path: Path):
    lock_path = tmp_path / ".kiwrap" / "lock"
    lock_path.parent.mkdir()
    lock_path.write_text("pid=1\n")
    old = time.time() - 7200
    import os

    os.utime(lock_path, (old, old))
    with ProjectLock(lock_path, timeout_s=1, stale_after_s=3600):
        pass


def test_stale_lock_breaking_never_deletes_a_fresh_lock(tmp_path: Path):
    """Finding (TOCTOU): B sees the old lock as stale; A breaks it and creates its own; B's
    stale-breaking step must not remove A's fresh lock, so B keeps waiting instead of both
    holding the mutation lock."""
    import os

    lock_path = tmp_path / ".kiwrap" / "lock"
    lock_path.parent.mkdir()
    lock_path.write_text("pid=1")
    old = time.time() - 7200
    os.utime(lock_path, (old, old))
    b = ProjectLock(lock_path, timeout_s=0.4, stale_after_s=3600)
    assert not b._try_acquire() and b._is_stale()
    a = ProjectLock(lock_path, timeout_s=1, stale_after_s=3600)
    a.acquire()  # breaks the stale lock, holds a fresh one
    assert lock_path.read_text().startswith(f"pid={os.getpid()}")
    b._break_stale()  # what B does next in the old code: unlink() - A's lock
    assert lock_path.exists() and not b._try_acquire()
    with pytest.raises(KiwrapError) as e:
        b.acquire()
    assert e.value.code == "E_FILE_LOCKED"
    assert not list(lock_path.parent.glob("lock.stale-*")) and not (lock_path.parent / "lock.break").exists()
    a.release()
    assert not lock_path.exists()
    b.acquire()
    b.release()


def test_project_lock_threads_serialise(tmp_path: Path):
    lock_path = tmp_path / "lock"
    active, peak = 0, 0
    guard = threading.Lock()

    def work():
        nonlocal active, peak
        with ProjectLock(lock_path, timeout_s=10):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


def test_editor_lock_refusal(tmp_path: Path):
    p = make_project(tmp_path)
    refuse_if_editor_open(p)
    (p.dir / "~proj.kicad_pcb.lck").write_text("{}")
    with pytest.raises(KiwrapError) as e, project_mutation(p):
        pass
    assert e.value.code == "E_FILE_LOCKED" and e.value.candidates == ["~proj.kicad_pcb.lck"]


# ---- snapshots (real git) ---------------------------------------------------------------------------


def _user_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """A plain git call in the USER's repository (never through kiwrap's store)."""
    return subprocess.run(
        ["git", "-c", "user.name=u", "-c", "user.email=u@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.skipif(not HAVE_GIT, reason="git not on PATH")
def test_snapshots_create_list_restore_diff(tmp_path: Path):
    p = make_project(tmp_path)
    assert snap.ensure_repo(p.dir) is True
    assert snap.ensure_repo(p.dir) is False
    assert snap.store_dir(p.dir) == p.dir / ".kiwrap" / "snapshots.git" and snap.has_store(p.dir)
    assert not (p.dir / ".git").exists() and not (p.dir / ".gitignore").exists()
    assert "snapshots.git/" in (p.dir / ".kiwrap" / ".gitignore").read_text()
    sha1 = snap.create(p.dir, "[kiwrap] kw_project create proj")
    assert sha1 and snap.head(p.dir) == sha1
    assert snap.create(p.dir, "[kiwrap] nothing") == sha1  # nothing changed -> same head
    p.board.write_text("(kicad_pcb (version 2))")
    (p.dir / "extra.kicad_sym").write_text("(kicad_symbol_lib)")
    sha2 = snap.create(p.dir, "[kiwrap] kw_pcb_drc ok")
    assert sha2 != sha1
    entries = snap.list_snapshots(p.dir)
    assert [e.sha for e in entries] == [sha2, sha1]
    assert entries[0].message == "[kiwrap] kw_pcb_drc ok" and entries[0].at
    d = snap.diff(p.dir, sha1, sha2)
    assert {f["path"] for f in d["files"]} == {"proj.kicad_pcb", "extra.kicad_sym"}
    # restore = universal undo, including removal of files added since
    sha3 = snap.restore(p.dir, sha1)
    assert p.board.read_text() == "(kicad_pcb (version 1))"
    assert not (p.dir / "extra.kicad_sym").exists()
    assert (
        sha3
        and sha3 != sha2
        and snap.list_snapshots(p.dir)[0].message.startswith("[kiwrap] kw_snapshot restore")
    )
    with pytest.raises(KiwrapError) as e:
        snap.resolve(p.dir, "nope")
    assert e.value.code == "E_BAD_ARGS" and e.value.candidates


def test_owned_files_is_an_allow_list():
    assert snap.is_owned_file("blinky.kicad_pro") and snap.is_owned_file("sub/sheet.kicad_sch")
    assert snap.is_owned_file("libs/x.pretty/r.kicad_mod") and snap.is_owned_file("rules.kicad_dru")
    assert snap.is_owned_file("fp-lib-table") and snap.is_owned_file(".kiwrap/state.json")
    for rel in ("blinky.kicad_prl", ".env", "notes.txt", "secrets.py", ".kiwrap/lock", "x.kicad_"):
        assert not snap.is_owned_file(rel), rel


@pytest.mark.skipif(not HAVE_GIT, reason="git not on PATH")
def test_snapshot_never_touches_a_users_repository(tmp_path: Path):
    """Finding: the project directory IS a user's repository root (uncommitted .env and WIP) and
    also sits inside another repository. Snapshots must neither commit into either repository
    nor sweep the unrelated files into kiwrap's own store."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _user_git(outer, "init", "-q")
    p = make_project(outer, "inner")
    _user_git(p.dir, "init", "-q")
    (p.dir / ".env").write_text("SECRET=abc")
    (p.dir / "unrelated_wip.py").write_text("x = 1\n")
    (p.dir / "inner.kicad_prl").write_text("{}")
    sha = snap.create(p.dir, "[kiwrap] kw_project create x")
    assert sha and snap.head(outer) is None
    own = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=p.dir, capture_output=True, text=True)
    assert own.returncode != 0 and own.stdout.strip() == ""  # the user's own repo got no commit
    tracked = snap._git(p.dir, "ls-files").stdout.split()
    assert sorted(tracked) == ["inner.kicad_pcb", "inner.kicad_pro", "inner.kicad_sch"]
    # and the store itself is invisible to the user's repository
    status = _user_git(p.dir, "status", "--porcelain", "--ignored", "--untracked-files=all").stdout
    assert "!! .kiwrap/snapshots.git/" in status


@pytest.mark.skipif(not HAVE_GIT, reason="git not on PATH")
def test_snapshot_ops_refuse_without_a_store_instead_of_using_the_enclosing_repo(tmp_path: Path):
    """Finding: a project that was never snapshotted but lives inside a repository must not resolve
    refs in (or check out from) that enclosing repository."""
    outer = tmp_path / "outer"
    proj = outer / "proj"
    proj.mkdir(parents=True)
    _user_git(outer, "init", "-q")
    (proj / "blinky.kicad_pcb").write_text("(kicad_pcb (version 1))")
    _user_git(outer, "add", "-A")
    _user_git(outer, "commit", "-q", "-m", "user commit")
    (proj / "blinky.kicad_pcb").write_text("(kicad_pcb (version 2) EDITED-BY-USER-UNCOMMITTED)")
    assert snap.list_snapshots(proj) == [] and snap.head(proj) is None
    for op in (
        lambda: snap.restore(proj, "HEAD", commit=False),
        lambda: snap.resolve(proj, "HEAD"),
        lambda: snap.diff(proj, "HEAD"),
    ):
        with pytest.raises(KiwrapError) as e:
            op()
        assert e.value.code == "E_BAD_ARGS" and "no snapshots yet" in e.value.message
    assert (proj / "blinky.kicad_pcb").read_text() == "(kicad_pcb (version 2) EDITED-BY-USER-UNCOMMITTED)"
    # a store created afterwards only ever sees its own history
    snap.create(proj, "[kiwrap] first")
    assert [s.message for s in snap.list_snapshots(proj)] == ["[kiwrap] first"]
    with pytest.raises(KiwrapError):
        snap.resolve(proj, "HEAD~1")


# ---- jobs ------------------------------------------------------------------------------------------


def test_jobs_run_progress_persist_and_cancel(tmp_path: Path):
    q = JobQueue(tmp_path / "jobs", max_workers=1)
    try:

        def ok(h: JobHandle) -> dict:
            h.progress(0.5, "half")
            return {"answer": 42}

        rec = q.submit("t", ok)
        done = q.wait(rec.job_id, 5)
        assert done.state == "done" and done.result == {"answer": 42} and done.progress == 1.0
        assert json.loads((tmp_path / "jobs" / f"{rec.job_id}.json").read_text())["state"] == "done"

        def boom(h: JobHandle) -> dict:
            raise KiwrapError("E_KICAD_FAILED", "nope")

        rec = q.submit("t", boom)
        assert q.wait(rec.job_id, 5).error["code"] == "E_KICAD_FAILED"

        def crash(h: JobHandle) -> dict:
            raise ValueError("bad")

        rec = q.submit("t", crash)
        assert q.wait(rec.job_id, 5).error["code"] == "E_INTERNAL"

        started = threading.Event()

        def slow(h: JobHandle) -> dict:
            started.set()
            for _ in range(200):
                h.check()
                time.sleep(0.01)
            return {}

        rec = q.submit("t", slow)
        started.wait(5)
        q.cancel(rec.job_id)
        assert q.wait(rec.job_id, 5).state == "cancelled"
        with pytest.raises(KiwrapError) as e:
            q.get("missing")
        assert e.value.code == "E_BAD_ARGS"
        assert [j.name for j in q.list()] == ["t"] * 4
    finally:
        q.shutdown()


def test_job_persisted_record_survives_new_queue(tmp_path: Path):
    q = JobQueue(tmp_path / "jobs")
    rec = q.submit("t", lambda h: {"x": 1})
    q.wait(rec.job_id, 5)
    q.shutdown()
    q2 = JobQueue(tmp_path / "jobs")
    assert q2.get(rec.job_id).result == {"x": 1}


# ---- receipts --------------------------------------------------------------------------------------


def test_receipt_to_result_with_image_and_schema():
    r = Receipt(
        provider_used="cli:10.0.0", stage="drc_clean", image=png_bytes(8, 6), data={"ok": True, "n": 1}
    )
    res = receipt_to_result("kw_pcb_render", r)
    assert not res.is_error
    assert [type(c).__name__ for c in res.content] == ["TextContent", "ImageContent"]
    assert base64.b64decode(res.content[1].data)[:8] == b"\x89PNG\r\n\x1a\n"
    assert res.structured_content["image_info"] == {
        "width": 8,
        "height": 6,
        "bytes": len(r.image),
        "mime_type": "image/png",
    }
    assert "image" not in res.structured_content
    ReceiptOut.model_validate(res.structured_content)  # matches the published outputSchema
    assert res.content[0].text.startswith("kw_pcb_render: ok  stage=drc_clean  provider=cli:10.0.0")
    assert png_size(b"nope") is None


def test_error_to_result_round_trip():
    err = KiwrapError("E_GATE", "no", why="w", fix="f", candidates=["a"], example="ex", data={"k": 1})
    res = error_to_result(err)
    assert res.is_error and res.structured_content == err.to_dict()
    ErrorOut.model_validate(res.structured_content)
    assert "E_GATE: no" in res.content[0].text and "fix: f" in res.content[0].text
