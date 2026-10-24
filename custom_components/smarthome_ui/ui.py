"""The dashboard itself: Core fetches the card from its (private) repository and serves it.

One installation instead of two: Core downloads ``dist/smarthome-dashboard-card.js`` from GitHub
(with the token in ``/config/.smarthome_ui_token`` or the one set in the options), keeps it in
``/config/smarthome_ui/`` and loads it on every page of Home Assistant – no resource, no
``shell_command``, no ``configuration.yaml``. New versions show up as an update ("Smarthome UI")
under Settings → Updates, with the release notes from the CHANGELOG.

The first version is installed by itself; later ones when the update is installed (in Home
Assistant or from the dashboard). Checked every six hours.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
from pathlib import Path
import re
from typing import Any

from aiohttp import ClientError

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DATA
from .store import CoreStore

_LOGGER = logging.getLogger(__name__)

REPO = "derPaul/smarthome-ui"
API = "https://api.github.com/repos/" + REPO + "/contents/"
CARD = "dist/smarthome-dashboard-card.js"
FILE = "smarthome-dashboard-card.js"
URL_BASE = "/smarthome_ui_ui"
DEFAULT_REF = "main"
CHECK_INTERVAL = timedelta(hours=6)
FIRST_CHECK = 60  # seconds after the start: GitHub does not hold up Home Assistant
SIGNAL_UI = "smarthome_ui_ui"
_VERSION = re.compile(r'CARD_VERSION = "([^"]+)"')
_NOTES_MAX = 30000


def version_newer(a: str | None, b: str | None) -> bool:
    """Return True if version a is newer than b (1.280.0 > 1.279.1)."""

    def parts(v: str | None) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v or ""))

    return bool(a) and parts(a) > parts(b)


def notes_since(changelog: str, installed: str | None, latest: str | None) -> str:
    """Return the CHANGELOG sections newer than the installed version (up to latest)."""
    out = []
    for section in re.split(r"^## ", changelog, flags=re.M)[1:]:
        ver = (re.match(r"\s*(\d+(?:\.\d+)*)", section) or [None, None])[1]
        if not ver or (installed and not version_newer(ver, installed)):
            continue
        if latest and version_newer(ver, latest):
            continue
        out.append("## " + section.strip())
    return "\n\n".join(out)[:_NOTES_MAX]


class UiManager:
    """Fetch, keep and serve the dashboard card."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, options: dict[str, Any]) -> None:
        """Initialize (options: ``ui_ref`` branch, ``ui_token``)."""
        self.hass = hass
        self.store = store
        self.ref = (options.get("ui_ref") or "").strip() or DEFAULT_REF
        self._own_token = (options.get("ui_token") or "").strip()
        self.folder = Path(hass.config.path("smarthome_ui"))
        self.busy = False
        self._url: str | None = None
        self._changelog = ""

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored state (installed version, latest, notes, error)."""
        return self.store.data.setdefault("ui", {})

    # ---------------------------------------------------------------- set up

    async def async_setup(self) -> None:
        """Serve the installed card (checking GitHub waits for async_start)."""
        await self.hass.async_add_executor_job(self.folder.mkdir, 0o755, True, True)
        if not await self.hass.async_add_executor_job((self.folder / FILE).exists):
            self.data.pop("installed", None)
        await self._register_path()
        self._serve()

    async def async_check_first(self) -> None:
        """Check, and install the first version by itself."""
        await self.async_check()
        if not self.data.get("installed") and self.data.get("latest"):
            try:
                await self.async_install()
            except UiError:
                pass

    @callback
    def async_start(self) -> Callable[[], None]:
        """Check a minute after the start, then every six hours (returns the stop function)."""

        async def first(_now: Any) -> None:
            await self.async_check_first()

        async def check(_now: Any) -> None:
            await self.async_check()

        stops = [
            async_call_later(self.hass, FIRST_CHECK, first),
            async_track_time_interval(self.hass, check, CHECK_INTERVAL),
        ]
        return lambda: [stop() for stop in stops] and None

    async def _register_path(self) -> None:
        holder = self.hass.data.setdefault("smarthome_ui_ui", {})
        if holder.get("path") or getattr(self.hass, "http", None) is None:
            return
        from homeassistant.components.http import StaticPathConfig

        try:
            await self.hass.http.async_register_static_paths(
                [StaticPathConfig(URL_BASE, str(self.folder), False)]
            )
        except RuntimeError as err:  # a frozen router (test web server): Core works without
            _LOGGER.warning("Smarthome UI: Pfad für die Karte nicht angemeldet: %s", err)
            return
        holder["path"] = True

    @callback
    def _serve(self) -> None:
        """Load the installed card on every page (the version in the address skips the cache)."""
        version = self.data.get("installed")
        url = f"{URL_BASE}/{FILE}?v={version}" if version else None
        if url == self._url:
            return
        try:
            from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url

            if self._url:
                remove_extra_js_url(self.hass, self._url)
            if url:
                add_extra_js_url(self.hass, url)
        except (ImportError, KeyError):  # frontend not loaded (tests)
            pass
        self._url = url

    @callback
    def async_unload(self) -> None:
        """Stop loading the card (the integration is removed or reloads)."""
        if self._url:
            try:
                from homeassistant.components.frontend import remove_extra_js_url

                remove_extra_js_url(self.hass, self._url)
            except (ImportError, KeyError, ValueError):
                pass
            self._url = None

    # ---------------------------------------------------------------- GitHub

    def _read_token(self) -> str:
        if self._own_token:
            return self._own_token
        path = Path(self.hass.config.path(".smarthome_ui_token"))
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    async def _get(self, path: str) -> str:
        token = await self.hass.async_add_executor_job(self._read_token)
        headers = {"Accept": "application/vnd.github.raw"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                API + path, params={"ref": self.ref}, headers=headers, timeout=60
            ) as res:
                if res.status in (401, 403, 404):
                    raise UiError(
                        {
                            401: "Token ungültig oder abgelaufen",
                            403: "Token ohne Berechtigung oder Abfrage-Grenze erreicht",
                            404: f"nicht gefunden (Branch „{self.ref}“ und Token prüfen)",
                        }[res.status]
                    )
                if res.status != 200:
                    raise UiError(f"GitHub antwortet mit {res.status}")
                return await res.text()
        except (ClientError, TimeoutError) as err:
            raise UiError(f"GitHub nicht erreichbar ({err})") from err

    # ---------------------------------------------------------------- check and install

    async def async_check(self) -> dict[str, Any]:
        """Look for the newest version and its release notes."""
        try:
            card = await self._get(CARD)
            latest = (_VERSION.search(card) or [None, None])[1]
            if not latest:
                raise UiError("keine Versionsnummer in der Karte")
            try:
                changelog = await self._get("CHANGELOG.md")
            except UiError:
                changelog = ""
            self._changelog = changelog
            self.data.update(latest=latest, error=None)
        except UiError as err:
            _LOGGER.warning("Smarthome UI: Prüfung fehlgeschlagen: %s", err)
            self.data["error"] = str(err)
        self.data["checked"] = int(dt_util.utcnow().timestamp() * 1000)
        self._changed()
        return self.info()

    async def async_install(self) -> dict[str, Any]:
        """Download the newest card and serve it."""
        if self.busy:
            return self.info()
        self.busy = True
        self._changed()
        try:
            card = await self._get(CARD)
            version = (_VERSION.search(card) or [None, None])[1]
            if not version:
                raise UiError("Karte unvollständig (keine Versionsnummer)")
            await self.hass.async_add_executor_job(self._write, card)
            self.data.update(installed=version, error=None)
            if version_newer(version, self.data.get("latest")):
                self.data["latest"] = version
            _LOGGER.info("Smarthome UI %s installiert", version)
        except UiError as err:
            self.data["error"] = str(err)
            raise
        finally:
            self.busy = False
            self._serve()
            self._changed()
        return self.info()

    def _write(self, card: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / (FILE + ".tmp")
        tmp.write_text(card, encoding="utf-8")
        tmp.replace(self.folder / FILE)

    @callback
    def _changed(self) -> None:
        self.store.save(SIGNAL_DATA)
        async_dispatcher_send(self.hass, SIGNAL_UI)

    # ---------------------------------------------------------------- for the dashboard

    def notes(self) -> str:
        """Return what is new since the installed version."""
        return notes_since(self._changelog, self.data.get("installed"), self.data.get("latest"))

    def info(self) -> dict[str, Any]:
        """Return the state for the dashboard (the release notes only on request)."""
        return {
            "installed": self.data.get("installed"),
            "latest": self.data.get("latest"),
            "ref": self.ref,
            "error": self.data.get("error"),
            "checked": self.data.get("checked"),
            "busy": self.busy,
            "url": self._url,
        }


class UiError(Exception):
    """Fetching the card failed (message in German, shown as is)."""
