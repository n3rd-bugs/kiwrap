"""kiwrap — AI-agent-facing wrapper over installed KiCad binaries.

Layering (enforced by import-linter, see pyproject.toml):
    core -> backends -> sandbox -> capability -> installation -> platform -> formats
`kiwrap.formats` has no dependency on anything else in kiwrap and no third-party runtime deps.
Nothing outside `kiwrap.backends` and `kiwrap/data/` may mention a KiCad version number.
"""

__version__ = "0.0.1"
