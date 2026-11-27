"""Fixtures."""

import pytest

from homeassistant.core import HomeAssistant, ServiceCall

PREFIX = "onx3248g035_dbbc54"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components."""
    return


@pytest.fixture
def panel_calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Register a fake panel's ESPHome actions and record calls to them."""
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    for action in ("show_message", "dismiss_message", "set_lyrics"):
        hass.services.async_register("esphome", f"{PREFIX}_{action}", record)
    return calls
