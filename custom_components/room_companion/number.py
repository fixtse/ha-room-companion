"""Lyrics offset: a live sync correction for a panel's lyrics."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoomCompanionConfigEntry, panel
from .const import ACTION_SET_LYRICS_OFFSET, CONF_PREFIX


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomCompanionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the lyrics offset for panels that show lyrics."""
    if panel.supports_lyrics(hass, entry.data[CONF_PREFIX]):
        async_add_entities([LyricsOffset(hass, entry)])


class LyricsOffset(RestoreNumber):
    """Positive shows each lyric line earlier. Applied on the panel at once."""

    _attr_has_entity_name = True
    _attr_translation_key = "lyrics_offset"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = -10.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-sync-outline"

    def __init__(self, hass: HomeAssistant, entry: RoomCompanionConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_PREFIX]}_lyrics_offset"
        self._attr_native_value = 0.0
        found = panel.find_panel(hass, entry.data[CONF_PREFIX])
        device = dr.async_get(hass).async_get(found.device_id) if found.device_id else None
        if device is not None:
            self._attr_device_info = DeviceInfo(identifiers=device.identifiers, connections=device.connections)
        else:
            self._attr_name = f"{entry.title} lyrics offset"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            await self.async_set_native_value(float(last.native_value))

    async def async_set_native_value(self, value: float) -> None:
        value = round(max(self.native_min_value, min(self.native_max_value, value)), 1)
        self._attr_native_value = value
        self._entry.runtime_data.offset_ms = round(value * 1000)
        await panel.call(self.hass, self._entry.data[CONF_PREFIX], ACTION_SET_LYRICS_OFFSET,
                         {"offset_ms": self._entry.runtime_data.offset_ms})
        self.async_write_ha_state()
