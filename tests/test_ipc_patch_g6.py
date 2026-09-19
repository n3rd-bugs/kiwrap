"""G6 (docs/UPSTREAM.md): job exports over the headless api-server, on every install that has them.

The gap card said the `RunBoardJobExport*` / `RunSchematicJobExport*` handlers were absent from
the headless server (T5 rows `-> AS_UNHANDLED`). They are not: the first T5 minimal instance sent
an empty `job_settings.document`, which `validateDocument` passes on with AS_UNHANDLED. So there
is NO kiwrap patch and this file deliberately carries no `kicad_patch("g6")` marker (that marker
would skip it everywhere, since the manifest lists no such patch): it runs on the stock 10.99
nightly and on every registered bench build, parametrised over the installs whose capability
record says `ipc.headless_jobs`, and it is the live proof behind that key together with the T5
rows (`conformance/snapshots/ipc-handlers/10.99.0.json`).

Every test copies the ecc83 demo into pytest's tmp dir, runs one sandboxed `kicad-cli api-server`
through `HeadlessSession`, and the hygiene fixture asserts the user's real KiCad config hash and
that no session survives.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiwrap.backends.cli import netlist as cli_netlist
from kiwrap.backends.cli import structured
from kiwrap.backends.ipc import jobs
from kiwrap.backends.ipc.sessions import HeadlessSession, IpcError, close_all, live_sessions
from kiwrap.capability import capabilities_for
from kiwrap.formats.reports.netlist import parse_netlist
from kiwrap.sandbox import user_config_hash

pytestmark = [pytest.mark.kicad, pytest.mark.kicad_version("10.99")]

KEY = "ipc.headless_jobs"


def _headless_job_installs(installs, storage_home):
    return [i for i in installs if capabilities_for(i, storage_home=storage_home).has(KEY)]


@pytest.fixture(scope="module", params=["pcb", "schematic"])
def kind(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def job_installs(installs, storage_home):
    found = _headless_job_installs(installs, storage_home)
    if not found:
        pytest.skip(f"no installed KiCad with {KEY}")
    return found


@pytest.fixture(scope="module")
def roots(job_installs, storage_home):
    from kiwrap import sandbox

    return {i.key(): sandbox.ensure(i, storage_home=storage_home) for i in job_installs}


@pytest.fixture
def project(job_installs, tmp_path) -> Path:
    src = job_installs[0].dir("demos") / "ecc83"
    if not src.is_dir():
        pytest.skip("ecc83 demo not shipped")
    dst = tmp_path / "ecc83"
    shutil.copytree(src, dst)
    return dst


@pytest.fixture(autouse=True)
def _hygiene(job_installs):
    before = {i.key(): user_config_hash(i) for i in job_installs}
    yield
    close_all()
    assert live_sessions() == []
    for i in job_installs:
        assert user_config_hash(i) == before[i.key()], (
            "a headless session modified the user's real KiCad config"
        )


def test_capability_matches_the_presence_probe(job_installs, roots, project, kind):
    """`ipc.headless_jobs` is a presence claim (docs/UPSTREAM.md 3.3): the direct probe must agree
    with the record on every install that carries the key, for both document types."""
    file = project / ("ecc83-pp.kicad_pcb" if kind == "pcb" else "ecc83-pp.kicad_sch")
    for install in job_installs:
        with HeadlessSession(install, file, config_root=roots[install.key()]) as s:
            assert jobs.probe_headless_jobs(s) is True, (install.root, kind, s.transcript()[-500:])
            assert s.capabilities.has(KEY)
    left = [p for i in job_installs for p in (roots[i.key()].temp_dir() / "probe-jobs").glob("probe-*")]
    assert left == [], "the presence probe must remove its own output"


def test_t5_style_empty_document_is_unhandled_not_missing(job_installs, roots, project):
    """The G6 artifact itself: the same command is AS_UNHANDLED with an empty nested document and
    handled with it filled. Keeps the T5 rule (`handlers._minimal_instance`) honest."""
    from kipy.errors import ApiError
    from kipy.proto.board import board_jobs_pb2
    from kipy.proto.common import ApiStatusCode
    from kipy.proto.common.types import jobs_pb2

    install = job_installs[0]
    with HeadlessSession(install, project / "ecc83-pp.kicad_pcb", config_root=roots[install.key()]) as s:
        empty = board_jobs_pb2.RunBoardJobExportStats()
        with pytest.raises(ApiError) as info:
            s.client().send(empty, jobs_pb2.RunJobResponse)
        assert info.value.code == ApiStatusCode.AS_UNHANDLED
        assert "no handler available" in (info.value.raw_message or str(info.value))

        filled = board_jobs_pb2.RunBoardJobExportStats()
        filled.format = board_jobs_pb2.SOF_REPORT
        filled.units = jobs._units_value("mm")
        outcome = jobs.run_job(s, filled, output_path=project / "stats.rpt")
        assert outcome.ok and outcome.status == "JS_SUCCESS", outcome
        assert (project / "stats.rpt").is_file()
        assert outcome.output_paths == (project / "stats.rpt",)


def test_export_board_stats_matches_kicad_cli(job_installs, roots, project, tmp_path):
    """The live export is the same job kicad-cli runs: identical counts and board area."""
    for install in job_installs:
        root = roots[install.key()]
        board = project / "ecc83-pp.kicad_pcb"
        with HeadlessSession(install, board, config_root=root) as s:
            live = jobs.export_board_stats(s, tmp_path / f"{install.root.name}-stats.json")
        cli = structured.export_stats(install, root, board, out_dir=tmp_path / f"{install.root.name}-cli")
        assert live.component_count == cli.component_count == 15, (install.root, live.components)
        assert live.pads == cli.pads and live.vias == cli.vias
        assert live.area_mm2 == cli.area_mm2 and live.width_mm == cli.width_mm
        assert live.kicad_version == install.version


def test_export_board_svg_one_page_and_per_layer(job_installs, roots, project, tmp_path):
    install = job_installs[0]
    root = roots[install.key()]
    with HeadlessSession(install, project / "ecc83-pp.kicad_pcb", config_root=root) as s:
        one = jobs.export_board_svg(s, tmp_path / "board.svg", black_and_white=True)
        assert one.ok and one.output_paths == (tmp_path / "board.svg",), one
        text = (tmp_path / "board.svg").read_text(encoding="utf-8")
        assert "<svg" in text and "</svg>" in text

        per_layer = tmp_path / "layers"
        per_layer.mkdir()
        many = jobs.export_board_svg(s, per_layer, layers=("F.Cu", "B.Cu"), each_layer_own_file=True)
        assert many.ok and len(many.output_paths) == 2, many
        assert sorted(p.name for p in per_layer.iterdir()) == sorted(p.name for p in many.output_paths)

        # The job refuses an empty layer list (verified: JS_ERROR "At least one layer must be
        # specified"); the helper refuses earlier, and the raw path surfaces KiCad's own message.
        with pytest.raises(IpcError):
            jobs.export_board_svg(s, tmp_path / "none.svg", layers=())
        from kipy.proto.board import board_jobs_pb2

        raw = board_jobs_pb2.RunBoardJobExportSvg()
        raw.page_mode = board_jobs_pb2.BJPM_ALL_LAYERS_ONE_PAGE
        raw.plot_settings.drill_marks = board_jobs_pb2.PDM_NONE
        with pytest.raises(jobs.JobFailed) as info:
            jobs.run_job(s, raw, output_path=tmp_path / "none.svg")
        assert "layer" in info.value.outcome.message.lower()
        assert info.value.code == "E_IPC_JOB"
        assert not (tmp_path / "none.svg").exists()


def test_export_schematic_netlist_matches_kicad_cli(job_installs, roots, project, tmp_path):
    """Same nets and nodes as `kicad-cli sch export netlist` on the same file (the file-tier parser
    reads both), so the live path can replace the CLI one without a change in what the agent sees."""
    for install in job_installs:
        root = roots[install.key()]
        sch = project / "ecc83-pp.kicad_sch"
        with HeadlessSession(install, sch, config_root=root) as s:
            out = tmp_path / f"{install.root.name}.net"
            outcome = jobs.export_schematic_netlist(s, out, format="kicadsexpr")
            assert outcome.ok and outcome.output_paths == (out,), outcome
            live = parse_netlist(
                out.read_text(encoding="utf-8"), source=str(sch), kicad_version=install.version
            )
        cli = cli_netlist.export_netlist(install, root, sch, tmp_path / f"{install.root.name}-cli.net")
        assert {n.name: sorted((x.ref, x.pin) for x in n.nodes) for n in live.nets} == {
            n.name: sorted((x.ref, x.pin) for x in n.nodes) for n in cli.nets
        }
        assert len(live.nets) == 13 and len(live.components) == 15

        with HeadlessSession(install, sch, config_root=root) as s, pytest.raises(IpcError):
            jobs.export_schematic_netlist(s, tmp_path / "x.net", format="verilog")


def test_layer_names_round_trip():
    """No KiCad needed: `jobs.layer_value` inverts `readers.layer_name` for every BoardLayer."""
    from kipy.proto.board.board_types_pb2 import BoardLayer

    from kiwrap.backends.ipc.readers import layer_name

    for value in BoardLayer.values():
        if value == 0:
            continue
        assert jobs.layer_value(layer_name(value)) == value, BoardLayer.Name(value)
    with pytest.raises(IpcError):
        jobs.layer_value("Front.Copper")


def test_t5_minimal_instance_fills_the_nested_job_document():
    """No KiCad needed: the probe rule that produced the G6 artifact, fixed. Every job command's
    `job_settings.document` names the open document; top-level and `ItemHeader` documents as before;
    nothing else is set (the message stays minimal)."""
    from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier, DocumentType

    from kiwrap.backends.ipc import handlers

    doc = DocumentSpecifier()
    doc.type = DocumentType.DOCTYPE_SCHEMATIC
    doc.board_filename = "x.kicad_sch"
    commands = handlers.command_messages()
    job_commands = [n for n in commands if n.startswith(("RunBoardJob", "RunSchematicJob"))]
    assert len(job_commands) == 22, job_commands
    for name in job_commands:
        msg = handlers._minimal_instance(commands[name], doc)
        assert msg.job_settings.document.board_filename == "x.kicad_sch", name
        assert msg.job_settings.output_path == "", name
        assert [f.name for f, _ in msg.ListFields()] == ["job_settings"], name
    assert (
        handlers._minimal_instance(commands["GetItems"], doc).header.document.type
        == DocumentType.DOCTYPE_SCHEMATIC
    )
    assert handlers._minimal_instance(commands["GetNets"], doc).board.board_filename == "x.kicad_sch"
    assert handlers._minimal_instance(commands["Ping"], doc).ByteSize() == 0
