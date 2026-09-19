"""Live presence probes for the IPC half of kiwrap's KiCad patches (docs/UPSTREAM.md §3.3).

`kiwrap.capability` cannot import this package (layer contract), so a patched build's `ipc.*` key
is first DERIVED there from the CLI half of the same patch (the api-server is the probed kicad-cli
binary). The first `HeadlessSession` that has a board open runs every probe listed here whose key
the record has not probed yet (`kiwrap.ipc_probed`), and the probe records the live answer through
`Capabilities.refine` (which rewrites the cache, so it runs once per install, not per session).

Each probe takes the session, sends one cheap request and returns True when a handler answered,
False on AS_UNHANDLED / AS_UNIMPLEMENTED; anything else propagates (a broken session must not read
as "patch absent"). A stock nightly costs one AS_UNHANDLED round trip per key, once.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from kiwrap.backends.ipc.queries import probe_get_ratsnest

__all__ = ["PROBES", "run_pending"]

#: capability key -> probe(session) -> bool (the probe itself calls `Capabilities.refine`)
PROBES: dict[str, Callable[[Any], bool]] = {
    "ipc.has_get_ratsnest": probe_get_ratsnest,
}


def run_pending(session: Any) -> list[str]:
    """Run the probes whose key the session's capability record has not probed yet; returns the
    keys probed. Probe failures other than "no handler" are swallowed here (the session's normal
    requests report them properly) but leave the key unprobed for the next session."""
    caps = session.capabilities
    done = set(caps.get("kiwrap.ipc_probed", []) or [])
    probed: list[str] = []
    for key, probe in PROBES.items():
        if key in done:
            continue
        with contextlib.suppress(Exception):
            probe(session)
            probed.append(key)
    return probed
