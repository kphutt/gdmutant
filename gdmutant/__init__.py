"""gdmutant — a language-agnostic mutation-testing tool (GDScript first).

See docs/decisions/0001 for the Python/gdtoolkit rationale and README.md for the
design goals. The mutate -> run -> tally -> report engine lands in the v0.1 engine
milestone, behind the approved DESIGN.md; this package is a skeleton until then.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gdmutant")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
