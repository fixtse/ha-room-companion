"""A notify entity per panel: notify.send_message shows it on the panel."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoomCompanionConfigEntry, panel
from .const import CONF_PREFIX


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomCompanionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the panel's notify entity."""
    async_add_entities([PanelNotify(hass, entry)])


class PanelNotify(NotifyEntity):
    """Shows a message in the panel's notification centre."""

    _attr_has_entity_name = True
    _attr_translation_key = "panel"

    def __init__(self, hass: HomeAssistant, entry: RoomCompanionConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_PREFIX]}_notify"
        found = panel.find_panel(hass, entry.data[CONF_PREFIX])
        device = dr.async_get(hass).async_get(found.device_id) if found.device_id else None
        if device is not None:
            # Shown on the panel's existing ESPHome device.
            self._attr_device_info = DeviceInfo(identifiers=device.identifiers, connections=device.connections)
        else:
            self._attr_name = entry.title

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Show the message on the panel."""
        await self._entry.runtime_data.messenger.show(title or "", message)
