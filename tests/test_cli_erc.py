"""`sch erc` adapter: the x100 JSON rescale (docs/VERIFICATION.md claim 1) offline against the
spike outputs, then live on 8.0 / 9.0 / 10.0 with the spike schematic (dangling wire at
(100.33, 50.8) mm, 20.32 mm long) and the ecc83 demo; `Unsupported` on 7.0."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import erc
from kiwrap.capability import Unsupported
from tests._fakes import (
    ERC_UNITS_SPIKE,
    SPIKES,
    demo_project,
    env_for,
)

ERC_FRESH_SPIKE = SPIKES / "versioning-strategy" / "out" / "erc_fresh.json"

# --------------------------------------------------------------------------------------------
# Offline
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mm", ["8.0", "9.0", "10.0"])
def test_rescale_matches_text_report_from_spike(mm):
    doc = json.loads((ERC_UNITS_SPIKE / f"erc_{mm}.json").read_text(encoding="utf-8"))
    text = (ERC_UNITS_SPIKE / f"erc_{mm}.txt").read_text(encoding="utf-8", errors="replace")
    report = erc.report_from_erc_json(doc, source="t.kicad_sch", position_scale=100)
    assert report.kind == "erc" and report.kicad_version.startswith(mm)
    assert report.errors == 1
    # every JSON position (x100) equals a position printed in the text report
    text_positions = {(float(x), float(y)) for x, y in re.findall(r"@\(([\d.]+) mm, ([\d.]+) mm\)", text)}
    for v in report.violations:
        assert v.pos is not None
        assert (round(v.pos.x_mm, 4), round(v.pos.y_mm, 4)) in text_positions
        assert v.items[0].description.endswith("length 20.3200 mm")
    # raw keeps the rescaled document, so consumers of `raw` never see the bug
    assert report.raw["sheets"][0]["violations"][0]["items"][0]["pos"] == {"x": 100.33, "y": 50.8}


def test_backend_has_no_second_rescaler():
    """The x100 fix lives in kiwrap.formats only (one implementation, one set of rules)."""
    assert not hasattr(erc, "rescale_json") and not hasattr(erc, "rescale_description")
    assert "kiwrap.formats.reports._common" not in Path(erc.__file__).read_text(encoding="utf-8")


def test_report_from_erc_json_keeps_library_names_intact():
    """Real 9.0.5 / 10.0.0 kicad-cli output (spikes/versioning-strategy/out/erc_fresh.json): the
    footprint_link_issues descriptions embed 'Connector_PinSocket_2.54mm', which is a library name,
    not a dimension, and must survive the x100 rescale unchanged."""
    doc = json.loads(ERC_FRESH_SPIKE.read_text(encoding="utf-8"))
    report = erc.report_from_erc_json(doc, source="t", position_scale=100)
    descriptions = [v.description for v in report.violations] + [
        i.description for v in report.violations for i in v.items
    ]
    hits = [d for d in descriptions if "Connector_PinSocket_2.54mm" in d]
    assert hits, "spike sample no longer carries the library-name description"
    assert not any("254.00mm" in d for d in descriptions)
    raw_descriptions = [v["description"] for s in report.raw["sheets"] for v in s["violations"]]
    assert not any("254.00mm" in d for d in raw_descriptions)


def test_scale_one_is_identity():
    doc = json.loads((ERC_UNITS_SPIKE / "erc_10.0.json").read_text(encoding="utf-8"))
    report = erc.report_from_erc_json(doc, source="t", position_scale=1)
    assert report.violations[0].pos.x_mm == pytest.approx(1.0033)


def test_units_in_are_converted_to_mm():
    doc = {
        "coordinate_units": "in",
        "kicad_version": "x",
        "sheets": [
            {
                "path": "/",
                "violations": [
                    {
                        "type": "t",
                        "severity": "warning",
                        "description": "d",
                        "items": [
                            {"uuid": "u", "description": "length 0.008 in", "pos": {"x": 0.01, "y": 0.02}}
                        ],
                    }
                ],
            }
        ],
    }
    report = erc.report_from_erc_json(doc, source="t", position_scale=100)
    assert report.violations[0].pos.x_mm == pytest.approx(25.4)
    assert report.violations[0].pos.y_mm == pytest.approx(50.8)
    assert report.warnings == 1 and report.errors == 0 and report.ok


def test_severity_flags():
    assert erc.severity_flags("all") == ["--severity-all"]
    assert erc.severity_flags("error,warning") == ["--severity-error", "--severity-warning"]
    with pytest.raises(ValueError):
        erc.severity_flags("bogus")


# --------------------------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------------------------

_ERC_VERSIONS = [
    pytest.param("8.0", marks=pytest.mark.kicad_version("8.0")),
    pytest.param("9.0", marks=pytest.mark.kicad_version("9.0")),
    pytest.param("10.0", marks=pytest.mark.kicad_version("10.0")),
]


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ERC_VERSIONS)
def test_run_erc_positions_are_millimetres(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    sch = tmp_path / "t.kicad_sch"
    shutil.copy(ERC_UNITS_SPIKE / "t.kicad_sch", sch)
    report = erc.run_erc(env.install, env.config, sch, caps=env.caps)
    assert report.kind == "erc" and report.kicad_version == env.install.version
    assert report.errors == 1 and not report.ok
    dangling = [v for v in report.violations if v.type == "wire_dangling"]
    assert len(dangling) == 1
    assert dangling[0].pos.x_mm == pytest.approx(100.33) and dangling[0].pos.y_mm == pytest.approx(50.8)
    assert dangling[0].items[0].description.endswith("length 20.3200 mm")
    for v in report.violations:  # everything sits on the sheet, i.e. inside 0..300 mm
        assert 0 < v.pos.x_mm < 300 and 0 < v.pos.y_mm < 300
    assert report.raw["_cli"]["exit_code_violations"] is True
    assert env.config.count("restore_seed") >= 1 and env.config.count("lock") >= 1


@pytest.mark.kicad
@pytest.mark.usefixtures("user_config_untouched")
@pytest.mark.parametrize("mm", _ERC_VERSIONS)
def test_run_erc_on_demo_project(cli_envs, mm, tmp_path: Path):
    env = env_for(cli_envs, mm)
    proj = demo_project(env.install, tmp_path)
    report = erc.run_erc(
        env.install, env.config, proj / "ecc83-pp.kicad_sch", caps=env.caps, severity="error,warning"
    )
    assert report.source.endswith("ecc83-pp.kicad_sch")
    assert isinstance(report.errors, int)
    assert not list(proj.glob("*.kicad_prl")), ".kicad_prl left behind"


@pytest.mark.kicad
@pytest.mark.kicad_version("7.0")
@pytest.mark.usefixtures("user_config_untouched")
def test_erc_unsupported_on_7(cli_envs, tmp_path: Path):
    env = env_for(cli_envs, "7.0")
    locks_before = env.config.count("lock")
    with pytest.raises(Unsupported) as ei:
        erc.run_erc(env.install, env.config, ERC_UNITS_SPIKE / "t.kicad_sch", caps=env.caps)
    assert ei.value.capability == "cli.has_erc"
    assert ei.value.oldest_kicad_with_it == "8.0.0"
    assert env.config.count("lock") == locks_before, "must not start kicad-cli for an unsupported capability"


def test_report_from_erc_json_rescales_document_and_positions():
    """`report_from_erc_json` undoes the x100 quirk on the document (so `Report.raw` is right) and
    parses it with `kiwrap.formats.reports.erc.parse_erc_json`; both views must agree."""
    from kiwrap.formats.reports.erc import parse_erc_json

    doc = json.loads((ERC_UNITS_SPIKE / "erc_10.0.json").read_text(encoding="utf-8"))
    report = erc.report_from_erc_json(doc, source="t", position_scale=100)
    direct = parse_erc_json(json.dumps(doc), position_scale=100, source="t")
    assert [(v.type, v.severity, v.sheet, [i.description for i in v.items]) for v in report.violations] == [
        (v.type, v.severity, v.sheet, [i.description for i in v.items]) for v in direct.violations
    ]
    for a, b in zip(report.violations, direct.violations, strict=True):
        for ia, ib in zip(a.items, b.items, strict=True):
            assert (ia.pos is None) == (ib.pos is None)
            if ia.pos and ib.pos:
                assert ia.pos.x_mm == pytest.approx(ib.pos.x_mm, abs=1e-6)
                assert ia.pos.y_mm == pytest.approx(ib.pos.y_mm, abs=1e-6)
    assert (report.kind, report.source, report.kicad_version) == ("erc", "t", direct.kicad_version)
    raw_positions = [
        it["pos"]
        for sheet in report.raw["sheets"]
        for v in sheet["violations"]
        for it in v["items"]
        if it.get("pos")
    ]
    parsed_positions = [(i.pos.x_mm, i.pos.y_mm) for v in report.violations for i in v.items if i.pos]
    assert raw_positions and [(p["x"], p["y"]) for p in raw_positions] == parsed_positions
