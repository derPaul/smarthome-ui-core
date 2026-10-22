"""Mailbox: post is there (flap opened) until it is emptied (door opened or "emptied" pressed).

Two contact sensors on the mailbox, found by their names like the dashboard does it ("Briefkasten",
"Einwurf", "Entnahme" …) or chosen in the dashboard (``mailbox/settings``):

* the **flap** (front, where the post comes in) – an opening is a delivery; openings within a
  minute count as one (the flap swings),
* the **door** (back, where it is emptied) – an opening is emptying; a short twitch (under 2 s) or
  a jolt up to 90 s after a delivery (the box shakes) does not count.

Without a door "emptied" is pressed (button, or the dashboard's done record ``mailbox:empty``).

Notifications (source "Briefkasten", ``hh.mail``): "Post ist da" with the first delivery after
emptying, "Briefkasten: Klappe/Tür steht offen" after ``OPEN_MINUTES``.

Stored in ``mailbox``: ``{"flap", "door", "enabled", "ins": [ms], "doors": [ms]}`` (7 days).
"""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_call_later

from .const import SIGNAL_DATA, SIGNAL_MAILBOX
from .store import CoreStore, now_ms

KEEP_MS = 7 * 86_400_000
BOUNCE_MS = 60_000
DOOR_MIN_MS = 2_000
SHAKE_MS = 90_000
SHAKE_MAX_MS = 15_000
AFTER_EMPTY_MS = 120_000
OPEN_MINUTES = 3
DONE_EMPTY = "mailbox:empty"
DONE_RESET = "mailbox:reset"

_BOX = re.compile(r"briefkasten|mailbox|letter_?box|postkasten|post_?box")
_DOOR = re.compile(
    r"entnahme|t(ü|ue)r\b|tuer|door|entleer|leeren|empt|r(ü|ue)ckseite|hinten|hinter|back|rear"
)
_FRONT = re.compile(r"einwurf|vorne|vorder|front|slot|eingang|post_?da")
_FLAP = re.compile(r"klappe|flap")


def _text(state: State) -> str:
    return f"{state.entity_id} {state.attributes.get('friendly_name', '')}".lower()


class Mailbox:
    """The mailbox: deliveries, emptying, open flap or door."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        notify: Callable[[str, str, str, str], None],
    ) -> None:
        """Initialize (notify(src, title, message, tag))."""
        self.hass = hass
        self.store = store
        self.notify = notify
        self._unsub: list[CALLBACK_TYPE] = []
        self._door_open_at: int | None = None
        self._open_timers: dict[str, CALLBACK_TYPE] = {}

    # ---------------------------------------------------------------- settings

    @property
    def data(self) -> dict[str, Any]:
        """Return settings and the deliveries / emptyings."""
        return self.store.data["mailbox"]

    def _sensors(self) -> list[State]:
        return [s for s in self.hass.states.async_all("binary_sensor") if _BOX.search(_text(s))]

    @property
    def flap(self) -> str | None:
        """Return the flap sensor (chosen, else found by its name)."""
        if self.data.get("enabled") is False:
            return None
        own = self.data.get("flap")
        if own and self.hass.states.get(own) is not None:
            return own
        box = self._sensors()
        for test in (
            lambda s: _FRONT.search(_text(s)) and not _DOOR.search(_text(s)),
            lambda s: _FLAP.search(_text(s)) and not _DOOR.search(_text(s)),
            lambda s: not _DOOR.search(_text(s)),
        ):
            if hit := next((s.entity_id for s in box if test(s)), None):
                return hit
        return None

    @property
    def door(self) -> str | None:
        """Return the door sensor (chosen, else found by its name)."""
        if (flap := self.flap) is None:
            return None
        own = self.data.get("door")
        if own and self.hass.states.get(own) is not None:
            return own
        return next(
            (
                s.entity_id
                for s in self._sensors()
                if s.entity_id != flap and _DOOR.search(_text(s))
            ),
            None,
        )

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take the sensors from the dashboard (None = find them)."""
        for key in ("flap", "door", "enabled"):
            if key in settings:
                self.data[key] = settings[key]
        self._listen()
        self._save()

    # ---------------------------------------------------------------- state

    def _done_at(self, key: str) -> int:
        return int((self.store.done.get(key) or {}).get("t") or 0)

    def deliveries(self) -> list[int]:
        """Return the deliveries of the last days (ms, oldest first)."""
        return list(self.data.get("ins") or [])

    def emptyings(self) -> list[int]:
        """Return when the door was opened to empty it (ms, oldest first)."""
        return list(self.data.get("doors") or [])

    def last_empty(self) -> tuple[int, str | None]:
        """Return when it was emptied last and how ("door" / "hand")."""
        doors = self.emptyings()
        last_door = doors[-1] if doors else 0
        manual = max(self._done_at(DONE_EMPTY), self._done_at(DONE_RESET))
        if not last_door and not manual:
            return 0, None
        return (last_door, "door") if last_door >= manual else (manual, "hand")

    def waiting(self) -> list[int]:
        """Return the deliveries since it was emptied."""
        at, how = self.last_empty()
        after = at + (AFTER_EMPTY_MS if how == "door" else 0) if at else 0
        return [t for t in self.deliveries() if t > after]

    def open_now(self) -> list[dict[str, Any]]:
        """Return flap / door open for some minutes."""
        out = []
        now = now_ms()
        for entity_id in (self.flap, self.door):
            state = self.hass.states.get(entity_id or "")
            if state is None or state.state != "on":
                continue
            since = int(state.last_changed.timestamp() * 1000)
            if now - since >= OPEN_MINUTES * 60_000:
                out.append({"id": entity_id, "since": since})
        return out

    @callback
    def emptied(self, who: str | None = None) -> None:
        """Emptied by hand."""
        self.store.mark_done(DONE_EMPTY, who=who, label="Briefkasten geleert")
        self._save()

    @callback
    def _save(self) -> None:
        self.store.save(SIGNAL_MAILBOX, SIGNAL_DATA)

    # ---------------------------------------------------------------- listening

    @callback
    def async_setup(self) -> None:
        """Start listening."""
        self._listen()

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in [*self._unsub, *self._open_timers.values()]:
            unsub()
        self._unsub.clear()
        self._open_timers.clear()

    @callback
    def _listen(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        flap, door = self.flap, self.door
        if flap is None:
            return
        watched = {flap, door} - {None}

        @callback
        def relevant(data: EventStateChangedData) -> bool:
            return data["entity_id"] in watched

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            eid = event.data["entity_id"]
            old, new = event.data["old_state"], event.data["new_state"]
            if new is None or old is None or old.state == new.state:
                return
            self._watch_open(eid, new)
            if eid == flap and new.state == "on":
                self._delivered()
            elif eid == door and new.state == "on":
                self._door_open_at = now_ms()
            elif eid == door and old.state == "on" and self._door_open_at:
                self._door_closed(now_ms() - self._door_open_at)
                self._door_open_at = None

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=relevant)
        )

    def _prune(self, items: list[int]) -> list[int]:
        start = now_ms() - KEEP_MS
        return [t for t in items if t >= start]

    @callback
    def _delivered(self) -> None:
        now = now_ms()
        ins = self.deliveries()
        if ins and now - ins[-1] <= BOUNCE_MS:
            return
        first = not self.waiting()
        self.data["ins"] = self._prune([*ins, now])
        self._save()
        if first and self.waiting():
            self.notify("hh.mail", "Briefkasten", "Post ist da", f"mail|{now}")

    @callback
    def _door_closed(self, duration: int) -> None:
        start = now_ms() - duration
        if duration < DOOR_MIN_MS:
            return
        if duration < SHAKE_MAX_MS and any(
            start >= t - 5_000 and start - t < SHAKE_MS for t in self.deliveries()
        ):
            return  # the box shook while the post came in
        doors = self.emptyings()
        if doors and start - doors[-1] <= BOUNCE_MS:
            return
        self.data["doors"] = self._prune([*doors, start])
        self._save()

    @callback
    def _watch_open(self, entity_id: str, state: State) -> None:
        if unsub := self._open_timers.pop(entity_id, None):
            unsub()
        if state.state != "on":
            return
        since = state.last_changed

        @callback
        def still_open(_now: Any) -> None:
            self._open_timers.pop(entity_id, None)
            cur = self.hass.states.get(entity_id)
            if cur is not None and cur.state == "on" and cur.last_changed == since:
                what = "Tür" if entity_id == self.door else "Klappe"
                self.notify(
                    "hh.mail",
                    "Briefkasten",
                    f"Briefkasten: {what} steht offen",
                    f"mailopen|{entity_id}",
                )
                self._save()  # "open" for the dashboard

        self._open_timers[entity_id] = async_call_later(self.hass, OPEN_MINUTES * 60, still_open)

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        at, how = self.last_empty()
        return {
            "flap": self.flap,
            "door": self.door,
            "waiting": self.waiting(),
            "ins": self.deliveries(),
            "doors": self.emptyings(),
            "last_empty": at or None,
            "empty_by": how,
            "open": self.open_now(),
        }
