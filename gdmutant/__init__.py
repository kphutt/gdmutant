"""gdmutant — a language-agnostic mutation-testing tool (GDScript first).

See docs/decisions/0001 for the Python/gdtoolkit rationale and README.md for the design goals.
The v0.1 engine (mutate -> run -> tally -> report) is built: the language-neutral `gdmutant.engine`
+ the `gdmutant.adapters.gdscript` adapter, run via the `gdmutant run` CLI.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gdmutant")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
