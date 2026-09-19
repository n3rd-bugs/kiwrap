"""kiwrap.core.planner: provider order, capability predicates (key + subcommand fallback), the
ipc11 -> file -> cli fall-through with `degraded` / warnings, and the E_UNSUPPORTED teaching
error when nothing can serve an operation. No KiCad involved."""

from __future__ import annotations

import pytest

from kiwrap.core import planner
from kiwrap.core.state import KiwrapError
from tests.test_core_fakes import CAPS_10, FakeCaps, FakeInstall

CAPS_11 = {
    **CAPS_10,
    "ipc.headless": True,
    "ipc.schematic": True,
    "cli.has_stats": True,
    "cli.has_stats_json": True,
    "cli.subcommands": [
        "sch erc",
        "pcb drc",
        "sch diff",
        "pcb export stats",
        "pcb export stackup",
        "sch export netlist",
    ],
}

INSTALL_10 = FakeInstall()
INSTALL_11 = FakeInstall(version="11.0.0")


def caps(values: dict, install: FakeInstall = INSTALL_10) -> FakeCaps:
    return FakeCaps(values, install)


# ---- the table -----------------------------------------------------------------------------------------


def test_every_operation_is_declared_with_known_providers_and_documented():
    for name in ("board.read", "schematic.read", "connectivity", "overlaps", "netlist", "stats", "diff"):
        op = planner.operation(name)
        assert op.providers, name
        assert all(p.name in planner.PROVIDERS for p in op.providers), name
    described = planner.describe()
    assert set(described) == set(planner.OPERATIONS)
    assert described["diff"]["fallback"] == "kw_sch_get on both files + compare"
    with pytest.raises(KiwrapError) as ei:
        planner.operation("nope")
    assert ei.value.code == "E_INTERNAL" and "board.read" in ei.value.candidates


def test_ipc11_first_when_headless_else_file():
    assert planner.plan("board.read", caps(CAPS_11)).candidates[0].name == "ipc11"
    p10 = planner.plan("board.read", caps(CAPS_10))
    assert [c.name for c in p10.candidates] == ["file"]
    assert p10.skipped == [("ipc11", "missing capability ipc.headless")]
    # the schematic read needs both keys
    assert planner.plan("schematic.read", caps({**CAPS_11, "ipc.schematic": False})).skipped == [
        ("ipc11", "missing capability ipc.schematic")
    ]


def test_cli_predicates_fall_back_to_the_subcommand_list_only_when_the_key_is_absent():
    # a record that lacks cli.has_sch_diff but lists the subcommand (an older cached probe)
    old = {k: v for k, v in CAPS_11.items() if k != "cli.has_sch_diff"}
    assert planner.plan("diff", caps(old)).candidates[0].name == "cli"
    # the record knows the key and says no: the subcommand list cannot override it
    assert not planner.plan("diff", caps({**CAPS_11, "cli.has_sch_diff": False})).candidates
    # KiCad 10: no `sch diff` subcommand -> nothing can diff
    p = planner.plan("diff", caps(CAPS_10))
    assert not p.candidates and p.skipped == [("cli", "missing capability cli.has_sch_diff")]


def test_prefer_reorders_only_eligible_providers_and_rejects_unknown_ones():
    p = planner.plan("board.read", caps(CAPS_11), prefer="file")
    assert [c.name for c in p.candidates] == ["file", "ipc11"]
    p = planner.plan("board.read", caps(CAPS_10), prefer="ipc11")  # not eligible: the rest, flagged
    assert [c.name for c in p.candidates] == ["file"]
    assert p.preferred == "ipc11" and p.preferred_reason.startswith("missing capability ipc.headless")
    assert p.to_dict()["preferred_unavailable"] == {"provider": "ipc11", "reason": p.preferred_reason}
    out = planner.run("board.read", caps(CAPS_10), {"file": lambda: "answer"}, install=None, prefer="ipc11")
    assert out.value == "answer" and out.degraded == ["ipc11_unavailable"]
    assert out.warnings[0].startswith("provider 'ipc11' was requested but is not available")
    assert out.plan.to_dict()["elapsed_s"] is not None
    with pytest.raises(KiwrapError) as ei:
        planner.plan("board.read", caps(CAPS_11), prefer="cli")
    assert ei.value.code == "E_BAD_ARGS" and ei.value.candidates == ["ipc11", "file"]


def test_netlist_ranks_cli_above_file_and_tags_the_file_estimate():
    op = planner.operation("netlist")
    assert op.names() == ["ipc11", "cli", "file"]
    assert op.providers[2].degraded == ("netlist_estimated",)
    assert planner.operation("connectivity").providers[1].degraded == ("ratsnest_estimated",)


# ---- run() ----------------------------------------------------------------------------------------------


def unsupported(msg: str = "component missing") -> KiwrapError:
    return KiwrapError("E_UNSUPPORTED", msg, data={"capability": "x"})


def test_run_uses_the_first_provider_that_answers():
    calls: list[str] = []
    out = planner.run(
        "board.read",
        caps(CAPS_11, INSTALL_11),
        {
            "ipc11": lambda: calls.append("ipc11") or "board-from-ipc",
            "file": lambda: calls.append("file") or "board-from-file",
        },
        install=INSTALL_11,
    )
    assert out.value == "board-from-ipc" and calls == ["ipc11"]
    assert out.provider_used(INSTALL_11) == "ipc11:11.0.0" and out.degraded == [] and out.warnings == []


def test_run_falls_through_ipc11_to_file_with_degraded_and_a_warning():
    def ipc_fails():
        raise unsupported("the headless IPC session is not wired")

    out = planner.run(
        "connectivity",
        caps(CAPS_11, INSTALL_11),
        {"ipc11": ipc_fails, "file": lambda: ["estimate"]},
        install=INSTALL_11,
    )
    assert out.value == ["estimate"] and out.provider_name == "file"
    assert out.provider_used(INSTALL_11) == "file"
    assert out.degraded == ["ipc11_unavailable", "ratsnest_estimated"]
    assert out.warnings == [
        "ipc11 unavailable for connectivity: E_UNSUPPORTED: the headless IPC session is not wired"
    ]
    assert [p for p, _ in out.errors] == ["ipc11"]
    assert ("ipc11", "E_UNSUPPORTED: the headless IPC session is not wired") in out.plan.skipped


def test_run_treats_ipc_timeouts_and_server_failures_as_unavailable_but_not_bad_args():
    def timeout():
        raise TimeoutError("api-server did not answer")

    out = planner.run(
        "board.read", caps(CAPS_11), {"ipc11": timeout, "file": lambda: "f"}, install=INSTALL_11
    )
    assert out.provider_name == "file" and out.degraded == ["ipc11_unavailable"]

    def bad():
        raise KiwrapError("E_BAD_ARGS", "no such ref")

    with pytest.raises(KiwrapError) as ei:
        planner.run("board.read", caps(CAPS_11), {"ipc11": bad, "file": lambda: "f"}, install=INSTALL_11)
    assert ei.value.code == "E_BAD_ARGS"

    def broken():
        raise RuntimeError("boom")

    with pytest.raises(KiwrapError) as ei:
        planner.run("board.read", caps(CAPS_11), {"ipc11": broken, "file": lambda: "f"}, install=INSTALL_11)
    assert ei.value.code == "E_INTERNAL"


def test_run_without_any_provider_is_a_teaching_error_naming_the_fallback():
    with pytest.raises(KiwrapError) as ei:
        planner.run("diff", caps(CAPS_10), {"cli": lambda: {}}, install=INSTALL_10)
    err = ei.value
    assert err.code == "E_UNSUPPORTED"
    assert err.data["capability"] == "cli.has_sch_diff" and err.data["kicad"] == "10.0.0"
    assert err.data["fallback"] == "kw_sch_get on both files + compare" and "kw_sch_get" in err.fix
    assert err.data["plan"]["skipped"] == [
        {"provider": "cli", "reason": "missing capability cli.has_sch_diff"}
    ]


def test_run_reports_every_attempt_when_all_providers_fail():
    def f1():
        raise unsupported("ipc missing")

    def f2():
        raise unsupported("file reader missing")

    with pytest.raises(KiwrapError) as ei:
        planner.run("board.read", caps(CAPS_11), {"ipc11": f1, "file": f2}, install=INSTALL_11)
    err = ei.value
    assert err.code == "E_UNSUPPORTED"
    assert [a["provider"] for a in err.data["attempts"]] == ["ipc11", "file"]
    assert "ipc11 -> E_UNSUPPORTED" in err.message and "file -> E_UNSUPPORTED" in err.message


def test_run_skips_candidates_the_caller_does_not_implement():
    out = planner.run("netlist", caps(CAPS_11), {"file": lambda: "nl"}, install=INSTALL_11)
    assert out.provider_name == "file" and out.degraded == ["netlist_estimated"]
    assert ("ipc11", "not attempted by this tool") in out.plan.skipped
    assert ("cli", "not attempted by this tool") in out.plan.skipped


def test_provider_label():
    assert planner.provider_label("file", INSTALL_11) == "file"
    assert planner.provider_label("cli", INSTALL_10) == "cli:10.0.0"
    assert planner.provider_label("ipc11", None) == "ipc11"
