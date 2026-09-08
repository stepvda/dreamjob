"""Source-adapter registry loading (NFR-601, FR-161, FR-363).

Adapters register themselves with ``@register_adapter`` at import time, so the
registry is only as complete as the set of modules that have been imported.
:func:`load_all` walks this package, imports every adapter module it finds and
then writes the catalogue rows the planner and the administration screens read.

Walking the package rather than keeping a list is what makes NFR-601 true in
practice: dropping ``adapters/jobboards/mynewboard.py`` into the tree is the
whole change - no registration list, no pipeline edit.  A module that fails to
import is logged and skipped, because one broken adapter must not stop the
application from starting.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from dreamjob.adapters.base import (
    SourceAdapter,
    all_adapters,
    get_adapter,
    register_adapter,
    sync_catalogue,
)

log = logging.getLogger(__name__)

#: Modules in this package that are shared machinery, not adapters.
_SUPPORT_MODULES = {
    "dreamjob.adapters.base",
    "dreamjob.adapters.vacancy_source",
    "dreamjob.adapters.ats.common",
    "dreamjob.adapters.ats.detect",
}


def discover() -> list[str]:
    """Import every adapter module under ``dreamjob.adapters``; return what loaded."""
    loaded: list[str] = []
    for module in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
        name = module.name
        if module.ispkg or name in _SUPPORT_MODULES or name.rsplit(".", 1)[-1].startswith("_"):
            continue
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - one broken adapter must not break startup
            log.exception("Could not import adapter module %s", name)
            continue
        loaded.append(name)
    return loaded


def load_all() -> int:
    """Import all adapters and synchronise the source catalogue (FR-161).

    Returns the number of registered adapters, which is what ``main.py`` logs
    at start-up.
    """
    discover()
    count = sync_catalogue()
    log.debug("Registered adapters: %s", ", ".join(sorted(all_adapters())))
    return count


__all__ = [
    "SourceAdapter",
    "all_adapters",
    "discover",
    "get_adapter",
    "load_all",
    "register_adapter",
    "sync_catalogue",
]
