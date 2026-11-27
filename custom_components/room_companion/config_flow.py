"""Config flow: pick a Room Companion panel, then choose what to send it."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import panel
from .const import (
    CONF_BASE_URL,
    CONF_GLYPH_FONT,
    CONF_KDE,
    CONF_KDE_DEVICES,
    CONF_LYRICS,
    CONF_MEDIA_PLAYERS,
    CONF_PREFIX,
    CONF_TIMEOUT,
    CONF_TOKEN,
    DEFAULT_TIMEOUT,
    DOMAIN,
    KDE_DOMAIN,
)


def options_schema(options: dict[str, Any]) -> vol.Schema:
    """The panel's options form."""
    return vol.Schema({
        vol.Optional(CONF_MEDIA_PLAYERS, default=options.get(CONF_MEDIA_PLAYERS, [])):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player", multiple=True)),
        vol.Optional(CONF_LYRICS, default=options.get(CONF_LYRICS, True)): selector.BooleanSelector(),
        vol.Optional(CONF_TIMEOUT, default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(CONF_KDE, default=options.get(CONF_KDE, False)): selector.BooleanSelector(),
        vol.Optional(CONF_KDE_DEVICES, default=options.get(CONF_KDE_DEVICES, [])):
            selector.DeviceSelector(selector.DeviceSelectorConfig(integration=KDE_DOMAIN, multiple=True)),
        vol.Optional(CONF_BASE_URL, description={"suggested_value": options.get(CONF_BASE_URL)}):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL)),
        vol.Optional(CONF_GLYPH_FONT, description={"suggested_value": options.get(CONF_GLYPH_FONT)}):
            selector.TextSelector(),
    })


def clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalise submitted options."""
    data = dict(user_input)
    data[CONF_TIMEOUT] = int(data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
    for key in (CONF_BASE_URL, CONF_GLYPH_FONT):
        value = str(data.get(key) or "").strip()
        if value:
            data[key] = value.rstrip("/") if key == CONF_BASE_URL else value
        else:
            data.pop(key, None)
    return data


class RoomCompanionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a panel."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        configured = {entry.unique_id for entry in self._async_current_entries()}
        panels = {p.prefix: p for p in panel.discover(self.hass) if p.prefix not in configured}
        if not panels:
            return self.async_abort(reason="no_panels")
        if user_input is not None:
            chosen = panels[user_input[CONF_PREFIX]]
            await self.async_set_unique_id(chosen.prefix)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=chosen.name,
                data={CONF_PREFIX: chosen.prefix, CONF_TOKEN: secrets.token_urlsafe(24)},
                options={CONF_LYRICS: True, CONF_TIMEOUT: DEFAULT_TIMEOUT},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PREFIX): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=p.prefix, label=p.name) for p in panels.values()],
                    mode=selector.SelectSelectorMode.LIST,
                )),
            }),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RoomCompanionOptionsFlow()


class RoomCompanionOptionsFlow(OptionsFlow):
    """Change what a panel receives."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=clean(user_input))
        return self.async_show_form(step_id="init", data_schema=options_schema(dict(self.config_entry.options)))
