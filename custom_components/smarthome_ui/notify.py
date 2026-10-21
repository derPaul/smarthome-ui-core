"""Notifications that run in Home Assistant, independent of an open dashboard.

Two kinds of input:

* **Rules** – the dashboard translates its detection (doorbell, windows, appliances, waste
  collection, calendars …) into simple rules that are evaluated here:
  ``state`` (state is one of ``to``), ``change`` (any state change, e.g. an event entity),
  ``numeric`` (``above``/``below``), ``template`` (a Jinja template is true), all optionally
  with ``for`` seconds; ``at`` (a point in time, skipped if ``skip_if_done`` is done) and
  ``calendar`` (events of calendar entities, ``lead`` minutes before / all-day ones at a time).
* **Reports** – everything else the dashboard detects; every open dashboard reports its
  current items and each one is sent exactly once, no matter how many dashboards report it.

Fallen-off conditions re-arm a rule; reported items are forgotten 15 minutes after no open
dashboard reports them anymore. Due tasks are announced on their due day at ``task_hour``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    TrackTemplate,
    async_track_state_change_event,
    async_track_template_result,
    async_track_time_interval,
)
from homeassistant.helpers.template import Template, result_as_boolean
from homeassistant.util import dt as dt_util

from .const import (
    CALENDAR_INTERVAL,
    CHANGE_COOLDOWN,
    DEFAULT_TASK_HOUR,
    FORGET_AFTER,
    RECENT_MAX,
    REPORT_TTL,
    SIGNAL_NOTIFY,
    SIGNAL_SENT,
    SIGNAL_TASKS,
    TICK_INTERVAL,
)
from .store import CoreStore, now_ms
from .tasks import task_states

_LOGGER = logging.getLogger(__name__)

INVALID = ("unknown", "unavailable", "")


def _parse_hhmm(value: Any, default: dtime) -> dtime:
    try:
        hour, minute = str(value).split(":")[:2]
        return dtime(int(hour), int(minute))
    except (ValueError, TypeError):
        return default


def in_quiet(quiet: dict[str, Any] | None, now: datetime) -> bool:
    """Return True if ``now`` (local) is within the quiet hours."""
    if not quiet or not quiet.get("from") or not quiet.get("to"):
        return False
    start = _parse_hhmm(quiet["from"], dtime(22, 0))
    end = _parse_hhmm(quiet["to"], dtime(7, 0))
    t = now.time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def urgent_data(urgent: bool) -> dict[str, Any]:
    """Return the notify data that makes a push time-sensitive (iOS) / high priority (Android)."""
    if not urgent:
        return {}
    return {
        "push": {"interruption-level": "time-sensitive"},
        "priority": "high",
        "ttl": 0,
        "channel": "Dringend",
    }


class NotifyEngine:
    """Evaluate rules, collect reports and send notifications."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the engine."""
        self.hass = hass
        self.store = store
        self._unsub: list[CALLBACK_TYPE] = []
        self._rule_unsub: list[CALLBACK_TYPE] = []
        self._pending: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._active: dict[str, bool] = {}
        self._last_change: dict[str, datetime] = {}
        self._reports: dict[str, tuple[datetime, dict[str, dict[str, Any]]]] = {}
        self._gone: dict[str, datetime] = {}
        self._calendar: list[dict[str, Any]] = []
        self._deferred: list[dict[str, Any]] = []
        self._priming = False
        self._dirty = False
        # further rules from the integration itself (e.g. the configured mowers)
        self.extra_rules: Callable[[], list[dict[str, Any]]] = list
        # further tasks from the integration itself (e.g. areas due for mowing)
        self.extra_tasks: Callable[[], list[dict[str, Any]]] = list

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Start listening."""
        self._unsub.append(async_dispatcher_connect(self.hass, SIGNAL_NOTIFY, self.reload))
        self._unsub.append(async_dispatcher_connect(self.hass, SIGNAL_TASKS, self._tick_now))
        self._unsub.append(async_track_time_interval(self.hass, self._tick, TICK_INTERVAL))
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_poll_calendars, CALENDAR_INTERVAL)
        )
        self.reload()
        self.hass.async_create_task(self._async_poll_calendars())

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        self._clear_rules()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @property
    def config(self) -> dict[str, Any]:
        """Return the notification settings."""
        return self.store.data["notify"]

    def rules(self) -> list[dict[str, Any]]:
        """Return the rules from the dashboard and from the integration."""
        return [*(self.config.get("rules") or []), *self.extra_rules()]

    @property
    def sent(self) -> dict[str, int]:
        """Return the keys sent and still active (ms)."""
        return self.store.data["sent"]

    @callback
    def _clear_rules(self) -> None:
        for unsub in self._rule_unsub:
            unsub()
        self._rule_unsub.clear()
        self._pending.clear()

    @callback
    def reload(self) -> None:
        """(Re)subscribe to the entities of all rules."""
        self._clear_rules()
        self._priming = True
        try:
            for rule in self.rules():
                try:
                    self._setup_rule(rule)
                except Exception:  # noqa: BLE001 - a broken rule must not stop the others
                    _LOGGER.warning("Ignoring invalid notification rule %s", rule.get("id"))
        finally:
            self._priming = False
        # forget rule states of rules that no longer exist
        ids = {f"rule:{r.get('id')}" for r in self.rules()}
        for key in [k for k in self.sent if k.startswith("rule:") and k not in ids]:
            self.sent.pop(key, None)
        self.hass.async_create_task(self._async_poll_calendars())

    # ---------------------------------------------------------------- rules

    @callback
    def _setup_rule(self, rule: dict[str, Any]) -> None:
        kind = rule.get("type")
        rid = f"rule:{rule['id']}"
        if kind in ("state", "numeric"):
            entity = rule["entity"]

            @callback
            def on_state(event: Event[EventStateChangedData]) -> None:
                self._set_condition(rule, self._entity_condition(rule))

            self._rule_unsub.append(async_track_state_change_event(self.hass, [entity], on_state))
            self._set_condition(rule, self._entity_condition(rule))
        elif kind == "change":
            entity = rule["entity"]

            @callback
            def on_change(event: Event[EventStateChangedData]) -> None:
                old, new = event.data["old_state"], event.data["new_state"]
                if old is None or new is None or new.state in INVALID or old.state in INVALID:
                    return
                if new.state == old.state:
                    return
                if rule.get("to") and new.state not in rule["to"]:
                    return
                now = dt_util.utcnow()
                if (
                    now - self._last_change.get(rid, datetime.min.replace(tzinfo=now.tzinfo))
                    < CHANGE_COOLDOWN
                ):
                    return
                self._last_change[rid] = now
                self.sent.pop(rid, None)
                self._fire(rule)

            self._rule_unsub.append(async_track_state_change_event(self.hass, [entity], on_change))
        elif kind == "template":
            template = Template(rule["template"], self.hass)

            @callback
            def on_template(event: Event | None, updates: list) -> None:
                for update in updates:
                    result = update.result
                    if isinstance(result, Exception):
                        continue
                    self._set_condition(rule, result_as_boolean(result))

            info = async_track_template_result(
                self.hass, [TrackTemplate(template, None)], on_template
            )
            self._rule_unsub.append(info.async_remove)
            try:
                self._set_condition(rule, result_as_boolean(template.async_render()))
            except Exception:  # noqa: BLE001
                self._set_condition(rule, False)
        # "at" and "calendar" rules are handled by the tick

    def _entity_condition(self, rule: dict[str, Any]) -> bool:
        state = self.hass.states.get(rule["entity"])
        if state is None or state.state in INVALID:
            return False
        if rule.get("type") == "numeric":
            try:
                value = float(state.state)
            except ValueError:
                return False
            above, below = rule.get("above"), rule.get("below")
            return (above is None or value > float(above)) and (
                below is None or value < float(below)
            )
        targets = rule.get("to")
        if targets:
            return state.state in targets or state.state.lower() in [
                str(t).lower() for t in targets
            ]
        not_to = rule.get("not_to")
        return bool(not_to) and state.state not in not_to

    @callback
    def _set_condition(self, rule: dict[str, Any], value: bool) -> None:
        rid = f"rule:{rule['id']}"
        previous = self._active.get(rid, False)
        self._active[rid] = value
        if not value:
            self._pending.pop(rid, None)
            # condition gone: next time it is a new notification
            self.sent.pop(rid, None)
            return
        if previous:
            return
        if self._priming:
            # already true at start: do not notify (e.g. after a restart), only when it comes again
            self.sent.setdefault(rid, now_ms())
            return
        if float(rule.get("for") or 0) <= 0:
            self._fire(rule)
            return
        # "for": checked by the tick (every 30 s) against the clock
        self._pending[rid] = (dt_util.utcnow(), rule)

    @callback
    def _fire(self, rule: dict[str, Any], key: str | None = None) -> None:
        key = key or f"rule:{rule['id']}"
        if key in self.sent:
            return
        self.sent[key] = now_ms()
        self._dirty = True
        self.send(
            rule.get("title") or "Smarthome",
            rule.get("message") or "",
            src=rule.get("src") or "",
            url=rule.get("url") or "/",
            urgent=bool(rule.get("urgent")),
            tag=key,
        )

    # ---------------------------------------------------------------- reports from dashboards

    @callback
    def report(self, client: str, items: list[dict[str, Any]]) -> None:
        """Take the current items of one open dashboard and send the new ones."""
        now = dt_util.utcnow()
        self._reports[client] = (now, {f"rep:{it['key']}": it for it in items if it.get("key")})
        self._process_reports(now)

    @callback
    def _process_reports(self, now: datetime) -> None:
        fresh = {c: r for c, r in self._reports.items() if now - r[0] < REPORT_TTL}
        self._reports = fresh
        current: dict[str, dict[str, Any]] = {}
        for _, items in fresh.values():
            current.update(items)
        for key, item in current.items():
            self._gone.pop(key, None)
            if key not in self.sent:
                self._fire(item, key)
        if not fresh:
            return  # nobody is looking: keep everything as it is
        for key in [k for k in self.sent if k.startswith("rep:") and k not in current]:
            since = self._gone.setdefault(key, now)
            if now - since > FORGET_AFTER:
                self.sent.pop(key, None)
                self._gone.pop(key, None)

    # ---------------------------------------------------------------- tick: times, tasks, calendars

    @callback
    def _tick_now(self) -> None:
        self._tick(dt_util.utcnow())

    @callback
    def _tick(self, now: datetime) -> None:
        local = dt_util.as_local(now)
        ms = int(now.timestamp() * 1000)
        self._send_deferred(local)
        for rid, (since, rule) in list(self._pending.items()):
            if self._active.get(rid) and (now - since).total_seconds() >= float(
                rule.get("for") or 0
            ):
                self._pending.pop(rid, None)
                self._fire(rule)
        done = self.store.done
        # points in time (waste collection the day before, …)
        for rule in self.rules():
            if rule.get("type") != "at":
                continue
            at = int(rule.get("at") or 0)
            key = f"at:{rule['id']}"
            skip = rule.get("skip_if_done")
            if skip and (done.get(skip) or {}).get("t"):
                continue
            if at <= ms < at + 6 * 3600_000:
                self._fire(rule, key)
        # calendar events
        for event in self._calendar:
            if event["at"] <= ms < event["at"] + 30 * 60_000:
                self._fire(event, event["key"])
        # due tasks
        hour = self.config.get("task_hour")
        hour = DEFAULT_TASK_HOUR if hour is None else int(hour)
        sources = self.config.get("sources") or {}
        today = local.date()
        for state in task_states([*self.store.data["tasks"], *self.extra_tasks()], done):
            cfg = sources.get(state.src) or {}
            if not cfg.get("push") or state.due_date is None or state.due_date > today:
                continue
            fire_at = max(
                state.due_datetime,
                dt_util.as_utc(datetime.combine(state.due_date, dtime(hour), local.tzinfo)),
            )
            if now < fire_at:
                continue
            key = f"task:{state.key}|{state.due_date.isoformat()}"
            if key in self.sent:
                continue
            overdue = (today - state.due_date).days
            self._fire(
                {
                    "title": "Fällig" if not overdue else "Überfällig",
                    "message": state.name
                    if not overdue
                    else f"{state.name} – seit {overdue} {'Tag' if overdue == 1 else 'Tagen'}",
                    "src": state.src,
                    "url": cfg.get("url") or "/",
                    "urgent": bool(cfg.get("urgent")),
                },
                key,
            )
        # reports and bookkeeping
        self._process_reports(now)
        for key, sent_at in list(self.sent.items()):
            if key.split(":", 1)[0] in ("at", "cal", "task") and ms - sent_at > 40 * 86400_000:
                self.sent.pop(key, None)
                self._dirty = True
        if self._dirty:
            self._dirty = False
            self.store.save()

    async def _async_poll_calendars(self, _now: datetime | None = None) -> None:
        rules = [r for r in self.rules() if r.get("type") == "calendar"]
        entities = sorted({e for r in rules for e in r.get("entities") or []})
        if not entities:
            self._calendar = []
            return
        now = dt_util.now()
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "start_date_time": now - timedelta(hours=1),
                    "end_date_time": now + timedelta(days=8),
                },
                target={"entity_id": entities},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("Calendars not available: %s", err)
            return
        events: list[dict[str, Any]] = []
        for rule in rules:
            lead = timedelta(minutes=float(rule.get("lead") or 0))
            days = int(rule.get("all_day_days") or 0)
            at_time = _parse_hhmm(rule.get("all_day_at"), dtime(8, 0))
            for entity in rule.get("entities") or []:
                for ev in (response or {}).get(entity, {}).get("events", []):
                    start = str(ev.get("start", ""))
                    summary = ev.get("summary") or "Termin"
                    if "T" in start:
                        begin = dt_util.parse_datetime(start)
                        if begin is None:
                            continue
                        fire = begin - lead
                        message = f"{dt_util.as_local(begin):%H:%M} · {summary}"
                    else:
                        day = dt_util.parse_date(start)
                        if day is None:
                            continue
                        fire = datetime.combine(day - timedelta(days=days), at_time, now.tzinfo)
                        message = (
                            summary
                            if not days
                            else f"{'Morgen' if days == 1 else day.strftime('%d.%m.')}: {summary}"
                        )
                    events.append(
                        {
                            "key": f"cal:{rule['id']}|{entity}|{start}|{summary}",
                            "at": int(fire.timestamp() * 1000),
                            "title": rule.get("title") or "Termin",
                            "message": message,
                            "src": rule.get("src") or "cal.events",
                            "url": rule.get("url") or "/",
                            "urgent": bool(rule.get("urgent")),
                        }
                    )
        self._calendar = events
        self._tick(dt_util.utcnow())

    # ---------------------------------------------------------------- sending

    def targets(self) -> list[str]:
        """Return the notify services to use (default: all mobile apps)."""
        services = self.hass.services.async_services().get("notify", {})
        own = self.config.get("targets") or []
        if own:
            return [t for t in own if t in services]
        return [t for t in services if t.startswith("mobile_app_")]

    @callback
    def send(
        self,
        title: str,
        message: str,
        *,
        src: str = "",
        url: str = "/",
        urgent: bool = False,
        tag: str | None = None,
        force: bool = False,
    ) -> bool:
        """Send a notification to all targets (unless paused or in quiet hours)."""
        if not force and not self.config.get("enabled", True):
            return False
        item = {
            "title": title,
            "message": message,
            "src": src,
            "url": url,
            "urgent": urgent,
            "tag": tag,
        }
        if not force and not urgent and in_quiet(self.config.get("quiet"), dt_util.now()):
            self._deferred.append(item)
            return False
        return self._deliver(item)

    @callback
    def _send_deferred(self, local: datetime) -> None:
        if not self._deferred or in_quiet(self.config.get("quiet"), local):
            return
        items, self._deferred = self._deferred, []
        for item in items:
            self._deliver(item)

    @callback
    def _deliver(self, item: dict[str, Any]) -> bool:
        targets = self.targets()
        if not targets:
            _LOGGER.warning("No notify target for: %s", item["message"])
            return False
        data = {"url": item["url"], "clickAction": item["url"], "group": item["src"] or "smarthome"}
        if item.get("tag"):
            data["tag"] = item["tag"]
        data.update(urgent_data(item["urgent"]))
        for target in targets:
            self.hass.async_create_task(
                self._async_call(
                    target, {"title": item["title"], "message": item["message"], "data": data}
                )
            )
        recent = self.store.data["recent"]
        recent.insert(
            0,
            {"t": now_ms(), "title": item["title"], "message": item["message"], "src": item["src"]},
        )
        del recent[RECENT_MAX:]
        self.store.save(SIGNAL_SENT)
        return True

    async def _async_call(self, target: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call("notify", target, data, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Sending to notify.%s failed: %s", target, err)
