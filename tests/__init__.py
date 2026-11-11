"""Test package. Loads the plugin the way Hermes does: the repository root as one package.

Run with ``python -m unittest discover -s tests -t .`` from the repository root.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "onair"

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)


def _offline_resolve(host: str) -> set[str]:
    """Tests never touch DNS: names are public unless they say otherwise."""
    if host.startswith("private."):
        return {"10.0.0.5"}
    if host.startswith("mixed."):
        return {"93.184.216.34", "127.0.0.1"}
    if host.startswith("missing."):
        raise OSError("no such host")
    return {"93.184.216.34"}


importlib.import_module(PACKAGE + ".core.weburl")._resolve = _offline_resolve
