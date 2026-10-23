"""Lists of the dashboard's pages, kept in Core: which cameras, windows, locks … a page shows.

The dashboard edits them (its list dialogs write here) and reads them back; Core keeps them so they
survive a new dashboard and are the same for every display. Shape: ``{page: {path: [items]}}``,
an item is ``{"entity": ..., ...}`` (extra fields like a name or a motion sensor stay as they are);
``None`` means "automatic" (the page finds them itself).

Besides the lists a few single entities that belong to no device are kept here (``values``): the
weather entity, the music player, the cinema's volume and scenes. They hold for every page.

The dashboard's own lists are taken over once per page (``lists/migrate``), its single values once
(page ``values``).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import SIGNAL_DATA
from .store import CoreStore

PAGES = {
    "security": (
        "cameras.entities",
        "openings.entities",
        "motion.entities",
        "locks",
        "doorbell.chimes",
        "night.persons",
        "night.animals",
        "night.vehicles",
        "night.motion",
        "exclude",
    ),
    "status": ("services", "exclude"),
    "home": (
        "scenes",
        "hcal.entities",
        "hrem.entities",
        "reminders.entities",
        "calendar.entities",
    ),
    "rooms": ("exclude",),
    "garden": ("devices.entities", "mower.tasks", "mower.zones"),
}
VALUES = (
    "weather.entity",
    "media_player",
    "cinema.volume",
    "cinema.scene",
    "cinema.scene_off",
)


class PageLists:
    """The lists of the pages."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store

    @property
    def data(self) -> dict[str, Any]:
        """Return {page: {path: items}} and the pages taken over."""
        return self.store.data["lists"]

    @callback
    def set(self, page: str, path: str, items: list[Any] | None) -> None:
        """Set one list (None = automatic again)."""
        if path not in PAGES.get(page, ()):
            raise ValueError(f"{page}/{path}")
        lists = self.data.setdefault("pages", {}).setdefault(page, {})
        if items is None:
            lists.pop(path, None)
        else:
            lists[path] = items
        self.store.save(SIGNAL_DATA)

    @callback
    def set_value(self, path: str, value: str | None) -> None:
        """Set one single entity (None = automatic again)."""
        if path not in VALUES:
            raise ValueError(path)
        values = self.data.setdefault("values", {})
        if value:
            values[path] = value
        else:
            values.pop(path, None)
        self.store.save(SIGNAL_DATA)

    @callback
    def migrate(self, page: str, lists: dict[str, Any]) -> int:
        """Take over a page's lists (or the single values) once."""
        done = self.data.setdefault("migrated", [])
        if page == "values" and page not in done:
            own = self.data.setdefault("values", {})
            made = 0
            for path, value in lists.items():
                if path in VALUES and isinstance(value, str) and value and path not in own:
                    own[path] = value
                    made += 1
            done.append(page)
            self.store.save(SIGNAL_DATA)
            return made
        if page in done or page not in PAGES:
            return 0
        own = self.data.setdefault("pages", {}).setdefault(page, {})
        made = 0
        for path, items in lists.items():
            if path in PAGES[page] and isinstance(items, list) and path not in own:
                own[path] = items
                made += 1
        done.append(page)
        self.store.save(SIGNAL_DATA)
        return made

    def info(self) -> dict[str, Any]:
        """Return the lists for the dashboard."""
        return {
            "pages": self.data.get("pages") or {},
            "values": self.data.get("values") or {},
            "value_paths": list(VALUES),
            "migrated": list(self.data.get("migrated") or []),
            "paths": {k: list(v) for k, v in PAGES.items()},
        }
