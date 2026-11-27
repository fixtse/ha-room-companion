"""Room Companion: the Home Assistant side of ESPHome Room Companion panels.

One config entry per panel. It sends synced lyrics, forwards notifications,
serves the panel's images (covers, blurred backdrops, emoji, icons, glyphs)
locally, and warns when the panel may not call Home Assistant actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import panel
from .assets import AssetStore, AssetView, InfoView
from .const import (
    ACTION_SET_COMPANION,
    CONF_BASE_URL,
    CONF_KDE,
    CONF_KDE_DEVICES,
    CONF_LYRICS,
    CONF_MEDIA_PLAYERS,
    CONF_PREFIX,
    CONF_TIMEOUT,
    CONF_TOKEN,
    DEFAULT_TIMEOUT,
    DOMAIN,
    URL_BASE,
)
from .lyrics import PanelLyrics
from .notifications import Messenger, internal_url

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NOTIFY]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CHECK_INTERVAL = timedelta(minutes=5)

ATTR_PANEL = "panel"
SERVICE_SHOW_MESSAGE = "show_message"
SERVICE_DISMISS_MESSAGE = "dismiss_message"
SERVICE_RESEND_LYRICS = "resend_lyrics"


@dataclass
class RoomCompanionData:
    """Runtime state of one panel."""

    prefix: str
    base: str | None
    messenger: Messenger
    lyrics: PanelLyrics
    unsubs: list[Any] = field(default_factory=list)


type RoomCompanionConfigEntry = ConfigEntry[RoomCompanionData]


def _store(hass: HomeAssistant) -> AssetStore:
    return hass.data[DOMAIN]["store"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the asset views and services once."""
    store = AssetStore(hass)
    hass.data[DOMAIN] = {"store": store}
    hass.http.register_view(InfoView())
    hass.http.register_view(AssetView(store))

    def _entries(call: ServiceCall) -> list[RoomCompanionConfigEntry]:
        ids = call.data[ATTR_PANEL]
        entries = []
        for entry_id in [ids] if isinstance(ids, str) else ids:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="panel_not_loaded",
                    translation_placeholders={"panel": entry_id},
                )
            entries.append(entry)
        return entries

    async def show_message(call: ServiceCall) -> None:
        for entry in _entries(call):
            await entry.runtime_data.messenger.show(
                call.data.get("title", ""), call.data["message"],
                call.data.get("image_url", ""), call.data.get("timeout"),
            )

    async def dismiss_message(call: ServiceCall) -> None:
        for entry in _entries(call):
            await entry.runtime_data.messenger.dismiss()

    async def resend_lyrics(call: ServiceCall) -> None:
        for entry in _entries(call):
            entry.runtime_data.lyrics.resend()

    panels = vol.All(cv.ensure_list, [cv.string])
    hass.services.async_register(DOMAIN, SERVICE_SHOW_MESSAGE, show_message, vol.Schema({
        vol.Required(ATTR_PANEL): panels,
        vol.Optional("title", default=""): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("image_url", default=""): cv.string,
        vol.Optional("timeout"): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
    }))
    hass.services.async_register(DOMAIN, SERVICE_DISMISS_MESSAGE, dismiss_message,
                                 vol.Schema({vol.Required(ATTR_PANEL): panels}))
    hass.services.async_register(DOMAIN, SERVICE_RESEND_LYRICS, resend_lyrics,
                                 vol.Schema({vol.Required(ATTR_PANEL): panels}))
    return True


def asset_base(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The base URL of this panel's assets, as the panel reaches it."""
    origin = (entry.options.get(CONF_BASE_URL) or "").strip().rstrip("/") or internal_url(hass)
    if not origin:
        return None
    return f"{origin}{URL_BASE}/{entry.data[CONF_TOKEN]}"


@callback
def _check_actions(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise or clear the repair for 'Allow the device to perform Home Assistant actions'."""
    found = panel.find_panel(hass, entry.data[CONF_PREFIX])
    issue = f"allow_actions_{entry.entry_id}"
    if panel.allows_actions(hass, found) is False:
        ir.async_create_issue(
            hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="allow_actions", translation_placeholders={"panel": found.name},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue)


async def async_setup_entry(hass: HomeAssistant, entry: RoomCompanionConfigEntry) -> bool:
    """Set up one panel."""
    prefix = entry.data[CONF_PREFIX]
    options = entry.options
    base = asset_base(hass, entry)
    messenger = Messenger(hass, _store(hass), prefix, base,
                          int(options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)))
    players = list(options.get(CONF_MEDIA_PLAYERS, [])) if options.get(CONF_LYRICS, True) else []
    lyrics = PanelLyrics(hass, prefix, players)
    data = RoomCompanionData(prefix, base, messenger, lyrics)
    entry.runtime_data = data

    lyrics.start()
    if options.get(CONF_KDE, False):
        data.unsubs.append(messenger.listen_kde(list(options.get(CONF_KDE_DEVICES, []))))

    async def announce() -> None:
        """Tell newer firmware where its assets are."""
        if base is not None and panel.has_action(hass, prefix, ACTION_SET_COMPANION):
            await panel.call(hass, prefix, ACTION_SET_COMPANION, {"url": base})

    # When the panel comes back (it restarted or reconnected): resend state.
    found = panel.find_panel(hass, prefix)
    if found.device_id is not None:
        registry = er.async_get(hass)
        watched = [e.entity_id for e in er.async_entries_for_device(registry, found.device_id)]

        @callback
        def _panel_changed(event: Event[EventStateChangedData]) -> None:
            old, new = event.data["old_state"], event.data["new_state"]
            if (old is None or old.state == STATE_UNAVAILABLE) and new is not None and new.state != STATE_UNAVAILABLE:
                lyrics.resend()
                hass.async_create_task(announce())

        if watched:
            # One entity is enough to see the device come back.
            data.unsubs.append(async_track_state_change_event(hass, watched[:1], _panel_changed))

    _check_actions(hass, entry)
    data.unsubs.append(async_track_time_interval(
        hass, lambda _now: _check_actions(hass, entry), CHECK_INTERVAL
    ))
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await announce()
    return True


async def _options_updated(hass: HomeAssistant, entry: RoomCompanionConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RoomCompanionConfigEntry) -> bool:
    """Unload one panel."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry.runtime_data.lyrics.stop()
        for unsub in entry.runtime_data.unsubs:
            unsub()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the panel's repair issue."""
    ir.async_delete_issue(hass, DOMAIN, f"allow_actions_{entry.entry_id}")
