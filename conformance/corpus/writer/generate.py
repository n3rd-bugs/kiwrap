"""Regenerate the committed writer fixtures (`conformance/corpus/writer/<design>_<era>/`).

    uv run python conformance/corpus/writer/generate.py [--only blink,hier]

For every stock install found, the blink and hierarchy designs of `tests/_sch_designs.py` are
written at that install's epoch with the install's own symbol libraries (embedded, so the
fixtures are self-contained), then validated by that install's kicad-cli: the ERC JSON and the
netlist are stored next to the schematic as evidence. Folder names carry the dialect era
(`blink_10`), so a newer install regenerating the same era overwrites it with identical
semantics. Only the newest install per era writes (10.0.6 for `_10`, 10.99.0 for `_11`, …);
the hierarchy fixture is kept for the primary eras 10 and 11 only (size).

Every kicad-cli run goes through the sandbox (`kiwrap.sandbox.ensure`); the user's KiCad config
is never touched.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from tests._sch_designs import build_blink, build_hierarchy  # noqa: E402
from tests._sch_verify import epoch_for  # noqa: E402

from kiwrap import sandbox  # noqa: E402
from kiwrap.backends.cli.erc import run_erc  # noqa: E402
from kiwrap.backends.cli.netlist import export_netlist_file  # noqa: E402
from kiwrap.capability import capabilities_for  # noqa: E402
from kiwrap.formats.epoch_dialects import dialect_for  # noqa: E402
from kiwrap.formats.schematic_writer import FallbackLibResolver, SchematicWriter  # noqa: E402
from kiwrap.installation import discover  # noqa: E402

DESIGNS = {"blink": ("blink.kicad_sch", build_blink), "hier": ("top.kicad_sch", build_hierarchy)}
#: the hierarchy fixture (embedded LM358 body, ~80 KB) is kept for the two primary eras only
HIER_ERAS = {"10", "11"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="blink,hier")
    args = ap.parse_args(argv)
    wanted = set(args.only.split(","))
    by_era: dict[str, object] = {}
    for install in discover():
        if install.is_bench_build:
            continue
        caps = capabilities_for(install)
        if not caps.has("cli.has_erc"):
            continue
        era = dialect_for(epoch_for(caps)).name
        by_era.setdefault(era, install)  # discover() is newest first
    for era, install in sorted(by_era.items()):
        caps = capabilities_for(install)
        config = sandbox.ensure(install)
        for name, (root_name, build) in DESIGNS.items():
            if name not in wanted or (name == "hier" and era not in HIER_ERAS):
                continue
            out = HERE / f"{name}_{era}"
            if out.exists():
                shutil.rmtree(out)
            out.mkdir(parents=True)
            w = SchematicWriter(
                out, epoch=epoch_for(caps), lib_resolver=FallbackLibResolver(install, None, out)
            )
            design = build(w, out / root_name)
            report = run_erc(
                install, config, design.root, caps=caps, out_json=out / f"erc_{install.version}.json"
            )
            export_netlist_file(
                install, config, design.root, out / f"netlist_{install.version}.net", caps=caps
            )
            print(
                f"{out.name}: KiCad {install.version} ERC errors={report.errors} warnings={report.warnings}"
            )
            if report.errors or report.warnings:
                for v in report.violations:
                    print("   ", v.severity, v.type, v.description)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
