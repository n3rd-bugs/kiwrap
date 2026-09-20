"""kw_intent and the intent diff: the model, the storage under <project>/.kiwrap/intent.json, the
diff against a netlist (missing / wrong_pin / extra / unmatched, name aliases, pin-overlap
matching) and its surfacing in kw_sch_check's receipt - against the faked lower layers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kiwrap.core.state import KiwrapError
from kiwrap.core.tools.intent import Intent, intent_diff
from kiwrap.formats.reports import Component, Net, Netlist, NetNode
from tests.test_core_fakes import FakeBackend, erc_report, violation
from tests.test_core_tools import Harness

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH (snapshots)")


def _nl(*nets: tuple[str, list[tuple[str, str]]]) -> Netlist:
    refs = sorted({r for _, nodes in nets for r, _ in nodes})
    return Netlist(
        source="blink.kicad_sch",
        kicad_version="10.0.6",
        components=tuple(
            Component(ref=r, value="x", footprint=None, lib_id="Device:R", sheet_path="/") for r in refs
        ),
        nets=tuple(
            Net(code=i + 1, name=name, nodes=tuple(NetNode(r, p) for r, p in nodes))
            for i, (name, nodes) in enumerate(nets)
        ),
    )


BLINKER = _nl(
    ("+3V3", [("U1", "2"), ("C2", "1"), ("U2", "8"), ("U2", "4"), ("R1", "1"), ("#PWR01", "1")]),
    ("GND", [("J1", "2"), ("C1", "2"), ("U1", "1"), ("C2", "2"), ("#PWR02", "1"), ("#FLG01", "1")]),
    ("/VIN", [("J1", "1"), ("C1", "1"), ("U1", "3")]),
    ("/blinker/BLINK", [("R4", "1"), ("U2", "3")]),
    ("Net-(U2-THRES)", [("U2", "6"), ("U2", "2"), ("R2", "2"), ("C3", "1")]),
    ("Net-(D1-A)", [("D1", "2"), ("R4", "2")]),
)


# ---- the model --------------------------------------------------------------------------------------------------


def test_intent_model_accepts_pairs_strings_and_objects_and_rejects_junk():
    it = Intent.from_dict(
        {
            "blocks": [{"name": "ldo", "parts": ["U1", "C1", "C2"]}],
            "rails": [{"name": "+3V3", "voltage_v": 3.3}],
            "interfaces": [{"type": "power-in", "connector": "J1"}],
            "nets": {"GND": [["U1", "1"], "C1.2", {"ref": "C2", "pin": 2}, ["U1", "1"]], " VIN ": ["J1.1"]},
            "constraints": {"layers": 2, "fab": "jlcpcb"},
        }
    )
    assert it.nets == {"GND": [("U1", "1"), ("C1", "2"), ("C2", "2")], "VIN": [("J1", "1")]}
    assert it.to_dict()["nets"]["GND"] == [["U1", "1"], ["C1", "2"], ["C2", "2"]]
    assert Intent.from_dict(it.to_dict()).nets == it.nets  # round trip
    with pytest.raises(KiwrapError) as ei:
        Intent.from_dict({"nets": {"GND": ["U1"]}, "blocks": "ldo"})
    assert ei.value.code == "E_BAD_ARGS" and "GND" in ei.value.message and "blocks" in ei.value.message
    merged = it.merged(
        Intent.from_dict(
            {
                "rails": [{"name": "+3V3", "voltage_v": 3.0}, {"name": "VIN"}],
                "nets": {"VIN": ["J1.1", "U1.3"]},
            }
        )
    )
    assert [r["name"] for r in merged.rails] == ["+3V3", "VIN"] and merged.rails[0]["voltage_v"] == 3.0
    assert merged.nets["VIN"] == [("J1", "1"), ("U1", "3")] and merged.nets["GND"] == it.nets["GND"]
    assert merged.blocks == it.blocks and merged.constraints == {"layers": 2, "fab": "jlcpcb"}


# ---- the diff ---------------------------------------------------------------------------------------------------


def test_intent_diff_clean_when_every_declared_pin_is_on_its_net():
    it = Intent.from_dict(
        {
            "nets": {
                "+3V3": ["U1.2", "C2.1", "U2.8", "U2.4", "R1.1"],
                "GND": ["J1.2", "C1.2", "U1.1", "C2.2"],
                "VIN": ["J1.1", "C1.1", "U1.3"],  # local label: `/VIN` in KiCad's netlist
                "/blinker/BLINK": ["R4.1", "U2.3"],
                "THRES": ["U2.6", "U2.2", "R2.2", "C3.1"],  # no such name: matched by its pins
            }
        }
    )
    d = intent_diff(it, BLINKER)
    assert d["ok"] and d["count"] == 0 and d["missing"] == [] and d["wrong_pin"] == [] and d["extra"] == []
    assert d["nets_matched"] == {
        "+3V3": "+3V3",
        "GND": "GND",
        "VIN": "/VIN",
        "/blinker/BLINK": "/blinker/BLINK",
        "THRES": "Net-(U2-THRES)",
    }
    assert d["unmatched"] == [] and d["nets_declared"] == 5


def test_intent_unknown_keys_are_reported_and_no_connect_is_checked():
    """Phase 1c finding: a typo (`constraint`), a misplaced `strict_nets` or the plan's own
    `unnamed` / `no_connect` sections were accepted silently, so the agent believed it had
    declared them. Unknown keys now warn with the nearest known key; `no_connect` is a first-class
    declaration the diff checks."""
    it = Intent.from_dict(
        {
            "nets": {"GND": ["J1.2"]},
            "constraint": {"fab": "jlcpcb"},
            "strict_nets": True,
            "unnamed": [[["R1", "2"], ["D1", "1"]]],
            "no_connect": [["U2", "5"], "U1.3"],
        }
    )
    assert it.unknown_keys == ["constraint", "strict_nets", "unnamed"]
    assert it.no_connect == [("U2", "5"), ("U1", "3")]
    (warning,) = it.unknown_key_warnings()
    assert "constraint -> constraints" in warning and "strict_nets -> constraints.strict_nets" in warning
    assert "unnamed -> nets" in warning and "known keys:" in warning
    assert it.to_dict()["no_connect"] == [["U2", "5"], ["U1", "3"]] and "constraint" not in it.to_dict()
    # U2.5 is unconnected in the blinker netlist, U1.3 is on /VIN -> connected_no_connect
    d = intent_diff(it, BLINKER)
    assert d["connected_no_connect"] == [{"ref": "U1", "pin": "3", "kicad_net": "/VIN"}]
    assert d["count"] == 1 and not d["ok"]
    # merge keeps both sides' no_connect
    merged = Intent.from_dict({"no_connect": ["U2.5"]}).merged(Intent.from_dict({"no_connect": ["U2.7"]}))
    assert merged.no_connect == [("U2", "5"), ("U2", "7")]
    with pytest.raises(KiwrapError):
        Intent.from_dict({"no_connect": ["nonsense"]})


def test_intent_diff_reports_missing_wrong_pin_extra_and_unmatched():
    it = Intent.from_dict(
        {
            "nets": {
                "GND": [
                    "J1.2",
                    "C1.2",
                    "U1.1",
                    "C2.2",
                    "D1.1",
                    "U2.1",
                ],  # D1.1 is unconnected, U2.1 unconnected
                "VIN": ["J1.1", "C1.1", "U1.2"],  # U1.2 is on +3V3 (should be U1.3): wrong_pin
                "+3V3": ["U1.2", "C2.1", "U2.8"],  # U2.4 and R1.1 are on it too: extra
                "SDA": ["U9.1", "R7.1"],  # nothing of the sort exists
                "LED": ["D1.2", "R4.1"],  # R4.1 is on BLINK, R4.2 is on the LED net: wrong_pin
            }
        }
    )
    d = intent_diff(it, BLINKER)
    assert not d["ok"]
    missing = {(m["net"], m["ref"], m["pin"]): m for m in d["missing"]}
    assert set(missing) == {("GND", "D1", "1"), ("GND", "U2", "1"), ("SDA", "U9", "1"), ("SDA", "R7", "1")}
    assert missing[("GND", "D1", "1")]["reason"].startswith("pin is unconnected")
    assert missing[("SDA", "U9", "1")]["reason"] == "no such component in the netlist"
    wrong = {(w["net"], w["ref"]): w for w in d["wrong_pin"]}
    assert wrong[("VIN", "U1")] == {
        "net": "VIN",
        "kicad_net": "/VIN",
        "ref": "U1",
        "expected_pin": "2",
        "actual_pin": ["3"],
        "found_in": "+3V3",
    }
    assert (
        wrong[("LED", "R4")]["actual_pin"] == ["2"] and wrong[("LED", "R4")]["found_in"] == "/blinker/BLINK"
    )
    assert d["nets_matched"]["LED"] == "Net-(D1-A)"
    extra = {(e["net"], e["ref"], e["pin"]) for e in d["extra"]}
    assert extra == {("+3V3", "U2", "4"), ("+3V3", "R1", "1")}  # #PWR never; R4.2 not extra (wrong_pin case)
    assert d["unmatched"] == ["SDA"]
    # partial declarations: extra pins are listed but not counted unless the intent is strict
    assert d["strict_nets"] is False and d["count"] == len(d["missing"]) + len(d["wrong_pin"]) == 6
    it.constraints["strict_nets"] = True
    d2 = intent_diff(it, BLINKER)
    assert d2["strict_nets"] and d2["count"] == 8 and d2["extra"] == d["extra"]


# ---- the tool + kw_sch_check -----------------------------------------------------------------------------------


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    backend = FakeBackend(net=BLINKER)
    return Harness(tmp_path, backend)


def test_kw_intent_set_get_diff_clear(h: Harness):
    proj = h.create()
    assert h.ok("kw_intent", project=proj)["data"]["intent"] is None
    h.fail("kw_intent", "E_BAD_ARGS", project=proj, action="set")
    h.fail("kw_intent", "E_BAD_ARGS", project=proj, action="set", intent={"nets": {"GND": ["U1"]}})
    h.fail("kw_intent", "E_BAD_ARGS", project=proj, action="diff")  # nothing declared yet
    sc = h.ok(
        "kw_intent",
        project=proj,
        action="set",
        intent={"blocks": [{"name": "ldo"}], "nets": {"GND": ["J1.2", "C1.2"], "VIN": ["J1.1", "U1.3"]}},
    )
    d = sc["data"]
    assert d["ok"] and d["nets"] == 2 and d["pins"] == 4 and d["blocks"] == 1 and sc["warnings"] == []
    # an unknown key is accepted with a warning that names the nearest known key
    sc_typo = h.ok("kw_intent", project=proj, action="set", intent={"constraint": {"fab": "jlcpcb"}})
    assert any("unknown keys ignored: constraint" in w and "constraints" in w for w in sc_typo["warnings"])
    assert sc_typo["data"]["intent"]["constraints"] == {}
    path = Path(proj) / ".kiwrap" / "intent.json"
    assert path.is_file() and sc["changed"]["added"] == [".kiwrap/intent.json"]
    assert json.loads(path.read_text())["nets"]["GND"] == [["J1", "2"], ["C1", "2"]]
    assert sc["next_suggested"] == "kw_sch_check"
    # merge: nets replaced per key, blocks merged by name
    sc = h.ok(
        "kw_intent",
        project=proj,
        action="set",
        intent={"nets": {"VIN": ["J1.1", "U1.2"]}, "blocks": [{"name": "led"}]},
    )
    got = h.ok("kw_intent", project=proj, action="get")["data"]["intent"]
    assert got["nets"] == {"GND": [["J1", "2"], ["C1", "2"]], "VIN": [["J1", "1"], ["U1", "2"]]}
    assert [b["name"] for b in got["blocks"]] == ["ldo", "led"]
    # diff through the (fake) netlist export: U1.2 is on +3V3, U1.3 is on /VIN -> wrong_pin
    sc = h.ok("kw_intent", project=proj, action="diff")
    d = sc["data"]
    assert d["ok"] is False and d["count"] == 1 and d["wrong_pin"][0]["ref"] == "U1"
    assert sc["validators"][0]["name"] == "intent_diff" and sc["validators"][0]["after"] == 1
    assert sc["next_suggested"] == "kw_sch_connect" and sc["provider_used"].startswith("cli:")
    # replace, then a clean diff
    sc = h.ok(
        "kw_intent", project=proj, action="set", merge=False, intent={"nets": {"VIN": ["J1.1", "U1.3"]}}
    )
    assert sc["data"]["nets"] == 1 and sc["data"]["blocks"] == 0
    d = h.ok("kw_intent", project=proj, action="diff")["data"]
    assert d["ok"] and d["count"] == 0 and d["nets_matched"] == {"VIN": "/VIN"}
    sc = h.ok("kw_intent", project=proj, action="clear")
    assert sc["data"]["existed"] and not path.exists() and sc["changed"]["removed"] == [".kiwrap/intent.json"]
    assert h.ok("kw_intent", project=proj, action="clear")["data"]["existed"] is False


def test_sch_check_surfaces_the_intent_diff(h: Harness):
    proj = h.create()
    sc = h.ok("kw_sch_check", project=proj)
    assert "intent_diff" not in {v["name"] for v in sc["validators"]} and sc["data"]["intent_diff"] is None
    h.ok(
        "kw_intent",
        project=proj,
        action="set",
        intent={"nets": {"GND": ["J1.2", "C1.2", "D1.1"], "VIN": ["J1.1", "U1.3"]}},
    )
    sc = h.ok("kw_sch_check", project=proj)
    v = {x["name"]: x for x in sc["validators"]}
    assert v["erc"]["after"] == 0 and sc["stage"] == "erc_clean"  # the diff never gates erc_clean
    assert v["intent_diff"]["before"] is None and v["intent_diff"]["after"] == 1
    assert v["intent_diff"]["detail"]["missing"][0]["ref"] == "D1"
    extra = {(e["net"], e["ref"]) for e in v["intent_diff"]["detail"]["extra"]}
    assert extra == {("GND", "U1"), ("GND", "C2"), ("VIN", "C1")}  # listed, not counted (partial declaration)
    assert sc["data"]["intent_diff"]["count"] == 1 and sc["next_suggested"] == "kw_sch_connect"
    assert any("intent_diff" in w for w in sc["warnings"])
    # fixed intent: before -> after recorded from the project history, next step is the sync
    h.ok("kw_intent", project=proj, action="set", intent={"nets": {"GND": ["J1.2", "C1.2"]}})
    sc = h.ok("kw_sch_check", project=proj)
    v = {x["name"]: x for x in sc["validators"]}
    assert v["intent_diff"] == {
        "name": "intent_diff",
        "before": 1,
        "after": 0,
        "detail": v["intent_diff"]["detail"],
    }
    assert sc["next_suggested"] == "kw_pcb_sync"
    # ERC errors win the next_suggested; the diff is still reported
    h.backend.erc = erc_report(violation("pin_not_connected"))
    sc = h.ok("kw_sch_check", project=proj)
    assert sc["next_suggested"] == "kw_sch_connect" and sc["data"]["intent_diff"]["count"] == 0
    # without a netlist there is no diff (and no crash)
    h.backend.erc = erc_report()
    sc = h.ok("kw_sch_check", project=proj, netlist=False)
    assert sc["data"]["intent_diff"] is None
