"""Finding Room Companion panels among ESPHome devices and calling their actions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

from .const import ACTION_SET_LYRICS, ACTION_SHOW_MESSAGE, ESPHOME_DOMAIN

_LOGGER = logging.getLogger(__name__)

# The ESPHome config entry option behind "Allow the device to perform Home
# Assistant actions" (homeassistant.components.esphome.const).
ESPHOME_ALLOW_ACTIONS = "allow_service_calls"
# ESPHome stores the node name under this config entry data key.
ESPHOME_DEVICE_NAME = "device_name"
# The firmware's ESPHome project name is "fixtse.ESPHome Room Companion <model>".
MODEL_PREFIX = "ESPHome Room Companion"


@dataclass(frozen=True)
class Panel:
    """A Room Companion panel known to the ESPHome integration."""

    prefix: str
    name: str
    device_id: str | None
    esphome_entry_id: str | None


def action_name(prefix: str, action: str) -> str:
    """ESPHome names a device's actions <node name>_<action>, '-' replaced by '_'."""
    return f"{prefix}_{action}"


def _prefixes(hass: HomeAssistant) -> set[str]:
    """Action prefixes of devices that provide both Room Companion actions."""
    services = hass.services.async_services_for_domain(ESPHOME_DOMAIN)
    suffix = f"_{ACTION_SET_LYRICS}"
    return {
        name[: -len(suffix)]
        for name in services
        if name.endswith(suffix) and action_name(name[: -len(suffix)], ACTION_SHOW_MESSAGE) in services
    }


def _entry_prefix(entry: ConfigEntry) -> str:
    return str(entry.data.get(ESPHOME_DEVICE_NAME, "")).replace("-", "_")


def find_panel(hass: HomeAssistant, prefix: str) -> Panel:
    """Match an action prefix to its ESPHome config entry and device."""
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        for device in devices:
            names = {_entry_prefix(entry), slugify(device.name or "")}
            if prefix in names:
                return Panel(prefix, device.name_by_user or device.name or prefix, device.id, entry.entry_id)
    return Panel(prefix, prefix, None, None)


def discover(hass: HomeAssistant) -> list[Panel]:
    """All panels whose Room Companion actions are registered."""
    return sorted((find_panel(hass, prefix) for prefix in _prefixes(hass)), key=lambda p: p.name)


def allows_actions(hass: HomeAssistant, panel: Panel) -> bool | None:
    """Whether the panel may call Home Assistant actions; None when unknown."""
    if panel.esphome_entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(panel.esphome_entry_id)
    if entry is None:
        return None
    return bool(entry.options.get(ESPHOME_ALLOW_ACTIONS, False))


def is_room_companion(device: dr.DeviceEntry) -> bool:
    """Whether a device registry entry is a Room Companion panel."""
    return (device.model or "").startswith(MODEL_PREFIX)


def has_action(hass: HomeAssistant, prefix: str, action: str) -> bool:
    """Whether the panel's firmware provides an action."""
    return hass.services.has_service(ESPHOME_DOMAIN, action_name(prefix, action))


async def call(hass: HomeAssistant, prefix: str, action: str, data: dict[str, Any] | None = None) -> bool:
    """Call one of the panel's actions; False when it is missing or fails."""
    if not has_action(hass, prefix, action):
        _LOGGER.debug("Panel %s has no %s action", prefix, action)
        return False
    try:
        await hass.services.async_call(
            ESPHOME_DOMAIN, action_name(prefix, action), data or {}, blocking=True
        )
    except Exception as err:  # noqa: BLE001 - an offline panel must not break callers.
        _LOGGER.warning("Calling %s on panel %s failed: %s", action, prefix, err)
        return False
    return True
